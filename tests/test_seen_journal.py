"""Журнал виденного: за что платим и что теряем (S34–S38).

Журнал заводили ради одного: не платить дважды за одну и ту же новость.
RSS отдаёт одни и те же записи часами, и до журнала в базе накопилось
676 оценок релевантности на 18 уникальных новостей.

Здесь проверяется, что журнал делает ровно то, ради чего сделан, и не
делает того, ради чего не сделан: не выбрасывает молча свежие новости,
за которые никто не заплатил.
"""
from __future__ import annotations

import pytest

from src import storage


def _isolate(tmp_path, monkeypatch):
    """Полная изоляция путей прогона: база, черновики, картинки, логи."""
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
    monkeypatch.setenv("PUBLISH_MODE", "draft")
    monkeypatch.setenv("MIN_EDITORIAL_AVG", "5.0")
    monkeypatch.setenv("MAX_CANDIDATES_PER_DAY", "3")
    monkeypatch.setenv("MAX_TOPICS_PER_RUN", "10")
    storage.init_db(db_file)
    return db_file


# Сюжеты нарочно РАЗНЫЕ: близкие заголовки склеит MinHash-дедуп ещё до
# журнала, и тест будет проверять не то, что задумано. И у каждого есть
# сюжетный маркер — иначе их снимет дешёвый отбор и до журнала они не
# дойдут: эти тесты про журнал, а не про отбор.
_STORIES = [
    ("Убийство раскрыто спустя двенадцать лет благодаря ДНК",
     "Следствие возобновило дело, генетическая экспертиза дала совпадение."),
    ("Похищение банкира в Марселе закончилось арестом посредника",
     "Полиция вышла на подозреваемого через оплату аренды автомобиля."),
    ("Схема с поддельными завещаниями нотариуса раскрыта спустя 8 лет",
     "Суд установил ущерб восьми семьям, дело вели четыре года."),
    ("Побег из тюрьмы через мастерскую вскрыл подлог документов",
     "Администрация не заметила подмены пропусков в течение месяца."),
]


def _news(n: int = 3):
    """Сырые записи ленты — такие же по форме, как отдаёт ingest_rss.

    Дата публикации — всегда «час назад»: с фиксированной датой записи
    старели, попадали под фильтр возраста (STORY_MAX_AGE_HOURS) и тесты
    журнала виденного падали по посторонней причине.
    """
    from datetime import datetime, timedelta, timezone

    fresh = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    return [
        {
            "title": _STORIES[i][0],
            "summary": _STORIES[i][1],
            "url": f"https://example.com/news/{i}",
            "published_at": fresh,
            "source": "BBC",
            "lang": "ru",
            "category": "rss",
        }
        for i in range(n)
    ]


def _fake_ingest(raws):
    """_ingest_live возвращает пару (записи, отчёт по источникам)."""
    return lambda cfg: (list(raws), [])


def _seen_hashes(db_file):
    with storage.get_conn(db_file) as conn:
        return {r[0] for r in conn.execute("SELECT url_hash FROM seen_items")}


def _hash_of(raw):
    from src.normalize import normalize_raw

    return normalize_raw(raw).url_hash


# ---------------------------------------------------------------------------
# S34. Уже виденное не уходит в платную оценку
# ---------------------------------------------------------------------------


def test_seen_items_do_not_reach_paid_scoring(tmp_path, monkeypatch):
    """Главное, ради чего журнал существует.

    Раньше отфильтрованный список вычислялся, печатался в лог и
    ВЫБРАСЫВАЛСЯ: в оценку уходил весь батч вместе с уже виденными, и
    каждый прогон платил за них заново.
    """
    from src import main as main_module, relevance

    db_file = _isolate(tmp_path, monkeypatch)
    raws = _news(3)
    storage.mark_seen([_hash_of(raws[0])], db_file)   # первую уже видели

    scored_titles = []
    original = relevance.score_item
    monkeypatch.setattr(
        relevance, "score_item",
        lambda cfg, item: (scored_titles.append(item.title), original(cfg, item))[1],
    )
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(raws))

    main_module.run([])

    assert raws[0]["title"] not in scored_titles, (
        "за уже виденную новость заплатили второй раз"
    )
    # Порядок оценки задаёт дешёвый отбор (сильные сюжеты первыми), поэтому
    # проверяем состав, а не позиции.
    assert set(scored_titles) == {raws[1]["title"], raws[2]["title"]}


# ---------------------------------------------------------------------------
# S35. Прогон, упёршийся в суточный лимит, не сжигает пачку
# ---------------------------------------------------------------------------


def test_daily_cap_does_not_burn_the_batch(tmp_path, monkeypatch):
    """Реальный случай: прогон упёрся в лимит и пометил виденными 137
    свежих новостей, ни одну не оценив. Больше они не всплывут никогда."""
    from src import main as main_module, relevance

    db_file = _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_CANDIDATES_PER_DAY", "1")
    storage.insert_post(
        post_id="already", event_id="evt", draft_ru="Текст", fmt="A",
        status="candidate", scores={}, db_path=db_file,
    )

    raws = _news(3)
    scored = []
    monkeypatch.setattr(relevance, "score_item", lambda cfg, item: scored.append(item) )
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(raws))

    main_module.run([])

    assert scored == [], "при достигнутом лимите модель дёргать нельзя"
    assert _seen_hashes(db_file) == set(), (
        "непросмотренная пачка помечена виденной и потеряна навсегда"
    )


# ---------------------------------------------------------------------------
# S36. Обрыв на середине помечает только оплаченное
# ---------------------------------------------------------------------------


def test_budget_stop_marks_only_scored_items(tmp_path, monkeypatch):
    """Бюджет кончился на второй новости: первая оценена и помечена,
    остальные должны вернуться в следующий прогон."""
    from src.llm_client import LLMBudgetExceeded
    from src import main as main_module, relevance

    db_file = _isolate(tmp_path, monkeypatch)
    raws = _news(3)
    calls = {"n": 0}
    original = relevance.score_item

    def flaky(cfg, item):
        calls["n"] += 1
        if calls["n"] > 1:
            raise LLMBudgetExceeded("дневной лимит исчерпан")
        return original(cfg, item)

    monkeypatch.setattr(relevance, "score_item", flaky)
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(raws))

    with pytest.raises(SystemExit):
        main_module.run([])

    seen = _seen_hashes(db_file)
    # Какая именно новость пошла первой, решает дешёвый отбор. Важно другое:
    # помечена РОВНО ОДНА — та, за которую заплатили, а не вся пачка.
    assert len(seen) == 1, f"помечено {len(seen)} новостей вместо одной оплаченной"
    assert seen <= {_hash_of(r) for r in raws}


# ---------------------------------------------------------------------------
# S37. Оплаченное помечено — второй прогон за него не платит
# ---------------------------------------------------------------------------


def test_scored_items_are_marked_so_next_run_is_free(tmp_path, monkeypatch):
    from src import main as main_module, relevance

    db_file = _isolate(tmp_path, monkeypatch)
    raws = _news(2)
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(raws))

    main_module.run([])
    assert _seen_hashes(db_file) == {_hash_of(r) for r in raws}

    scored = []
    original = relevance.score_item
    monkeypatch.setattr(
        relevance, "score_item",
        lambda cfg, item: (scored.append(item), original(cfg, item))[1],
    )
    main_module.run([])

    assert scored == [], "второй прогон обязан обойтись без единой платной оценки"


# ---------------------------------------------------------------------------
# S38. Прогон на тестовых данных журнал не трогает
# ---------------------------------------------------------------------------


def test_dry_run_does_not_touch_the_journal(tmp_path, monkeypatch):
    """Иначе один прогон на sample-данных отравил бы боевой журнал."""
    from src import main as main_module

    db_file = _isolate(tmp_path, monkeypatch)
    main_module.run(["--dry-run"])
    assert _seen_hashes(db_file) == set()


# ---------------------------------------------------------------------------
# S39. Оплаченный, но упавший вызов — помечаем, чтобы не платить дважды
# ---------------------------------------------------------------------------


def test_paid_but_failed_scoring_is_marked(tmp_path, monkeypatch):
    """Стоимость пишется сразу после ответа провайдера, а разбор JSON идёт
    после. Модель ответила мусором дважды — деньги списаны, результата нет.
    Такую новость надо пометить: следующий прогон иначе заплатит снова.
    """
    from src.llm_client import LLMResponseError
    from src import main as main_module, relevance

    db_file = _isolate(tmp_path, monkeypatch)
    raws = _news(2)

    def paid_then_fails(cfg, item):
        storage.log_cost(stage="relevance", provider="gemini",
                         model="gemini-flash-lite-latest", usd_cost=0.00018)
        raise LLMResponseError("LLM вернул невалидный JSON дважды подряд")

    monkeypatch.setattr(relevance, "score_item", paid_then_fails)
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(raws))

    with pytest.raises(LLMResponseError):
        main_module.run([])

    seen = _seen_hashes(db_file)
    assert len(seen) == 1, (
        "за новость заплатили, но не пометили — следующий прогон заплатит снова"
    )
    assert seen <= {_hash_of(r) for r in raws}


def test_failure_without_payment_is_not_marked(tmp_path, monkeypatch):
    """Обратный случай: лимит сработал ДО обращения к провайдеру, денег не
    потрачено — новость должна вернуться в следующий прогон."""
    from src.llm_client import LLMBudgetExceeded
    from src import main as main_module, relevance

    db_file = _isolate(tmp_path, monkeypatch)
    raws = _news(2)

    def fails_before_paying(cfg, item):
        raise LLMBudgetExceeded("дневной лимит исчерпан до вызова")

    monkeypatch.setattr(relevance, "score_item", fails_before_paying)
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(raws))

    with pytest.raises(SystemExit):
        main_module.run([])

    assert _seen_hashes(db_file) == set(), "неоплаченное помечать нельзя"
