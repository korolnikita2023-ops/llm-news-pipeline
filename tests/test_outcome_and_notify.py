"""Исход подготовки и автодоставка дайджеста.

Написано по боевому сбою 28.08. Владелец выбрал историю, конвейер честно
отработал и получил отказ редактора (8.0 → 8.17 → 6.17 при пороге 8.6), а
бот сказал «что-то сорвалось по дороге» и списал платную попытку.

Причина: историю уже готовили раньше, от той подготовки остался пост со
статусом 'rejected'. Идентификатор черновика детерминирован, поэтому новый
вердикт лёг бы в ту же строку — но защита от перезаписи его не пустила.
Бот же определял исход запросом «самый свежий пост по сюжету» и читал
ЧУЖУЮ старую запись без причины отказа.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import FakeCallback, handlers_of, post_status, seed_post
from config import Config
from src import approval_bot, notify, storage


def make_cfg() -> Config:
    cfg = Config()
    cfg.publish_mode = "live"
    cfg.workflow_mode = "digest"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = "-100999"
    return cfg


def seed_digest(n: int = 3, run_id: str = "run1") -> list:
    rows = [{"url_hash": f"h{i}", "source": "bbc.com",
             "source_url": f"https://e.com/{i}", "title": f"История {i}",
             "summary": "Суть истории.", "score": 20 - i, "category": "crime",
             "signals": "серийность"} for i in range(1, n + 1)]
    storage.save_digest(run_id, rows)
    return storage.digest_pool()


# ---------------------------------------------------------------------------
# Тот самый боевой сбой
# ---------------------------------------------------------------------------


def test_repeat_preparation_records_its_own_verdict(db, images_dir):
    """Повторная подготовка должна суметь записать новый вердикт.

    Старая запись со статусом 'rejected' блокировала перезапись, и новый
    результат — хоть отказ, хоть успешный пост — пропадал молча.
    """
    seed_post("p1", status="rejected")
    with storage.get_conn() as c:
        c.execute("UPDATE posts SET reject_reason=? WHERE post_id='p1'",
                  (storage.REJECT_BY_EDITOR,))

    # Без разрешения — как было: запись не ложится.
    assert storage.insert_post(
        post_id="p1", event_id="evt_p1", draft_ru="Новый текст.", fmt="A",
        status="candidate", scores={"average": 9.0}, manual_only=False,
    ) is False

    # С разрешением — ложится.
    assert storage.insert_post(
        post_id="p1", event_id="evt_p1", draft_ru="Новый текст.", fmt="A",
        status="candidate", scores={"average": 9.0}, manual_only=False,
        supersede_auto_reject=True,
    ) is True
    assert post_status("p1") == "candidate"


def test_owner_rejection_is_never_overwritten(db, images_dir):
    """Решение владельца, нажавшего ❌, переписать нельзя даже с разрешением."""
    seed_post("p1", status="rejected")
    with storage.get_conn() as c:
        c.execute("UPDATE posts SET reject_reason=? WHERE post_id='p1'",
                  (storage.REJECT_BY_USER,))

    assert storage.insert_post(
        post_id="p1", event_id="evt_p1", draft_ru="Другой текст.", fmt="A",
        status="candidate", scores={}, manual_only=False,
        supersede_auto_reject=True,
    ) is False
    assert post_status("p1") == "rejected"


@pytest.mark.parametrize("status", ["published", "hold", "scheduled", "publishing"])
def test_decisions_are_never_overwritten(db, images_dir, status):
    """Опубликованное, отложенное и запланированное не трогается никогда."""
    seed_post("p1", status=status)
    assert storage.insert_post(
        post_id="p1", event_id="evt_p1", draft_ru="Другой текст.", fmt="A",
        status="candidate", scores={}, manual_only=False,
        supersede_auto_reject=True,
    ) is False
    assert post_status("p1") == status


def test_editorial_rejection_is_reported_as_such_not_as_a_crash(db, images_dir,
                                                                monkeypatch):
    """Главный симптом: отказ редактора называется своим именем.

    Старая запись по тому же сюжету больше не подменяет исход.
    """
    from src import main as pipeline

    rows = seed_digest(1)
    storage.insert_raw_item({
        "url_hash": rows[0]["url_hash"], "source": "bbc.com", "title": "T",
        "body": "B", "url": "https://e.com/1", "published_at": "",
        "fetched_at": "2026-08-28T00:00:00+00:00", "lang": "en",
        "category": "", "sample": False,
    })
    # Остаток прошлой подготовки: отказ редактора без записанной причины.
    storage.insert_event(f"evt_{rows[0]['url_hash']}", rows[0]["url_hash"], 1)
    storage.insert_post(post_id="old1", event_id=f"evt_{rows[0]['url_hash']}",
                        draft_ru="Старый забракованный текст.", fmt="A",
                        status="rejected", scores={}, manual_only=False)

    def fake_process(cfg, item, relevance, stats, borderline_ok=False,
                     supersede_auto_reject=False, auto_floor=None):
        stats["outcome"] = {
            "kind": storage.REJECT_BY_EDITOR, "post_id": None,
            "scores": {"average": 6.17, "originality": 6, "tone_match": 7},
            "paid_work": True,
        }

    monkeypatch.setattr(pipeline, "_process_item", fake_process)
    out = approval_bot._generate_from_digest(make_cfg(), dict(rows[0]))

    assert out["outcome"] == "editorial", out
    assert out["average"] == 6.17
    assert out["paid_work"] is True


def test_skip_costs_nothing_and_says_so(db, images_dir, monkeypatch):
    """По сюжету уже есть пост — работы нет, попытка не расходуется."""
    from src import schedule

    rows = seed_digest(2)
    storage.insert_raw_item({
        "url_hash": rows[0]["url_hash"], "source": "bbc.com", "title": "T",
        "body": "B", "url": "https://e.com/1", "published_at": "",
        "fetched_at": "2026-08-28T00:00:00+00:00", "lang": "en",
        "category": "", "sample": False,
    })
    storage.insert_event(f"evt_{rows[0]['url_hash']}", rows[0]["url_hash"], 1)
    storage.insert_post(post_id="live1", event_id=f"evt_{rows[0]['url_hash']}",
                        draft_ru="Уже готовый пост.", fmt="A",
                        status="candidate", scores={}, manual_only=False)

    cfg = make_cfg()
    dp = approval_bot.build_dispatcher(cfg)
    call = FakeCallback(f"pick:{rows[0]['id']}")
    asyncio.run(handlers_of(dp)["pick"](call))

    attempts, successes = storage.count_selections_today(*schedule.day_bounds_utc(cfg))
    assert (attempts, successes) == (0, 0), "пропуск без работы не должен стоить попытки"

    text = call.message.answers[-1]
    assert "ждёт вашего решения" in text
    assert "не потрачено" in text
    assert "сорвалось" not in text
    assert storage.digest_item(rows[0]["id"])["chosen"] == 0, "история вернулась"


def test_skip_is_recorded_but_not_counted(db, images_dir, monkeypatch):
    """Строка в журнале остаётся — по ней видно, сколько было холостых нажатий."""
    from src import schedule

    rows = seed_digest(1)
    storage.insert_raw_item({
        "url_hash": rows[0]["url_hash"], "source": "s", "title": "T", "body": "B",
        "url": "https://e.com/1", "published_at": "",
        "fetched_at": "2026-08-28T00:00:00+00:00", "lang": "ru",
        "category": "", "sample": False,
    })
    storage.insert_event(f"evt_{rows[0]['url_hash']}", rows[0]["url_hash"], 1)
    storage.insert_post(post_id="live1", event_id=f"evt_{rows[0]['url_hash']}",
                        draft_ru="Готовый.", fmt="A", status="published",
                        scores={}, manual_only=False)

    cfg = make_cfg()
    dp = approval_bot.build_dispatcher(cfg)
    call = FakeCallback(f"pick:{rows[0]['id']}")
    asyncio.run(handlers_of(dp)["pick"](call))

    with storage.get_conn() as c:
        outcome = c.execute(
            "SELECT outcome FROM selection_attempts ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert outcome == storage.ATTEMPT_NO_WORK
    attempts, _ = storage.count_selections_today(*schedule.day_bounds_utc(cfg))
    assert attempts == 0
    assert "уже опубликован" in call.message.answers[-1]


def test_unsaved_post_is_not_reported_as_ready(db, images_dir, monkeypatch):
    """Если запись не легла, успехом это считать нельзя."""
    from src import main as pipeline

    rows = seed_digest(1)
    storage.insert_raw_item({
        "url_hash": rows[0]["url_hash"], "source": "s", "title": "T", "body": "B",
        "url": "https://e.com/1", "published_at": "",
        "fetched_at": "2026-08-28T00:00:00+00:00", "lang": "ru",
        "category": "", "sample": False,
    })

    def fake_process(cfg, item, relevance, stats, borderline_ok=False,
                     supersede_auto_reject=False, auto_floor=None):
        stats["outcome"] = {"kind": "not_saved", "post_id": None,
                            "scores": {}, "paid_work": True}

    monkeypatch.setattr(pipeline, "_process_item", fake_process)
    out = approval_bot._generate_from_digest(make_cfg(), dict(rows[0]))
    assert out["post_id"] is None
    assert out["outcome"] == "not_saved"

    text = approval_bot._explain_outcome(make_cfg(), "История", out, 1)
    assert "записать его не удалось" in text


# ---------------------------------------------------------------------------
# Автодоставка дайджеста
# ---------------------------------------------------------------------------


def _admins(*ids):
    with storage.get_conn() as c:
        for i in ids:
            c.execute(
                "INSERT OR REPLACE INTO admins (user_id, role, label, added_by, added_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (i, "admin", f"a{i}", 0, storage.now_iso()),
            )


def test_digest_is_sent_to_every_admin(db, images_dir, monkeypatch):
    """Получатели берутся из системы прав, а не из настроек."""
    rows = seed_digest(3)
    _admins(111, 222)
    sent = []
    monkeypatch.setattr(notify, "_send_message",
                        lambda cfg, chat_id, text, kb=None: sent.append((chat_id, text, kb)))

    delivered = notify.send_digest(make_cfg(), "run1", rows,
                                   {"collected_at": "28.08 в 19:03", "raw": 220,
                                    "scored": 12})
    assert delivered == 2
    assert {c for c, _, _ in sent} == {111, 222}
    text = sent[0][1]
    assert "220" in text and "12" in text and "28.08 в 19:03" in text
    assert "История 1" in text


def test_digest_is_not_sent_twice(db, images_dir, monkeypatch):
    """Перезапуск или повтор прогона не должен слать дубль."""
    rows = seed_digest(2)
    _admins(111)
    sent = []
    monkeypatch.setattr(notify, "_send_message",
                        lambda cfg, chat_id, text, kb=None: sent.append(chat_id))

    notify.send_digest(make_cfg(), "run1", rows, {"raw": 1, "scored": 1})
    notify.send_digest(make_cfg(), "run1", rows, {"raw": 1, "scored": 1})
    assert sent == [111], sent


def test_partial_delivery_is_retried_for_the_missed_admin(db, images_dir, monkeypatch):
    """Один заблокировал бота — второй всё равно получит при повторе.

    С одной общей отметкой на прогон повтор был бы подавлен целиком, и
    второй адресат не увидел бы список никогда.
    """
    rows = seed_digest(1)
    _admins(111, 222)
    blocked = {222}
    sent = []

    def flaky(cfg, chat_id, text, kb=None):
        if chat_id in blocked:
            raise RuntimeError("bot was blocked by the user")
        sent.append(chat_id)

    monkeypatch.setattr(notify, "_send_message", flaky)
    assert notify.send_digest(make_cfg(), "run1", rows, {}) == 1

    blocked.clear()
    assert notify.send_digest(make_cfg(), "run1", rows, {}) == 1
    assert sent == [111, 222], sent


def test_long_digest_is_split_into_messages(db, images_dir, monkeypatch):
    """Десять историй не влезают в лимит Telegram — режем по строкам."""
    rows = seed_digest(10)
    with storage.get_conn() as c:
        c.execute("UPDATE digest_items SET summary=?", ("Очень длинное описание. " * 20,))
    rows = storage.digest_pool()

    chunks = notify.build_digest_message(make_cfg(), rows,
                                         {"raw": 220, "scored": 12})
    assert len(chunks) > 1, "должно разбиться на несколько сообщений"
    assert all(len(c) <= 4096 for c in chunks), [len(c) for c in chunks]


def test_buttons_go_with_the_last_message(db, images_dir, monkeypatch):
    rows = seed_digest(3)
    _admins(111)
    sent = []
    monkeypatch.setattr(notify, "_send_message",
                        lambda cfg, chat_id, text, kb=None: sent.append(kb))
    notify.send_digest(make_cfg(), "run1", rows, {})
    assert sent[-1] is not None, "кнопки должны быть на последнем сообщении"
    assert all(k is None for k in sent[:-1])
    data = [b["callback_data"] for row in sent[-1]["inline_keyboard"] for b in row]
    assert all(d.startswith("pick:") for d in data), data


def test_chosen_stories_have_no_button(db, images_dir):
    rows = seed_digest(3)
    storage.start_selection_attempt(rows[0]["id"], "2000-01-01T00:00:00+00:00",
                                    "2100-01-01T00:00:00+00:00", 9, 9)
    rows = storage.digest_pool()
    kb = notify._keyboard(rows)
    data = [b["callback_data"] for row in kb["inline_keyboard"] for b in row]
    assert len(data) == 2, data


def test_empty_digest_still_notifies(db, images_dir, monkeypatch):
    """Молчание владелец прочтёт как поломку и полезет проверять сервер."""
    _admins(111)
    sent = []
    monkeypatch.setattr(notify, "_send_message",
                        lambda cfg, chat_id, text, kb=None: sent.append((text, kb)))
    notify.send_digest(make_cfg(), "run1", [], {"collected_at": "28.08 в 19:03",
                                               "raw": 220, "scored": 0})
    assert len(sent) == 1
    assert "новых историй для дайджеста нет" in sent[0][0]
    assert sent[0][1] is None, "кнопок быть не должно"


def test_telegram_outage_does_not_fail_the_run(db, images_dir, monkeypatch):
    """Сбор новостей сделан — недоступный Telegram его не отменяет."""
    rows = seed_digest(2)
    _admins(111)

    def boom(*a, **k):
        raise RuntimeError("Telegram недоступен")

    monkeypatch.setattr(notify, "_send_message", boom)
    assert notify.send_digest(make_cfg(), "run1", rows, {}) == 0   # не бросило


def test_pipeline_survives_a_broken_notifier(db, images_dir, monkeypatch, capsys):
    """Даже падение самой рассылки не должно ронять прогон."""
    from src import main as pipeline

    def boom(*a, **k):
        raise RuntimeError("рассылка сломалась")

    monkeypatch.setattr(notify, "send_digest", boom)
    pipeline._notify_digest(make_cfg(), "run1", 220, 12)
    assert "не отменяет сбор" in capsys.readouterr().out


def test_no_extra_model_call_in_notification():
    """Рассылка не должна обращаться к модели: перевод уже сделан в прогоне."""
    import inspect

    source = inspect.getsource(notify).lower()
    assert "llm_client" not in source
    assert "generate_json" not in source


def test_notification_carries_the_daily_budget(db, images_dir, monkeypatch):
    rows = seed_digest(2)
    _admins(111)
    sent = []
    monkeypatch.setattr(notify, "_send_message",
                        lambda cfg, chat_id, text, kb=None: sent.append(text))
    cfg = make_cfg()
    cfg.max_successful_selections_per_day = 2
    cfg.max_preparation_attempts_per_day = 4
    notify.send_digest(cfg, "run1", rows, {})
    text = "\n".join(sent)
    assert "постов 2 из 2" in text
    assert "попыток 4 из 4" in text


# ---------------------------------------------------------------------------
# Находки независимого ревью 28.08.2026
# ---------------------------------------------------------------------------


def test_approved_post_is_never_overwritten_even_with_permission(db, images_dir):
    """Одобренный владельцем пост не перезаписывается никогда.

    Разрешение на замену автоматического отказа не должно распространяться
    на решение человека: он нажал ✅, и пост ждёт публикации.
    """
    seed_post("p1", status="approved")
    assert storage.insert_post(
        post_id="p1", event_id="evt_p1", draft_ru="Подменённый текст.", fmt="A",
        status="candidate", scores={}, manual_only=False,
        supersede_auto_reject=True,
    ) is False
    assert post_status("p1") == "approved"
    with storage.get_conn() as c:
        text = c.execute("SELECT draft_ru FROM posts WHERE post_id='p1'").fetchone()[0]
    assert "Подменённый" not in text


def test_supersede_replaces_the_whole_draft_not_just_the_status(db, images_dir):
    """Обновляется весь черновик, а не только статус и оценки.

    Иначе повторная подготовка сделала бы кандидатом СТАРЫЙ текст — тот, что
    раньше не пропустил юридический гейт, — приклеив к нему новый статус.
    """
    seed_post("p1", status="rejected", draft="Старый текст, забракованный гейтом.")
    with storage.get_conn() as c:
        c.execute("UPDATE posts SET reject_reason=? WHERE post_id='p1'",
                  (storage.REJECT_BY_LEGAL,))

    assert storage.insert_post(
        post_id="p1", event_id="evt_p1", draft_ru="Новый безопасный текст.",
        fmt="B", status="candidate", scores={"average": 9.0}, manual_only=True,
        headline="Новый заголовок", supersede_auto_reject=True,
    ) is True

    with storage.get_conn() as c:
        row = c.execute(
            "SELECT draft_ru, format, manual_only, headline, status "
            "FROM posts WHERE post_id='p1'").fetchone()
    assert row[0] == "Новый безопасный текст.", "старый текст остался бы в канале"
    assert row[1] == "B"
    assert row[2] == 1
    assert row[3] == "Новый заголовок"
    assert row[4] == "candidate"


def test_empty_run_does_not_resend_the_previous_digest(db, images_dir, monkeypatch):
    """Пустой сбор не должен рассылать вчерашний список под своими цифрами."""
    seed_digest(3, run_id="вчерашний")
    _admins(111)
    sent = []
    monkeypatch.setattr(notify, "_send_message",
                        lambda cfg, chat_id, text, kb=None: sent.append(text))

    rows_now = storage.digest_for_run("сегодняшний-пустой")
    assert rows_now == []

    notify.send_digest(make_cfg(), "сегодняшний-пустой", rows_now,
                       {"collected_at": "28.08 в 19:03", "raw": 220, "scored": 0})
    assert len(sent) == 1
    assert "новых историй для дайджеста нет" in sent[0]
    assert "История 1" not in sent[0], "чужой список рассылать нельзя"


def test_digest_for_run_returns_only_its_own_rows(db, images_dir):
    """Строки конкретного прогона — теперь обе пачки живут в пуле рядом."""
    seed_digest(2, run_id="первый")
    rows = [{"url_hash": f"x{i}", "source": "s", "source_url": "u",
             "title": f"Новая {i}", "summary": "", "score": 5,
             "category": "", "signals": ""} for i in range(3)]
    storage.save_digest("второй", rows)

    assert len(storage.digest_for_run("второй")) == 3
    assert len(storage.digest_for_run("первый")) == 2
    assert len(storage.digest_pool()) == 5, "обе пачки остаются доступными"


def test_failed_send_can_be_retried(db, images_dir, monkeypatch):
    """Сорвавшаяся отправка снимает захват, и повтор возможен."""
    rows = seed_digest(1)
    _admins(111)
    attempts = {"n": 0}

    def flaky(cfg, chat_id, text, kb=None):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise RuntimeError("сеть моргнула")

    monkeypatch.setattr(notify, "_send_message", flaky)
    assert notify.send_digest(make_cfg(), "run1", rows, {}) == 0
    assert notify.send_digest(make_cfg(), "run1", rows, {}) == 1, (
        "после сбоя повтор должен пройти")


def test_send_is_claimed_before_sending(db, images_dir, monkeypatch):
    """Захват до отправки: два одновременных вызова не продублируют."""
    rows = seed_digest(1)
    _admins(111)
    assert notify._claim("run1", 111) is True
    sent = []
    monkeypatch.setattr(notify, "_send_message",
                        lambda *a, **k: sent.append(1))
    assert notify.send_digest(make_cfg(), "run1", rows, {}) == 0
    assert sent == [], "захваченного адресата второй раз не шлём"


def test_overlong_line_is_truncated_not_dropped(db, images_dir):
    """Строка длиннее лимита режется, а не роняет всё сообщение."""
    chunks = notify._split(["ы" * 9000, "короткая"])
    assert all(len(c) <= notify.CHUNK_LIMIT + 20 for c in chunks), [len(c) for c in chunks]
    assert any("короткая" in c for c in chunks)


def test_unknown_outcome_counts_as_paid(db, images_dir, monkeypatch):
    """Неизвестный исход считаем оплаченным: деньги защищать важнее."""
    from src import main as pipeline

    rows = seed_digest(1)
    storage.insert_raw_item({
        "url_hash": rows[0]["url_hash"], "source": "s", "title": "T", "body": "B",
        "url": "https://e.com/1", "published_at": "",
        "fetched_at": "2026-08-28T00:00:00+00:00", "lang": "ru",
        "category": "", "sample": False,
    })
    monkeypatch.setattr(pipeline, "_process_item",
                        lambda *a, **k: None)      # исход не выставлен
    out = approval_bot._generate_from_digest(make_cfg(), dict(rows[0]))
    assert out["outcome"] == "lost"
    assert out["paid_work"] is True


def test_send_error_never_leaks_the_bot_token(db, images_dir, monkeypatch):
    """Токен не должен попадать ни в журнал, ни в сообщение.

    httpx кладёт в текст исключения полный URL, а в URL Bot API стоит
    токен. 28.08 одна неудачная отправка положила его в журнал systemd
    открытым текстом.
    """
    class Resp:
        status_code = 400

        def json(self):
            return {"description": "chat not found"}

    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: Resp())
    cfg = make_cfg()
    with pytest.raises(notify.SendFailed) as exc:
        notify._send_message(cfg, 111, "текст")

    assert cfg.telegram_bot_token not in str(exc.value)
    assert "api.telegram.org" not in str(exc.value)
    assert "400" in str(exc.value)
    assert "chat not found" in str(exc.value)


def test_network_error_is_reported_without_the_url(db, images_dir, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError(
            "Connection failed for url 'https://api.telegram.org/bot42:SECRET/sendMessage'")

    monkeypatch.setattr(notify.httpx, "post", boom)
    with pytest.raises(notify.SendFailed) as exc:
        notify._send_message(make_cfg(), 111, "текст")
    assert "SECRET" not in str(exc.value)
    assert "api.telegram.org" not in str(exc.value)


def test_delivery_log_line_is_scrubbed(db, images_dir, monkeypatch, capsys):
    """Даже если токен куда-то просочился, в лог он не попадёт."""
    rows = seed_digest(1)
    _admins(111)
    cfg = make_cfg()

    def leaky(*a, **k):
        raise RuntimeError(f"upstream said no: {cfg.telegram_bot_token}")

    monkeypatch.setattr(notify, "_send_message", leaky)
    notify.send_digest(cfg, "run1", rows, {})
    out = capsys.readouterr().out
    assert cfg.telegram_bot_token not in out
    assert "токен скрыт" in out
