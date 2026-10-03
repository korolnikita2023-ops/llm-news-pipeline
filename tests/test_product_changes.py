"""Продуктовые изменения 27.08.2026:
1) короткий заголовок на карточке (отдельное поле, а не обрезка текста);
2) кликабельная ссылка на исходную статью в строке «По данным ...»;
3) сводка расходов на модель для команды /budget.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import FakeMessage, handlers_of, seed_post
from config import Config
from src import approval_bot, legal_gate, publisher, storage
from src.publisher import build_html_with_source_link
from src.rewrite import HEADLINE_MAX_CHARS, _clean_headline, _headline_from_hook


def make_cfg() -> Config:
    cfg = Config()
    cfg.publish_mode = "live"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = "-100999"
    cfg.daily_cost_limit_usd = 3.0
    return cfg


# ---------------------------------------------------------------------------
# 1. Заголовок карточки
# ---------------------------------------------------------------------------


def test_headline_is_cleaned():
    assert _clean_headline('  «Трампа вывезли в фургоне.»  ') == "Трампа вывезли в фургоне"


def test_too_long_headline_is_rejected_not_cut():
    """Слишком длинный заголовок отбрасывается целиком — на карточке нужен
    осмысленный текст, а не обрубок на середине слова."""
    assert _clean_headline("Очень длинный заголовок, который не влезет на карточку никак") == ""


def test_fallback_headline_never_cuts_mid_word():
    hook = "В аэропорту ЮАР задержали пассажира с самодельным инкубатором в клади"
    out = _headline_from_hook(hook)
    assert len(out) <= HEADLINE_MAX_CHARS
    assert out.split()[-1] in hook.split(), "последнее слово обрезано на середине"


def test_fallback_uses_first_clause():
    """Хук с запятой режется по смысловой границе, а не по счётчику букв."""
    hook = "На острове нашли тела пропавших, чьи дела полиция закрыла заранее"
    assert _headline_from_hook(hook) == "На острове нашли тела пропавших"


def test_empty_hook_gives_placeholder():
    assert _headline_from_hook("") == "Без заголовка"


def test_headline_defaults_to_empty_for_old_drafts():
    """Поле необязательное: код, создающий черновик без заголовка, работает."""
    from src.rewrite import PostDraft

    d = PostDraft(
        id="x", source_item_hash="h", source_url="u", source_name="s",
        category="", format="A", hook="Хук", body="", detail_hook="",
        source_line="", full_text="Хук",
    )
    assert d.headline == ""


# --- гейт для заголовка (он печатается на картинке и уходит в канал) ---


@pytest.mark.parametrize(
    "headline",
    ["Убийца пойман", "Маньяк задержан", "Педофил работал в школе"],
)
def test_headline_gate_blocks_accusations(headline):
    """Текст на карточке — тоже публикация. check_post его не видит,
    поэтому есть отдельная проверка."""
    assert legal_gate.check_headline(headline).hard_fail is True


@pytest.mark.parametrize(
    "headline",
    [
        "Дверь палаты была заперта",
        "Попугаи вылупились при досмотре",
        "Оправданный по Локерби умер в Ливии",
        "Суд признал Кариньяна виновным",
    ],
)
def test_headline_gate_passes_clean(headline):
    assert legal_gate.check_headline(headline).hard_fail is False


def test_headline_gate_flags_minor():
    r = legal_gate.check_headline("Пропала Аня Кузнецова, ученица 6 класса")
    assert r.manual_only is True


def test_headline_gate_survives_empty():
    assert legal_gate.check_headline("").hard_fail is False


# ---------------------------------------------------------------------------
# 2. Ссылка на источник
# ---------------------------------------------------------------------------


def test_source_line_becomes_link():
    html, linked = build_html_with_source_link(
        "Текст поста.\n\nПо данным BBC News.", "https://bbc.co.uk/news/articles/x1"
    )
    assert linked is True
    assert '<a href="https://bbc.co.uk/news/articles/x1">BBC News</a>' in html
    assert html.endswith("</a>.")


def test_link_points_to_article_not_homepage():
    """Ссылка ведёт на конкретный материал — именно тот URL, что в базе."""
    url = "https://www.wctrib.com/news/the-vault/she-survived-harvey-carignans-hammer"
    html, _ = build_html_with_source_link("Т.\n\nПо данным издания InForum.", url)
    assert url in html
    assert html.count("href") == 1


def test_html_special_chars_are_escaped():
    """Иначе Telegram отклонит сообщение с ошибкой разбора разметки."""
    html, _ = build_html_with_source_link(
        "Компания «А&Б» подала иск <в суд>.\n\nПо данным BBC.", "https://b.co/1"
    )
    assert "&amp;" in html and "&lt;в суд&gt;" in html
    assert "<в суд>" not in html


def test_no_link_when_url_missing():
    html, linked = build_html_with_source_link("Т.\n\nПо данным BBC.", "")
    assert linked is False
    assert "<a" not in html


def test_no_link_for_non_http_url():
    """Схемы вроде javascript: не должны попадать в href."""
    _, linked = build_html_with_source_link(
        "Т.\n\nПо данным BBC.", "javascript:alert(1)"
    )
    assert linked is False


def test_no_link_when_source_line_absent():
    html, linked = build_html_with_source_link("Просто текст.", "https://b.co/1")
    assert linked is False
    assert html == "Просто текст."


def test_link_uses_last_source_line():
    """Если «По данным» встречается дважды, ссылкой становится последняя —
    она и есть строка источника поста."""
    text = "По данным следствия, дело закрыто.\n\nТекст.\n\nПо данным BBC News."
    html, _ = build_html_with_source_link(text, "https://b.co/1")
    assert html.count("<a href") == 1
    assert "BBC News</a>" in html


def test_publish_passes_source_url(db, images_dir, monkeypatch):
    """URL из базы должен доходить до отправки — иначе ссылки не будет."""
    seed_post("p1", url="https://elpais.com/mexico/2026/story")
    captured = {}

    def fake(cfg, text, image_path=None, source_url=""):
        captured["url"] = source_url
        return True, "ok"

    monkeypatch.setattr(publisher, "send_live_text", fake)
    approval_bot._publish_post(make_cfg(), "p1")

    assert captured["url"] == "https://elpais.com/mexico/2026/story"


# ---------------------------------------------------------------------------
# 3. Сводка расходов (/budget)
# ---------------------------------------------------------------------------


def test_cost_summary_on_empty_db(db):
    s = storage.get_cost_summary()
    assert s["today"] == 0 and s["month"] == 0 and s["all_time"] == 0
    assert s["by_stage"] == {}


def test_cost_summary_aggregates(db):
    storage.log_cost("relevance", "gemini", "flash-lite", 0.02)
    storage.log_cost("rewrite", "gemini", "flash", 0.01)
    storage.log_cost("rewrite", "gemini", "flash", 0.03)

    s = storage.get_cost_summary()

    assert round(s["today"], 4) == 0.06
    assert round(s["all_time"], 4) == 0.06
    assert s["calls_today"] == 3
    assert s["by_stage"]["rewrite"] == (0.04, 2)
    assert set(s["models"]) == {"flash-lite", "flash"}


def test_budget_command_reports_numbers(db, images_dir):
    storage.log_cost("rewrite", "gemini", "flash", 0.5)
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = FakeMessage(text="/budget")

    asyncio.run(handlers_of(dp)["msg_cmd_budget"](msg))

    out = msg.answers[0]
    assert "$0.5000" in out
    assert "Дневной лимит" in out
    assert "недоступен" in out, "надо честно сказать, что баланс Google не отдаёт"


def test_budget_command_on_empty_db(db, images_dir):
    """Пустая база не должна ронять команду делением на ноль."""
    dp = approval_bot.build_dispatcher(make_cfg())
    msg = FakeMessage(text="/budget")

    asyncio.run(handlers_of(dp)["msg_cmd_budget"](msg))

    assert msg.answers


# ---------------------------------------------------------------------------
# Рубрика на карточке — категория дела, а не тип источника
# ---------------------------------------------------------------------------


def _item(category: str, relevance: dict):
    from src.normalize import NewsItem

    it = NewsItem(
        url_hash="h", source="BBC", title="t", body="b", url="https://b/1",
        published_at="", fetched_at="", category=category,
    )
    it.relevance = relevance
    return it


@pytest.mark.parametrize("ingest", ["rss", "gdelt", "RSS", "sample", ""])
def test_ingest_marker_is_not_a_rubric(ingest):
    """В поле category у новости лежит ТИП ИСТОЧНИКА — на карточке в рубрике
    из-за этого печаталось «RSS» вместо категории дела."""
    from src.rewrite import _pick_category

    assert _pick_category(_item(ingest, {"category": "курьёз"})) == "курьёз"


def test_real_category_from_source_is_kept():
    from src.rewrite import _pick_category

    assert _pick_category(_item("громкое дело", {})) == "громкое дело"


def test_no_category_gives_empty_rubric():
    from src.rewrite import _pick_category

    assert _pick_category(_item("rss", {})) == ""
