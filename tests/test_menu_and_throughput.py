"""Постоянное меню владельца и управление потоком кандидатов.

Задача владельца: 1–2 качественных поста в день, конечный batch-прогон,
никакого непрерывного мониторинга, предсказуемые расходы.
"""
from __future__ import annotations

import asyncio

import pytest


from conftest import FakeBot, FakeMessage, handlers_of, seed_post
from config import Config
from src import approval_bot, storage


def make_cfg(*, owner: str = "777") -> Config:
    cfg = Config()
    cfg.publish_mode = "live"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = owner
    cfg.telegram_channel_id = "-100999"
    cfg.daily_cost_limit_usd = 3.0
    # Config() читает настоящий .env. Список админов оттуда сделал бы тесты
    # зависимыми от машины разработчика — обнуляем явно.
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    return cfg


# ---------------------------------------------------------------------------
# Постоянное меню
# ---------------------------------------------------------------------------


def test_menu_has_three_main_actions():
    kb = approval_bot._main_menu()
    texts = [b.text for row in kb.keyboard for b in row]
    assert approval_bot.BTN_NEXT in texts
    assert approval_bot.BTN_STATUS in texts
    assert approval_bot.BTN_ADMINS in texts


def test_menu_is_persistent_and_compact():
    """Клавиатура должна оставаться на экране, а не прятаться после нажатия,
    и не занимать пол-экрана."""
    kb = approval_bot._main_menu()
    assert kb.is_persistent is True
    assert kb.resize_keyboard is True


def test_start_attaches_menu(db, images_dir):
    """Клавиатура приходит владельцу вместе со справкой."""
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = FakeMessage(text="/start")

    asyncio.run(handlers_of(dp)["msg_cmd_start"](msg))

    assert msg.markups and msg.markups[-1] is not None


def test_stranger_does_not_get_menu():
    """Посторонний не проходит фильтр владельца — значит, ни одного
    обработчика, включая тот, что выдаёт клавиатуру, он не достигнет."""
    dp = approval_bot.build_dispatcher(make_cfg(owner="777"))
    flt = dp.message._handler.filters[0]

    from types import SimpleNamespace

    stranger = SimpleNamespace(from_user=SimpleNamespace(id=999))
    owner = SimpleNamespace(from_user=SimpleNamespace(id=777))
    assert bool(flt.callback(stranger)) is False
    assert bool(flt.callback(owner)) is True


def test_next_button_equals_review(db, images_dir):
    """Кнопка «Следующий пост» обязана делать ровно то же, что /review."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())
    h = handlers_of(dp)

    msg_btn = FakeMessage(text=approval_bot.BTN_NEXT)
    asyncio.run(h["msg_btn_next"](msg_btn))
    via_button = msg_btn.bot.sent_count

    storage.update_post_status("p1", "candidate")
    msg_cmd = FakeMessage(text="/review")
    asyncio.run(h["msg_cmd_review"](msg_cmd))
    via_command = msg_cmd.bot.sent_count

    assert via_button == via_command == 1


def test_next_button_on_empty_queue(db, images_dir):
    """Пустая очередь — короткое понятное сообщение, а не молчание."""
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = FakeMessage(text=approval_bot.BTN_NEXT)

    asyncio.run(handlers_of(dp)["msg_btn_next"](msg))

    assert msg.answers and "Новых постов пока нет" in msg.answers[0]


def test_status_button_reports_queue_and_cost(db, images_dir):
    seed_post("p1")
    storage.log_cost("rewrite", "gemini", "flash", 0.02)
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = FakeMessage(text=approval_bot.BTN_STATUS)

    asyncio.run(handlers_of(dp)["msg_btn_status"](msg))

    out = msg.answers[0]
    assert "Ждут решения: 1" in out
    assert "$0.0200" in out


def test_admins_button_shows_access(db, images_dir):
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = FakeMessage(text=approval_bot.BTN_ADMINS)

    asyncio.run(handlers_of(dp)["msg_btn_admins"](msg))

    out = msg.answers[0]
    assert "777" in out, "должен показывать разрешённый ID"
    assert "-100999" in out, "должен показывать канал публикации"


def test_menu_survives_restart(db, images_dir):
    """Меню не хранит состояния: после пересборки диспетчера (то есть после
    рестарта процесса) кнопки работают так же."""
    seed_post("p1")
    for _ in range(2):
        dp = approval_bot.build_dispatcher(make_cfg())
        msg = FakeMessage(text=approval_bot.BTN_STATUS)
        asyncio.run(handlers_of(dp)["msg_btn_status"](msg))
        assert msg.answers


def test_bot_commands_declared():
    """Список команд для кнопки «Меню» Telegram."""
    names = [c for c, _ in approval_bot.BOT_COMMANDS]
    assert "review" in names and "budget" in names and "queue" in names


def test_menu_setup_failure_does_not_break_startup():
    """Сбой регистрации меню не должен мешать боту запуститься."""

    class BrokenBot:
        async def set_my_commands(self, *a, **k):
            raise RuntimeError("Telegram недоступен")

        async def set_chat_menu_button(self, *a, **k):
            raise RuntimeError("Telegram недоступен")

    assert asyncio.run(approval_bot.setup_bot_menu(BrokenBot())) is False


# ---------------------------------------------------------------------------
# Управление потоком: 1–2 поста в день
# ---------------------------------------------------------------------------


def test_editorial_threshold_is_configurable():
    """MIN_EDITORIAL_AVG была объявлена, но нигде не читалась — порог был
    зашит числом 7.0, и правка .env не давала ничего."""
    from src.scorer import ScoreResult

    r = ScoreResult(
        tone_match=8, news_value=8, engagement=8,
        originality=8, factual_safety=8, legal_safety=8, hard_fail=False,
    )
    r.min_average = 7.0
    assert r.decision == "approve"
    r.min_average = 8.6
    assert r.decision == "revise", "порог из настроек обязан влиять на решение"


def test_score_draft_applies_config_threshold(monkeypatch):
    from src import scorer
    from src.rewrite import PostDraft

    cfg = make_cfg()
    cfg.llm_provider = "mock"
    cfg.min_editorial_avg = 9.9

    draft = PostDraft(
        id="d", source_item_hash="h", source_url="https://b/1", source_name="BBC",
        category="", format="A", hook="Хук", body="Тело",
        detail_hook="Деталь", source_line="По данным BBC.",
        full_text="Хук\n\nТело\n\nДеталь\n\nПо данным BBC.",
    )
    result = scorer.score_draft(cfg, draft)

    assert result.min_average == 9.9
    assert result.decision != "approve", "при пороге 9.9 шаблонный текст не проходит"


def test_known_hashes_prevent_reprocessing(db):
    """Уже обработанная новость не должна снова уходить в платную оценку.
    Раньше дедуп работал только внутри батча: в боевой базе накопилось
    676 вызовов relevance на 18 уникальных новостей."""
    storage.insert_raw_item({
        "url_hash": "abc", "source": "BBC", "title": "t", "body": "b",
        "url": "https://b/1", "published_at": "", "fetched_at": "",
        "lang": "ru", "category": "", "sample": False,
    })

    known = storage.known_url_hashes()

    assert "abc" in known
    assert "new-one" not in known


def test_daily_candidate_counter(db):
    seed_post("p1")
    seed_post("p2")
    seed_post("p3", status="rejected")

    # Отклонённые не занимают дневную квоту: владелец их не увидит в канале.
    assert storage.count_candidates_created_today() == 2


def test_defaults_target_one_two_posts_per_day(monkeypatch):
    """Значения по умолчанию подобраны под цель «1–2 поста в день»
    при двух прогонах в сутки, а не взяты наугад."""
    for var in ("MAX_TOPICS_PER_RUN", "MAX_CANDIDATES_PER_DAY", "MIN_EDITORIAL_AVG"):
        monkeypatch.delenv(var, raising=False)
    cfg = Config()

    assert cfg.max_topics_per_run == 2, "не больше двух дорогих переписываний за прогон"
    assert cfg.max_candidates_per_day == 3, "суточный потолок очереди"
    assert cfg.min_editorial_avg == 8.6, "отсекает нижнюю половину по реальному распределению"


def test_zero_daily_limit_means_unlimited(monkeypatch):
    monkeypatch.setenv("MAX_CANDIDATES_PER_DAY", "0")
    assert Config().max_candidates_per_day == 0


# ---------------------------------------------------------------------------
# Журнал виденного: повторно обработанная новость не создаёт кандидата
# ---------------------------------------------------------------------------


def test_seen_items_marked_before_scoring(db):
    """Новость отмечается ДО платной оценки — иначе отсеянные оценивались
    бы заново каждый прогон (676 вызовов на 18 новостей в боевой базе)."""
    storage.mark_seen(["h1", "h2"])
    known = storage.known_url_hashes()
    assert {"h1", "h2"} <= known


def test_mark_seen_is_idempotent(db):
    storage.mark_seen(["h1"])
    storage.mark_seen(["h1", "h2"])
    assert storage.known_url_hashes() >= {"h1", "h2"}


def test_seen_includes_both_journals(db):
    """Виденное = журнал отсеянных + сохранённые новости."""
    storage.mark_seen(["only-seen"])
    storage.insert_raw_item({
        "url_hash": "stored", "source": "BBC", "title": "t", "body": "b",
        "url": "https://b/1", "published_at": "", "fetched_at": "",
        "lang": "ru", "category": "", "sample": False,
    })
    assert {"only-seen", "stored"} <= storage.known_url_hashes()


def test_old_seen_records_are_purged(db):
    storage.mark_seen(["old"])
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE seen_items SET first_seen='2020-01-01T00:00:00+00:00'"
        )

    removed = storage.purge_old_seen(days=30)

    assert removed == 1
    assert "old" not in storage.known_url_hashes()


def test_recent_seen_records_survive_purge(db):
    storage.mark_seen(["fresh"])
    assert storage.purge_old_seen(days=30) == 0
    assert "fresh" in storage.known_url_hashes()


def test_batch_run_is_finite_and_respects_daily_cap(tmp_path, monkeypatch):
    """Один прогон завершается сам и не выходит за суточный потолок."""
    import config
    from src import image_card, main as main_module, publisher, storage as st

    drafts, images, db_file = tmp_path / "d", tmp_path / "d" / "i", tmp_path / "t.db"
    images.mkdir(parents=True)
    for module, name, value in (
        (config, "DATA_DIR", tmp_path), (config, "DRAFTS_DIR", drafts),
        (config, "IMAGES_DIR", images), (config, "DB_PATH", db_file),
        (config, "LOGS_DIR", tmp_path / "l"), (st, "DB_PATH", db_file),
        (publisher, "DRAFTS_DIR", drafts), (main_module, "DB_PATH", db_file),
        (main_module, "DRAFTS_DIR", drafts), (main_module, "LOGS_DIR", tmp_path / "l"),
        (image_card, "IMAGES_DIR", images),
    ):
        monkeypatch.setattr(module, name, value, raising=False)

    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("MIN_EDITORIAL_AVG", "5.0")
    monkeypatch.setenv("MAX_CANDIDATES_PER_DAY", "1")
    monkeypatch.setenv("MAX_TOPICS_PER_RUN", "10")

    assert main_module.run(["--dry-run"]) == 0, "прогон обязан завершиться сам"

    with st.get_conn(db_file) as conn:
        made = conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status NOT IN ('rejected')"
        ).fetchone()[0]
    assert made <= 1, f"суточный потолок не соблюдён: создано {made}"


def test_topics_per_run_caps_expensive_stage(tmp_path, monkeypatch):
    """MAX_TOPICS_PER_RUN ограничивает вход в дорогую стадию."""
    from src import relevance
    from config import Config

    cfg = Config()
    cfg.max_topics_per_run = 2
    cfg.llm_provider = "mock"

    from src.normalize import normalize_batch

    raws = [
        {"title": f"Убийство раскрыто спустя {i} лет", "body": "Следствие, ДНК, приговор",
         "url": f"https://b/{i}", "published_at": "", "source": "BBC"}
        for i in range(10)
    ]
    items = normalize_batch(raws)
    out = relevance.score_and_filter(cfg, items)

    assert len(out) <= 2


# ---------------------------------------------------------------------------
# Кнопка «Следующий пост» должна ЛИСТАТЬ, а не приносить один и тот же пост
# ---------------------------------------------------------------------------


def test_button_advances_through_queue(db, images_dir):
    """Найдено при живом тесте владельцем: кнопка приносила один и тот же
    пост, пока по нему не принято решение, — выглядело как зависание."""
    for i in range(3):
        seed_post(f"p{i}", created_at=f"2026-08-2{i}T00:00:00+00:00",
                  draft=f"Пост номер {i}.\n\nПо данным BBC News.")

    approval_bot._last_shown.clear()
    cfg = make_cfg()
    seen = []
    for _ in range(3):
        bot = FakeBot()
        asyncio.run(approval_bot.send_next_candidate(bot, cfg, advance=True))
        seen.append(bot.last_text())

    assert len({t.splitlines()[0] for t in seen}) == 3, "кнопка не листает очередь"


def test_button_wraps_to_start_at_end(db, images_dir):
    """Дойдя до конца очереди, листание начинается сначала."""
    seed_post("a", created_at="2026-08-20T00:00:00+00:00", draft="Первый.\n\nПо данным BBC.")
    seed_post("b", created_at="2026-08-21T00:00:00+00:00", draft="Второй.\n\nПо данным BBC.")

    approval_bot._last_shown.clear()
    cfg = make_cfg()
    texts = []
    for _ in range(3):
        bot = FakeBot()
        asyncio.run(approval_bot.send_next_candidate(bot, cfg, advance=True))
        texts.append(bot.last_text().splitlines()[0])

    assert texts[0] == texts[2], "после конца очереди должен быть возврат к началу"


def test_review_command_starts_from_oldest(db, images_dir):
    """Команда /review и показ после решения по-прежнему дают самый старый
    нерешённый пост — это правильная семантика очереди."""
    seed_post("a", created_at="2026-08-20T00:00:00+00:00", draft="Первый.\n\nПо данным BBC.")
    seed_post("b", created_at="2026-08-21T00:00:00+00:00", draft="Второй.\n\nПо данным BBC.")

    approval_bot._last_shown.clear()
    cfg = make_cfg()
    bot1 = FakeBot()
    asyncio.run(approval_bot.send_next_candidate(bot1, cfg, advance=True))
    bot2 = FakeBot()
    asyncio.run(approval_bot.send_next_candidate(bot2, cfg))  # без advance

    assert "Первый" in bot2.last_text()


def test_advance_survives_deleted_post(db, images_dir):
    """Последний показанный пост исчез из базы — листание не должно падать."""
    seed_post("a", draft="Единственный.\n\nПо данным BBC.")
    approval_bot._last_shown.clear()
    approval_bot._last_shown[str(make_cfg().telegram_owner_chat_id)] = "нет-такого"

    bot = FakeBot()
    assert asyncio.run(
        approval_bot.send_next_candidate(bot, make_cfg(), advance=True)
    ) is True


# ---------------------------------------------------------------------------
# Одна история — один кандидат
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", ["candidate", "approved", "published", "hold"])
def test_active_post_blocks_second_candidate(db, status):
    """Повторный прогон по той же статье не должен делать второго кандидата.
    Реальный случай: попугаи и Локерби пришли в очередь дважды."""
    seed_post("p1", status=status)
    assert storage.has_active_post_for_event("evt_p1") is True


def test_rejected_post_does_not_block(db):
    """А вот отклонённый сюжет можно взять снова — вдруг появились детали."""
    seed_post("p1", status="rejected")
    assert storage.has_active_post_for_event("evt_p1") is False


def test_unknown_event_is_free(db):
    assert storage.has_active_post_for_event("evt_ничего") is False


# ---------------------------------------------------------------------------
# Несколько администраторов
# ---------------------------------------------------------------------------


def _cfg_multi(owner="777", admins="111,222", labels=""):
    cfg = make_cfg(owner=owner)
    cfg.telegram_admin_ids = admins
    cfg.telegram_admin_labels = labels
    return cfg


def test_owner_is_always_admin():
    """Владельца не нужно дублировать в списке — он включён всегда."""
    assert 777 in _cfg_multi().admin_ids()


def test_all_listed_admins_have_access(db):
    dp = approval_bot.build_dispatcher(_cfg_multi())
    flt = dp.message._handler.filters[0]

    from types import SimpleNamespace

    for uid in (777, 111, 222):
        ok = flt.callback(SimpleNamespace(from_user=SimpleNamespace(id=uid)))
        assert bool(ok) is True, f"админ {uid} не получил доступ"


def test_stranger_still_blocked_with_multiple_admins(db):
    dp = approval_bot.build_dispatcher(_cfg_multi())
    flt = dp.callback_query._handler.filters[0]

    from types import SimpleNamespace

    assert bool(flt.callback(SimpleNamespace(from_user=SimpleNamespace(id=999)))) is False


def test_admin_list_deduplicates():
    """Владелец, случайно указанный ещё и в списке, не дублируется."""
    ids = _cfg_multi(owner="777", admins="777,111").admin_ids()
    assert ids.count(777) == 1


@pytest.mark.parametrize("bad", ["не-число", "", "  ", "111,,222", "111, abc ,222"])
def test_malformed_admin_list_does_not_break(bad):
    """Кривая строка не должна лишать доступа остальных."""
    ids = _cfg_multi(admins=bad).admin_ids()
    assert 777 in ids, "владелец обязан остаться в списке при любом мусоре"


def test_admin_labels_parsed():
    labels = approval_bot._admin_labels(
        _cfg_multi(labels="111=Alice @alice_example, 222=Bob")
    )
    assert labels[111] == "Alice @alice_example"
    assert labels[222] == "Bob"


def test_admins_screen_lists_everyone(db, images_dir):
    cfg = _cfg_multi(labels="777=Владелец, 111=Alice, 222=Bob")
    dp = approval_bot.build_dispatcher(cfg)
    msg = FakeMessage(text=approval_bot.BTN_ADMINS)

    asyncio.run(handlers_of(dp)["msg_btn_admins"](msg))

    out = msg.answers[0]
    for name in ("Владелец", "Alice", "Bob"):
        assert name in out, f"{name} не показан в списке админов"
    assert "← это вы" in out, "текущий админ должен быть отмечен"


def test_candidate_goes_to_requesting_admin(db, images_dir):
    """Пост приходит тому, кто его запросил. Раньше адрес был жёстко
    прописан на владельца — второй админ нажимал кнопку, а пост уходил
    первому."""
    seed_post("p1")
    cfg = _cfg_multi()
    bot = FakeBot()

    asyncio.run(approval_bot.send_next_candidate(bot, cfg, chat_id=222))

    sent = (bot.photos + bot.messages)[0]
    assert sent["chat_id"] == 222


def test_each_admin_has_own_position_in_queue(db, images_dir):
    """Листание у разных админов независимое: один не сбивает другому место."""
    seed_post("a", created_at="2026-08-20T00:00:00+00:00", draft="Первый.\n\nПо данным BBC.")
    seed_post("b", created_at="2026-08-21T00:00:00+00:00", draft="Второй.\n\nПо данным BBC.")
    approval_bot._last_shown.clear()
    cfg = _cfg_multi()

    b1 = FakeBot()
    asyncio.run(approval_bot.send_next_candidate(b1, cfg, advance=True, chat_id=111))
    asyncio.run(approval_bot.send_next_candidate(FakeBot(), cfg, advance=True, chat_id=111))

    b2 = FakeBot()
    asyncio.run(approval_bot.send_next_candidate(b2, cfg, advance=True, chat_id=222))

    assert "Первый" in b2.last_text(), "второй админ должен начать с начала очереди"
