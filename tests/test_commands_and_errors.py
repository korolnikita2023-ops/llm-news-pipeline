"""Команды бота, обработка ошибок и защита бюджета.

Найдено в adversarial-проходе:
- на /start бот молчал (выглядело как «сломался»);
- падение внутри обработчика уходило только в лог aiogram;
- дневной лимит расходов проверялся в конвейере, но не в кнопке 🔁 —
  лимит обходился одним нажатием.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import FakeCallback, FakeMessage, handlers_of, seed_post
from config import Config
from src import approval_bot, storage


def make_cfg(*, live: bool = True, limit: float = 3.0) -> Config:
    cfg = Config()
    cfg.publish_mode = "live" if live else "draft"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = "-100999"
    cfg.daily_cost_limit_usd = limit
    return cfg


class Msg(FakeMessage):
    def __init__(self, text: str = "/start"):
        super().__init__(text=text)


# ---------------------------------------------------------------------------
# Команды
# ---------------------------------------------------------------------------


def test_start_explains_buttons(db, images_dir):
    """/start обязан объяснить, что делать. Раньше бот на него молчал."""
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = Msg()

    asyncio.run(handlers_of(dp)["msg_cmd_start"](msg))

    answer = msg.answers[0]
    for button in ("✅", "✏️", "🔁", "❌", "⏸"):
        assert button in answer, f"кнопка {button} не описана в справке"
    assert "/review" in answer


def test_start_shows_queue_size(db, images_dir):
    seed_post("p1")
    seed_post("p2")
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = Msg()

    asyncio.run(handlers_of(dp)["msg_cmd_start"](msg))

    assert "Сейчас в очереди: 2" in msg.answers[0]


def test_queue_command_reports_statuses(db, images_dir):
    seed_post("a", status="candidate")
    seed_post("b", status="published")
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = Msg("/queue")

    asyncio.run(handlers_of(dp)["msg_cmd_queue"](msg))

    out = msg.answers[0]
    assert "candidate: 1" in out
    assert "published: 1" in out


def test_queue_on_empty_db(db, images_dir):
    """Пустая база не должна ронять команду."""
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = Msg("/queue")

    asyncio.run(handlers_of(dp)["msg_cmd_queue"](msg))

    assert msg.answers, "ответ должен быть даже на пустой базе"


def test_review_on_empty_queue(db, images_dir):
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = Msg("/review")

    asyncio.run(handlers_of(dp)["msg_cmd_review"](msg))

    assert "Кандидатов на модерацию нет" in msg.answers[0]


# ---------------------------------------------------------------------------
# Дневной лимит расходов
# ---------------------------------------------------------------------------


def test_regenerate_refuses_over_budget(db, images_dir, monkeypatch):
    """Лимит исчерпан → перегенерация не делает платный вызов модели."""
    seed_post("p1")
    storage.log_cost("rewrite", "gemini", "flash", 5.0)

    called = []
    monkeypatch.setattr(
        approval_bot, "generate_draft" if hasattr(approval_bot, "generate_draft") else "_mark",
        lambda *a, **k: called.append(1),
        raising=False,
    )

    item = approval_bot._load_news_item("p1")
    with pytest.raises(approval_bot.LLMBudgetExceededError):
        approval_bot._regenerate(make_cfg(limit=3.0), "p1", item)


def test_regenerate_allowed_under_budget(db, images_dir, monkeypatch):
    """Под лимитом перегенерация не блокируется бюджетом."""
    seed_post("p1")
    storage.log_cost("rewrite", "gemini", "flash", 0.01)

    import src.rewrite as rewrite_mod

    class FakeDraft:
        id = "p1"
        full_text = "Новый.\n\nПо данным BBC News."
        source_url = "https://bbc.com/1"
        hook = "Новый"
        category = "громкое дело"
        source_name = "BBC"
        format = "A"

    monkeypatch.setattr(rewrite_mod, "generate_draft", lambda *a, **k: FakeDraft())

    class FakeScore:
        decision = "approve"
        average = 9.0

        def to_dict(self):
            return {"average": 9.0, "decision": "approve"}

    import src.scorer as scorer_mod

    monkeypatch.setattr(scorer_mod, "score_draft", lambda *a, **k: FakeScore())

    item = approval_bot._load_news_item("p1")
    result = approval_bot._regenerate(make_cfg(limit=3.0), "p1", item)

    assert result is not None
    assert "Новый" in result


def test_budget_error_reaches_owner(db, images_dir, monkeypatch):
    """Отказ по бюджету должен дойти до владельца текстом, а не молчанием."""
    seed_post("p1")

    def over_budget(cfg, pid, item):
        raise approval_bot.LLMBudgetExceededError("дневной лимит расходов исчерпан")

    monkeypatch.setattr(approval_bot, "_regenerate", over_budget)
    dp = approval_bot.build_dispatcher(make_cfg())
    call = FakeCallback("regen:p1")

    asyncio.run(handlers_of(dp)["regen"](call))

    assert any("лимит" in a for a in call.message.answers)


# ---------------------------------------------------------------------------
# Обработчик ошибок
# ---------------------------------------------------------------------------


def test_error_handler_registered(db, images_dir):
    """Глобальная ловушка исключений должна быть зарегистрирована —
    иначе падение обработчика видно только в логах aiogram."""
    dp = approval_bot.build_dispatcher(make_cfg())
    assert dp.errors.handlers, "не зарегистрирован ни один error-handler"


def test_error_handler_notifies_owner(db, images_dir, capsys):
    """Ловушка пишет трейс в лог и шлёт владельцу понятное сообщение."""
    dp = approval_bot.build_dispatcher(make_cfg())
    handler = dp.errors.handlers[0].callback

    class FakeUpdateBot:
        def __init__(self):
            self.sent = []

        async def send_message(self, **kw):
            self.sent.append(kw)

    bot = FakeUpdateBot()

    class Event:
        exception = ValueError("что-то пошло не так")
        update = type("U", (), {"bot": bot})()

    handled = asyncio.run(handler(Event()))

    assert handled is True
    assert bot.sent, "владелец должен получить уведомление об ошибке"
    assert "ValueError" in bot.sent[0]["text"]
    assert "НЕОБРАБОТАННАЯ ОШИБКА" in capsys.readouterr().out


def test_error_handler_survives_notify_failure(db, images_dir):
    """Если даже уведомить не получилось — ловушка не должна упасть сама."""
    dp = approval_bot.build_dispatcher(make_cfg())
    handler = dp.errors.handlers[0].callback

    class BrokenBot:
        async def send_message(self, **kw):
            raise RuntimeError("Telegram недоступен")

    class Event:
        exception = ValueError("ошибка")
        update = type("U", (), {"bot": BrokenBot()})()

    assert asyncio.run(handler(Event())) is True
