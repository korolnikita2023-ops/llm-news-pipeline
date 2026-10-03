"""Экраны бота: статус, расписание, причины отказа, следующий сбор.

Написано по разбору 28.08: владелец видел «hold: 9, published: 8,
rejected: 7» и не понимал ни что делать, ни когда придут новые материалы,
ни кто именно забраковал отклонённое. Отказ после выбора отправлял его
читать логи сервера, а история, набравшая 8.5 при пороге 8.6, исчезала
молча вместе с оплаченной попыткой.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone

import pytest

from conftest import FakeCallback, FakeMessage, handlers_of, post_status, seed_post
from config import Config
from src import approval_bot, news_schedule, storage


def make_cfg(*, live: bool = True, mode: str = "digest") -> Config:
    cfg = Config()
    cfg.publish_mode = "live" if live else "draft"
    cfg.workflow_mode = mode
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = "-100999"
    return cfg


# ---------------------------------------------------------------------------
# Пограничная редакторская оценка
# ---------------------------------------------------------------------------


class FakeScore:
    """Оценка скорера с управляемым средним баллом."""

    def __init__(self, average: float, legal_safety: int = 9, hard_fail: bool = False):
        self._avg = average
        self.legal_safety = legal_safety
        self.hard_fail = hard_fail
        self.tone_match = self.news_value = self.engagement = 8
        self.originality = self.factual_safety = 8
        self.min_average = 8.6
        self.issues: list = []
        self.suggested_fix = ""

    @property
    def average(self) -> float:
        return self._avg

    @property
    def decision(self) -> str:
        if self.hard_fail:
            return "reject"
        if self._avg >= self.min_average and self.legal_safety >= 6:
            return "approve"
        return "revise" if self._avg >= 5.0 else "reject"

    def to_dict(self) -> dict:
        return {"tone_match": 8, "news_value": 8, "engagement": 8,
                "originality": 8, "factual_safety": 8,
                "legal_safety": self.legal_safety, "average": self._avg,
                "hard_fail": self.hard_fail, "decision": self.decision,
                "issues": [], "suggested_fix": ""}


def _run_pipeline_item(monkeypatch, db, images_dir, average: float,
                       borderline_ok: bool, legal_safety: int = 9,
                       hard_fail: bool = False):
    """Прогоняет одну новость через _process_item с заданной оценкой."""
    from src import main as pipeline
    from src.normalize import NewsItem
    from src.relevance import RelevanceResult
    from src.rewrite import PostDraft

    item = NewsItem(
        url_hash="h1", source="bbc.com",
        title="Fraudster jailed for eight years",
        body="The judge sentenced him on Friday.",
        url="https://bbc.co.uk/news/1", published_at="",
        fetched_at="2026-08-28T00:00:00+00:00", lang="en", category="",
        sample=False,
    )
    draft = PostDraft(
        id="px1", source_item_hash="h1", source_url=item.url,
        source_name="BBC", category="громкое дело", format="A",
        hook="Суд приговорил мужчину к восьми годам.",
        body="Суд признал его виновным в мошенничестве.",
        detail_hook="Ущерб составил миллион фунтов.",
        source_line="По данным BBC.",
        full_text=("Суд приговорил мужчину к восьми годам.\n\n"
                   "Суд признал его виновным в мошенничестве.\n\n"
                   "Ущерб составил миллион фунтов.\n\nПо данным BBC."),
        headline="Приговор мошеннику",
    )
    score = FakeScore(average, legal_safety=legal_safety, hard_fail=hard_fail)

    monkeypatch.setattr(pipeline, "generate_draft", lambda *a, **k: draft)
    monkeypatch.setattr(pipeline, "score_draft", lambda cfg, d: score)
    monkeypatch.setattr(pipeline, "render_card", lambda **k: None)
    monkeypatch.setattr(pipeline, "write_draft_markdown", lambda *a, **k: None)

    cfg = make_cfg()
    relevance = RelevanceResult(relevant=True, curiosity=8, emotion=7, unusual=7,
                                shareable=7, ru_fit=7, category="громкое дело")
    stats = {"approved": 0, "manual_only": 0, "hard_rejected": 0, "regenerated": 0}
    pipeline._process_item(cfg, item, relevance, stats, borderline_ok=borderline_ok)
    return storage


def test_borderline_score_survives_in_manual_mode(db, images_dir, monkeypatch):
    """8.5 при пороге 8.6 — пост доходит до владельца, а не исчезает.

    Ровно на этом 28.08 пропала готовая история и сгорела оплаченная
    попытка: не хватило одной десятой балла.
    """
    _run_pipeline_item(monkeypatch, db, images_dir, average=8.5, borderline_ok=True)

    assert post_status("px1") == "candidate"
    with storage.get_conn() as c:
        scores = c.execute("SELECT scores FROM posts WHERE post_id='px1'").fetchone()[0]
    assert '"borderline": true' in scores


def test_borderline_band_does_not_apply_to_automatic_mode(db, images_dir, monkeypatch):
    """Без ручного режима правило не действует — прежнее поведение."""
    _run_pipeline_item(monkeypatch, db, images_dir, average=8.5, borderline_ok=False)
    assert post_status("px1") == "rejected"


def test_score_below_the_band_is_still_a_rejection(db, images_dir, monkeypatch):
    """Ниже 8.0 — по-прежнему отказ, даже в ручном режиме."""
    _run_pipeline_item(monkeypatch, db, images_dir, average=7.5, borderline_ok=True)
    assert post_status("px1") == "rejected"


def test_borderline_requires_legal_safety(db, images_dir, monkeypatch):
    """Пограничная полоса не пропускает текст, слабый именно по праву."""
    _run_pipeline_item(monkeypatch, db, images_dir, average=8.5,
                       borderline_ok=True, legal_safety=4)
    assert post_status("px1") == "rejected"


def test_legal_gate_failure_never_becomes_borderline(db, images_dir, monkeypatch):
    """Юридический гейт остаётся обязательным при любой оценке."""
    from src import main as pipeline
    from src.normalize import NewsItem
    from src.relevance import RelevanceResult
    from src.rewrite import PostDraft

    item = NewsItem(url_hash="h2", source="tass.ru", title="Задержан подозреваемый",
                    body="Полиция задержала мужчину.", url="https://tass.ru/1",
                    published_at="", fetched_at="2026-08-28T00:00:00+00:00",
                    lang="ru", category="", sample=False)
    draft = PostDraft(
        id="px2", source_item_hash="h2", source_url=item.url, source_name="ТАСС",
        category="громкое дело", format="A",
        hook="Сергей Петров зарезал соседа во время ссоры.",
        body="Тело нашли утром.", detail_hook="Идёт следствие.",
        source_line="По данным ТАСС.",
        full_text=("Сергей Петров зарезал соседа во время ссоры.\n\n"
                   "Тело нашли утром.\n\nИдёт следствие.\n\nПо данным ТАСС."),
        headline="Задержан подозреваемый",
    )
    monkeypatch.setattr(pipeline, "generate_draft", lambda *a, **k: draft)
    monkeypatch.setattr(pipeline, "score_draft", lambda cfg, d: FakeScore(9.5))
    monkeypatch.setattr(pipeline, "render_card", lambda **k: None)

    relevance = RelevanceResult(relevant=True, curiosity=8, emotion=8, unusual=8,
                                shareable=8, ru_fit=8, category="громкое дело")
    stats = {"approved": 0, "manual_only": 0, "hard_rejected": 0, "regenerated": 0}
    pipeline._process_item(make_cfg(), item, relevance, stats, borderline_ok=True)

    assert post_status("px2") == "rejected"
    with storage.get_conn() as c:
        reason = c.execute(
            "SELECT reject_reason FROM posts WHERE post_id='px2'").fetchone()[0]
    assert reason == storage.REJECT_BY_LEGAL


def test_borderline_preview_warns_and_offers_regeneration(db, images_dir, monkeypatch):
    """Пометка стоит ПЕРЕД текстом, и есть кнопка «Перегенерировать»."""
    import json

    from conftest import FakeBot

    seed_post("p1", scores={"average": 8.5, "decision": "revise", "borderline": True})
    bot = FakeBot()
    asyncio.run(approval_bot._send_post_preview(bot, make_cfg(), "p1", 777))

    text = bot.last_text()
    assert text.startswith("⚠️ Пограничная редакторская оценка: 8.5")
    kb = (bot.photos or bot.messages)[-1].get("reply_markup")
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "🔁 Перегенерировать" in labels
    assert "🌅 Утро" in labels and "❌ Отклонить" in labels
    assert json  # импорт используется для читаемости теста


def test_normal_post_has_no_warning_and_no_regen_button(db, images_dir):
    from conftest import FakeBot

    seed_post("p2", scores={"average": 9.1, "decision": "approve"})
    bot = FakeBot()
    asyncio.run(approval_bot._send_post_preview(bot, make_cfg(), "p2", 777))

    assert "Пограничная" not in bot.last_text()
    kb = (bot.photos or bot.messages)[-1].get("reply_markup")
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "🔁 Перегенерировать" not in labels


# ---------------------------------------------------------------------------
# Причина отказа словами
# ---------------------------------------------------------------------------


def test_editorial_rejection_names_score_and_threshold(db):
    cfg = make_cfg()
    text = approval_bot._explain_outcome(
        cfg, "Участник кибергруппировки получил срок",
        {"outcome": "editorial", "average": 8.5, "threshold": 8.6,
         "weakest": [("оригинальность", 7), ("тон", 8)], "issues": []},
        attempts_used=3,
    ).replace("{posts}", "1")

    assert "8.5" in text and "8.6" in text
    assert "не хватило 0.1" in text
    assert "оригинальность 7" in text
    assert "Юридических претензий нет" in text
    assert "логах" not in text.lower(), "владельца не отправляют в логи"


def test_legal_rejection_quotes_the_issue(db):
    cfg = make_cfg()
    text = approval_bot._explain_outcome(
        cfg, "История",
        {"outcome": "legal_gate", "average": None, "threshold": 8.6,
         "weakest": [], "issues": ["утверждение о виновности без оговорки"]},
        attempts_used=2,
    ).replace("{posts}", "0")

    assert "юридический гейт" in text.lower()
    assert "утверждение о виновности" in text


def test_rejection_message_shows_remaining_budget(db):
    cfg = make_cfg()
    cfg.max_preparation_attempts_per_day = 4
    cfg.max_successful_selections_per_day = 2
    text = approval_bot._explain_outcome(
        cfg, "История", {"outcome": "editorial", "average": 8.0, "threshold": 8.6,
                         "weakest": [], "issues": []}, attempts_used=3,
    ).replace("{posts}", "1")

    assert "постов 1 из 2" in text
    assert "попыток осталось 1 из 4" in text


# ---------------------------------------------------------------------------
# Причины отказа различаются
# ---------------------------------------------------------------------------


def test_user_rejection_is_recorded_separately(db, images_dir, monkeypatch):
    """❌ владельца и отказ гейта — разные вещи, и это видно в базе."""
    seed_post("p1")
    dp = approval_bot.build_dispatcher(make_cfg())
    asyncio.run(handlers_of(dp)["reject"](FakeCallback("reject:p1")))

    assert post_status("p1") == "rejected"
    assert storage.rejected_by_reason() == {storage.REJECT_BY_USER: 1}


def test_status_screen_separates_rejection_reasons(db, images_dir):
    seed_post("p1", status="rejected")
    with storage.get_conn() as c:
        c.execute("UPDATE posts SET reject_reason=? WHERE post_id='p1'",
                  (storage.REJECT_BY_EDITOR,))
    seed_post("p2", status="rejected")
    with storage.get_conn() as c:
        c.execute("UPDATE posts SET reject_reason=? WHERE post_id='p2'",
                  (storage.REJECT_BY_USER,))

    text = approval_bot._status_screen(make_cfg())
    assert "слабая редакторская оценка: 1" in text
    assert "отклонил я: 1" in text
    assert "REJECT_BY" not in text, "внутренние значения владельцу не показываем"
    assert "editorial" not in text


# ---------------------------------------------------------------------------
# Экран статуса
# ---------------------------------------------------------------------------


def test_status_screen_is_human_readable(db, images_dir):
    """Вместо сырой сводки — что делать и когда придут новые материалы."""
    seed_post("p1")
    seed_post("p2", status="hold")

    text = approval_bot._status_screen(
        make_cfg(), "сегодня в 08:03 по Москве (через 2 ч)")
    assert "Ждут решения: 1" in text
    assert "В запасе: 1" in text
    assert "Следующий сбор новостей: сегодня в 08:03 по Москве" in text
    assert "Сегодня: постов 0 из 2" in text
    assert "hold:" not in text, "сырые статусы базы больше не показываем"


def test_status_screen_shows_nearest_scheduled_post(db, images_dir):
    from src import schedule

    seed_post("p1")
    with storage.get_conn() as c:
        c.execute("UPDATE posts SET headline=? WHERE post_id='p1'",
                  ("Ограбление музея",))
    storage.schedule_post("p1", schedule.next_slot_utc(make_cfg(), "evening"), "evening")

    text = approval_bot._status_screen(make_cfg())
    assert "Ближайшая публикация: 🌙 Вечер" in text
    assert "Ограбление музея" in text
    assert "расписание недоступно" in text.lower()


def test_status_screen_survives_without_systemd(db, images_dir):
    """На машине без systemd экран не падает и не выдумывает время."""
    text = approval_bot._status_screen(make_cfg(), None)
    assert "Следующий сбор новостей: расписание недоступно" in text


def test_status_handler_does_not_block_the_event_loop(db, images_dir, monkeypatch):
    """systemctl зовётся из отдельного потока.

    Синхронный вызов держал бы обработку ВСЕХ сообщений бота на время
    ответа systemctl — до двух секунд на каждое нажатие «Статус».
    """
    import inspect

    source = inspect.getsource(approval_bot.build_dispatcher)
    assert "asyncio.to_thread(news_schedule.describe_next_run" in source

    called = {"n": 0}
    monkeypatch.setattr(news_schedule, "describe_next_run",
                        lambda cfg, **k: called.__setitem__("n", called["n"] + 1)
                        or "сегодня в 08:03 по Москве")
    dp = approval_bot.build_dispatcher(make_cfg())
    message = FakeMessage(text=approval_bot.BTN_STATUS)
    asyncio.run(handlers_of(dp)["msg_btn_status"](message))

    assert called["n"] == 1
    assert "сегодня в 08:03 по Москве" in message.answers[-1]


# ---------------------------------------------------------------------------
# «Запланировано» с заголовком
# ---------------------------------------------------------------------------


def test_scheduled_screen_shows_the_headline(db, images_dir):
    from src import schedule

    seed_post("p1")
    with storage.get_conn() as c:
        c.execute("UPDATE posts SET headline=? WHERE post_id='p1'",
                  ("Кража из музея за четыре минуты",))
    storage.schedule_post("p1", schedule.next_slot_utc(make_cfg(), "morning"), "morning")

    text, kb = approval_bot._scheduled_screen(make_cfg())
    assert "«Кража из музея за четыре минуты»" in text
    assert "🌅 Утро" in text
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert "🚀 Сейчас" in labels and "✖️ Снять" in labels
    assert any(b.startswith("👁") for b in labels)


def test_scheduled_screen_without_headline_still_works(db, images_dir):
    """Записи, сделанные до появления колонки, не должны ломать экран."""
    from src import schedule

    seed_post("p1")
    storage.schedule_post("p1", schedule.next_slot_utc(make_cfg(), "morning"), "morning")
    text, _ = approval_bot._scheduled_screen(make_cfg())
    assert "🌅 Утро" in text


# ---------------------------------------------------------------------------
# Следующий сбор новостей
# ---------------------------------------------------------------------------


def test_next_run_reads_systemd_not_a_hardcoded_time(monkeypatch):
    """Время берётся у systemd, а не из константы в коде."""
    calls = []

    def fake_ask(prop, unit):
        calls.append((prop, unit))
        return str(int(datetime(2026, 8, 28, 5, 3, tzinfo=timezone.utc).timestamp()
                       * 1_000_000))

    monkeypatch.setattr(news_schedule, "_ask_systemd", fake_ask)
    when = news_schedule.next_run_utc()

    assert calls == [("NextElapseUSecRealtime", news_schedule.NEWS_TIMER)]
    assert when == datetime(2026, 8, 28, 5, 3, tzinfo=timezone.utc)


@pytest.mark.parametrize("raw", ["", "0", None])
def test_no_schedule_means_no_invented_time(monkeypatch, raw):
    """Таймер выключен или systemd нет — время не выдумывается."""
    monkeypatch.setattr(news_schedule, "_ask_systemd", lambda p, u: raw)
    assert news_schedule.next_run_utc() is None
    assert news_schedule.describe_next_run(make_cfg()) is None


def test_next_run_is_described_in_the_owner_timezone(monkeypatch):
    """Сервер и владелец в разных зонах. Показываем зону владельца (SCHEDULE_TZ)."""
    target = datetime(2026, 8, 28, 5, 3, tzinfo=timezone.utc)   # 08:03 МСК
    monkeypatch.setattr(news_schedule, "next_run_utc", lambda unit=None: target)

    cfg = make_cfg()
    cfg.schedule_tz = "Europe/Moscow"
    now = target - timedelta(hours=2, minutes=15)
    text = news_schedule.describe_next_run(cfg, now=now)

    assert "08:03" in text
    assert "по Москве" in text
    assert "через 2 ч 15 мин" in text


def test_timezone_label_follows_the_setting(monkeypatch):
    """Подпись зоны не захардкожена: сменив SCHEDULE_TZ, получим другую."""
    cfg = make_cfg()
    cfg.schedule_tz = "Asia/Yekaterinburg"
    assert news_schedule.zone_label(cfg) == "по Екатеринбургу"
    cfg.schedule_tz = "Europe/Lisbon"
    assert "Europe/Lisbon" in news_schedule.zone_label(cfg)


def test_tomorrow_is_called_tomorrow(monkeypatch):
    target = datetime(2026, 8, 29, 5, 3, tzinfo=timezone.utc)
    monkeypatch.setattr(news_schedule, "next_run_utc", lambda unit=None: target)
    cfg = make_cfg()
    text = news_schedule.describe_next_run(cfg, now=target - timedelta(hours=20))
    assert text.startswith("завтра в")


def test_systemctl_failure_is_not_fatal(monkeypatch):
    """Упавший systemctl не должен ронять обработчик кнопки."""
    import subprocess

    def boom(*a, **k):
        raise subprocess.SubprocessError("systemctl умер")

    monkeypatch.setattr(news_schedule.subprocess, "run", boom)
    monkeypatch.setattr(news_schedule.shutil, "which", lambda name: "/bin/systemctl")
    assert news_schedule.next_run_utc() is None


# ---------------------------------------------------------------------------
# «Новых постов пока нет»
# ---------------------------------------------------------------------------


def test_empty_queue_message_names_the_next_collection(db, images_dir, monkeypatch):
    monkeypatch.setattr(news_schedule, "describe_next_run",
                        lambda cfg, **k: "сегодня в 08:03 по Москве (через 2 ч 15 мин)")
    dp = approval_bot.build_dispatcher(make_cfg())
    message = FakeMessage(text=approval_bot.BTN_NEXT)
    asyncio.run(handlers_of(dp)["msg_btn_next"](message))

    text = message.answers[-1]
    assert "Следующий сбор новостей: сегодня в 08:03 по Москве" in text
    assert "через 2 ч 15 мин" in text


def test_empty_queue_message_without_schedule_says_so(db, images_dir, monkeypatch):
    monkeypatch.setattr(news_schedule, "describe_next_run", lambda cfg, **k: None)
    dp = approval_bot.build_dispatcher(make_cfg())
    message = FakeMessage(text=approval_bot.BTN_NEXT)
    asyncio.run(handlers_of(dp)["msg_btn_next"](message))

    text = message.answers[-1]
    assert "недоступно" in text
    assert "08:0" not in text, "время не выдумывается"


def test_legal_gate_issues_reach_the_owner(db, images_dir, monkeypatch):
    """Претензии гейта должны доехать до Telegram, а не остаться в логах.

    Скорер до забракованного гейтом текста не доходит, поэтому его
    замечаний нет вовсе — класть их некуда, кроме поля оценок.
    """
    from src import main as pipeline
    from src.normalize import NewsItem
    from src.relevance import RelevanceResult
    from src.rewrite import PostDraft

    item = NewsItem(url_hash="h3", source="tass.ru", title="Задержан подозреваемый",
                    body="Полиция задержала мужчину.", url="https://tass.ru/1",
                    published_at="", fetched_at="2026-08-28T00:00:00+00:00",
                    lang="ru", category="", sample=False)
    draft = PostDraft(
        id="px3", source_item_hash="h3", source_url=item.url, source_name="ТАСС",
        category="громкое дело", format="A",
        hook="Сергей Петров зарезал соседа во время ссоры.",
        body="Тело нашли утром.", detail_hook="Идёт следствие.",
        source_line="По данным ТАСС.",
        full_text=("Сергей Петров зарезал соседа во время ссоры.\n\n"
                   "Тело нашли утром.\n\nИдёт следствие.\n\nПо данным ТАСС."),
        headline="Задержан подозреваемый",
    )
    monkeypatch.setattr(pipeline, "generate_draft", lambda *a, **k: draft)
    monkeypatch.setattr(pipeline, "render_card", lambda **k: None)

    relevance = RelevanceResult(relevant=True, curiosity=8, emotion=8, unusual=8,
                                shareable=8, ru_fit=8, category="громкое дело")
    stats = {"approved": 0, "manual_only": 0, "hard_rejected": 0, "regenerated": 0}
    pipeline._process_item(make_cfg(), item, relevance, stats, borderline_ok=True)

    with storage.get_conn() as c:
        scores = c.execute(
            "SELECT scores FROM posts WHERE post_id='px3'").fetchone()[0]
    assert "legal_issues" in scores
    assert "виновност" in scores


# ---------------------------------------------------------------------------
# Находки независимого ревью 28.08.2026
# ---------------------------------------------------------------------------


def _news_item():
    """Исходная новость для перегенерации: из неё читается судебный статус."""
    from src.normalize import NewsItem

    return NewsItem(
        url_hash="p1", source="bbc.com",
        title="Fraudster jailed for eight years",
        body="The judge sentenced him on Friday.",
        url="https://example.com/news/1", published_at="",
        fetched_at="2026-08-28T00:00:00+00:00", lang="en", category="",
        sample=False,
    )


def test_borderline_band_has_an_explicit_upper_bound(db, images_dir, monkeypatch):
    """Оценка выше порога не должна попадать в пограничную полосу.

    Сейчас такая до неё и не доходит — её приняли бы раньше. Но полагаться
    на это нельзя: измени кто-нибудь условия approve, и «пограничным» стал
    бы любой текст, забракованный скорером по другой причине.
    """
    import inspect

    from src import main as pipeline

    source = inspect.getsource(pipeline._process_item)
    assert "< cfg.min_editorial_avg" in source


def test_score_rejected_for_another_reason_does_not_become_borderline(
        db, images_dir, monkeypatch):
    """hard_fail скорера — не пограничный случай ни при какой оценке."""
    _run_pipeline_item(monkeypatch, db, images_dir, average=9.0,
                       borderline_ok=True, hard_fail=True)
    assert post_status("px1") == "rejected"


def test_systemd_text_time_is_converted_by_the_system(monkeypatch):
    """Сокращение зоны (CEST) разбирает система, а не мы.

    strptime смещение по имени зоны не восстанавливает, а подстановка
    текущего смещения сервера врёт на час по другую сторону перевода часов.
    """
    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"] = argv

        class R:
            returncode = 0
            stdout = "1787893416\n"
        return R()

    monkeypatch.setattr(news_schedule, "_ask_systemd",
                        lambda p, u: "Fri 2026-08-28 07:03:36 CEST")
    monkeypatch.setattr(news_schedule.shutil, "which", lambda name: f"/bin/{name}")
    monkeypatch.setattr(news_schedule.subprocess, "run", fake_run)

    when = news_schedule.next_run_utc()
    assert when == datetime(2026, 8, 28, 5, 3, 36, tzinfo=timezone.utc)
    # Строка передана отдельным аргументом: подставить команду нельзя.
    assert seen["argv"][:2] == ["date", "-d"]
    assert seen["argv"][2] == "Fri 2026-08-28 07:03:36 CEST"


def test_absurd_timestamp_is_not_fatal(monkeypatch):
    """Бессмысленное число не должно ронять обработчик."""
    monkeypatch.setattr(news_schedule, "_ask_systemd", lambda p, u: "9" * 25)
    assert news_schedule.next_run_utc() is None


def test_date_failure_means_unknown_schedule(monkeypatch):
    monkeypatch.setattr(news_schedule, "_ask_systemd", lambda p, u: "какая-то чушь")
    monkeypatch.setattr(news_schedule.shutil, "which", lambda name: f"/bin/{name}")

    class R:
        returncode = 1
        stdout = ""
    monkeypatch.setattr(news_schedule.subprocess, "run", lambda *a, **k: R())
    assert news_schedule.next_run_utc() is None


def test_regeneration_keeps_the_borderline_mark(db, images_dir, monkeypatch):
    """Переписанный текст, оставшийся ниже порога, — по-прежнему пограничный.

    Иначе предупреждение исчезало после нажатия «Перегенерировать», а
    причина его — нет.
    """
    import json

    from src.rewrite import PostDraft

    seed_post("p1", scores={"average": 8.5, "borderline": True})
    draft = PostDraft(
        id="p1new", source_item_hash="p1", source_url="https://example.com/news/1",
        source_name="BBC", category="громкое дело", format="A",
        hook="Суд приговорил мужчину к восьми годам.",
        body="Суд признал его виновным.", detail_hook="Ущерб — миллион.",
        source_line="По данным BBC.",
        full_text=("Суд приговорил мужчину к восьми годам.\n\n"
                   "Суд признал его виновным.\n\nУщерб — миллион.\n\nПо данным BBC."),
        headline="Новый заголовок",
    )
    monkeypatch.setattr(approval_bot, "_case_status_of", lambda pid, **k: "sentenced")
    monkeypatch.setattr("src.rewrite.generate_draft", lambda *a, **k: draft)
    monkeypatch.setattr("src.scorer.score_draft", lambda cfg, d: FakeScore(8.2))
    monkeypatch.setattr("src.image_card.render_card", lambda **k: None)

    approval_bot._regenerate(make_cfg(), "p1", _news_item())

    with storage.get_conn() as c:
        scores, headline = c.execute(
            "SELECT scores, headline FROM posts WHERE post_id='p1'").fetchone()
    assert json.loads(scores).get("borderline") is True
    assert headline == "Новый заголовок", "заголовок в расписании остался бы старым"


def test_regeneration_above_threshold_drops_the_mark(db, images_dir, monkeypatch):
    import json

    from src.rewrite import PostDraft

    seed_post("p1", scores={"average": 8.5, "borderline": True})
    draft = PostDraft(
        id="p1new", source_item_hash="p1", source_url="https://example.com/news/1",
        source_name="BBC", category="громкое дело", format="A",
        hook="Суд приговорил мужчину к восьми годам.",
        body="Суд признал его виновным.", detail_hook="Ущерб — миллион.",
        source_line="По данным BBC.",
        full_text=("Суд приговорил мужчину к восьми годам.\n\n"
                   "Суд признал его виновным.\n\nУщерб — миллион.\n\nПо данным BBC."),
        headline="Заголовок получше",
    )
    monkeypatch.setattr(approval_bot, "_case_status_of", lambda pid, **k: "sentenced")
    monkeypatch.setattr("src.rewrite.generate_draft", lambda *a, **k: draft)
    monkeypatch.setattr("src.scorer.score_draft", lambda cfg, d: FakeScore(9.1))
    monkeypatch.setattr("src.image_card.render_card", lambda **k: None)

    approval_bot._regenerate(make_cfg(), "p1", _news_item())

    with storage.get_conn() as c:
        scores = c.execute("SELECT scores FROM posts WHERE post_id='p1'").fetchone()[0]
    assert "borderline" not in json.loads(scores)


def test_technical_failure_is_recorded_as_such(db, images_dir):
    """Сбой подготовки не должен выглядеть в статистике как решение гейта."""
    seed_post("p1")
    assert storage.mark_event_post_failed("evt_p1") == 1
    assert storage.rejected_by_reason() == {storage.REJECT_BY_ERROR: 1}

    text = approval_bot._status_screen(make_cfg(), None)
    assert "сбой при подготовке: 1" in text


def test_published_post_is_never_marked_failed(db, images_dir):
    """Пометка сбоя не должна трогать то, что уже ушло в канал."""
    seed_post("p1", status="published")
    assert storage.mark_event_post_failed("evt_p1") == 0
    assert post_status("p1") == "published"


def test_old_rejections_are_not_reported_as_a_failure(db, images_dir):
    """Записи до появления колонки — не «причина не записана», а история.

    Формулировка намекала бы на сбой там, где его не было.
    """
    seed_post("p1", status="rejected")
    text = approval_bot._status_screen(make_cfg(), None)
    assert "решения до 28.08" in text
    assert "причина не записана" not in text
