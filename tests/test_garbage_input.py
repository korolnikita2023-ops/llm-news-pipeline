"""Мусорный и неожиданный ввод: битые данные в базе, странные символы,
отсутствующие файлы, кривой callback_data.

Смысл: ни один из этих случаев не должен ронять бота или приводить к
публикации чего-то неожиданного. Максимум — понятный отказ.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import (
    FakeBot,
    FakeCallback,
    FakeMessage,
    handlers_of,
    post_status,
    seed_post,
)
from config import Config
from src import approval_bot, publisher, storage


def make_cfg(*, live: bool = True) -> Config:
    cfg = Config()
    cfg.publish_mode = "live" if live else "draft"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = "-100999"
    return cfg


@pytest.fixture
def sent(monkeypatch):
    calls = []
    monkeypatch.setattr(
        publisher,
        "send_live_text",
        lambda cfg, t, i=None, source_url='': (calls.append(t), (True, "ok"))[1],
    )
    return calls


# ---------------------------------------------------------------------------
# Битые данные в базе
# ---------------------------------------------------------------------------


def test_broken_json_in_scores_does_not_break_review(db, images_dir):
    """В поле scores оказался не JSON — кандидат всё равно должен показаться."""
    seed_post("p1")
    with storage.get_conn() as conn:
        conn.execute("UPDATE posts SET scores='не json {{{' WHERE post_id='p1'")

    bot = FakeBot()
    ok = asyncio.run(approval_bot.send_next_candidate(bot, make_cfg()))

    assert ok is True
    assert "оценка ?/10" in bot.last_text(), "должен подставиться прочерк"


def test_null_scores(db, images_dir):
    seed_post("p1")
    with storage.get_conn() as conn:
        conn.execute("UPDATE posts SET scores=NULL WHERE post_id='p1'")

    bot = FakeBot()
    assert asyncio.run(approval_bot.send_next_candidate(bot, make_cfg())) is True


def test_empty_draft_text_is_not_published(db, images_dir, sent):
    """Пустой текст поста не должен уйти в канал."""
    seed_post("p1", draft="   ")

    ok, status = approval_bot._publish_post(make_cfg(), "p1")

    assert ok is False
    assert sent == []
    assert post_status("p1") != "published"


def test_post_with_broken_event_link(db, images_dir, sent):
    """Пост ссылается на несуществующее событие — публикация обязана
    остановиться на гейте (нет source_url), а не упасть."""
    storage.insert_post(
        post_id="orphan", event_id="evt_nope", draft_ru="Текст.\n\nПо данным BBC.",
        fmt="A", status="candidate", scores={}, manual_only=False,
    )

    ok, status = approval_bot._publish_post(make_cfg(), "orphan")

    assert ok is False
    assert "гейт" in status
    assert sent == []


# ---------------------------------------------------------------------------
# Странные символы и длина
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Эмодзи 🔪🚔💀 в тексте.\n\nПо данным BBC News.",
        # Разметка в русском тексте: звёздочки и подчёркивания не должны ломать
        # отправку. Текст намеренно остаётся русским — пост на латинице гейт
        # обязан блокировать, это проверяется отдельно.
        "Суд *отклонил* жалобу _защиты_ и оставил меру.\n\nПо данным BBC News.",
        "Китайские иероглифы 中文 и арабица العربية.\n\nПо данным BBC News.",
        "Кавычки «ёлочки» и — тире.\n\nПо данным BBC News.",
        "Перенос\nстрок\n\n\n\nмного.\n\nПо данным BBC News.",
    ],
)
def test_special_characters_pass_through_unchanged(db, images_dir, text, monkeypatch):
    """Текст не должен искажаться при отправке. parse_mode не используется —
    значит, разметка уходит как обычные символы и не ломает сообщение."""
    seed_post("p1", draft=text)
    captured = []
    monkeypatch.setattr(
        publisher,
        "send_live_text",
        lambda cfg, t, i=None, source_url='': (captured.append(t), (True, "ok"))[1],
    )

    ok, _ = approval_bot._publish_post(make_cfg(), "p1")

    assert ok is True
    assert captured[0] == text


def test_missing_card_file_does_not_block_publish(db, images_dir, sent):
    """Карточки нет на диске — пост уходит текстом, а не падает."""
    seed_post("p1")

    ok, _ = approval_bot._publish_post(make_cfg(), "p1")

    assert ok is True
    assert len(sent) == 1


# ---------------------------------------------------------------------------
# Кривой callback_data
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("data", ["approve:", "approve:   ", "approve:../../etc/passwd"])
def test_malformed_callback_data(db, images_dir, sent, data):
    """Пустой или мусорный id в кнопке — отказ, без падения и без публикации."""
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback(data)

    asyncio.run(handlers_of(dp)["approve"](call))

    assert sent == []


def test_sql_injection_in_post_id(db, images_dir, sent):
    """post_id идёт в SQL только параметром — инъекция не должна сработать."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback("approve:'; DROP TABLE posts; --")

    asyncio.run(handlers_of(dp)["approve"](call))

    with storage.get_conn() as conn:
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 1
    assert sent == []


# ---------------------------------------------------------------------------
# Неожиданный порядок действий
# ---------------------------------------------------------------------------


def test_edit_then_edit_again(db, images_dir, sent):
    """Владелец правит пост дважды подряд — второй раз он уже опубликован."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())
    h = handlers_of(dp)["msg_on_new_text"]

    from tests.test_approval_bot import FakeState  # переиспользуем заглушку

    msg1 = FakeMessage(text="Первый.\n\nПо данным BBC News.")
    asyncio.run(h(msg1, FakeState({"editing_post_id": "p1"})))
    msg2 = FakeMessage(text="Второй.\n\nПо данным BBC News.")
    asyncio.run(h(msg2, FakeState({"editing_post_id": "p1"})))

    assert len(sent) == 1, "второй раз публиковать уже опубликованное нельзя"
    assert any("уже опубликован" in a for a in msg2.answers)


def test_regen_after_publish(db, images_dir, sent, monkeypatch):
    """🔁 на уже опубликованном посте не должно ломать состояние."""
    seed_post("p1")
    approval_bot._publish_post(make_cfg(), "p1")
    assert post_status("p1") == "published"

    monkeypatch.setattr(
        approval_bot, "_regenerate", lambda cfg, pid, item: "Новый.\n\nПо данным BBC."
    )
    dp = approval_bot.build_dispatcher(make_cfg())
    asyncio.run(handlers_of(dp)["regen"](FakeCallback("regen:p1")))

    assert len(sent) == 1, "перегенерация не должна публиковать повторно"
