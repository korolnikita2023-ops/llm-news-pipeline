"""Дайджест дня, выбор владельца и публикация по расписанию.

Проверяется главное обещание этого этапа: дорогая генерация запускается
ТОЛЬКО для выбранного человеком, а публикация по расписанию не обходит
единственную точку публикации и не выпускает утренний пост ночью.
"""
from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeCallback, handlers_of, post_status, seed_post
from config import Config
from src import approval_bot, digest, publish_due, publisher, schedule, storage


def make_cfg(*, live: bool = True, mode: str = "digest") -> Config:
    cfg = Config()
    cfg.publish_mode = "live" if live else "draft"
    cfg.workflow_mode = mode
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = "-100999"
    return cfg


def _llm_cfg() -> Config:
    """Config with a non-mock provider (fake key), so that digest.translate
    takes the LLM path. The LLM call itself is always monkeypatched in tests;
    without this the result would depend on whether the machine has a .env."""
    cfg = make_cfg()
    cfg.llm_provider = "gemini"
    cfg.gemini_api_key = "test-key"
    return cfg


def ok_outcome(post_id: str, average: float = 9.0) -> dict:
    """Успешная подготовка в том виде, в каком её возвращает бот."""
    return {"post_id": post_id, "outcome": "ok", "average": average,
            "threshold": 8.6, "weakest": [], "issues": []}


def rejected_outcome(kind: str = "editorial", average: float = 7.5) -> dict:
    """Отказ: пост не создан, причина известна."""
    return {"post_id": None, "outcome": kind, "average": average,
            "threshold": 8.6, "weakest": [("оригинальность", 7)], "issues": []}


def seed_digest(n: int = 3, run_id: str = "run1") -> list:
    rows = [
        {
            "url_hash": f"h{i}", "source": "bbc.com",
            "source_url": f"https://example.com/{i}",
            "title": f"История {i}", "summary": f"Суть истории {i}.",
            "score": 20 - i, "category": "crime", "signals": "серийность",
        }
        for i in range(1, n + 1)
    ]
    storage.save_digest(run_id, rows)
    return storage.digest_pool()


# ---------------------------------------------------------------------------
# Дайджест строится из уже оплаченных данных
# ---------------------------------------------------------------------------


def test_translation_is_one_call_for_the_whole_list(db, monkeypatch):
    """Перевод — ОДИН вызов на весь список, а не по вызову на историю.

    Десять отдельных обращений стоили бы вдесятеро и упёрлись бы в потолок
    вызовов прогона ради строчки, которую владелец читает секунду.
    """
    calls = []

    def fake_json(cfg, tier, stage, system_prompt, user_prompt, db_path=None):
        calls.append((tier, stage, json.loads(user_prompt)))
        return {"items": [{"id": i, "headline": f"Заголовок {i}",
                           "summary": f"Суть {i}."} for i in range(1, 11)]}

    from src import llm_client
    monkeypatch.setattr(llm_client, "generate_json", fake_json)

    rows = [{"title": f"Story {i}", "summary": "Body.", "source": "bbc.com"}
            for i in range(1, 11)]
    out = digest.translate(_llm_cfg(), rows)

    assert len(calls) == 1
    assert calls[0][0] == "cheap"
    assert len(calls[0][2]["items"]) == 10
    assert out[0]["title"] == "Заголовок 1"
    assert out[0]["title_src"] == "Story 1"


def test_failed_translation_keeps_the_digest(db, monkeypatch):
    """Дайджест уже собран и оплачен — ронять его из-за перевода нельзя."""
    from src import llm_client

    def boom(*a, **k):
        raise RuntimeError("провайдер недоступен")

    monkeypatch.setattr(llm_client, "generate_json", boom)
    rows = [{"title": "Story", "summary": "Body.", "source": "bbc.com"}]
    out = digest.translate(make_cfg(), rows)
    assert out[0]["title"] == "Story"


def test_translation_keeps_untranslated_rows(db, monkeypatch):
    """Модель вернула не все строки — остальные остаются, а не пропадают."""
    from src import llm_client
    monkeypatch.setattr(llm_client, "generate_json", lambda *a, **k: {
        "items": [{"id": 1, "headline": "Первая", "summary": "Суть."}]
    })
    rows = [{"title": "One", "summary": "A.", "source": "s"},
            {"title": "Two", "summary": "B.", "source": "s"}]
    out = digest.translate(_llm_cfg(), rows)
    assert out[0]["title"] == "Первая"
    assert out[1]["title"] == "Two"


def test_digest_screen_lists_stories_and_left_quota(db):
    seed_digest(3)
    cfg = make_cfg()
    cfg.max_successful_selections_per_day = 2
    cfg.max_preparation_attempts_per_day = 4
    text, keyboard = approval_bot._digest_screen(cfg)
    assert "История 1" in text and "История 3" in text
    assert "0 из 2" in text          # готовых постов за сегодня
    assert "4 из 4" in text          # платных попыток осталось
    assert len(keyboard.inline_keyboard[0]) == 3


def test_digest_screen_when_empty(db):
    text, keyboard = approval_bot._digest_screen(make_cfg())
    assert keyboard is None
    assert "пуст" in text.lower()


def test_new_run_keeps_still_fresh_stories(db):
    """Сбор ДОБАВЛЯЕТ истории, а не вытесняет прежние.

    Раньше каждый сбор выбрасывал весь прежний список: было пять историй,
    прошёл сбор — осталась одна, и четыре ещё живые темы исчезали у
    владельца из-под рук.
    """
    seed_digest(2, run_id="run1")
    rows = [{"url_hash": f"new{i}", "source": "s", "source_url": "u",
             "title": f"Новая {i}", "summary": "", "score": 30,
             "category": "", "signals": ""} for i in (1, 2)]
    storage.save_digest("run2", rows)

    pool = storage.digest_pool()
    assert len(pool) == 4, [r["title"] for r in pool]
    assert {r["run_id"] for r in pool} == {"run1", "run2"}


def test_same_story_is_not_added_twice(db):
    """Одна новость приходит из лент много прогонов подряд."""
    rows = [{"url_hash": "a", "source": "s", "source_url": "u", "title": "A",
             "summary": "", "score": 10, "category": "", "signals": ""}]
    assert storage.save_digest("run1", rows) == 1
    assert storage.save_digest("run2", rows) == 0
    assert len(storage.digest_pool()) == 1


def test_stale_digest_archived_by_ttl(db):
    seed_digest(2)
    stale = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    with storage.get_conn() as conn:
        conn.execute("UPDATE digest_items SET created_at=?", (stale,))
    assert storage.archive_stale_digest(24) == 2
    assert storage.digest_pool() == []


# ---------------------------------------------------------------------------
# Выбор: потолок и атомарность ДО дорогой генерации
# ---------------------------------------------------------------------------


def test_double_tap_generates_once(db, images_dir, monkeypatch):
    """Второе нажатие по той же истории не должно платить модели ещё раз."""
    rows = seed_digest(1)
    calls = []

    def fake_generate(cfg, item_row):
        calls.append(item_row["id"])
        seed_post("p1")
        return ok_outcome("p1")

    monkeypatch.setattr(approval_bot, "_generate_from_digest", fake_generate)
    dp = approval_bot.build_dispatcher(make_cfg())
    pick = handlers_of(dp)["pick"]

    asyncio.run(pick(FakeCallback(f"pick:{rows[0]['id']}")))
    asyncio.run(pick(FakeCallback(f"pick:{rows[0]['id']}")))

    assert len(calls) == 1


def test_failed_attempts_do_not_burn_the_daily_post_quota(db, monkeypatch):
    """Главное требование владельца: провал гейта не съедает слот поста.

    Две неудачные попытки подряд — и дневная норма готовых постов должна
    остаться нетронутой, иначе день кончается, а постов нет.
    """
    rows = seed_digest(4)
    monkeypatch.setattr(approval_bot, "_generate_from_digest",
                        lambda cfg, row: rejected_outcome())
    cfg = make_cfg()
    cfg.max_successful_selections_per_day = 2
    cfg.max_preparation_attempts_per_day = 4
    dp = approval_bot.build_dispatcher(cfg)
    pick = handlers_of(dp)["pick"]

    for row in rows[:2]:
        asyncio.run(pick(FakeCallback(f"pick:{row['id']}")))

    attempts, successes = storage.count_selections_today(*schedule.day_bounds_utc(cfg))
    assert (attempts, successes) == (2, 0)


def test_attempts_cap_stops_endless_paid_retries(db, monkeypatch):
    """Бесконечных платных повторов быть не должно.

    Даже когда каждая попытка проваливается и слот поста не расходуется,
    платить за подготовку можно ограниченное число раз в сутки.
    """
    rows = seed_digest(6)
    calls = []

    def fake_generate(cfg, item_row):
        calls.append(item_row["id"])
        return rejected_outcome()

    monkeypatch.setattr(approval_bot, "_generate_from_digest", fake_generate)
    cfg = make_cfg()
    cfg.max_successful_selections_per_day = 2
    cfg.max_preparation_attempts_per_day = 4
    dp = approval_bot.build_dispatcher(cfg)
    pick = handlers_of(dp)["pick"]

    for row in rows:
        asyncio.run(pick(FakeCallback(f"pick:{row['id']}")))

    assert len(calls) == 4


def test_retrying_the_same_story_still_counts_attempts(db, monkeypatch):
    """Повтор одной и той же истории — это отдельные платные попытки.

    Пока учёт вёлся отметкой в самой истории, повторный выбор переписывал
    её, и вторая попытка исчезала из счёта: потолок обходился повтором
    одной неудачной новости.
    """
    rows = seed_digest(1)
    calls = []
    monkeypatch.setattr(approval_bot, "_generate_from_digest",
                        lambda cfg, row: (calls.append(1), rejected_outcome())[1])
    cfg = make_cfg()
    cfg.max_preparation_attempts_per_day = 2
    dp = approval_bot.build_dispatcher(cfg)
    pick = handlers_of(dp)["pick"]

    for _ in range(4):
        asyncio.run(pick(FakeCallback(f"pick:{rows[0]['id']}")))

    assert len(calls) == 2
    attempts, _ = storage.count_selections_today(*schedule.day_bounds_utc(cfg))
    assert attempts == 2


def test_successful_post_counts_toward_the_post_quota(db, images_dir, monkeypatch):
    """Готовый пост расходует норму постов, а не только попытку."""
    rows = seed_digest(2)

    def fake_generate(cfg, item_row):
        seed_post(f"p{item_row['id']}")
        return ok_outcome(f"p{item_row['id']}")

    monkeypatch.setattr(approval_bot, "_generate_from_digest", fake_generate)
    cfg = make_cfg()
    cfg.max_successful_selections_per_day = 1
    dp = approval_bot.build_dispatcher(cfg)
    pick = handlers_of(dp)["pick"]

    asyncio.run(pick(FakeCallback(f"pick:{rows[0]['id']}")))
    asyncio.run(pick(FakeCallback(f"pick:{rows[1]['id']}")))

    attempts, successes = storage.count_selections_today(*schedule.day_bounds_utc(cfg))
    assert (attempts, successes) == (1, 1)


def test_daily_cap_counts_selection_time_not_digest_time(db):
    """Дайджест живёт сутки и переживает полночь.

    Если считать по времени создания списка, сегодняшние выборы из
    вчерашнего дайджеста не считались бы вовсе — и потолок обходился бы сам.
    """
    rows = seed_digest(2)
    old = (datetime.now(timezone.utc) - timedelta(hours=20)).isoformat(timespec="seconds")
    with storage.get_conn() as conn:
        conn.execute("UPDATE digest_items SET created_at=?", (old,))
    storage.start_selection_attempt(
        rows[0]["id"], *schedule.day_bounds_utc(make_cfg()), 9, 9)

    attempts, successes = storage.count_selections_today(
        *schedule.day_bounds_utc(make_cfg()))
    assert (attempts, successes) == (1, 0)


def test_failed_generation_returns_story_to_digest(db, monkeypatch):
    rows = seed_digest(1)

    def boom(cfg, item_row):
        raise RuntimeError("модель недоступна")

    monkeypatch.setattr(approval_bot, "_generate_from_digest", boom)
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback(f"pick:{rows[0]['id']}")
    asyncio.run(handlers_of(dp)["pick"](call))

    assert storage.digest_item(rows[0]["id"])["chosen"] == 0
    assert any("сбой при подготовке" in a.lower() for a in call.message.answers)


def test_rejected_by_gate_returns_story_to_digest(db, monkeypatch):
    rows = seed_digest(1)
    monkeypatch.setattr(approval_bot, "_generate_from_digest",
                        lambda cfg, row: rejected_outcome("legal_gate"))
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback(f"pick:{rows[0]['id']}")
    asyncio.run(handlers_of(dp)["pick"](call))
    assert storage.digest_item(rows[0]["id"])["chosen"] == 0
    # Причина отказа — прямо в сообщении, а не в логах сервера.
    assert "юридический гейт" in call.message.answers[-1].lower()


# ---------------------------------------------------------------------------
# Расписание: время, слоты, статусы
# ---------------------------------------------------------------------------


def test_slot_time_is_local_but_stored_utc(db):
    cfg = make_cfg()
    cfg.schedule_tz = "Europe/Moscow"
    cfg.morning_slot = "08:30"
    now = datetime(2026, 8, 28, 3, 0, tzinfo=timezone.utc)  # 06:00 МСК
    at = schedule.next_slot_utc(cfg, schedule.MORNING, now=now)
    assert at == "2026-08-28T05:30:00+00:00"          # 08:30 МСК
    assert schedule.describe(cfg, at) == "28.08 в 08:30"


def test_passed_slot_moves_to_tomorrow(db):
    cfg = make_cfg()
    cfg.schedule_tz = "Europe/Moscow"
    cfg.morning_slot = "08:30"
    now = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)  # 12:00 МСК
    assert schedule.next_slot_utc(cfg, schedule.MORNING, now=now).startswith("2026-08-29")


def test_one_slot_one_post(db):
    seed_post("p1")
    seed_post("p2")
    at = schedule.next_slot_utc(make_cfg(), schedule.MORNING)
    assert storage.schedule_post("p1", at, "morning") is True
    assert storage.schedule_post("p2", at, "morning") is False
    assert post_status("p2") == "candidate"


def test_scheduled_post_is_invisible_in_review_queue(db):
    seed_post("p1")
    storage.schedule_post("p1", schedule.next_slot_utc(make_cfg(), "morning"), "morning")
    assert approval_bot._next_candidate() is None
    assert approval_bot._candidates_left() == 0


def test_scheduled_post_blocks_duplicate_from_pipeline(db):
    """Пока пост ждёт слота, конвейер не должен делать по тому же сюжету второй."""
    seed_post("p1")
    storage.schedule_post("p1", schedule.next_slot_utc(make_cfg(), "morning"), "morning")
    assert storage.has_active_post_for_event("evt_p1") is True


@pytest.mark.parametrize("action,prefix", [
    ("approve", "approve:"), ("hold", "hold:"), ("regen", "regen:"),
])
def test_old_buttons_do_not_break_schedule(db, images_dir, monkeypatch, action, prefix):
    """Кнопки под старым сообщением не должны трогать запланированный пост.

    ✅ опубликовал бы его мимо слота, 🔁 вернул бы в очередь и потерял время.
    """
    seed_post("p1")
    at = schedule.next_slot_utc(make_cfg(), "morning")
    storage.schedule_post("p1", at, "morning")

    def never(*args, **kwargs):
        raise AssertionError("запланированный пост тронут старой кнопкой")

    monkeypatch.setattr(approval_bot, "_publish_post", never)
    monkeypatch.setattr(approval_bot, "_regenerate", never)

    dp = approval_bot.build_dispatcher(make_cfg())
    asyncio.run(handlers_of(dp)[action](FakeCallback(f"{prefix}p1")))

    assert post_status("p1") == "scheduled"
    with storage.get_conn() as conn:
        kept = conn.execute("SELECT publish_at FROM posts WHERE post_id=?", ("p1",)).fetchone()[0]
    assert kept == at


def test_cancel_button_unschedules_and_rejects(db, images_dir, monkeypatch):
    """❌ у запланированного поста — не ошибка, а решение «не публиковать».

    Эта кнопка висит и под свежим превью, и под старым сообщением, но
    смысл у неё в обоих случаях один, и он безопасный: в канал ничего не
    уходит. Поэтому её, в отличие от ✅ и 🔁, не блокируем — просто сначала
    снимаем пост со слота.
    """
    seed_post("p1")
    storage.schedule_post("p1", schedule.next_slot_utc(make_cfg(), "morning"), "morning")

    def never(*args, **kwargs):
        raise AssertionError("отмена не должна ничего публиковать")

    monkeypatch.setattr(approval_bot, "_publish_post", never)
    dp = approval_bot.build_dispatcher(make_cfg())
    asyncio.run(handlers_of(dp)["reject"](FakeCallback("reject:p1")))

    assert post_status("p1") == "rejected"
    with storage.get_conn() as conn:
        left = conn.execute("SELECT publish_at, slot FROM posts WHERE post_id=?",
                            ("p1",)).fetchone()
    assert tuple(left) == (None, None)


def test_interrupted_selection_returns_to_digest_on_restart(db):
    """Бот умер между «выбрана» и появлением поста.

    Без восстановления история осталась бы отмеченной выбранной без поста:
    видно в дайджесте, но нажать уже нельзя. Потраченная попытка при этом
    из суточного счёта не исчезает — деньги за неё могли уйти.
    """
    rows = seed_digest(2)
    storage.start_selection_attempt(
        rows[0]["id"], *schedule.day_bounds_utc(make_cfg()), 9, 9)

    assert storage.release_stale_digest_claims() == 1
    assert storage.digest_item(rows[0]["id"])["chosen"] == 0
    attempts, successes = storage.count_selections_today(
        *schedule.day_bounds_utc(make_cfg()))
    assert (attempts, successes) == (1, 0)


def test_completed_selection_is_not_released(db, images_dir):
    """История с готовым постом остаётся выбранной — её восстанавливать нечего."""
    rows = seed_digest(1)
    seed_post("p1")
    storage.start_selection_attempt(
        rows[0]["id"], *schedule.day_bounds_utc(make_cfg()), 9, 9)
    storage.link_digest_post(rows[0]["id"], "p1")

    assert storage.release_stale_digest_claims() == 0
    assert storage.digest_item(rows[0]["id"])["chosen"] == 1


def test_failed_publish_keeps_status_and_schedule_consistent(db, images_dir):
    """Статус и расписание меняются вместе, а не двумя запросами подряд.

    Раздельные шаги оставляли пост «запланированным» с пустым временем:
    таймер его не видит (ему нужно publish_at), очередь на решение — тоже.
    """
    seed_post("p1")
    with storage.get_conn() as conn:
        conn.execute("UPDATE posts SET status='publishing', publish_at=?, slot='morning' "
                     "WHERE post_id=?", ("2026-01-01T00:00:00+00:00", "p1"))
    storage.finish_publishing("p1", sent=False)

    with storage.get_conn() as conn:
        row = conn.execute("SELECT status, publish_at, slot FROM posts WHERE post_id=?",
                           ("p1",)).fetchone()
    assert tuple(row) == ("approved", None, None)


def test_finish_publishing_does_not_touch_foreign_status(db):
    """Переход разрешён только из собственного захвата 'publishing'."""
    seed_post("p1", status="candidate")
    storage.finish_publishing("p1", sent=True)
    assert post_status("p1") == "candidate"


def test_unschedule_returns_post_to_queue(db):
    seed_post("p1")
    storage.schedule_post("p1", schedule.next_slot_utc(make_cfg(), "morning"), "morning")
    assert storage.unschedule_post("p1") is True
    assert post_status("p1") == "candidate"
    with storage.get_conn() as conn:
        left = conn.execute("SELECT publish_at FROM posts WHERE post_id=?", ("p1",)).fetchone()[0]
    assert left is None


def test_scheduled_screen_shows_time_and_buttons(db):
    seed_post("p1")
    storage.schedule_post("p1", schedule.next_slot_utc(make_cfg(), "evening"), "evening")
    text, keyboard = approval_bot._scheduled_screen(make_cfg())
    assert "Вечер" in text
    assert len(keyboard.inline_keyboard) == 1


# ---------------------------------------------------------------------------
# Выпуск по расписанию
# ---------------------------------------------------------------------------


def _make_due(post_id: str, minutes_ago: int) -> str:
    at = (datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    seed_post(post_id)
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE posts SET status='scheduled', publish_at=?, slot='morning' WHERE post_id=?",
            (at, post_id),
        )
    return at


def _sender(sent: list, ok: bool = True):
    def send(cfg, text, image_path=None, source_url=""):
        sent.append(text)
        return (ok, "ok" if ok else "telegram: 400")
    return send


def test_due_post_is_published_through_single_point(db, images_dir, monkeypatch):
    _make_due("p1", 1)
    sent = []
    monkeypatch.setattr(publisher, "send_live_text", _sender(sent))
    monkeypatch.setattr(publish_due, "get_config", lambda: make_cfg())

    assert publish_due.run() == 0
    assert len(sent) == 1
    assert post_status("p1") == "published"


def test_future_post_is_not_published(db, images_dir, monkeypatch):
    seed_post("p1")
    storage.schedule_post("p1", schedule.next_slot_utc(make_cfg(), "morning"), "morning")
    sent = []
    monkeypatch.setattr(publisher, "send_live_text", _sender(sent))
    monkeypatch.setattr(publish_due, "get_config", lambda: make_cfg())

    assert publish_due.run() == 0
    assert sent == []
    assert post_status("p1") == "scheduled"


def test_overdue_post_goes_back_to_owner(db, images_dir, monkeypatch):
    """Утренний пост не должен уйти в канал ночью после простоя сервера."""
    _make_due("p1", 300)  # пять часов опоздания при запасе в два
    sent = []
    monkeypatch.setattr(publisher, "send_live_text", _sender(sent))
    cfg = make_cfg()
    cfg.schedule_grace_minutes = 120
    monkeypatch.setattr(publish_due, "get_config", lambda: cfg)

    assert publish_due.run() == 0
    assert sent == []
    assert post_status("p1") == "candidate"


def test_late_but_within_grace_is_published(db, images_dir, monkeypatch):
    _make_due("p1", 30)
    sent = []
    monkeypatch.setattr(publisher, "send_live_text", _sender(sent))
    cfg = make_cfg()
    cfg.schedule_grace_minutes = 120
    monkeypatch.setattr(publish_due, "get_config", lambda: cfg)

    publish_due.run()
    assert len(sent) == 1


def test_draft_mode_blocks_scheduled_publishing(db, images_dir, monkeypatch):
    """PUBLISH_MODE не live — таймер не имеет права ничего отправить."""
    _make_due("p1", 1)
    sent = []
    monkeypatch.setattr(publisher, "send_live_text", _sender(sent))
    monkeypatch.setattr(publish_due, "get_config", lambda: make_cfg(live=False))

    assert publish_due.run() == 1
    assert sent == []
    assert post_status("p1") != "published"


def test_failed_send_clears_schedule(db, images_dir, monkeypatch):
    """Провалившийся слот не остаётся в расписании — иначе таймер бьётся в стену."""
    _make_due("p1", 1)
    monkeypatch.setattr(publisher, "send_live_text", _sender([], ok=False))
    monkeypatch.setattr(publish_due, "get_config", lambda: make_cfg())

    publish_due.run()
    assert post_status("p1") == "approved"
    with storage.get_conn() as conn:
        left = conn.execute("SELECT publish_at FROM posts WHERE post_id=?", ("p1",)).fetchone()[0]
    assert left is None


def test_crash_recovery_does_not_leave_phantom_schedule(db):
    seed_post("p1")
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE posts SET status='publishing', publish_at=?, slot='morning' WHERE post_id=?",
            ("2026-01-01T00:00:00+00:00", "p1"),
        )
    approval_bot.recover_stuck_posts()
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT status, publish_at, slot FROM posts WHERE post_id=?", ("p1",)
        ).fetchone()
    assert tuple(row) == ("approved", None, None)


def test_relevance_is_restored_exactly_not_divided(db):
    """Оценка берётся из дайджеста как есть.

    Раньше разбор восстанавливался делением суммы на пять, и в базу поста
    попадали числа, которых модель не ставила.
    """
    row = {"relevance": '{"curiosity": 9, "emotion": 3, "unusual": 8, '
                        '"shareable": 7, "ru_fit": 6, "category": "курьёз"}',
           "score": 33, "category": ""}
    r = approval_bot._relevance_from_digest(row)
    assert (r.curiosity, r.emotion, r.unusual, r.shareable, r.ru_fit) == (9, 3, 8, 7, 6)
    assert r.total == 33
    assert r.category == "курьёз"


def test_relevance_without_breakdown_is_marked_as_reconstructed(db):
    """Строка от старой версии: числа приблизительные — и это видно в базе."""
    r = approval_bot._relevance_from_digest({"relevance": None, "score": 33, "category": "c"})
    assert r.total == 33
    assert r.red_flags == ["оценка восстановлена из суммы"]


def test_dry_run_does_not_write_sample_digest(db, monkeypatch):
    """Пробный прогон не должен вытеснять настоящий дайджест примерами.

    Иначе владелец нажмёт на выдуманную новость и оплатит по ней генерацию.
    """
    from src import main as pipeline

    seed_digest(2, run_id="real")
    monkeypatch.setattr(pipeline.storage, "insert_raw_item",
                        lambda *a, **k: pytest.fail("dry-run пишет в базу"))
    cfg = make_cfg()
    pipeline._save_digest(cfg, "sample-run", [], {}, [], [], 0, 0, 0, 0.0, dry_run=True)

    rows = storage.digest_pool()
    assert {r["run_id"] for r in rows} == {"real"}


# ---------------------------------------------------------------------------
# Находки независимого ревью 28.08.2026
# ---------------------------------------------------------------------------


def test_unknown_timezone_stops_loudly(db):
    """Опечатка в SCHEDULE_TZ не должна молча сдвинуть слоты на три часа."""
    cfg = make_cfg()
    cfg.schedule_tz = "Europe/Mockba"
    with pytest.raises(schedule.ScheduleConfigError):
        schedule.next_slot_utc(cfg, schedule.MORNING)


@pytest.mark.parametrize("bad", ["25:00", "08:70", "восемь", "0830"])
def test_impossible_slot_time_stops_loudly(db, bad):
    """Раньше «25:00» разбиралось успешно и падало внутри обработчика."""
    cfg = make_cfg()
    cfg.morning_slot = bad
    with pytest.raises(schedule.ScheduleConfigError):
        schedule.next_slot_utc(cfg, schedule.MORNING)


def test_slot_is_always_in_the_future_in_utc(db):
    """Слот сравнивается и по местному времени, и по UTC.

    В зоне с переводом часов местное «позже» может дать UTC-момент в
    прошлом — и таймер опубликовал бы пост немедленно.
    """
    cfg = make_cfg()
    cfg.schedule_tz = "Europe/London"   # зона с переводом часов
    cfg.morning_slot = "01:30"
    now = datetime(2026, 10, 25, 0, 45, tzinfo=timezone.utc)  # ночь перевода
    at = datetime.fromisoformat(schedule.next_slot_utc(cfg, schedule.MORNING, now=now))
    assert at > now


def test_zero_grace_does_not_expire_on_microseconds(db):
    """При нулевом запасе пост, наступивший ровно сейчас, ещё не просрочен."""
    cfg = make_cfg()
    cfg.schedule_grace_minutes = 0
    now = datetime(2026, 8, 28, 5, 30, 0, 500, tzinfo=timezone.utc)
    assert schedule.is_too_late(cfg, "2026-08-28T05:30:00+00:00", now=now) is False


def test_timer_does_not_publish_post_owner_just_unscheduled(db, images_dir, monkeypatch):
    """Гонка таймера и владельца: «✖️ Снять» между чтением списка и отправкой."""
    _make_due("p1", 1)
    sent = []
    monkeypatch.setattr(publisher, "send_live_text", _sender(sent))
    storage.unschedule_post("p1")          # владелец успел снять

    ok, status = approval_bot._publish_post(make_cfg(), "p1", require_status="scheduled")
    assert ok is False
    assert sent == []
    assert "решение изменилось" in status


def test_manual_publish_still_works_without_required_status(db, images_dir, monkeypatch):
    """Кнопка ✅ у владельца по-прежнему публикует из обычного статуса."""
    seed_post("p1", status="approved")
    sent = []
    monkeypatch.setattr(publisher, "send_live_text", _sender(sent))
    ok, _ = approval_bot._publish_post(make_cfg(), "p1")
    assert ok is True and len(sent) == 1


def test_digest_screen_respects_ttl_without_pipeline_run(db):
    """День без новостей заканчивается ранним выходом конвейера.

    Срок жизни списка при этом всё равно должен соблюдаться, иначе
    владелец увидит вчерашние истории и оплатит генерацию по протухшей.
    """
    seed_digest(2)
    stale = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    with storage.get_conn() as conn:
        conn.execute("UPDATE digest_items SET created_at=?", (stale,))

    text, keyboard = approval_bot._digest_screen(make_cfg())
    assert keyboard is None
    assert "пуст" in text.lower()


def test_stale_story_cannot_be_picked_from_an_old_message(db, monkeypatch):
    """Кнопка под вчерашним сообщением не должна запускать платную генерацию."""
    rows = seed_digest(1)
    stale = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    with storage.get_conn() as conn:
        conn.execute("UPDATE digest_items SET created_at=?", (stale,))

    def never(cfg, item_row):
        raise AssertionError("генерация по протухшей истории")

    monkeypatch.setattr(approval_bot, "_generate_from_digest", never)
    dp = approval_bot.build_dispatcher(make_cfg())
    asyncio.run(handlers_of(dp)["pick"](FakeCallback(f"pick:{rows[0]['id']}")))
    assert storage.digest_item(rows[0]["id"])["chosen"] == 0


def test_publish_due_stops_on_broken_schedule_config(db, monkeypatch):
    """Неверная зона останавливает прогон таймера с ненулевым кодом."""
    cfg = make_cfg()
    cfg.schedule_tz = "Nowhere/Nothing"
    monkeypatch.setattr(publish_due, "get_config", lambda: cfg)
    assert publish_due.run() == 1


def test_gate_rejected_post_is_not_offered_for_scheduling(db, images_dir, monkeypatch):
    """Забракованный гейтом текст не должен приехать с кнопками «Утро/Вечер».

    `_process_item` пишет запись и при отказе — со статусом 'rejected'.
    Раньше выбиралась просто последняя запись по сюжету, и владельцу
    предлагали запланировать ровно то, что гейт не пропустил.
    """
    from src import main as pipeline

    rows = seed_digest(1)
    storage.insert_raw_item({
        "url_hash": rows[0]["url_hash"], "source": "bbc.com", "title": "T",
        "body": "B", "url": "https://e.com/1", "published_at": "", 
        "fetched_at": "2026-08-28T00:00:00+00:00", "lang": "en",
        "category": "", "sample": False,
    })

    def fake_process(cfg, item, relevance, stats, borderline_ok=False,
                     supersede_auto_reject=False, auto_floor=None):
        storage.insert_event(f"evt_{item.url_hash}", item.url_hash, n_sources=1)
        storage.insert_post(post_id="bad1", event_id=f"evt_{item.url_hash}",
                            draft_ru="Текст, не прошедший гейт.", fmt="A",
                            status="rejected", scores={}, manual_only=False,
                            reject_reason=storage.REJECT_BY_LEGAL)
        # Конвейер теперь сам сообщает исход своего прогона.
        stats["outcome"] = {"kind": storage.REJECT_BY_LEGAL, "post_id": None,
                            "scores": {}, "gate_issues": ["утверждение о виновности"],
                            "paid_work": True}

    monkeypatch.setattr(pipeline, "_process_item", fake_process)
    out = approval_bot._generate_from_digest(make_cfg(), dict(rows[0]))
    assert out["post_id"] is None


# ---------------------------------------------------------------------------
# Находки второго прохода независимого ревью 28.08.2026
# ---------------------------------------------------------------------------


def test_two_fast_picks_cannot_exceed_the_post_quota(db, images_dir, monkeypatch):
    """Два быстрых нажатия по РАЗНЫМ историям при норме в один пост.

    Обе проверки видели один и тот же остаток и обе проходили — владелец
    получал постов больше нормы. Резервация делается внутри захвата.
    """
    rows = seed_digest(3)
    cfg = make_cfg()
    cfg.max_successful_selections_per_day = 1
    day = schedule.day_bounds_utc(cfg)

    first = storage.start_selection_attempt(rows[0]["id"], *day, 1, 9)
    second = storage.start_selection_attempt(rows[1]["id"], *day, 1, 9)

    assert isinstance(first, int)
    assert second == storage.POSTS_EXHAUSTED


def test_slot_frees_up_when_the_attempt_fails(db):
    """Неудачная попытка возвращает зарезервированный слот поста."""
    rows = seed_digest(3)
    cfg = make_cfg()
    day = schedule.day_bounds_utc(cfg)

    first = storage.start_selection_attempt(rows[0]["id"], *day, 1, 9)
    storage.finish_selection_attempt(first, None, "rejected")

    second = storage.start_selection_attempt(rows[1]["id"], *day, 1, 9)
    assert isinstance(second, int)


def test_success_counts_toward_the_day_it_finished(db):
    """Выбор в 23:59, ставший постом в 00:01, относится к НОВЫМ суткам.

    Иначе за первые минуты дня можно получить три поста: один вчерашней
    попыткой и два сегодняшними.
    """
    rows = seed_digest(1)
    cfg = make_cfg()
    day_start, day_end = schedule.day_bounds_utc(cfg)

    attempt = storage.start_selection_attempt(rows[0]["id"], day_start, day_end, 9, 9)
    storage.finish_selection_attempt(attempt, "p1", "ok", item_id=rows[0]["id"])

    # Сдвигаем начало попытки во вчера, завершение оставляем сегодняшним.
    yesterday = (datetime.fromisoformat(day_start) - timedelta(minutes=1)
                 ).isoformat(timespec="seconds")
    with storage.get_conn() as conn:
        conn.execute("UPDATE selection_attempts SET started_at=?", (yesterday,))

    attempts, successes = storage.count_selections_today(day_start, day_end)
    assert attempts == 0        # попытка была вчера
    assert successes == 1       # а пост получен сегодня


def test_orphan_attempt_does_not_hold_the_slot_forever(db):
    """Оборванная попытка держала бы слот поста вечно."""
    rows = seed_digest(3)
    cfg = make_cfg()
    day = schedule.day_bounds_utc(cfg)

    storage.start_selection_attempt(rows[0]["id"], *day, 1, 9)   # процесс умер
    assert storage.close_orphan_attempts() == 1

    second = storage.start_selection_attempt(rows[1]["id"], *day, 1, 9)
    assert isinstance(second, int)


def test_backfill_moves_old_selections_into_the_journal(db):
    """На обновлённой базе счётчики не должны обнуляться.

    Иначе в день обновления оба потолка обходятся сами собой.
    """
    rows = seed_digest(2)
    now = storage.now_iso()
    with storage.get_conn() as conn:
        conn.execute("UPDATE digest_items SET chosen=1, chosen_at=?, post_id='p1' "
                     "WHERE id=?", (now, rows[0]["id"]))
        conn.execute("UPDATE digest_items SET chosen=1, chosen_at=? WHERE id=?",
                     (now, rows[1]["id"]))

    assert storage.backfill_selection_attempts() == 2
    assert storage.backfill_selection_attempts() == 0     # идемпотентно

    attempts, successes = storage.count_selections_today(
        *schedule.day_bounds_utc(make_cfg()))
    assert (attempts, successes) == (2, 1)


def test_post_and_attempt_are_linked_in_one_transaction(db):
    """Пост привязывается к истории и закрывает попытку вместе."""
    rows = seed_digest(1)
    day = schedule.day_bounds_utc(make_cfg())
    attempt = storage.start_selection_attempt(rows[0]["id"], *day, 9, 9)
    storage.finish_selection_attempt(attempt, "p1", "ok", item_id=rows[0]["id"])

    assert storage.digest_item(rows[0]["id"])["post_id"] == "p1"
    _, successes = storage.count_selections_today(*day)
    assert successes == 1


def test_legacy_cap_variable_is_not_silently_ignored(monkeypatch, capsys):
    """Старая MAX_SELECTIONS_PER_DAY не должна молча обнулять настройку.

    Установка с лимитом 1 иначе получила бы новые умолчания и стала бы
    тратить вчетверо больше.
    """
    monkeypatch.setenv("MAX_SELECTIONS_PER_DAY", "1")
    monkeypatch.delenv("MAX_SUCCESSFUL_SELECTIONS_PER_DAY", raising=False)
    monkeypatch.delenv("MAX_PREPARATION_ATTEMPTS_PER_DAY", raising=False)

    cfg = Config()
    assert cfg.max_successful_selections_per_day == 1
    assert cfg.max_preparation_attempts_per_day == 2
    assert "УСТАРЕЛА" in capsys.readouterr().out


def test_malformed_translation_response_does_not_kill_the_digest(db, monkeypatch):
    """Валидный JSON неожиданной формы не должен ронять собранный дайджест."""
    from src import llm_client

    for bad in (None, {"items": None}, {"items": ["строка вместо объекта"]},
                {"items": [{"id": "не число"}]}):
        monkeypatch.setattr(llm_client, "generate_json", lambda *a, **k: bad)
        rows = [{"title": "Story", "summary": "Body.", "source": "s"}]
        out = digest.translate(make_cfg(), rows)
        assert out[0]["title"] == "Story"


def test_digest_generation_uses_case_status_from_the_source(db, images_dir, monkeypatch):
    """Путь «выбор в боте» обязан знать судебный статус.

    Он идёт через тот же _process_item, что и конвейер, — статус читается
    из сырой новости, а не из текста, который написала модель.
    """
    import inspect

    from src import main as pipeline

    assert "case_status" in inspect.getsource(pipeline._process_item)
    assert "case_status=case_status" in inspect.getsource(pipeline._process_item)


# ---------------------------------------------------------------------------
# Постоянное меню. Владелец обновил бота и не увидел новой кнопки — 28.08.
# ---------------------------------------------------------------------------


def _menu_labels():
    return [b.text for row in approval_bot._main_menu().keyboard for b in row]


def test_permanent_menu_has_digest_and_schedule_buttons():
    """Обе новые кнопки должны быть в постоянной клавиатуре.

    Без них режим дайджеста недостижим из интерфейса: команды знать
    необязательно, меню — единственный способ, которым владелец работает.
    """
    labels = _menu_labels()
    assert approval_bot.BTN_DIGEST in labels
    assert approval_bot.BTN_SCHEDULED in labels
    assert approval_bot.BTN_NEXT in labels


def test_menu_does_not_depend_on_workflow_mode():
    """Клавиатура одна и та же в обоих режимах.

    Меню, зависящее от WORKFLOW_MODE, давало бы разный интерфейс на
    сервере и на ноутбуке и путало бы владельца.
    """
    import inspect

    source = inspect.getsource(approval_bot._main_menu)
    assert "workflow_mode" not in source
    assert "cfg" not in source


def test_start_message_carries_the_new_keyboard(db, images_dir):
    """`/start` — единственный способ обновить клавиатуру у клиента.

    Telegram держит постоянную клавиатуру на стороне чата и меняет её
    только тогда, когда приходит сообщение с новой reply_markup. После
    обновления бота у владельца остаётся старая, пока он не нажмёт /start.
    """
    from conftest import FakeMessage

    dp = approval_bot.build_dispatcher(make_cfg())
    handler = handlers_of(dp)["msg_cmd_start"]
    message = FakeMessage(text="/start")
    asyncio.run(handler(message))

    assert message.markups, "у /start нет reply_markup — клавиатура не обновится"
    labels = [b.text for row in message.markups[-1].keyboard for b in row]
    assert approval_bot.BTN_DIGEST in labels
    assert approval_bot.BTN_SCHEDULED in labels


def test_start_text_mentions_the_new_buttons_and_commands(db, images_dir):
    """Справка обязана называть новые кнопки.

    Иначе владелец, обновив бота, не знает, что появилось, — ровно это и
    случилось 28.08: кнопки в коде были, а в тексте /start остались три
    старые.
    """
    from conftest import FakeMessage

    dp = approval_bot.build_dispatcher(make_cfg())
    message = FakeMessage(text="/start")
    asyncio.run(handlers_of(dp)["msg_cmd_start"](message))

    text = message.answers[-1]
    assert approval_bot.BTN_DIGEST in text
    assert approval_bot.BTN_SCHEDULED in text
    assert "/digest" in text
    assert "/scheduled" in text


def test_digest_and_scheduled_commands_are_in_the_telegram_menu():
    """Команды видны в списке Telegram — второй путь помимо клавиатуры."""
    names = [c[0] for c in approval_bot.BOT_COMMANDS]
    assert "digest" in names
    assert "scheduled" in names


def test_button_handlers_are_reachable_before_the_catch_all(db, images_dir):
    """Нажатие на кнопку не должно перехватываться общим обработчиком.

    aiogram выбирает первый подошедший обработчик в порядке регистрации.
    Общий `@dp.message()` обязан стоять последним, иначе он съест текст
    кнопки и владелец получит «не понял команду».
    """
    dp = approval_bot.build_dispatcher(make_cfg())
    names = [h.callback.__name__ for h in dp.message.handlers]

    assert names.index("btn_digest") < names.index("on_unhandled_message")
    assert names.index("btn_scheduled") < names.index("on_unhandled_message")
    assert names[-1] == "on_unhandled_message"


def test_digest_button_text_matches_the_handler_filter(db, images_dir, monkeypatch):
    """Текст кнопки и фильтр обработчика — одна и та же строка.

    Разъехавшись на один символ (эмодзи, неразрывный пробел), они дали бы
    молчащую кнопку: нажатие уходит в общий обработчик.
    """
    from conftest import FakeMessage

    dp = approval_bot.build_dispatcher(make_cfg())
    message = FakeMessage(text=approval_bot.BTN_DIGEST)
    asyncio.run(handlers_of(dp)["msg_btn_digest"](message))

    assert message.answers, "обработчик кнопки ничего не ответил"
    assert "Дайджест" in message.answers[-1]


def test_suite_is_independent_of_the_machine_workflow_mode(monkeypatch):
    """Прогон тестов не должен зависеть от режима, включённого в .env.

    Локально режим не задан, на боевом сервере — `digest`. Тест, читающий
    его из окружения, зелёный на одной машине и красный на другой, и
    деплойный гейт превращается в лотерею.
    """
    import os

    assert os.environ.get("WORKFLOW_MODE") == "legacy"


def test_candidate_cap_does_not_block_digest_collection(db, monkeypatch, capsys):
    """Потолок кандидатов — правило прежнего режима, дайджеста он не касается.

    Он считает кандидатов, созданных за сутки, а дайджест-прогон их не
    создаёт вовсе. Проверка стоит до ветвления и рубила прогон целиком:
    28.08 вечерний сбор завершился, ничего не сделав, потому что днём
    владелец подготовил четыре поста при потолке в три.
    """
    from src import main as pipeline

    for i in range(4):
        seed_post(f"c{i}")

    monkeypatch.setenv("WORKFLOW_MODE", "digest")
    monkeypatch.setenv("MAX_CANDIDATES_PER_DAY", "3")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("GEMINI_API_KEY", "")

    assert storage.count_candidates_created_today() >= 3, "потолок должен быть превышен"

    pipeline.run(["--dry-run"])
    out = capsys.readouterr().out
    assert "Суточный лимит кандидатов достигнут" not in out
    assert "дайджест" in out.lower(), out[-500:]


def test_candidate_cap_still_blocks_in_legacy_mode(db, monkeypatch, capsys):
    """В прежнем режиме потолок работает ровно как раньше."""
    from src import main as pipeline

    for i in range(4):
        seed_post(f"c{i}")

    monkeypatch.setenv("WORKFLOW_MODE", "legacy")
    monkeypatch.setenv("MAX_CANDIDATES_PER_DAY", "3")
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("GEMINI_API_KEY", "")

    assert pipeline.run(["--dry-run"]) == 0
    assert "Суточный лимит кандидатов достигнут" in capsys.readouterr().out
