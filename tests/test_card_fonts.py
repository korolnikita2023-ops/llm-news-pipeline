"""Карточка поста: кириллица должна рисоваться буквами, а не квадратами (S61–S66).

Живой случай 28.08.2026, 00:00:55. Владелец нажал «Следующий пост» → «🔁
Перегенерить», и бот прислал карточку, где вместо русского текста стояли
пустые квадраты.

Причина: шрифт выбирался по факту существования файла. На боевом сервере
все пути в списках были только для Windows, ни один не существовал, и
срабатывал `ImageFont.load_default()`. В Pillow 12 это не растровая
заглушка, а полноценный FreeType-шрифт — но БЕЗ кириллицы, поэтому каждая
буква превращалась в `.notdef`.

Прежние карточки выглядели нормально только потому, что их рисовали на
машине разработчика под Windows и копировали на сервер вместе с базой: за
всё время сервер не отрисовал ни одной карточки сам.
"""
from __future__ import annotations

import pytest
from PIL import Image, ImageFont

from src import image_card

# Реальный заголовок, на котором дефект и проявился.
BROKEN_HEADLINE = "Чиновника задержали из-за парома"


def _ink(path) -> int:
    """Сколько пикселей на карточке не совпадают с фоном — то есть нарисовано."""
    img = Image.open(path).convert("L")
    background = img.getpixel((5, img.height // 2))
    # get_flattened_data вместо getdata: последний объявлен устаревшим.
    return sum(1 for px in img.get_flattened_data() if abs(px - background) > 30)


# ---------------------------------------------------------------------------
# S61. Проверка пригодности шрифта
# ---------------------------------------------------------------------------


def test_pillow_default_font_has_no_cyrillic():
    """Тот самый ложный запасной вариант. Комментарий в коде утверждал, что
    «на Pillow>=10 кириллица работает» — это неверно, и на этом веровании
    дефект прожил незамеченным."""
    assert image_card._renders_cyrillic(ImageFont.load_default(56), 56) is False


def test_real_font_passes_the_probe():
    font = image_card._load_font(image_card._FONT_CANDIDATES_SERIF_BOLD, 56)
    assert image_card._renders_cyrillic(font, 56) is True


def test_probe_covers_many_glyphs_not_one():
    """Одной буквы мало: шрифт с частичной кириллицей прошёл бы случайно."""
    assert len(set(image_card._CYRILLIC_PROBES)) >= 12
    assert "Ё" in image_card._CYRILLIC_PROBES
    assert "щ" in image_card._CYRILLIC_PROBES


def test_font_without_cyrillic_is_skipped(monkeypatch):
    """Шрифт есть на диске, открывается, но кириллицу не рисует — берём
    следующий, а не первый попавшийся."""
    calls = []
    real_truetype = ImageFont.truetype

    def fake_truetype(path, size):
        calls.append(path)
        if str(path).endswith("latin-only.ttf"):
            return ImageFont.load_default(size)     # заведомо без кириллицы
        return real_truetype(path, size)

    monkeypatch.setattr(image_card.ImageFont, "truetype", fake_truetype)
    monkeypatch.setattr(image_card.Path, "exists", lambda self: True)

    good = [p for p in image_card._FONT_CANDIDATES_SERIF_BOLD if "dejavu" in p or "Windows" in p]
    font = image_card._load_font(["/fake/latin-only.ttf", *good], 40)

    assert calls[0] == "/fake/latin-only.ttf", "первый кандидат должен быть опробован"
    assert image_card._renders_cyrillic(font, 40), "выбран шрифт без кириллицы"


# ---------------------------------------------------------------------------
# S62. Отсутствие пригодного шрифта — громкий отказ, а не битая картинка
# ---------------------------------------------------------------------------


def test_no_usable_font_raises_instead_of_drawing_boxes():
    with pytest.raises(RuntimeError, match="кириллиц"):
        image_card._load_font([], 56)


def test_error_message_says_what_to_install():
    try:
        image_card._load_font(["/nope/one.ttf"], 56)
    except RuntimeError as exc:
        assert "fonts-dejavu" in str(exc), "сообщение должно подсказывать решение"
    else:
        pytest.fail("ожидали RuntimeError")


# ---------------------------------------------------------------------------
# S63. Карточка с русским заголовком действительно рисуется
# ---------------------------------------------------------------------------


def test_cyrillic_headline_is_actually_drawn(tmp_path, monkeypatch):
    monkeypatch.setattr(image_card, "IMAGES_DIR", tmp_path)

    with_text = image_card.render_card(
        title=BROKEN_HEADLINE, category="громкое дело",
        source_name="ТАСС", draft_id="withtext",
    )
    empty = image_card.render_card(
        title="", category="громкое дело", source_name="ТАСС", draft_id="empty",
    )

    ink_text, ink_empty = _ink(with_text), _ink(empty)
    assert ink_text > ink_empty * 1.5, (
        f"русский заголовок почти не добавил краски: {ink_text} против {ink_empty}"
    )


def test_broken_font_would_be_caught(tmp_path, monkeypatch):
    """Контрольная проверка самого теста: со шрифтом без кириллицы карточка
    не должна собираться вообще."""
    monkeypatch.setattr(image_card, "IMAGES_DIR", tmp_path)
    monkeypatch.setattr(image_card, "_FONT_CANDIDATES_SERIF_BOLD", [])
    monkeypatch.setattr(image_card, "_FONT_CANDIDATES_SERIF", [])
    monkeypatch.setattr(image_card, "_FONT_CANDIDATES_MONO", [])

    with pytest.raises(RuntimeError):
        image_card.render_card(
            title=BROKEN_HEADLINE, category="громкое дело",
            source_name="ТАСС", draft_id="broken",
        )


# ---------------------------------------------------------------------------
# S64. Тот самый сценарий: следующий пост → перегенерировать → карточка
# ---------------------------------------------------------------------------


def test_regenerate_flow_writes_readable_card(db, images_dir, monkeypatch):
    """Живой сценарий владельца целиком. Проверяем, что после перегенерации
    карточка для ИСХОДНОГО post_id перерисована и на ней есть краска."""
    import asyncio

    from conftest import FakeCallback, handlers_of, seed_post
    from config import Config
    from src import approval_bot, rewrite

    seed_post("p1")

    def fake_draft(cfg, item, attempt=0, case_status=None):
        return rewrite.PostDraft(
            id="newdraft", source_item_hash=item.url_hash, source_url=item.url,
            source_name="ТАСС", category="громкое дело", format="A",
            hook="Чиновника задержали после крушения парома.",
            body="Под стражу взят глава ведомства, которому принадлежало судно.",
            detail_hook="Судно числилось на балансе этой структуры.",
            source_line="По данным ТАСС.",
            full_text="Чиновника задержали после крушения парома.\n\n"
                      "Под стражу взят глава ведомства, которому принадлежало судно.\n\n"
                      "Судно числилось на балансе этой структуры.\n\nПо данным ТАСС.",
            headline=BROKEN_HEADLINE,
        )

    class OkScore:
        decision = "approve"
        average = 9.0

        def to_dict(self):
            return {"average": 9.0, "decision": "approve"}

    monkeypatch.setattr("src.rewrite.generate_draft", fake_draft)
    monkeypatch.setattr("src.scorer.score_draft", lambda cfg, draft: OkScore())
    monkeypatch.setattr(image_card, "IMAGES_DIR", images_dir)
    monkeypatch.setattr("config.IMAGES_DIR", images_dir)

    cfg = Config()
    cfg.llm_provider = "mock"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    cfg.daily_cost_limit_usd = 3.0
    cfg.monthly_cost_limit_usd = 5.0

    dp = approval_bot.build_dispatcher(cfg)
    asyncio.run(handlers_of(dp)["regen"](FakeCallback("regen:p1")))

    card = images_dir / "p1.png"
    assert card.exists(), "после перегенерации карточка исходного поста не появилась"
    assert _ink(card) > 500, "карточка пустая — текст не нарисовался"


# ---------------------------------------------------------------------------
# S65. Сбой рендера не должен оставлять старую карточку
# ---------------------------------------------------------------------------


def test_failed_rerender_removes_stale_card(db, images_dir, monkeypatch):
    """Найдено независимым ревью. Если перерисовка упала, старый файл
    оставался на месте, и владелец получал НОВЫЙ текст со СТАРОЙ картинкой —
    с прежним заголовком или с прежними квадратами. Выглядит так, будто
    перегенерация не сработала."""
    import asyncio

    from conftest import FakeCallback, handlers_of, seed_post
    from config import Config
    from src import approval_bot, rewrite

    seed_post("p1")
    stale = images_dir / "p1.png"
    stale.write_bytes(b"\x89PNG\r\n\x1a\n" + b"0" * 200)   # «старая» карточка

    def fake_draft(cfg, item, attempt=0, case_status=None):
        return rewrite.PostDraft(
            id="newdraft", source_item_hash=item.url_hash, source_url=item.url,
            source_name="ТАСС", category="громкое дело", format="A",
            hook="Новый текст поста.", body="Тело.", detail_hook="Деталь.",
            source_line="По данным ТАСС.",
            full_text="Новый текст поста.\n\nТело.\n\nДеталь.\n\nПо данным ТАСС.",
            headline="Новый заголовок",
        )

    class OkScore:
        decision = "approve"
        average = 9.0

        def to_dict(self):
            return {"average": 9.0, "decision": "approve"}

    def broken_render(**kwargs):
        raise RuntimeError("не найден ни один шрифт с кириллицей")

    monkeypatch.setattr("src.rewrite.generate_draft", fake_draft)
    monkeypatch.setattr("src.scorer.score_draft", lambda cfg, draft: OkScore())
    monkeypatch.setattr("src.image_card.render_card", broken_render)
    monkeypatch.setattr("config.IMAGES_DIR", images_dir)

    cfg = Config()
    cfg.llm_provider = "mock"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    cfg.daily_cost_limit_usd = 3.0
    cfg.monthly_cost_limit_usd = 5.0

    dp = approval_bot.build_dispatcher(cfg)
    asyncio.run(handlers_of(dp)["regen"](FakeCallback("regen:p1")))

    assert not stale.exists(), (
        "устаревшая карточка осталась — владелец увидит новый текст со старой картинкой"
    )


def test_post_without_card_warns_in_text(db, images_dir):
    """Без файла карточки бот обязан честно предупредить, что уйдёт текстом."""
    import asyncio

    from conftest import FakeBot, seed_post
    from config import Config
    from src import approval_bot

    seed_post("p1")
    cfg = Config()
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = "777"
    bot = FakeBot()

    asyncio.run(approval_bot.send_next_candidate(bot, cfg, chat_id=777))

    assert bot.messages, "пост должен уйти текстом"
    assert "карточка не сгенерирована" in bot.last_text()


# ---------------------------------------------------------------------------
# S66. Тесты не пишут в боевую папку карточек
# ---------------------------------------------------------------------------


def test_tests_never_write_to_production_images_dir():
    """Страховка после утечки: image_card держит собственную ссылку на
    IMAGES_DIR, поэтому подмены только в config не хватало — карточки
    тестов писались в боевой каталог."""
    import config

    assert image_card.IMAGES_DIR == config.IMAGES_DIR, (
        "подмена должна действовать в обоих модулях сразу"
    )
    assert "guard_images" in str(image_card.IMAGES_DIR) or "pytest" in str(image_card.IMAGES_DIR), (
        f"тесты смотрят в боевую папку карточек: {image_card.IMAGES_DIR}"
    )
