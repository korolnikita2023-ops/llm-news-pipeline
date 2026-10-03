"""Неожиданный порядок действий и «старые кнопки».

Кнопки в переписке живут вечно: владелец может пролистать чат вверх и нажать
на сообщение недельной давности, когда пост давно отклонён или опубликован.
Найдено независимым ревью 27.08.2026.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import FakeBot, FakeCallback, handlers_of, post_status, post_text, seed_post
from config import Config
from src import approval_bot, publisher


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
# ✅ после ❌
# ---------------------------------------------------------------------------


def test_approve_after_reject_does_not_publish(db, images_dir, sent):
    """Владелец отклонил пост, потом случайно нажал ✅ на старом сообщении.
    Отклонение — осознанное решение, отменять его случайным тапом нельзя."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())
    h = handlers_of(dp)

    asyncio.run(h["reject"](FakeCallback("reject:p1")))
    call = FakeCallback("approve:p1")
    asyncio.run(h["approve"](call))

    assert sent == [], "отклонённый пост не должен уходить в канал"
    assert post_status("p1") == "rejected"
    assert "отклонён" in call.message.caption


def test_approve_after_hold_still_works(db, images_dir, sent):
    """А вот «в запас» — это отложить, а не забраковать: ✅ после ⏸
    должно работать, иначе отложенные посты не достать."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())
    h = handlers_of(dp)

    asyncio.run(h["hold"](FakeCallback("hold:p1")))
    asyncio.run(h["approve"](FakeCallback("approve:p1")))

    assert len(sent) == 1
    assert post_status("p1") == "published"


# ---------------------------------------------------------------------------
# 🔁 после публикации
# ---------------------------------------------------------------------------


def test_regen_after_publish_is_refused(db, images_dir, sent, monkeypatch):
    """Перегенерация опубликованного поста переписала бы текст в базе,
    а в канале остался бы старый — они разъехались бы молча."""
    seed_post("p1", draft="Оригинал.\n\nПо данным BBC News.")
    dp = approval_bot.build_dispatcher(make_cfg())
    h = handlers_of(dp)

    asyncio.run(h["approve"](FakeCallback("approve:p1")))
    assert post_status("p1") == "published"

    called = []
    monkeypatch.setattr(
        approval_bot, "_regenerate", lambda *a: called.append(1) or "Новый."
    )
    call = FakeCallback("regen:p1")
    asyncio.run(h["regen"](call))

    assert called == [], "перегенерация опубликованного не должна запускаться"
    assert post_text("p1") == "Оригинал.\n\nПо данным BBC News."
    assert any("уже в канале" in a for a in call.message.answers)


def test_edit_after_publish_is_refused(db, images_dir, sent):
    """Та же логика для ручной правки: в канал повторно не уйдёт."""
    from tests.test_approval_bot import FakeState
    from conftest import FakeMessage

    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())
    h = handlers_of(dp)

    asyncio.run(h["approve"](FakeCallback("approve:p1")))
    msg = FakeMessage(text="Правка.\n\nПо данным BBC News.")
    asyncio.run(h["msg_on_new_text"](msg, FakeState({"editing_post_id": "p1"})))

    assert len(sent) == 1, "второй публикации быть не должно"
    assert any("уже опубликован" in a for a in msg.answers)


# ---------------------------------------------------------------------------
# Предупреждения гейта доходят до владельца
# ---------------------------------------------------------------------------


def test_gate_warnings_shown_to_owner(db, images_dir):
    """Раньше issues юр. гейта вычислялись и терялись: публикация смотрела
    только на hard_fail. Теперь владелец видит их в сообщении."""
    seed_post(
        "p1",
        draft=(
            "Военнослужащих ВС РФ обвиняют в пытках задержанных.\n\n"
            "По данным BBC News."
        ),
    )

    bot = FakeBot()
    asyncio.run(approval_bot.send_next_candidate(bot, make_cfg()))

    assert "⚠️" in bot.last_text()
    assert "207.3" in bot.last_text()


def test_clean_post_has_no_warnings(db, images_dir):
    """Обычный пост не должен обрастать предупреждениями на пустом месте."""
    seed_post("p1")
    (images_dir / "p1.png").write_bytes(b"png")  # иначе сработает своё ⚠️ о карточке

    bot = FakeBot()
    asyncio.run(approval_bot.send_next_candidate(bot, make_cfg()))

    assert "⚠️" not in bot.last_text()
