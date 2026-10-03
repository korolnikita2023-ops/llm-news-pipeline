"""Автоматическая редакция: система сама выбирает, что выйдет.

Главное правило, которое здесь защищается: **три поста в сутки — потолок,
а не план**. Если критериям отвечает одна история, выходит одна. Если ни
одной — не выходит ничего, и это нормальный день, а не сбой.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from src import auto_editor, schedule, storage

PRODUCTION = "-100111"
SANDBOX = "-100222"


def make_cfg(**over) -> Config:
    cfg = Config()
    cfg.publish_mode = "live"
    cfg.workflow_mode = "auto"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = PRODUCTION
    cfg.schedule_tz = "Europe/Moscow"
    cfg.morning_slot = "09:00"
    cfg.day_slot = "14:00"
    cfg.evening_slot = "20:00"
    cfg.auto_max_posts_per_day = 3
    cfg.auto_lead_minutes = 25
    cfg.auto_freshness_hours = 24
    cfg.max_preparation_attempts_per_day = 6
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


def add_story(url_hash: str, score: int, age_hours: float = 0.0) -> None:
    storage.save_digest("run1", [{
        "url_hash": url_hash, "source": "bbc.com", "source_url": "https://e.com/1",
        "title": f"История {url_hash}", "summary": "Суть.", "score": score,
        "category": "crime", "signals": "серийность",
    }])
    if age_hours:
        old = (datetime.now(timezone.utc)
               - timedelta(hours=age_hours)).isoformat(timespec="seconds")
        with storage.get_conn() as c:
            c.execute("UPDATE digest_items SET created_at=? WHERE url_hash=?",
                      (old, url_hash))


def at_moscow(hour: int, minute: int = 0) -> datetime:
    """Момент сегодняшнего дня по Москве, в UTC."""
    from zoneinfo import ZoneInfo

    tz = ZoneInfo("Europe/Moscow")
    local = datetime.now(tz).replace(hour=hour, minute=minute, second=0, microsecond=0)
    return local.astimezone(timezone.utc)


# ---------------------------------------------------------------------------
# Слоты
# ---------------------------------------------------------------------------


def test_three_slots_are_configurable(db):
    cfg = make_cfg(morning_slot="07:15", day_slot="12:45", evening_slot="21:30")
    states = auto_editor.slot_state(cfg, at_moscow(6))
    assert [st["slot"] for st in states] == ["morning", "day", "evening"]
    assert [st["at_local"][-5:] for st in states] == ["07:15", "12:45", "21:30"]


def test_duplicate_slot_times_are_rejected(db):
    cfg = make_cfg(day_slot="09:00")
    with pytest.raises(schedule.ScheduleConfigError):
        schedule.validate(cfg)


def test_slot_is_due_shortly_before_its_time(db):
    cfg = make_cfg(auto_lead_minutes=25)
    before = {st["slot"]: st["due"] for st in
              auto_editor.slot_state(cfg, at_moscow(8, 20))}
    assert before["morning"] is False, "за сорок минут ещё рано"

    close = {st["slot"]: st["due"] for st in
             auto_editor.slot_state(cfg, at_moscow(8, 40))}
    assert close["morning"] is True, "за двадцать минут — пора"
    assert close["day"] is False and close["evening"] is False


# ---------------------------------------------------------------------------
# Отбор: потолок, а не план
# ---------------------------------------------------------------------------


def _fake_prepare(results):
    """Подмена подготовки: отдаёт заранее заданные исходы по порядку.

    Успешный исход заводит настоящую запись поста — иначе назначению в
    слот нечего ставить, и проверка молча меряла бы не то.
    """
    from conftest import seed_post

    calls = []

    def prepare(cfg, story, day, report):
        calls.append(story["url_hash"])
        outcome = results.pop(0) if results else None
        if outcome is None:
            report["rejected"].append({"title": story["title"], "why": "проверки"})
            return None
        seed_post(outcome["post_id"])
        return outcome

    prepare.calls = calls
    return prepare


def test_only_good_stories_are_published(db, monkeypatch):
    """Норму не добивают слабыми историями.

    Если проверки прошла одна — выйдет одна. Это главное правило режима.
    """
    for i, score in enumerate([40, 35, 30]):
        add_story(f"s{i}", score)

    monkeypatch.setattr(auto_editor, "_prepare",
                        _fake_prepare([None, {"post_id": "p1", "average": 9.0}, None]))
    cfg = make_cfg()
    report = auto_editor.fill_due_slots(cfg, at_moscow(8, 40))

    assert len(report["assigned"]) == 1, report
    assert len(report["rejected"]) == 1, "до успеха забраковали одну"


def test_no_suitable_story_means_no_post(db, monkeypatch):
    """День без подходящих историй — нормальный день, а не сбой."""
    add_story("s1", 40)
    monkeypatch.setattr(auto_editor, "_prepare", _fake_prepare([None]))
    report = auto_editor.fill_due_slots(make_cfg(), at_moscow(8, 40))
    assert report["assigned"] == []


def test_strongest_story_goes_first(db, monkeypatch):
    add_story("weak", 20)
    add_story("strong", 45)
    prepare = _fake_prepare([{"post_id": "p1", "average": 9.0}])
    monkeypatch.setattr(auto_editor, "_prepare", prepare)

    auto_editor.fill_due_slots(make_cfg(), at_moscow(8, 40))
    assert prepare.calls[0] == "strong"


def test_failed_story_falls_through_to_the_next(db, monkeypatch):
    add_story("first", 45)
    add_story("second", 40)
    prepare = _fake_prepare([None, {"post_id": "p2", "average": 9.1}])
    monkeypatch.setattr(auto_editor, "_prepare", prepare)

    report = auto_editor.fill_due_slots(make_cfg(), at_moscow(8, 40))
    assert prepare.calls == ["first", "second"]
    assert report["assigned"][0]["post_id"] == "p2"


def test_daily_cap_is_a_ceiling(db, images_dir, monkeypatch):
    """Больше трёх за сутки система не выпустит."""
    from conftest import seed_post

    for i in range(3):
        seed_post(f"done{i}", status="published")
    with storage.get_conn() as c:
        c.execute("UPDATE posts SET channel_id=? WHERE status='published'",
                  (PRODUCTION,))

    add_story("s1", 40)
    monkeypatch.setattr(auto_editor, "_prepare",
                        _fake_prepare([{"post_id": "p1", "average": 9.0}]))
    report = auto_editor.fill_due_slots(make_cfg(), at_moscow(8, 40))
    assert report["assigned"] == []
    assert any("норма" in s for s in report["skipped"]), report["skipped"]


def test_attempts_cap_stops_endless_retries(db, monkeypatch):
    """Бесконечного перебора историй быть не должно."""
    for i in range(10):
        add_story(f"s{i}", 40 - i)

    real_prepare = auto_editor._prepare

    def failing(cfg, story, day, report):
        # Настоящий учёт попытки, но подготовка не удалась.
        attempt = storage.start_selection_attempt(
            story["id"], *day, cfg.auto_max_posts_per_day,
            cfg.max_preparation_attempts_per_day)
        if not isinstance(attempt, int):
            report["skipped"].append(str(attempt))
            return None
        report["attempts_used"] += 1
        storage.release_digest_item(story["id"])
        storage.finish_selection_attempt(attempt, None, "rejected")
        report["rejected"].append({"title": story["title"], "why": "оценка"})
        return None

    monkeypatch.setattr(auto_editor, "_prepare", failing)
    cfg = make_cfg(max_preparation_attempts_per_day=3)
    report = auto_editor.fill_due_slots(cfg, at_moscow(8, 40))

    assert report["attempts_used"] <= 3, report["attempts_used"]
    assert real_prepare  # использован для читаемости замысла


# ---------------------------------------------------------------------------
# Свежесть и повторы
# ---------------------------------------------------------------------------


def test_stale_stories_are_not_published(db):
    """Накопившаяся очередь не должна занять слоты вчерашними новостями."""
    add_story("fresh", 30, age_hours=2)
    add_story("stale", 45, age_hours=30)
    names = [r["url_hash"] for r in auto_editor.candidates(make_cfg())]
    assert names == ["fresh"], names


def test_freshness_window_is_configurable(db):
    add_story("old", 40, age_hours=10)
    assert auto_editor.candidates(make_cfg(auto_freshness_hours=6)) == []
    assert len(auto_editor.candidates(make_cfg(auto_freshness_hours=24))) == 1


def test_story_published_to_this_channel_is_not_offered(db, images_dir):
    """Повторов в канале быть не должно — правило прежнее, канальное."""
    add_story("a", 40)
    storage.insert_event("evt_a", "a", n_sources=1)
    storage.insert_post(post_id="p1", event_id="evt_a", draft_ru="Т.", fmt="A",
                        status="published", scores={}, manual_only=False,
                        channel_id=PRODUCTION)
    assert auto_editor.candidates(make_cfg()) == []


def test_sandbox_publication_still_does_not_block(db, images_dir):
    add_story("a", 40)
    storage.insert_event("evt_a", "a", n_sources=1)
    storage.insert_post(post_id="p1", event_id="evt_a", draft_ru="Т.", fmt="A",
                        status="published", scores={}, manual_only=False,
                        channel_id=SANDBOX)
    assert len(auto_editor.candidates(make_cfg())) == 1


def test_occupied_slot_is_not_filled_twice(db, images_dir):
    """Один слот — один пост."""
    from conftest import seed_post

    add_story("a", 40)
    seed_post("p1")
    storage.schedule_post("p1", schedule.slot_time_today_utc(make_cfg(), "morning"),
                          "morning")
    report = auto_editor.fill_due_slots(make_cfg(), at_moscow(8, 40))
    assert report["assigned"] == []


# ---------------------------------------------------------------------------
# Строгость и безопасность
# ---------------------------------------------------------------------------


def test_auto_uses_the_strict_threshold(db):
    """Пограничная полоса — для человека. Здесь взвешивать некому."""
    import inspect

    source = inspect.getsource(auto_editor._prepare)
    assert "borderline_ok=False" in source


def test_dry_run_costs_nothing_and_writes_nothing(db, monkeypatch):
    add_story("a", 40)
    called = []
    monkeypatch.setattr(auto_editor, "_prepare",
                        lambda *a, **k: called.append(1))

    report = auto_editor.fill_due_slots(make_cfg(), at_moscow(8, 40), dry_run=True)
    assert called == [], "разбор не должен готовить посты"
    assert len(report["assigned"]) == 1
    with storage.get_conn() as c:
        assert c.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 0
        assert c.execute("SELECT COUNT(*) FROM selection_attempts").fetchone()[0] == 0


def test_auto_does_not_publish_by_itself(db):
    """Публикует только publish_due по слоту — второго пути нет."""
    import inspect

    source = inspect.getsource(auto_editor)
    assert "_publish_post" not in source
    assert "send_live" not in source


def test_filling_failure_does_not_stop_publishing(db, images_dir, monkeypatch):
    """Сбой подбора не должен помешать выйти уже назначенному посту."""
    from src import publish_due

    def boom(cfg, **k):
        raise RuntimeError("подбор сломался")

    monkeypatch.setattr(auto_editor, "fill_due_slots", boom)
    monkeypatch.setattr(publish_due, "get_config", lambda: make_cfg())
    # Ничего не запланировано — прогон завершается штатно, не падая.
    assert publish_due.run() == 0


# ---------------------------------------------------------------------------
# Находки независимого ревью 29.08.2026
# ---------------------------------------------------------------------------


def test_scheduled_posts_count_towards_the_cap(db, images_dir, monkeypatch):
    """Назначенный пост — почти публикация, и потолок его учитывает.

    Иначе при потолке в два поста два назначенных не мешали бы подготовить
    третий: считались только вышедшие.
    """
    from conftest import seed_post

    seed_post("p1")
    storage.schedule_post("p1", schedule.slot_time_today_utc(make_cfg(), "morning"),
                          "morning")
    with storage.get_conn() as c:
        c.execute("UPDATE posts SET channel_id=?", (PRODUCTION,))

    assert auto_editor.published_today(make_cfg()) == 1


def test_story_rejected_today_is_not_retried_at_the_next_slot(db, images_dir):
    """Слабая история не должна съедать попытки во всех трёх слотах.

    Она сильнейшая в пуле, поэтому к каждому слоту снова оказывалась бы
    первой, и деньги уходили бы на один и тот же материал.
    """
    add_story("strong_but_bad", 45)
    add_story("second", 30)
    cfg = make_cfg()
    day = schedule.day_bounds_utc(cfg)

    rows = storage.digest_pool(PRODUCTION)
    bad = next(r for r in rows if r["url_hash"] == "strong_but_bad")
    attempt = storage.start_selection_attempt(bad["id"], *day, 9, 9)
    storage.release_digest_item(bad["id"])
    storage.finish_selection_attempt(attempt, None, "rejected")

    names = [r["url_hash"] for r in auto_editor.candidates(cfg)]
    assert names == ["second"], names


def test_api_outage_does_not_bar_the_story_for_the_day(db, images_dir):
    """Провайдер молчал — история не виновата, к следующему слоту доступна."""
    add_story("a", 40)
    cfg = make_cfg()
    day = schedule.day_bounds_utc(cfg)

    rows = storage.digest_pool(PRODUCTION)
    attempt = storage.start_selection_attempt(rows[0]["id"], *day, 9, 9)
    storage.release_digest_item(rows[0]["id"])
    storage.finish_selection_attempt(attempt, None, storage.ATTEMPT_API_ERROR)

    assert len(auto_editor.candidates(cfg)) == 1


def test_naive_timestamp_does_not_break_the_pick(db):
    """Отметка времени без зоны не должна ронять весь подбор."""
    add_story("a", 40)
    with storage.get_conn() as c:
        c.execute("UPDATE digest_items SET created_at='2026-08-29T10:00:00'")
    cfg = make_cfg(auto_freshness_hours=24 * 3650)
    assert len(auto_editor.candidates(cfg)) == 1


def test_slot_times_come_from_settings(db):
    """Время слотов задаётся настройками, а не зашито в коде."""
    import inspect

    from src import schedule as sched

    source = inspect.getsource(sched._slot_time)
    assert "cfg.morning_slot" in source and "cfg.evening_slot" in source
    assert "day_slot" in source
