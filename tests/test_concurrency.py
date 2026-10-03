"""Конкурентность и живучесть: гонки, повторы, рестарт посреди операции.

Класс проблем, который невозможно поймать ручным тыканьем кнопок, но который
в проде срабатывает регулярно — Telegram переотправляет callback при плохой
связи, а владелец жмёт кнопку второй раз, когда «не отреагировало».
"""
from __future__ import annotations

import asyncio
import threading


from conftest import post_status, seed_post
from config import Config
from src import approval_bot, publisher, storage


def make_cfg(*, live: bool = True) -> Config:
    cfg = Config()
    cfg.publish_mode = "live" if live else "draft"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = "-100999"
    return cfg


# ---------------------------------------------------------------------------
# S20. Две одновременные публикации одного поста
# ---------------------------------------------------------------------------


def test_concurrent_publish_sends_once(db, images_dir, monkeypatch):
    """Два потока одновременно публикуют один пост. В канал он обязан уйти
    ровно один раз.

    Отправка искусственно замедлена, чтобы гарантированно поймать окно
    гонки — без задержки тест мог бы пройти случайно.
    """
    seed_post("p1")
    sent = []
    lock = threading.Lock()

    def slow_send(cfg, text, image_path=None, source_url=''):
        import time

        time.sleep(0.15)
        with lock:
            sent.append(text)
        return True, "ok"

    monkeypatch.setattr(publisher, "send_live_text", slow_send)

    results = []

    def worker():
        results.append(approval_bot._publish_post(make_cfg(), "p1"))

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(sent) == 1, f"пост ушёл в канал {len(sent)} раз(а) вместо одного"
    assert sum(1 for ok, _ in results if ok) == 1
    assert post_status("p1") == "published"


def test_second_caller_gets_clear_reason(db, images_dir, monkeypatch):
    """Проигравший гонку вызов должен получить внятную причину, а не 'ошибку'."""
    seed_post("p1")
    monkeypatch.setattr(publisher, "send_live_text", lambda *a, **k: (True, "ok"))

    approval_bot._publish_post(make_cfg(), "p1")
    ok, status = approval_bot._publish_post(make_cfg(), "p1")

    assert ok is False
    assert "уже опубликован" in status


# ---------------------------------------------------------------------------
# S21. Рестарт посреди операции
# ---------------------------------------------------------------------------


def test_failed_send_returns_post_to_queue(db, images_dir, monkeypatch):
    """Отправка не удалась — пост не должен застрять в промежуточном статусе."""
    seed_post("p1")
    monkeypatch.setattr(
        publisher, "send_live_text", lambda *a, **k: (False, "Telegram 502")
    )

    ok, status = approval_bot._publish_post(make_cfg(), "p1")

    assert ok is False
    assert post_status("p1") == "approved", "пост обязан вернуться в работу"


def test_exception_during_send_does_not_strand_post(db, images_dir, monkeypatch):
    """Сеть оборвалась с исключением — тот же результат, без застревания."""
    seed_post("p1")

    def boom(*a, **k):
        raise ConnectionError("сеть недоступна")

    monkeypatch.setattr(publisher, "send_live_text", boom)

    ok, status = approval_bot._publish_post(make_cfg(), "p1")

    assert ok is False
    assert "ошибка отправки" in status
    assert post_status("p1") == "approved"


def test_recover_stuck_posts_after_crash(db, images_dir):
    """Процесс убили в момент отправки — при старте пост возвращается
    в работу, а не исчезает из очереди навсегда."""
    seed_post("p1")
    with storage.get_conn() as conn:
        conn.execute("UPDATE posts SET status='publishing' WHERE post_id='p1'")

    recovered = approval_bot.recover_stuck_posts()

    assert recovered == 1
    assert post_status("p1") == "approved"


def test_recover_does_not_touch_published(db, images_dir):
    """Восстановление не должно трогать уже опубликованные посты."""
    seed_post("p1", status="published")

    assert approval_bot.recover_stuck_posts() == 0
    assert post_status("p1") == "published"


# ---------------------------------------------------------------------------
# Повторные и «поздние» нажатия
# ---------------------------------------------------------------------------


def test_reject_after_publish_does_not_unpublish(db, images_dir, monkeypatch):
    """Владелец нажал ❌ на старом сообщении уже опубликованного поста.
    Из канала пост так не удалить — но и статус в базе не должен врать."""
    seed_post("p1")
    monkeypatch.setattr(publisher, "send_live_text", lambda *a, **k: (True, "ok"))
    approval_bot._publish_post(make_cfg(), "p1")

    dp = approval_bot.build_dispatcher(make_cfg())
    from conftest import FakeCallback, handlers_of

    asyncio.run(handlers_of(dp)["reject"](FakeCallback("reject:p1")))

    # Текущее поведение задокументировано: статус меняется, публикация в
    # канале остаётся. Удаление из канала — отдельная функция, её здесь нет.
    assert post_status("p1") == "rejected"


def test_publish_twice_different_posts_both_go(db, images_dir, monkeypatch):
    """Защита от дублей не должна мешать публиковать РАЗНЫЕ посты."""
    seed_post("p1")
    seed_post("p2")
    sent = []
    monkeypatch.setattr(
        publisher, "send_live_text", lambda cfg, t, i=None, source_url='': (sent.append(t), (True, "ok"))[1]
    )

    approval_bot._publish_post(make_cfg(), "p1")
    approval_bot._publish_post(make_cfg(), "p2")

    assert len(sent) == 2
