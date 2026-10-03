"""Безопасность доступа к боту и лимиты Telegram API.

Оба класса проблем найдены в ходе adversarial-прохода 27.08.2026:
- бот отвечал кому угодно (не было проверки отправителя);
- публикация молча обрезала пост до 1024 символов, срезая строку источника.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from conftest import FakeBot, seed_post
from config import Config
from src import approval_bot, publisher
from src.publisher import TELEGRAM_TEXT_LIMIT


def make_cfg(owner: str = "777", *, live: bool = True) -> Config:
    cfg = Config()
    cfg.publish_mode = "live" if live else "draft"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = owner
    cfg.telegram_channel_id = "-100999"
    # Config() читает настоящий .env — список админов оттуда сделал бы
    # проверки доступа зависимыми от машины разработчика.
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    return cfg


def _event_from(user_id: int):
    return SimpleNamespace(from_user=SimpleNamespace(id=user_id))


# ---------------------------------------------------------------------------
# S19. Доступ к боту
# ---------------------------------------------------------------------------


def test_owner_passes_filter(db):
    dp = approval_bot.build_dispatcher(make_cfg("777"))
    flt = dp.message._handler.filters[0]
    assert bool(flt.callback(_event_from(777))) is True


def test_stranger_is_blocked_in_messages(db):
    """Посторонний не должен проходить дальше фильтра сообщений."""
    dp = approval_bot.build_dispatcher(make_cfg("777"))
    flt = dp.message._handler.filters[0]
    assert bool(flt.callback(_event_from(999))) is False


def test_stranger_is_blocked_in_callbacks(db):
    """Самое важное: чужой не должен уметь нажимать кнопки публикации."""
    dp = approval_bot.build_dispatcher(make_cfg("777"))
    flt = dp.callback_query._handler.filters[0]
    assert bool(flt.callback(_event_from(999))) is False
    assert bool(flt.callback(_event_from(777))) is True


def test_malformed_owner_id_keeps_other_admins(db, capsys):
    """Кривой TELEGRAM_OWNER_CHAT_ID не должен ронять бота и не должен
    лишать доступа остальных админов — он просто пропускается."""
    cfg = make_cfg("не-число")
    cfg.telegram_admin_ids = "111"
    dp = approval_bot.build_dispatcher(cfg)

    assert dp is not None
    flt = dp.message._handler.filters[0]
    assert bool(flt.callback(_event_from(111))) is True, "живой админ теряться не должен"
    assert "не число" in capsys.readouterr().out


def test_no_valid_admins_warns_loudly(db, capsys):
    """А вот если не осталось НИ ОДНОГО валидного админа — фильтр выключен,
    и об этом обязано быть громко сказано: бот отвечает кому угодно."""
    cfg = make_cfg("не-число")
    cfg.telegram_admin_ids = ""
    approval_bot.build_dispatcher(cfg)

    assert "фильтр НЕ включён" in capsys.readouterr().out


@pytest.mark.parametrize("owner", ["777", " 777 ", 777])
def test_owner_id_parsing_variants(owner):
    cfg = make_cfg()
    cfg.telegram_owner_chat_id = owner
    assert approval_bot._owner_id(cfg) == 777


# ---------------------------------------------------------------------------
# Лимиты Telegram: пост не должен молча обрезаться
# ---------------------------------------------------------------------------


@pytest.fixture
def http(monkeypatch):
    """Перехватывает HTTP-запросы к Telegram."""
    calls = []

    class Resp:
        def raise_for_status(self):
            return None

    def fake_post(url, **kw):
        calls.append({"url": url, "data": kw.get("data", {}), "files": kw.get("files")})
        return Resp()

    monkeypatch.setattr(publisher.httpx, "post", fake_post)
    return calls


def test_short_post_goes_as_photo_caption(http, tmp_path):
    img = tmp_path / "c.png"
    img.write_bytes(b"png")
    text = "Короткий пост.\n\nПо данным BBC News."

    ok, status = publisher.send_live_text(make_cfg(), text, img)

    assert ok is True
    assert len(http) == 1
    assert http[0]["url"].endswith("/sendPhoto")
    assert http[0]["data"]["caption"] == text, "текст не должен меняться"


def test_long_post_is_not_truncated(http, tmp_path):
    """Регрессия: раньше пост резался до 1024 символов вместе со строкой
    источника. Теперь длинный пост уходит целиком отдельным сообщением."""
    img = tmp_path / "c.png"
    img.write_bytes(b"png")
    body = "А" * 1200
    text = f"{body}\n\nПо данным BBC News."

    ok, status = publisher.send_live_text(make_cfg(), text, img)

    assert ok is True
    assert len(http) == 2, "ожидаем фото + отдельное сообщение"
    assert http[0]["url"].endswith("/sendPhoto")
    assert "caption" not in http[0]["data"]
    assert http[1]["url"].endswith("/sendMessage")
    sent_text = http[1]["data"]["text"]
    assert sent_text == text
    assert "По данным BBC News" in sent_text, "источник обязан дойти до канала"


def test_text_only_post_uses_full_4096_limit(http):
    """Без карточки лимит 4096, а не 1024 — раньше резалось по 1024."""
    text = "Б" * 3000 + "\n\nПо данным BBC News."

    ok, _ = publisher.send_live_text(make_cfg(), text, None)

    assert ok is True
    assert http[0]["data"]["text"] == text


def test_oversized_post_refuses_loudly(http):
    """Больше 4096 — громкий отказ, а не тихая обрезка."""
    text = "В" * 5000

    ok, status = publisher.send_live_text(make_cfg(), text, None)

    assert ok is False
    assert "лимите" in status
    assert http == [], "ничего не должно было уйти"


def test_empty_post_refuses(http):
    ok, status = publisher.send_live_text(make_cfg(), "   ", None)
    assert ok is False
    assert http == []


def test_review_message_splits_when_too_long(db, images_dir):
    """Кандидат длиннее лимита подписи уходит владельцу двумя сообщениями,
    а не падает с ошибкой Telegram."""
    long_draft = "Г" * 1100 + "\n\nПо данным BBC News."
    seed_post("p1", draft=long_draft)
    (images_dir / "p1.png").write_bytes(b"png")

    bot = FakeBot()
    ok = asyncio.run(approval_bot.send_next_candidate(bot, make_cfg()))

    assert ok is True
    assert len(bot.photos) == 1 and "caption" not in bot.photos[0]
    assert len(bot.messages) == 1
    assert bot.messages[0]["reply_markup"] is not None, "кнопки не должны потеряться"
    assert len(bot.messages[0]["text"]) <= TELEGRAM_TEXT_LIMIT
