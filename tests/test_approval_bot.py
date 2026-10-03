"""Тесты approve-бота: сценарии владельца, нажатия кнопок, кривой ввод.

Ни один тест не ходит в Telegram и не вызывает LLM — всё замокано.
"""
from __future__ import annotations

import asyncio

import pytest

from config import Config
from conftest import (
    FakeBot,
    FakeCallback,
    FakeMessage,
    handlers_of,
    post_status,
    post_text,
    seed_post,
)
from src import approval_bot, storage


def make_cfg(*, live: bool = True) -> Config:
    cfg = Config()
    cfg.publish_mode = "live" if live else "draft"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = "-100999"
    return cfg


@pytest.fixture
def sent(monkeypatch):
    """Перехватывает реальную отправку в канал."""
    calls = []

    def fake_send(cfg, text, image_path=None, source_url=''):
        calls.append({"text": text, "image": image_path})
        return True, "ok"

    from src import publisher

    monkeypatch.setattr(publisher, "send_live_text", fake_send)
    return calls


# ---------------------------------------------------------------------------
# S1-S2. Выдача кандидата владельцу
# ---------------------------------------------------------------------------


def test_candidate_sent_with_card(db, images_dir):
    """S1. Кандидат уходит владельцу ФОТОГРАФИЕЙ с карточкой — он должен
    видеть ровно то, что уйдёт в канал."""
    seed_post("p1")
    (images_dir / "p1.png").write_bytes(b"fake-png")

    bot = FakeBot()
    ok = asyncio.run(approval_bot.send_next_candidate(bot, make_cfg()))

    assert ok is True
    assert len(bot.photos) == 1, "кандидат с карточкой должен уходить фото"
    assert len(bot.messages) == 0
    assert "Заголовок поста" in bot.photos[0]["caption"]


def test_candidate_without_card_falls_back_to_text(db, images_dir):
    """S16. Карточка не сгенерировалась — не молчим и не падаем, шлём текст
    с явным предупреждением."""
    seed_post("p1")

    bot = FakeBot()
    ok = asyncio.run(approval_bot.send_next_candidate(bot, make_cfg()))

    assert ok is True
    assert len(bot.messages) == 1
    assert "карточка не сгенерирована" in bot.messages[0]["text"]


def test_empty_queue_returns_false(db, images_dir):
    """S2. Очередь пуста — бот сообщает об этом, а не падает."""
    bot = FakeBot()
    assert asyncio.run(approval_bot.send_next_candidate(bot, make_cfg())) is False


def test_queue_counter_excludes_current(db, images_dir):
    """Счётчик 'в очереди ещё' не считает показываемый прямо сейчас пост."""
    for i in range(3):
        seed_post(f"p{i}", created_at=f"2026-08-2{i}T00:00:00+00:00")

    bot = FakeBot()
    asyncio.run(approval_bot.send_next_candidate(bot, make_cfg()))
    assert "в очереди ещё: 2" in bot.last_text()


def test_oldest_candidate_goes_first(db, images_dir):
    """Порядок очереди — от старых к новым."""
    seed_post("new", created_at="2026-08-26T00:00:00+00:00", draft="Новая.\n\nПо данным BBC.")
    seed_post("old", created_at="2026-08-13T00:00:00+00:00", draft="Старая.\n\nПо данным BBC.")

    bot = FakeBot()
    asyncio.run(approval_bot.send_next_candidate(bot, make_cfg()))
    assert "Старая" in bot.last_text()


# ---------------------------------------------------------------------------
# S3-S7. Кнопки решений
# ---------------------------------------------------------------------------


def test_approve_publishes_once(db, images_dir, sent):
    """S3. ✅ → пост уходит в канал ровно один раз и помечается published."""
    seed_post("p1")
    (images_dir / "p1.png").write_bytes(b"png")
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback("approve:p1")

    asyncio.run(handlers_of(dp)["approve"](call))

    assert len(sent) == 1
    assert sent[0]["image"] is not None, "карточка должна прикладываться"
    assert post_status("p1") == "published"
    assert "опубликовано" in call.message.caption.lower()


def test_double_approve_does_not_double_publish(db, images_dir, sent):
    """S5. Владелец нажал ✅ дважды (тормозит связь) — второй раз в канал
    ничего не уходит."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())
    h = handlers_of(dp)["approve"]

    asyncio.run(h(FakeCallback("approve:p1")))
    asyncio.run(h(FakeCallback("approve:p1")))

    assert len(sent) == 1, "повторное нажатие не должно публиковать второй раз"


def test_approve_when_publishing_disabled(db, images_dir, sent):
    """S4. PUBLISH_MODE не live — пост одобрен, но в канал не ушёл, и бот
    честно об этом пишет."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg(live=False))
    call = FakeCallback("approve:p1")

    asyncio.run(handlers_of(dp)["approve"](call))

    assert len(sent) == 0
    assert post_status("p1") == "approved"
    assert "не ушло" in call.message.caption


def test_reject_does_not_publish(db, images_dir, sent):
    """S6. ❌ — статус rejected, в канал ничего."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())

    asyncio.run(handlers_of(dp)["reject"](FakeCallback("reject:p1")))

    assert post_status("p1") == "rejected"
    assert len(sent) == 0


def test_hold_keeps_post(db, images_dir, sent):
    """S7. ⏸ — пост сохраняется в запас, не удаляется и не публикуется."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())

    asyncio.run(handlers_of(dp)["hold"](FakeCallback("hold:p1")))

    assert post_status("p1") == "hold"
    assert len(sent) == 0


def test_decision_sends_next_candidate(db, images_dir, sent):
    """После решения бот сам присылает следующий пост — без /review."""
    seed_post("p1", created_at="2026-08-20T00:00:00+00:00")
    seed_post("p2", created_at="2026-08-21T00:00:00+00:00")
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback("reject:p1")

    asyncio.run(handlers_of(dp)["reject"](call))

    assert call.bot.sent_count == 1, "следующий кандидат должен уйти автоматически"


def test_unknown_post_id_does_not_crash(db, images_dir, sent):
    """S14. Кнопка от давно удалённого поста — бот не падает."""
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback("approve:ghost")

    asyncio.run(handlers_of(dp)["approve"](call))

    assert len(sent) == 0
    assert "не ушло" in call.message.caption


# ---------------------------------------------------------------------------
# S8-S10. Перегенерация
# ---------------------------------------------------------------------------


def test_regenerate_rewrites_post(db, images_dir, monkeypatch, sent):
    """S8. 🔁 — пост переписывается СРАЗУ и остаётся кандидатом.
    Регрессия: раньше кнопка ставила needs_regenerate и пост умирал навсегда.
    """
    seed_post("p1", draft="Старый текст.\n\nПо данным BBC News.")
    monkeypatch.setattr(
        approval_bot,
        "_regenerate",
        lambda cfg, pid, item: _apply_new_text(pid, "Новый текст.\n\nПо данным BBC News."),
    )
    dp = approval_bot.build_dispatcher(make_cfg())

    asyncio.run(handlers_of(dp)["regen"](FakeCallback("regen:p1")))

    assert post_status("p1") == "candidate"
    assert "Новый текст" in post_text("p1")
    assert len(sent) == 0, "перегенерация сама ничего не публикует"


def _apply_new_text(post_id: str, text: str) -> str:
    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE posts SET draft_ru=?, status='candidate' WHERE post_id=?",
            (text, post_id),
        )
    return text


def test_regenerate_without_source_is_graceful(db, images_dir, sent):
    """S9. Исходной новости в базе нет — бот объясняет, а не падает."""
    storage.insert_post(
        post_id="orphan", event_id="evt_missing", draft_ru="Текст.",
        fmt="A", status="candidate", scores={}, manual_only=False,
    )
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback("regen:orphan")

    asyncio.run(handlers_of(dp)["regen"](call))

    assert any("переписать не из чего" in a for a in call.message.answers)
    assert post_status("orphan") == "candidate"


def test_regenerate_failure_rejects_post(db, images_dir, monkeypatch, sent):
    """S10. Новый вариант не прошёл гейт — пост отклоняется, владельцу
    не подсовывают заведомо негодный текст."""
    seed_post("p1")
    monkeypatch.setattr(approval_bot, "_regenerate", lambda cfg, pid, item: None)
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback("regen:p1")

    asyncio.run(handlers_of(dp)["regen"](call))

    assert post_status("p1") == "rejected"
    assert any("не прошёл" in a for a in call.message.answers)


def test_regenerate_crash_is_reported(db, images_dir, monkeypatch, sent):
    """Падение LLM во время перегенерации не роняет бота и не молчит."""
    seed_post("p1")

    def boom(cfg, pid, item):
        raise RuntimeError("LLM недоступен")

    monkeypatch.setattr(approval_bot, "_regenerate", boom)
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback("regen:p1")

    asyncio.run(handlers_of(dp)["regen"](call))

    assert any("не удалась" in a for a in call.message.answers)


# ---------------------------------------------------------------------------
# S11-S13. Ручная правка
# ---------------------------------------------------------------------------


class FakeEditMessage(FakeMessage):
    def __init__(self, text: str):
        super().__init__(text=text)


class FakeState:
    def __init__(self, data: dict | None = None):
        self._data = data or {}
        self.cleared = False

    async def get_data(self):
        return self._data

    async def update_data(self, **kw):
        self._data.update(kw)

    async def set_state(self, state):
        self._data["_state"] = state

    async def clear(self):
        self.cleared = True
        self._data = {}


def test_edit_publishes(db, images_dir, sent):
    """S11. ✏️ + новый текст → пост уходит в канал.
    Регрессия: раньше правка молча помечала пост approved и не публиковала.
    """
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = FakeEditMessage("Мой текст.\n\nПо данным BBC News.")

    asyncio.run(
        handlers_of(dp)["msg_on_new_text"](msg, FakeState({"editing_post_id": "p1"}))
    )

    assert len(sent) == 1
    assert sent[0]["text"] == "Мой текст.\n\nПо данным BBC News."
    assert post_status("p1") == "published"


def test_edited_text_still_passes_legal_gate(db, images_dir, sent):
    """S12. КРИТИЧНО: текст, написанный владельцем руками, тоже проходит
    юридический гейт. Слово о виновности без оговорки о статусе дела —
    в канал не уходит."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = FakeEditMessage("Убийца задержан вчера.\n\nПо данным BBC News.")

    asyncio.run(
        handlers_of(dp)["msg_on_new_text"](msg, FakeState({"editing_post_id": "p1"}))
    )

    assert len(sent) == 0, "гейт обязан остановить публикацию"
    assert post_status("p1") != "published"
    assert any("гейт" in a for a in msg.answers)


def test_edit_without_selected_post(db, images_dir, sent):
    """S13. Владелец прислал текст, не нажав ✏️ — бот объясняет, что делать."""
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = FakeEditMessage("просто текст")

    asyncio.run(handlers_of(dp)["msg_on_new_text"](msg, FakeState({})))

    assert len(sent) == 0
    assert any("/review" in a for a in msg.answers)


# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------


def test_load_news_item_finds_source(db):
    """Перегенерация должна уметь достать исходную новость по посту."""
    seed_post("p1", url="https://elpais.com/story", title="Original title")

    item = approval_bot._load_news_item("p1")

    assert item is not None
    assert item.url == "https://elpais.com/story"
    assert item.title == "Original title"


def test_load_news_item_missing_returns_none(db):
    assert approval_bot._load_news_item("nope") is None


def test_publish_blocks_post_without_source_url(db, images_dir, sent):
    """Пост без ссылки на источник не публикуется никогда — hard fail гейта."""
    seed_post("p1", url="")
    ok, status = approval_bot._publish_post(make_cfg(), "p1")

    assert ok is False
    assert "гейт" in status
    assert len(sent) == 0
