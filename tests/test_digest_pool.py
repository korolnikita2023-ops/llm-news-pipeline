"""Дайджест как живой пул и защита от дублей с учётом канала.

Две продуктовые претензии владельца 28.08:

1. Истории, подготовленные и опубликованные только в песочницу ДО
   переключения на боевой канал, блокировали повторный выбор для настоящего
   канала. Это тестовая история, она не должна закрывать сюжет навсегда.
2. Сбор в 16:16 заменил весь список: было пять историй, осталась одна.
   Четыре ещё живые темы исчезли у владельца из-под рук.
"""
from __future__ import annotations


from conftest import seed_post
from config import Config
from src import storage

SANDBOX = "-100222"
PRODUCTION = "-100111"


def make_cfg(channel: str = PRODUCTION) -> Config:
    cfg = Config()
    cfg.publish_mode = "live"
    cfg.workflow_mode = "digest"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = channel
    return cfg


def add_story(url_hash: str, score: int = 10, run_id: str = "run1",
              title: str | None = None) -> None:
    storage.save_digest(run_id, [{
        "url_hash": url_hash, "source": "bbc.com", "source_url": "https://e.com/1",
        "title": title or f"История {url_hash}", "summary": "Суть.",
        "score": score, "category": "crime", "signals": "серийность",
    }])


def add_post(post_id: str, url_hash: str, status: str, channel: str,
             reject_reason: str | None = None) -> None:
    storage.insert_raw_item({
        "url_hash": url_hash, "source": "bbc.com", "title": "T", "body": "B",
        "url": f"https://bbc.co.uk/news/{url_hash}", "published_at": "",
        "fetched_at": "2026-08-28T00:00:00+00:00", "lang": "en",
        "category": "", "sample": False,
    })
    storage.insert_event(f"evt_{url_hash}", url_hash, n_sources=1)
    storage.insert_post(post_id=post_id, event_id=f"evt_{url_hash}",
                        draft_ru="Нормальный текст поста.\n\nПо данным BBC.",
                        fmt="A", status=status,
                        scores={}, manual_only=False, reject_reason=reject_reason,
                        channel_id=channel)


# ---------------------------------------------------------------------------
# 1. Песочница не закрывает историю для боевого канала
# ---------------------------------------------------------------------------


def test_sandbox_publication_does_not_block_production(db, images_dir):
    """Опубликованное в песочницу — тестовая история, а не боевая."""
    add_story("a")
    add_post("p_sandbox", "a", "published", SANDBOX)

    assert [r["url_hash"] for r in storage.digest_pool(PRODUCTION)] == ["a"]
    assert storage.has_active_post_for_event("evt_a", PRODUCTION) is False


def test_production_publication_does_block(db, images_dir):
    """А вот настоящая публикация закрывает сюжет: второй раз не предлагаем."""
    add_story("a")
    add_post("p_live", "a", "published", PRODUCTION)

    assert storage.digest_pool(PRODUCTION) == []
    assert storage.has_active_post_for_event("evt_a", PRODUCTION) is True


def test_sandbox_candidate_does_not_block_production(db, images_dir):
    """Черновик, подготовленный для песочницы, тоже не должен мешать."""
    add_story("a")
    add_post("p_sandbox", "a", "candidate", SANDBOX)
    assert [r["url_hash"] for r in storage.digest_pool(PRODUCTION)] == ["a"]


def test_sandbox_draft_is_out_of_the_production_queue(db, images_dir):
    """Остаток песочницы не должен лежать в боевой очереди на решение.

    Нажатие ✅ на нём отправило бы в настоящий канал материал, сделанный
    для проверки. Запись при этом остаётся в истории — её не удаляют.
    """
    from src import approval_bot

    add_post("p_sandbox", "a", "candidate", SANDBOX)
    add_post("p_live", "b", "candidate", PRODUCTION)

    assert approval_bot._candidates_left(PRODUCTION) == 1
    nxt = approval_bot._next_candidate(channel_id=PRODUCTION)
    assert nxt["post_id"] == "p_live"

    # Запись песочницы никуда не делась — она в истории.
    with storage.get_conn() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM posts WHERE post_id='p_sandbox'").fetchone()[0] == 1


def test_legacy_rows_without_channel_stay_visible(db, images_dir):
    """Записи старше колонки считаем своими: между выкладкой кода и
    переносом старых строк из очереди ничего пропасть не должно."""
    from src import approval_bot

    seed_post("old1")          # channel_id остаётся пустым
    assert approval_bot._candidates_left(PRODUCTION) == 1


def test_owner_rejection_removes_the_story_from_the_pool(db, images_dir):
    """Сказал «мимо» — второй раз предлагать невежливо."""
    add_story("a")
    add_post("p1", "a", "rejected", PRODUCTION, reject_reason=storage.REJECT_BY_USER)
    assert storage.digest_pool(PRODUCTION) == []


def test_automatic_rejection_keeps_the_story_available(db, images_dir):
    """Гейт или редактор забраковали — владелец вправе попробовать снова."""
    add_story("a")
    add_post("p1", "a", "rejected", PRODUCTION, reject_reason=storage.REJECT_BY_EDITOR)
    assert [r["url_hash"] for r in storage.digest_pool(PRODUCTION)] == ["a"]


# ---------------------------------------------------------------------------
# 2. Пул: новое добавляется, старое живое остаётся
# ---------------------------------------------------------------------------


def test_new_run_adds_without_evicting(db, images_dir):
    add_story("a", score=10, run_id="утренний")
    add_story("b", score=30, run_id="вечерний")
    pool = storage.digest_pool(PRODUCTION)
    assert [r["url_hash"] for r in pool] == ["b", "a"], "сортировка по оценке"
    assert len(pool) == 2


def test_pool_is_ordered_by_score_then_freshness(db, images_dir):
    add_story("old", score=20, run_id="r1")
    add_story("new", score=20, run_id="r2")
    pool = storage.digest_pool(PRODUCTION)
    assert [r["url_hash"] for r in pool] == ["new", "old"], (
        "при равной оценке свежее выше")


def test_pool_is_limited_to_top_n(db, images_dir):
    for i in range(15):
        add_story(f"s{i}", score=i, run_id="r1")
    assert len(storage.digest_pool(PRODUCTION, limit=10)) == 10
    assert len(storage.digest_pool(PRODUCTION)) == 15


def test_chosen_story_leaves_the_pool(db, images_dir):
    add_story("a")
    add_story("b")
    rows = storage.digest_pool(PRODUCTION)
    storage.start_selection_attempt(rows[0]["id"], "2000-01-01T00:00:00+00:00",
                                    "2100-01-01T00:00:00+00:00", 9, 9)
    assert len(storage.digest_pool(PRODUCTION)) == 1


def test_released_story_returns_to_the_pool(db, images_dir):
    """Подготовка не удалась — история снова доступна."""
    add_story("a")
    rows = storage.digest_pool(PRODUCTION)
    storage.start_selection_attempt(rows[0]["id"], "2000-01-01T00:00:00+00:00",
                                    "2100-01-01T00:00:00+00:00", 9, 9)
    assert storage.digest_pool(PRODUCTION) == []
    storage.release_digest_item(rows[0]["id"])
    assert len(storage.digest_pool(PRODUCTION)) == 1


def test_pool_does_not_grow_forever(db, images_dir):
    """Срок жизни — единственное, что не даёт пулу расти бесконечно."""
    from datetime import datetime, timedelta, timezone

    add_story("old", run_id="вчерашний")
    add_story("fresh", run_id="сегодняшний")
    stale = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    with storage.get_conn() as c:
        c.execute("UPDATE digest_items SET created_at=? WHERE url_hash='old'", (stale,))

    assert storage.archive_stale_digest(24) == 1
    assert [r["url_hash"] for r in storage.digest_pool(PRODUCTION)] == ["fresh"]


def test_duplicate_story_is_not_added_again(db, images_dir):
    """Одна новость приходит из лент много прогонов подряд."""
    add_story("a", run_id="r1")
    add_story("a", run_id="r2")
    assert len(storage.digest_pool(PRODUCTION)) == 1


def test_archived_story_can_come_back_later(db, images_dir):
    """Проверка на дубли смотрит только на ЖИВЫЕ строки.

    Иначе однажды устаревшая история не смогла бы вернуться в пул, даже
    если снова стала актуальной.
    """
    add_story("a", run_id="r1")
    with storage.get_conn() as c:
        c.execute("UPDATE digest_items SET archived=1")
    add_story("a", run_id="r2")
    assert len(storage.digest_pool(PRODUCTION)) == 1


# ---------------------------------------------------------------------------
# Экраны показывают пул
# ---------------------------------------------------------------------------


def test_digest_screen_shows_the_pool(db, images_dir):
    from src import approval_bot

    add_story("a", score=10, run_id="утренний", title="Утренняя история")
    add_story("b", score=30, run_id="вечерний", title="Вечерняя история")

    text, kb = approval_bot._digest_screen(make_cfg())
    assert "Утренняя история" in text, "старая живая история не должна исчезать"
    assert "Вечерняя история" in text
    assert sum(len(r) for r in kb.inline_keyboard) == 2


def test_digest_screen_hides_sandbox_leftovers(db, images_dir):
    from src import approval_bot

    add_story("a", title="Уже опубликована в песочницу")
    add_post("p1", "a", "published", SANDBOX)
    text, _ = approval_bot._digest_screen(make_cfg())
    assert "Уже опубликована в песочницу" in text, (
        "история песочницы снова доступна для боевого канала")


def test_notification_sends_the_pool_not_one_run(db, images_dir, monkeypatch):
    from src import notify

    add_story("a", score=10, run_id="утренний", title="Утренняя")
    add_story("b", score=30, run_id="вечерний", title="Вечерняя")
    with storage.get_conn() as c:
        c.execute("INSERT OR REPLACE INTO admins "
                  "(user_id, role, label, added_by, added_at) "
                  "VALUES (111, 'admin', 'a', 0, ?)", (storage.now_iso(),))

    sent = []
    monkeypatch.setattr(notify, "_send_message",
                        lambda cfg, chat_id, text, kb=None: sent.append(text))
    rows = storage.digest_pool(PRODUCTION, 10)
    notify.send_digest(make_cfg(), "вечерний", rows, {"raw": 220, "scored": 2})

    text = "\n".join(sent)
    assert "Утренняя" in text and "Вечерняя" in text


# ---------------------------------------------------------------------------
# Находки независимого ревью 28.08.2026
# ---------------------------------------------------------------------------


def test_sandbox_draft_cannot_be_published_to_the_live_channel(db, images_dir,
                                                               monkeypatch):
    """Старая кнопка ✅ под черновиком песочницы не должна бить в эфир.

    Кнопки живут в переписке вечно. Без сверки канала нажатие на давнем
    сообщении отправило бы в настоящий канал материал, сделанный для
    проверки.
    """
    from src import approval_bot, publisher

    add_post("p_sandbox", "a", "candidate", SANDBOX)
    sent = []
    monkeypatch.setattr(publisher, "send_live_text",
                        lambda *a, **k: (sent.append(1), (True, "ok"))[1])

    ok, status = approval_bot._publish_post(make_cfg(PRODUCTION), "p_sandbox")
    assert ok is False
    assert sent == [], "в канал ничего уйти не должно"
    assert "другого канала" in status


def test_production_draft_publishes_normally(db, images_dir, monkeypatch):
    from src import approval_bot, publisher

    add_post("p_live", "b", "candidate", PRODUCTION)
    sent = []
    monkeypatch.setattr(publisher, "send_live_text",
                        lambda *a, **k: (sent.append(1), (True, "ok"))[1])

    ok, _ = approval_bot._publish_post(make_cfg(PRODUCTION), "p_live")
    assert ok is True and len(sent) == 1


def test_empty_channel_string_counts_as_no_channel(db, images_dir):
    """Пустая строка — то же, что отсутствие значения.

    Иначе такая запись исчезла бы из очереди, а опубликованный пост
    вернулся бы в пул.
    """
    from src import approval_bot

    add_story("a")
    add_post("p1", "a", "candidate", "")
    assert approval_bot._candidates_left(PRODUCTION) == 1
    assert storage.digest_pool(PRODUCTION) == [], (
        "пост без канала считается своим и закрывает сюжет")


def test_duplicates_inside_one_batch_are_collapsed(db, images_dir):
    """Один сюжет дважды в одной пачке дал бы две одинаковые кнопки."""
    row = {"url_hash": "a", "source": "s", "source_url": "u", "title": "A",
           "summary": "", "score": 10, "category": "", "signals": ""}
    assert storage.save_digest("r1", [row, dict(row)]) == 1
    assert len(storage.digest_pool(PRODUCTION)) == 1


def test_pool_query_has_an_index_to_lean_on(db, images_dir):
    """Пул на каждый показ спрашивает posts — без индекса это перебор."""
    with storage.get_conn() as c:
        names = [r[0] for r in c.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")]
    assert "idx_posts_event_channel" in names


def test_draft_id_differs_per_channel(db, images_dir):
    """Одну историю могут готовить для разных каналов в один день.

    Без канала в основе идентификатора новая подготовка сталкивалась с
    записью песочницы: либо затирала её, теряя историю, либо не могла
    записаться вовсе.
    """
    from src.normalize import NewsItem
    from src.rewrite import _draft_id

    item = NewsItem(url_hash="a", source="s", title="T", body="B",
                    url="https://e.com/1", published_at="",
                    fetched_at="2026-08-28T00:00:00+00:00", lang="ru",
                    category="", sample=False)
    sandbox_id = _draft_id(item, 0, SANDBOX)
    production_id = _draft_id(item, 0, PRODUCTION)
    assert sandbox_id != production_id
    # В пределах одного канала он по-прежнему устойчив.
    assert _draft_id(item, 0, PRODUCTION) == production_id


def test_sandbox_published_story_can_be_prepared_for_production(db, images_dir):
    """История, опубликованная в песочницу, готовится для боевого заново.

    Запись песочницы при этом остаётся: у новой свой идентификатор.
    """
    add_story("a")
    add_post("p_sandbox", "a", "published", SANDBOX)

    assert [r["url_hash"] for r in storage.digest_pool(PRODUCTION)] == ["a"]
    assert storage.insert_post(
        post_id="p_production", event_id="evt_a", draft_ru="Свежий текст.",
        fmt="A", status="candidate", scores={}, manual_only=False,
        channel_id=PRODUCTION,
    ) is True

    with storage.get_conn() as c:
        rows = {r[0]: r[1] for r in c.execute(
            "SELECT post_id, status FROM posts WHERE event_id='evt_a'")}
    assert rows == {"p_sandbox": "published", "p_production": "candidate"}, (
        "запись песочницы должна сохраниться")
