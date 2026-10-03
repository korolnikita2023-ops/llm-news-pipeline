"""Discovery-слой: реестр источников, дешёвый отбор, потолки (S40–S52).

Главный риск этого куска — не «источник не подключился», а тихий рост
расходов: оценка модели вызывается на каждую запись ленты, и без потолка
цена прогона растёт вслед за числом источников. Второй риск — что упавшая
лента утащит за собой весь прогон.
"""
from __future__ import annotations

import pytest

from config import Config
from src import prefilter, sources, storage
from src.normalize import normalize_batch, normalize_raw


def make_cfg(**over) -> Config:
    cfg = Config()
    cfg.llm_provider = "mock"
    cfg.gemini_api_key = ""
    cfg.discovery_enabled = True
    cfg.sources_disabled = ""
    cfg.sources_enabled = ""
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def item(title, body="", source="DOJ", url="https://example.com/1"):
    return normalize_raw({"title": title, "summary": body, "url": url, "source": source})


# ---------------------------------------------------------------------------
# S40. Реестр и выключатели
# ---------------------------------------------------------------------------


def test_four_sources_enabled_on_first_rollout():
    """Решение владельца от 27.08: ровно четыре источника, Guardian-law ждёт."""
    names = {s.name for s in sources.enabled_sources(make_cfg())}
    assert {"DOJ", "Europol", "NCA", "Guardian-crime"} <= names
    assert "Guardian-law" not in names, "включён источник, который решили придержать"
    assert "RAPSI" not in names, "исследовательский источник не должен включаться сам"


def test_old_feeds_survive_alongside_new():
    """BBC, ТАСС и Интерфакс продолжают работать — они страховка."""
    names = {s.name for s in sources.enabled_sources(make_cfg())}
    assert {"BBC", "TASS", "Interfax"} <= names


def test_source_can_be_disabled_by_name():
    cfg = make_cfg(sources_disabled="Europol, NCA")
    names = {s.name for s in sources.enabled_sources(cfg)}
    assert "Europol" not in names and "NCA" not in names
    assert "DOJ" in names, "выключение одного не должно гасить остальные"


def test_whole_discovery_layer_can_be_switched_off():
    """Аварийный тумблер: одна строка в .env возвращает конвейер к трём лентам."""
    cfg = make_cfg(discovery_enabled=False)
    names = {s.name for s in sources.enabled_sources(cfg)}
    assert names == {"BBC", "TASS", "Interfax"}


def test_research_source_can_be_switched_on():
    cfg = make_cfg(sources_enabled="RAPSI")
    assert "RAPSI" in {s.name for s in sources.enabled_sources(cfg)}


def test_disabled_source_is_never_fetched(monkeypatch):
    """Выключенный источник не должен даже опрашиваться."""
    from src import ingest_rss

    fetched = []
    monkeypatch.setattr(
        ingest_rss, "fetch_feed",
        lambda url, source_label=None, **kw: (fetched.append(source_label), [])[1],
    )
    enabled = sources.enabled_sources(make_cfg(sources_disabled="Europol"))
    with pytest.raises(RuntimeError):        # все ленты пустые — это громкий сбой
        ingest_rss.fetch_sources(enabled, polite_delay=0)

    assert "Europol" not in fetched
    assert "DOJ" in fetched


# ---------------------------------------------------------------------------
# S41. Изоляция сбоев источника
# ---------------------------------------------------------------------------


def test_one_broken_source_does_not_break_the_others(monkeypatch):
    """Сбой одной ленты не должен уносить остальные и весь прогон.

    Ломаем на уровне сети (feedparser), а не подменой fetch_feed: подмена
    самой fetch_feed проверяла бы мок, а не поведение кода. Прошлая версия
    этого теста ожидала RuntimeError — то есть проверяла ровно обратное
    тому, что заявлено в названии.
    """
    from src import ingest_rss

    class Parsed:
        bozo = 0
        feed = {"title": "лента"}
        entries = [{"title": "Ограбление банка в Лидсе раскрыто по телефону",
                    "link": "https://example.com/1", "summary": "Текст.",
                    "published": "2026-08-27T10:00:00+00:00"}]

    def parse(url, **kw):
        if "justice.gov" in url:
            raise OSError("connection reset by peer")
        return Parsed()

    monkeypatch.setattr(ingest_rss.feedparser, "parse", parse)
    monkeypatch.setattr(ingest_rss.time, "sleep", lambda s: None)
    enabled = [s for s in sources.enabled_sources(make_cfg()) if s.name in ("DOJ", "NCA")]

    items, report = ingest_rss.fetch_sources(enabled, retries=0, polite_delay=0)

    statuses = {r["name"]: r["status"] for r in report}
    assert statuses["DOJ"] == "ПУСТО/СБОЙ", "упавшая лента обязана быть видна в отчёте"
    assert statuses["NCA"] == "ok", "живая лента обязана отработать"
    assert len(items) == 1, "записи живого источника должны дойти"


def test_real_fetch_feed_never_raises(monkeypatch):
    """Настоящая функция обязана глотать любой сбой сети и парсинга."""
    from src import ingest_rss

    def explode(*a, **kw):
        raise OSError("connection reset")

    monkeypatch.setattr(ingest_rss.feedparser, "parse", explode)
    assert ingest_rss.fetch_feed("https://example.com/rss", "DOJ") == []


def test_empty_feed_is_reported_not_crashed(monkeypatch):
    from src import ingest_rss

    monkeypatch.setattr(
        ingest_rss, "fetch_feed",
        lambda url, source_label=None, **kw: [] if source_label == "DOJ" else [
            {"title": "Похищение в Марселе", "url": "https://x/1", "summary": "",
             "source": source_label, "published_at": ""}],
    )
    enabled = [s for s in sources.enabled_sources(make_cfg()) if s.name in ("DOJ", "NCA")]
    items, report = ingest_rss.fetch_sources(enabled, polite_delay=0)

    statuses = {r["name"]: r["status"] for r in report}
    assert statuses["DOJ"] == "ПУСТО/СБОЙ", "молчащую ленту надо показать в отчёте"
    assert len(items) == 1, "живой источник должен отработать"


def test_malformed_feed_gives_no_items(monkeypatch):
    """Битый XML — это ноль записей и предупреждение, а не падение."""
    from src import ingest_rss

    class Broken:
        bozo = 1
        bozo_exception = "not well-formed (invalid token)"
        entries = []
        feed = {}

    monkeypatch.setattr(ingest_rss.feedparser, "parse", lambda *a, **kw: Broken())
    assert ingest_rss.fetch_feed("https://example.com/rss", "DOJ") == []


def test_retry_is_bounded(monkeypatch):
    """Повторы конечны: одна лишняя попытка, а не бесконечный цикл."""
    from src import ingest_rss

    calls = {"n": 0}

    def always_empty(*a, **kw):
        calls["n"] += 1
        return []

    monkeypatch.setattr(ingest_rss, "_fetch_once", always_empty)
    monkeypatch.setattr(ingest_rss.time, "sleep", lambda s: None)
    ingest_rss.fetch_feed("https://example.com/rss", "DOJ", retries=2)

    assert calls["n"] == 3, f"ожидали 1 попытку + 2 повтора, было {calls['n']}"


# ---------------------------------------------------------------------------
# S42. Дешёвый отбор
# ---------------------------------------------------------------------------


def test_prefilter_keeps_crime_story():
    v = prefilter.score_item(item("Убийство раскрыто спустя 12 лет благодаря ДНК"))
    assert v.score >= 2, f"сюжетная история не прошла: {v.reasons}"


def test_source_weight_cannot_carry_an_empty_item():
    """Урок двух живых прогонов: вес источника не должен протаскивать
    материал без сюжета. Сначала +3 официальным вынесло наверх рутину
    минюста, потом +2 изданию — колонки Guardian."""
    empty = item("Burnham and the prisons crisis: an honest conversation is overdue",
                 source="Guardian-crime")
    story = item("Cold case murder solved after 30 years by DNA genealogy", source="DOJ")

    assert prefilter.score_item(empty).score < prefilter.score_item(story).score
    # Вес источника не участвует в оценке — только в сортировке равных.
    assert prefilter.tiebreak(story, {"DOJ": 1}) == 1
    assert "источник" not in " ".join(prefilter.score_item(story).reasons)


def test_prefilter_drops_obvious_noise():
    for title in (
        "Season 4 of The Europol Podcast available now",
        "Europol and Frontex strengthen cooperation with new Working Arrangement",
        "Национальная технологическая олимпиада открыла регистрацию на новый сезон",
    ):
        v = prefilter.score_item(item(title))
        assert v.score < 2, f"шум прошёл отбор: {title} (оценка {v.score})"


def test_war_news_is_not_crime_news():
    """Канал про преступления, а не про фронт. Сводки лезли наверх через
    криминальную лексику в тексте."""
    v = prefilter.score_item(item("Израиль ликвидировал командира военного крыла ХАМАС"))
    assert v.score < 0, f"военная сводка прошла как криминал: {v.reasons}"


def test_routine_penalty_is_not_a_hard_reject():
    """За стандартной формулировкой может прятаться сильная история."""
    plain = prefilter.score_item(item("Man Charged with Unlawfully Possessing a Firearm"))
    with_story = prefilter.score_item(item(
        "Fugitive charged with unlawfully possessing a firearm caught after 12 years on the run"
    ))
    assert plain.score < 0, "рутина обязана уходить вниз"
    assert with_story.score > plain.score, "история обязана вытягивать рутинную формулировку"
    assert any("рутинная" in r for r in with_story.reasons), "штраф применён, но не убил материал"


def test_story_markers_lift_the_score():
    for title, marker in (
        ("Serial killer sentenced for murders of five women", "серийность"),
        ("Cold case from 1998 solved after DNA breakthrough", "висяк раскрыт"),
        ("Man exonerated after 20 years in prison for murder", "судебная ошибка"),
        ("Prisoner escapes during transfer, manhunt under way", "похищение"),
        ("Killer buried victim in concrete under his own house", "необычный способ"),
    ):
        v = prefilter.score_item(item(title))
        assert v.score >= 3, f"{title} → {v.score} ({v.reasons})"
        assert any(marker in r for r in v.reasons), f"не сработал фактор {marker}: {v.reasons}"


def test_prefilter_ranks_story_above_routine():
    story = prefilter.score_item(item("Cold case murder solved after 30 years by DNA genealogy"))
    routine = prefilter.score_item(item("Man pleads guilty to possession of child pornography"))
    assert story.score > routine.score


def test_prefilter_limit_is_a_hard_cap():
    """Сколько бы лент ни добавили, дороже прогон не станет."""
    items = [
        item(f"Убийство раскрыто спустя {i} лет благодаря ДНК",
             source=f"src{i % 10}", url=f"https://x/{i}")
        for i in range(200)
    ]
    kept, dropped = prefilter.select(items, weights={}, min_score=2, limit=40)

    assert len(kept) == 40
    assert len(kept) + len(dropped) == 200, "материалы не должны теряться"


def test_one_source_cannot_take_the_whole_shortlist():
    """Квота: одна говорливая лента не занимает больше 40% потолка."""
    items = [item(f"Убийство раскрыто спустя {i} лет благодаря ДНК",
                  source="DOJ", url=f"https://x/{i}") for i in range(100)]
    kept, _ = prefilter.select(items, weights={}, min_score=2, limit=40, quota_share=0.4)

    assert len(kept) == 16, f"квота 40% от 40 — это 16, получили {len(kept)}"


def test_quota_leaves_room_for_other_sources():
    loud = [item(f"Убийство раскрыто спустя {i} лет благодаря ДНК",
                 source="DOJ", url=f"https://d/{i}") for i in range(100)]
    quiet = [item("Похищение банкира в Марселе закончилось арестом посредника",
                  source="Europol", url="https://e/1")]
    kept, _ = prefilter.select(loud + quiet, weights={}, min_score=2, limit=40, quota_share=0.4)

    assert "Europol" in {v.item.source for v in kept}, "тихий источник вытеснен громким"


def test_prefilter_never_loses_items():
    items = [item("Убийство", url="https://x/1"), item("Погода на завтра", url="https://x/2")]
    kept, dropped = prefilter.select(items, weights={}, min_score=2, limit=10)
    assert len(kept) + len(dropped) == len(items)


# ---------------------------------------------------------------------------
# S43. Дедуп между источниками
# ---------------------------------------------------------------------------


def test_same_case_from_two_sources_gives_one_candidate():
    """Требование владельца: одно дело из DOJ и Europol — один кандидат."""
    from src.dedupe_l1 import drop_cross_source_repeats

    doj = item(
        "Justice Department announces takedown of Sinaloa fentanyl network, 47 arrested",
        "Federal prosecutors charged 47 defendants across three states in a fentanyl "
        "trafficking network linked to the Sinaloa cartel.",
        source="DOJ", url="https://justice.gov/1",
    )
    europol = item(
        "Europol supports operation against Sinaloa fentanyl network, 47 arrests",
        "A coordinated action against a fentanyl trafficking network linked to the "
        "Sinaloa cartel led to 47 arrests across three states.",
        source="Europol", url="https://europol.europa.eu/2",
    )
    kept = drop_cross_source_repeats([(doj, None), (europol, None)])

    assert len(kept) == 1
    assert kept[0][0].source == "DOJ", "остаться должен первый по оценке редактора"


def test_different_cases_are_not_merged():
    from src.dedupe_l1 import drop_cross_source_repeats

    a = item("Ограбление банка в Лидсе раскрыто по забытому телефону",
             "Мужчина осуждён за разбой после того, как выронил мобильный.",
             source="Guardian-crime", url="https://g/1")
    b = item("Похищение банкира в Марселе закончилось арестом посредника",
             "Полиция вышла на подозреваемого через оплату аренды автомобиля.",
             source="NCA", url="https://n/2")

    assert len(drop_cross_source_repeats([(a, None), (b, None)])) == 2


def test_same_source_repeating_one_story_is_deduped():
    """Лента отдаёт одну новость дважды с разными ссылками."""
    from src.dedupe_l1 import dedupe

    raws = [
        {"title": "Похищение банкира в Марселе закончилось арестом посредника",
         "summary": "Полиция вышла на подозреваемого через оплату аренды.",
         "url": "https://doj.gov/a", "source": "DOJ"},
        {"title": "Похищение банкира в Марселе закончилось арестом посредника",
         "summary": "Полиция вышла на подозреваемого через оплату аренды.",
         "url": "https://doj.gov/b", "source": "DOJ"},
    ]
    assert len(dedupe(normalize_batch(raws))) == 1


# ---------------------------------------------------------------------------
# S44. Провенанс доезжает до кандидата
# ---------------------------------------------------------------------------


def test_provenance_survives_to_the_database(db):
    """Источник и ссылка нужны для строки «По данным DOJ» под постом."""
    raw = {
        "title": "Похищение банкира в Марселе", "summary": "Текст.",
        "url": "https://www.justice.gov/news/case-1?utm_source=rss",
        "source": "DOJ", "source_kind": "official", "source_role": "discovery",
        "published_at": "2026-08-27T10:00:00+00:00",
    }
    news = normalize_raw(raw)
    storage.insert_raw_item(news.to_dict())

    with storage.get_conn() as conn:
        row = dict(conn.execute(
            "SELECT source, url, published_at, source_kind, source_role FROM raw_items"
        ).fetchone())

    assert row["source"] == "DOJ"
    assert row["url"] == "https://www.justice.gov/news/case-1", "трекинг убран, ссылка цела"
    assert row["published_at"] == "2026-08-27T10:00:00+00:00"
    assert row["source_kind"] == "official"
    assert row["source_role"] == "discovery"


def test_missing_date_does_not_break_anything(db):
    """У Europol в ленте нет дат — это не должно ломать запись."""
    news = normalize_raw({"title": "78 arrests in smuggling bust", "summary": "Text.",
                          "url": "https://europol.europa.eu/x", "source": "Europol"})
    storage.insert_raw_item(news.to_dict())
    with storage.get_conn() as conn:
        assert conn.execute("SELECT published_at FROM raw_items").fetchone()[0] == ""


# ---------------------------------------------------------------------------
# S45. Потолки
# ---------------------------------------------------------------------------


def test_pending_queue_counter_counts_only_undecided(db):
    from conftest import seed_post

    seed_post("a", status="candidate")
    seed_post("b", status="approved")
    seed_post("c", status="published")
    seed_post("d", status="rejected")
    seed_post("e", status="hold")

    assert storage.count_pending_posts() == 2, "ждут решения только candidate и approved"


def test_full_queue_stops_expensive_stage(tmp_path, monkeypatch):
    """Очередь полна — дорогую генерацию не запускаем."""
    from conftest import seed_post
    from test_seen_journal import _isolate, _fake_ingest, _news
    from src import main as main_module, relevance

    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_PENDING_QUEUE", "2")
    seed_post("a", status="candidate")
    seed_post("b", status="candidate")

    scored = []
    monkeypatch.setattr(relevance, "score_item", lambda cfg, i: scored.append(i))
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(_news(3)))

    assert main_module.run([]) == 0
    assert scored == [], "при полной очереди модель дёргать нельзя"


def test_run_cost_cap_shrinks_the_batch():
    """Денежный потолок считается ДО стадии и режет размер пачки."""
    cfg = make_cfg(max_run_cost_usd=0.001, max_llm_items_per_run=40)
    cfg.llm_provider = "gemini"
    cfg.gemini_api_key = "test-key"

    from src.main import _worst_case_item_cost

    worst = _worst_case_item_cost(cfg)
    assert worst > 0, "для живого провайдера худшая цена обязана быть больше нуля"
    affordable = int(cfg.max_run_cost_usd / worst)
    assert affordable < 40, "при копеечном потолке пачка обязана сжаться"


def test_budget_caps_are_nested():
    """Три потолка вложены: прогон ≤ сутки, сутки×30 ≤ месяц. Прежняя
    конфигурация задавала $3 в СУТКИ, то есть $90 в месяц при бюджете
    проекта $3–5."""
    cfg = Config()
    assert cfg.max_run_cost_usd <= cfg.daily_cost_limit_usd
    assert cfg.daily_cost_limit_usd * 30 <= cfg.monthly_cost_limit_usd
    assert cfg.monthly_cost_target_usd < cfg.monthly_cost_limit_usd


def test_monthly_cap_stops_the_run(tmp_path, monkeypatch):
    """Месячный предел — главный: бюджет проекта задан в месяц."""
    from test_seen_journal import _isolate, _fake_ingest, _news
    from src import main as main_module

    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("MONTHLY_COST_LIMIT_USD", "0.001")
    storage.log_cost("relevance", "gemini", "m", 0.002)
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(_news(2)))

    with pytest.raises(SystemExit, match="есячный предел"):
        main_module.run([])


def test_daily_cap_stops_the_run(tmp_path, monkeypatch):
    """Дневной — аварийный предохранитель поверх месячного."""
    from test_seen_journal import _isolate, _fake_ingest, _news
    from src import main as main_module

    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("DAILY_COST_LIMIT_USD", "0.001")
    monkeypatch.setenv("MONTHLY_COST_LIMIT_USD", "5.0")
    storage.log_cost("relevance", "gemini", "m", 0.002)
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(_news(2)))

    with pytest.raises(SystemExit, match="невной лимит|дневной лимит"):
        main_module.run([])


# ---------------------------------------------------------------------------
# S46–S50. Найдено независимым ревью: потолки были не жёсткими
# ---------------------------------------------------------------------------


def test_run_cost_cap_reserves_before_spending():
    """Сравнивать потраченное с лимитом ПОСЛЕ вызова поздно: переписывание
    с регенерациями стоит на порядок дороже оценки, и один элемент успел бы
    увести прогон за лимит. Резерв считается заранее и покрывает все
    попытки, а не одну."""
    from src.main import _worst_case_post_cost

    cfg = make_cfg()
    cfg.llm_provider = "gemini"
    cfg.gemini_api_key = "test-key"

    cfg.max_regenerate_attempts = 2
    with_retries = _worst_case_post_cost(cfg)
    cfg.max_regenerate_attempts = 0
    without_retries = _worst_case_post_cost(cfg)

    assert with_retries > without_retries > 0


def test_almost_full_queue_does_not_overflow(tmp_path, monkeypatch):
    """Очередь 2 из 3: за прогон нельзя создать столько, чтобы стало 4.
    Проверка очереди только перед стадией этого не гарантировала."""
    from conftest import seed_post
    from test_seen_journal import _isolate, _fake_ingest, _news
    from src import main as main_module

    _isolate(tmp_path, monkeypatch)
    monkeypatch.setenv("MAX_PENDING_QUEUE", "3")
    monkeypatch.setenv("MAX_CANDIDATES_PER_DAY", "10")
    monkeypatch.setenv("MAX_TOPICS_PER_RUN", "10")
    seed_post("a", status="candidate")
    seed_post("b", status="candidate")
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(_news(4)))

    main_module.run([])

    pending = storage.count_pending_posts()
    assert pending <= 3, f"очередь переполнена: {pending} при потолке 3"


def test_discovery_only_writes_nothing(tmp_path, monkeypatch):
    """Режим разведки не должен ни создавать схему, ни пополнять журнал."""
    from test_seen_journal import _isolate, _fake_ingest, _news
    from src import main as main_module, relevance

    db_file = _isolate(tmp_path, monkeypatch)
    db_file.unlink()                      # базы нет вовсе
    scored = []
    monkeypatch.setattr(relevance, "score_item", lambda cfg, i: scored.append(i))
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(_news(3)))

    assert main_module.run(["--discovery-only"]) == 0
    assert scored == [], "разведка не должна дёргать модель"
    assert not db_file.exists(), "разведка не должна создавать базу"


def test_cross_source_dedup_runs_before_truncation(db, images_dir, monkeypatch):
    """Дубль не должен занимать слот дня: склейка идёт ДО обрезки, и на
    освободившееся место встаёт следующая уникальная тема."""
    from src import relevance
    from src.dedupe_l1 import drop_cross_source_repeats
    from src.relevance import RelevanceResult

    cfg = make_cfg()
    cfg.max_topics_per_run = 2
    cfg.min_relevance_sum = 0

    batch = [
        item("Justice Department announces takedown of Sinaloa fentanyl network, 47 arrested",
             "Federal prosecutors charged 47 defendants across three states in a fentanyl "
             "trafficking network linked to the Sinaloa cartel.", "DOJ", "https://a/1"),
        item("Europol supports operation against Sinaloa fentanyl network, 47 arrests",
             "A coordinated action against a fentanyl trafficking network linked to the "
             "Sinaloa cartel led to 47 arrests across three states.", "Europol", "https://b/2"),
        item("Похищение банкира в Марселе закончилось арестом посредника",
             "Полиция вышла на подозреваемого через оплату аренды автомобиля.",
             "NCA", "https://c/3"),
    ]
    totals = iter([10, 9, 8])
    monkeypatch.setattr(
        relevance, "score_item",
        lambda cfg_, it: RelevanceResult(True, next(totals), 0, 0, 0, 0, "громкое дело"),
    )

    out = relevance.score_and_filter(cfg, batch, dedupe_sorted=drop_cross_source_repeats)

    got = [pair[0].source for pair in out]
    assert len(out) == 2, f"ожидали две темы, получили {got}"
    assert "Europol" not in got, "дубль должен быть склеен"
    assert "NCA" in got, "на освободившийся слот обязана встать третья тема"


def test_migration_survives_parallel_processes(db):
    """Бот и конвейер стартуют независимо и оба зовут init_db: гонка между
    проверкой колонки и ALTER не должна ронять второй процесс."""
    import sqlite3

    from src import storage as st

    class RacingConn:
        """Соединение, в котором ALTER всегда проигрывает гонку."""

        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *a, **kw):
            if sql.startswith("ALTER TABLE"):
                raise sqlite3.OperationalError("duplicate column name: source_kind")
            return self.inner.execute(sql, *a, **kw)

    with st.get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS raw_items")
        conn.execute(
            "CREATE TABLE raw_items (url_hash TEXT PRIMARY KEY, source TEXT, "
            "title TEXT, body TEXT, url TEXT, published_at TEXT, "
            "fetched_at TEXT, lang TEXT, category TEXT, sample INTEGER)"
        )

    with st.get_conn() as conn:
        st._migrate(RacingConn(conn))     # не должно бросить исключение


def test_migration_reraises_real_errors(db):
    """А настоящую ошибку миграции глушить нельзя."""
    import sqlite3

    import pytest as _pytest

    from src import storage as st

    class BrokenConn:
        def __init__(self, inner):
            self.inner = inner

        def execute(self, sql, *a, **kw):
            if sql.startswith("ALTER TABLE"):
                raise sqlite3.OperationalError("database disk image is malformed")
            return self.inner.execute(sql, *a, **kw)

    with st.get_conn() as conn:
        conn.execute("DROP TABLE IF EXISTS raw_items")
        conn.execute(
            "CREATE TABLE raw_items (url_hash TEXT PRIMARY KEY, source TEXT, "
            "title TEXT, body TEXT, url TEXT, published_at TEXT, "
            "fetched_at TEXT, lang TEXT, category TEXT, sample INTEGER)"
        )

    with st.get_conn() as conn:
        with _pytest.raises(sqlite3.OperationalError):
            st._migrate(BrokenConn(conn))


# ---------------------------------------------------------------------------
# S53–S60. Найдено независимым ревью скоринга
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title", [
    "Concrete plans for city centre approved",       # ловилось на «concrete»
    "Shopping spree begins this weekend",            # ловилось на «spree»
    "Village mystery solved after 20 years",         # ловилось на «solved after»
])
def test_story_words_alone_do_not_prove_a_crime(title):
    """Гейт обходили широкие слова сюжетных факторов. Доказывать преступление
    может только узкая формулировка, которая сама про преступление."""
    assert prefilter.score_item(item(title)).score < 0, f"мусор прошёл: {title}"


def test_noise_is_a_hard_reject_even_with_a_story():
    """Подкаст остаётся подкастом, какой бы сюжет в названии ни стоял.
    Со штрафом вместо отказа «Podcast: Cold case murder solved» набирал
    проходной балл."""
    v = prefilter.score_item(item("Podcast: Cold case murder solved after 30 years by DNA"))
    assert v.score < 0, f"анонс прошёл как история: {v.reasons}"


def test_serial_killings_is_not_lost():
    """«serial killer» ловился, «serial killings» — нет."""
    v = prefilter.score_item(item("Serial killings linked across three states"))
    assert v.score >= 3, f"сильная история потеряна: {v.reasons}"


def test_war_in_body_is_caught():
    """Сводка с фронта в теле новости проходила как криминал."""
    v = prefilter.score_item(item(
        "Kidnapped hostages freed after manhunt",
        "Israeli forces and Hamas in Gaza strip ceasefire talks continue.",
    ))
    assert v.score < 0, f"война прошла как криминал: {v.reasons}"


def test_corruption_of_officials_is_our_genre():
    """Слова «министр», «выборы», «президент» убраны из военного фильтра:
    взятка чиновника — ровно наш жанр, а фильтр её резал."""
    v = prefilter.score_item(item(
        "Экс-министр осуждён за взятку, деньги нашли замурованными в стене"
    ))
    assert v.score > 0, f"коррупционное дело отсеяно: {v.reasons}"


@pytest.mark.parametrize("limit,expected", [(1, 1), (2, 2), (4, 4)])
def test_quota_does_not_starve_small_batches(limit, expected):
    """int(4 × 0.4) = 1 — на малых потолках квота вырождалась и резала
    список вдвое. Округляем вверх, на совсем маленьких не применяем."""
    items = [
        item(f"Убийство раскрыто спустя {i} лет благодаря ДНК",
             source="A" if i % 2 else "B", url=f"https://x/{i}")
        for i in range(10)
    ]
    kept, _ = prefilter.select(items, weights={}, min_score=1, limit=limit)
    assert len(kept) == expected


def test_prefilter_threshold_matches_the_example_file():
    """Дефолт кода и .env.example должны совпадать, иначе деплой без
    переменной отбирает иначе, чем ожидается."""
    import pathlib
    import re as _re

    cfg = Config()
    example = pathlib.Path(__file__).resolve().parents[1] / ".env.example"
    match = _re.search(r"^PREFILTER_MIN_SCORE=(-?\d+)", example.read_text(encoding="utf-8"), _re.M)
    assert match, "в .env.example нет PREFILTER_MIN_SCORE"
    assert int(match.group(1)) == cfg.prefilter_min_score


# ---------------------------------------------------------------------------
# S61+. Смягчение отбора 30.08.2026: ранжирование вместо порога допуска
# ---------------------------------------------------------------------------


def test_zero_factor_crime_passes_relaxed_admission():
    """Продуктовая модель: сюжетные факторы ранжируют, а не решают судьбу.
    Пять боевых прогонов подряд требование «хотя бы один фактор» пропускало
    1–2 новости из 220, и AUTO оставался без кандидатов."""
    cfg = Config()
    plain_crime = item("Couriers convicted over £13.8 million cannabis importation attempt")
    kept, _ = prefilter.select([plain_crime], weights={},
                               min_score=cfg.prefilter_min_score, limit=40)
    assert len(kept) == 1, "криминал без сюжетного фактора обязан доходить до оценки"


def test_hard_rejects_stay_out_at_relaxed_admission():
    """Объективный мусор не проходит и при смягчённом допуске."""
    cfg = Config()
    junk = [
        item("Израиль ликвидировал командира военного крыла ХАМАС"),   # война
        item("Season 4 of The Europol Podcast available now"),          # не новость
        item("Iceland votes against restarting talks on joining EU"),   # не криминал
    ]
    kept, dropped = prefilter.select(junk, weights={},
                                     min_score=cfg.prefilter_min_score, limit=40)
    assert kept == [], f"мусор прошёл: {[v.item.title for v in kept]}"
    assert all(v.score <= prefilter.HARD_REJECT for v in dropped)


def test_penalties_cannot_add_up_to_hard_reject():
    """На зазоре между −9 и −10 держится допуск: сумма штрафов (рутина −5,
    куцый заголовок −2) не имеет права дотянуться до балла жёсткого отказа.
    Новый штраф, ломающий это, обязан уронить этот тест."""
    worst = prefilter.score_item(item("Seized 40 kg indicted"))  # рутина + куцый
    assert any("рутинная" in r for r in worst.reasons)
    assert any("куцый" in r for r in worst.reasons)
    assert worst.score > prefilter.HARD_REJECT, (
        f"штрафы дотянулись до жёсткого отказа: {worst.score} {worst.reasons}")


def test_factors_still_rank_the_queue():
    """Ранжирование не отменено: сюжетная история выше рутины и выше
    бессюжетного криминала при общем допуске."""
    cfg = Config()
    batch = [
        item("Man pleads guilty to possession of child pornography", url="https://x/1"),
        item("Portland man arrested for burglary", url="https://x/2"),
        item("Cold case murder solved after 30 years by DNA genealogy", url="https://x/3"),
    ]
    kept, _ = prefilter.select(batch, weights={},
                               min_score=cfg.prefilter_min_score, limit=40)
    titles = [v.item.title for v in kept]
    assert titles[0].startswith("Cold case"), f"сюжет не на первом месте: {titles}"
    assert titles[-1].startswith("Man pleads guilty"), f"рутина не в хвосте: {titles}"


def test_narcotics_seizure_is_crime_news():
    """«Cocaine worth £232m seized» уходило в «не про преступление»:
    наркотиков по-английски в словаре допуска не было вовсе."""
    v = prefilter.score_item(item("Cocaine worth £232m seized in National Crime Agency investigation"))
    assert "не про преступление" not in v.reasons, v.reasons


def test_methamphetamine_does_not_catch_the_word_method():
    """В словаре полное слово methamphetamine, а не meth: иначе любой
    «method» делал новость криминальной."""
    v = prefilter.score_item(item("New method of teaching approved by ministry"))
    assert v.score <= prefilter.HARD_REJECT, v.reasons


def test_relevance_admission_is_semantic_not_numeric(monkeypatch):
    """С 30.08 пригодность решает жанровый фильтр (флаг relevant), сумма
    осей только ранжирует. Числовой порог в промпте превращал
    существование истории в лотерею (разброс оценщика ±несколько баллов:
    «Эквадор» — 18 и 14 на одном материале). Этот тест не даст порогу
    незаметно вернуться ни в промпт, ни в дефолт конфига."""
    import pathlib

    monkeypatch.delenv("MIN_RELEVANCE_SUM", raising=False)
    prompt = (pathlib.Path(__file__).resolve().parents[1]
              / "prompts" / "relevance_judge.md").read_text(encoding="utf-8")
    assert "total >=" not in prompt, "числовой порог допуска вернулся в промпт"
    assert "НЕ входят" in prompt, "промпт больше не запрещает числовое решение"
    assert Config().min_relevance_sum == 0, "числовой floor снова включён по умолчанию"


def test_verb_forms_and_shooting_are_crime(monkeypatch):
    """Живые пропуски 30.08: словарь знал «задержан», но не «задержали»,
    и не знал «стрельба» вовсе."""
    for title in (
        "В Одинцове мужчину задержали за стрельбу из окна",
        "В Марселе при стрельбе погиб человек",
        "Во Владимире украли картину из городского музея",
    ):
        v = prefilter.score_item(item(title))
        assert "не про преступление" not in v.reasons, f"{title}: {v.reasons}"


def test_mock_relevance_uses_config_floor():
    """Mock-контур обязан отбирать тем же порогом, что и боевой: раньше в
    нём жило своё число 20, и смена MIN_RELEVANCE_SUM не меняла dry-run."""
    from src import relevance as rel

    story = item("Убийство и мошенничество: банда арестована, приговор суда",
                 "Расследование убийства завершено, обвиняемый осуждён.")
    strict = make_cfg(min_relevance_sum=50)
    relaxed = make_cfg(min_relevance_sum=1)
    assert not rel.score_item(strict, story).relevant
    assert rel.score_item(relaxed, story).relevant


# ---------------------------------------------------------------------------
# S65. Возраст новости — объективный отказ
# ---------------------------------------------------------------------------


def test_stale_news_is_dropped_by_age():
    from src.main import _too_old

    old = item("Ограбление банка в Лидсе раскрыто по забытому телефону")
    old.published_at = "2026-08-01T10:00:00+00:00"
    assert _too_old(old, 48)


def test_rfc822_rss_date_is_understood():
    """Боевые ленты отдают даты в RFC 822, не в ISO. Первая версия фильтра
    парсила только ISO и молча считала ВСЕ боевые записи свежими — фильтр
    был no-op, нашло независимое ревью."""
    from email.utils import format_datetime
    from datetime import datetime, timedelta, timezone

    from src.main import _too_old

    stale = item("Ограбление банка в Лидсе раскрыто по забытому телефону")
    stale.published_at = "Thu, 20 Aug 2026 12:38:34 +0000"
    assert _too_old(stale, 48), "RFC 822-дата не разобрана — фильтр слеп к боевым лентам"

    fresh = item("Похищение банкира в Марселе")
    fresh.published_at = format_datetime(datetime.now(timezone.utc) - timedelta(hours=2))
    assert not _too_old(fresh, 48), "свежая RFC 822-дата принята за старую"


def test_dateless_feed_is_not_punished():
    """У Europol в лентах нет дат — пустое поле не повод резать источник."""
    from src.main import _too_old

    fresh = item("78 arrests in smuggling bust")
    fresh.published_at = ""
    assert not _too_old(fresh, 48)


def test_broken_date_is_not_a_crash():
    from src.main import _too_old

    weird = item("Похищение банкира в Марселе")
    weird.published_at = "вчера вечером"
    assert not _too_old(weird, 48)


def test_fresh_news_passes_age_filter():
    from datetime import datetime, timedelta, timezone

    from src.main import _too_old

    fresh = item("Похищение банкира в Марселе")
    fresh.published_at = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat()
    assert not _too_old(fresh, 48)


def test_story_age_sla_matches_the_pool_sla():
    """Один предел свежести на весь конвейер: новость, слишком старая для
    публикации из пула, не должна проходить на платную оценку, и наоборот."""
    cfg = Config()
    assert cfg.story_max_age_hours == cfg.auto_freshness_hours


def test_stale_item_never_reaches_paid_scoring(tmp_path, monkeypatch):
    """Фильтр возраста стоит ДО платной оценки: старьё не тратит ни лимит
    вызовов, ни деньги — и не может попасть в пул «свежим»."""
    from test_seen_journal import _isolate, _fake_ingest, _news
    from src import main as main_module, relevance

    _isolate(tmp_path, monkeypatch)
    raws = _news(2)
    raws[0]["published_at"] = "2026-08-01T10:00:00+00:00"   # давно протухло

    scored = []
    original = relevance.score_item
    monkeypatch.setattr(
        relevance, "score_item",
        lambda cfg, it: (scored.append(it.title), original(cfg, it))[1],
    )
    monkeypatch.setattr(main_module, "_ingest_live", _fake_ingest(raws))

    assert main_module.run([]) == 0
    assert raws[0]["title"] not in scored, "старьё дошло до платной оценки"
    assert raws[1]["title"] in scored, "свежая новость потерялась вместе со старой"


def test_dry_run_sample_data_is_not_age_filtered(tmp_path, monkeypatch):
    """Sample-данные вне времени: --dry-run обязан работать и через год,
    когда все даты в файле безнадёжно устареют."""
    from test_seen_journal import _isolate
    from src import main as main_module, relevance
    from src.relevance import RelevanceResult

    _isolate(tmp_path, monkeypatch)
    scored = []
    monkeypatch.setattr(
        relevance, "score_item",
        lambda cfg, it: (scored.append(it.title),
                         RelevanceResult(False, 0, 0, 0, 0, 0, "не по теме"))[1],
    )

    assert main_module.run(["--dry-run"]) == 0
    assert scored, "фильтр возраста выкосил sample-данные в dry-run"


def test_card_fonts_are_findable_on_linux():
    """Конвейер живёт на Ubuntu. Список шрифтов состоял только из путей
    Windows: на сервере PIL молча брал встроенный растровый шрифт, и первая
    же карточка ушла бы в канал испорченной."""
    from src import image_card

    for candidates in (
        image_card._FONT_CANDIDATES_SERIF,
        image_card._FONT_CANDIDATES_SERIF_BOLD,
        image_card._FONT_CANDIDATES_MONO,
    ):
        assert any(p.startswith("/usr/share/fonts") for p in candidates), (
            f"нет ни одного пути для Linux: {candidates}"
        )
