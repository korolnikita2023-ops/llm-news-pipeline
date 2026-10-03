"""Наблюдаемость AUTO и честный UX дайджеста (postmortem 30.08).

Вечерний слот прошёл пустым, и по журналу не было видно, что AUTO вообще
смотрел: пул был, но весь «уже пробовали сегодня», а дайджест при этом
показывал историю как доступную. Здесь закреплены оба контура: диагностика
пустого тика и пометки эксклюзии в интерфейсе.
"""
from __future__ import annotations

from src import auto_editor, schedule, storage
from test_auto_editor import PRODUCTION, add_story, at_moscow, make_cfg


def _fail_today(url_hash: str, cfg) -> int:
    """Помечает историю провалившей сегодняшнюю подготовку — тем же путём,
    каким это делает боевой auto_editor."""
    day = schedule.day_bounds_utc(cfg)
    row = next(r for r in storage.digest_pool(PRODUCTION)
               if r["url_hash"] == url_hash)
    attempt = storage.start_selection_attempt(row["id"], *day, 9, 9)
    storage.release_digest_item(row["id"])
    storage.finish_selection_attempt(attempt, None, "rejected")
    return row["id"]


# ---------------------------------------------------------------------------
# eligibility(): цифры разбора
# ---------------------------------------------------------------------------


def test_eligibility_counts_each_exclusion(db):
    cfg = make_cfg()
    add_story("fresh", 40)
    add_story("failed", 45)
    add_story("stale", 30, age_hours=30)
    _fail_today("failed", cfg)

    eligible, diag = auto_editor.eligibility(cfg)

    assert [r["url_hash"] for r in eligible] == ["fresh"]
    assert diag == {"pool": 3, "failed_today": 1, "stale": 1, "eligible": 1}


def test_candidates_matches_eligibility(db):
    cfg = make_cfg()
    add_story("a", 40)
    add_story("b", 30, age_hours=48)

    assert ([r["url_hash"] for r in auto_editor.candidates(cfg)]
            == [r["url_hash"] for r in auto_editor.eligibility(cfg)[0]])


# ---------------------------------------------------------------------------
# Диагностика тика
# ---------------------------------------------------------------------------


def test_empty_tick_report_carries_the_diag(db):
    """Слот в окне, готовить нечего — отчёт обязан объяснить цифрами."""
    cfg = make_cfg()
    add_story("failed", 45)
    _fail_today("failed", cfg)

    report = auto_editor.fill_due_slots(cfg, at_moscow(8, 40))

    assert report["assigned"] == []
    assert report["diag"]["eligible"] == 0
    assert report["diag"]["failed_today"] == 1
    assert any("failed_today=1" in s for s in report["skipped"]), report["skipped"]


def test_no_due_slot_means_no_diag(db):
    """Вне окна подготовки тик молчит, как и раньше, — иначе журнал
    заспамило бы каждые пять минут весь день."""
    cfg = make_cfg()
    add_story("s", 40)

    report = auto_editor.fill_due_slots(cfg, at_moscow(11, 0))

    assert "diag" not in report


def test_empty_tick_prints_one_server_log_line(db, monkeypatch, capsys):
    """publish_due печатает разбор пустого тика в stdout сервера."""
    from src import publish_due

    cfg = make_cfg()
    monkeypatch.setattr(
        auto_editor, "fill_due_slots",
        lambda cfg_: {"assigned": [], "rejected": [],
                      "skipped": ["в пуле нет свежих историй"],
                      "diag": {"pool": 1, "failed_today": 1, "stale": 0,
                               "eligible": 0}},
    )
    publish_due._fill_slots(cfg)

    out = capsys.readouterr().out
    assert "[auto] pool=1 failed_today=1 stale=0 eligible=0" in out


def test_assigned_tick_prints_the_report_not_the_diag(db, monkeypatch, capsys):
    from src import publish_due

    cfg = make_cfg()
    monkeypatch.setattr(
        auto_editor, "fill_due_slots",
        lambda cfg_: {"assigned": [{"slot": "day", "title": "☀️ День",
                                    "at_local": "x", "average": 9.0,
                                    "story": {"title": "t", "score": 30}}],
                      "rejected": [], "skipped": [],
                      "diag": {"pool": 1, "failed_today": 0, "stale": 0,
                               "eligible": 1}},
    )
    publish_due._fill_slots(cfg)

    out = capsys.readouterr().out
    assert "[auto] ☀️ День" in out
    assert "pool=" not in out


# ---------------------------------------------------------------------------
# UX: пометки в дайджесте
# ---------------------------------------------------------------------------


def test_digest_screen_marks_failed_today(db, monkeypatch):
    """История, уже провалившая подготовку, не должна выглядеть доступной:
    30.08 экран говорил «1 история», а AUTO видел ноль."""
    from src import approval_bot

    cfg = make_cfg(workflow_mode="auto", telegram_channel_id=PRODUCTION)
    add_story("burned", 45)
    add_story("alive", 30)
    _fail_today("burned", cfg)

    monkeypatch.setattr(approval_bot, "get_config", lambda: cfg, raising=False)
    text, _ = approval_bot._digest_screen(cfg)

    assert "уже пробовали сегодня" in text
    assert "доступно AUTO сейчас: 1" in text


def test_digest_screen_in_manual_mode_is_unchanged(db):
    from src import approval_bot

    cfg = make_cfg(workflow_mode="digest", telegram_channel_id=PRODUCTION)
    add_story("s1", 40)

    text, _ = approval_bot._digest_screen(cfg)

    assert "доступно AUTO" not in text
    assert "уже пробовали" not in text


def test_notify_marks_failed_today_in_auto(db, monkeypatch):
    from src import notify

    cfg = make_cfg(workflow_mode="auto", telegram_channel_id=PRODUCTION)
    add_story("burned", 45)
    _fail_today("burned", cfg)
    rows = storage.digest_pool(PRODUCTION)

    chunks = notify.build_digest_message(cfg, rows, {"collected_at": "x",
                                                     "raw": 10, "scored": 2})
    text = "\n".join(chunks)
    assert "⚪ уже пробовали сегодня" in text


def test_notify_survives_broken_failed_lookup(db, monkeypatch):
    """Недоступный разбор провалов не имеет права сорвать рассылку —
    список уходит просто без пометок."""
    from src import notify

    cfg = make_cfg(workflow_mode="auto", telegram_channel_id=PRODUCTION)
    add_story("s1", 40)
    rows = storage.digest_pool(PRODUCTION)

    def boom(*a, **k):
        raise RuntimeError("база занята")

    monkeypatch.setattr(notify.storage, "failed_item_ids_today", boom)
    chunks = notify.build_digest_message(cfg, rows, {"collected_at": "x",
                                                     "raw": 10, "scored": 2})
    assert chunks, "рассылка сорвалась из-за пометок"
    assert "⚪" not in "\n".join(chunks)


# ---------------------------------------------------------------------------
# Семантический допуск relevance (кодовое условие, не только промпт)
# ---------------------------------------------------------------------------


def test_relevant_story_with_zero_total_enters_the_pool(monkeypatch):
    """relevant=true с нулевыми осями проходит: число ранжирует, не решает."""
    from src import relevance
    from src.normalize import normalize_raw
    from src.relevance import RelevanceResult

    cfg = make_cfg()
    cfg.min_relevance_sum = 0
    item = normalize_raw({"title": "Кража века", "summary": "",
                          "url": "https://x/1", "source": "BBC"})
    monkeypatch.setattr(
        relevance, "score_item",
        lambda cfg_, it: RelevanceResult(True, 0, 0, 0, 0, 0, "громкое дело"))

    out = relevance.score_and_filter(cfg, [item])
    assert len(out) == 1


def test_genre_rejected_story_never_enters_even_with_top_score(monkeypatch):
    from src import relevance
    from src.normalize import normalize_raw
    from src.relevance import RelevanceResult

    cfg = make_cfg()
    cfg.min_relevance_sum = 0
    item = normalize_raw({"title": "Сводка с фронта", "summary": "",
                          "url": "https://x/2", "source": "BBC"})
    monkeypatch.setattr(
        relevance, "score_item",
        lambda cfg_, it: RelevanceResult(False, 10, 10, 10, 10, 10, "не по теме"))

    assert relevance.score_and_filter(cfg, [item]) == []
