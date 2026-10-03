"""Карточка поста: только своё, никаких чужих фото/AI-фотореализма.

Тёмный фон #0d0f12, акцент #b3392e, золото #c9a557 — стиль карточки.
Рубрика + заголовок + плашка
источника. Шрифты — системные fallback, PT Serif если найдётся на диске.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from PIL import Image, ImageDraw, ImageFont

from config import IMAGES_DIR

BG_COLOR = "#0d0f12"
ACCENT_COLOR = "#b3392e"
GOLD_COLOR = "#c9a557"
TEXT_COLOR = "#f2f0ea"
MUTED_COLOR = "#8a8f98"

CARD_SIZE = (1080, 1080)  # квадрат — универсален для Telegram/Instagram-repost

# Где искать шрифты. Порядок: PT Serif/PT Mono и системные шрифты Windows,
# затем системные засечные/моноширинные Linux.
#
# Список был только из путей Windows, а конвейер переехал на сервер: там ни
# одного из них нет, PIL молча брал встроенный растровый шрифт, и первая же
# карточка, нарисованная на сервере, ушла бы в канал испорченной. На
# Ubuntu стоит DejaVu — засечный и моноширинный, ближе всего к фирменной паре.
_FONT_CANDIDATES_SERIF = [
    "C:/Windows/Fonts/ptserif.ttf",
    "C:/Windows/Fonts/PTSerif-Regular.ttf",
    "C:/Windows/Fonts/georgia.ttf",  # fallback с засечками
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Regular.ttf",
]
_FONT_CANDIDATES_SERIF_BOLD = [
    "C:/Windows/Fonts/ptserif-bold.ttf",
    "C:/Windows/Fonts/PTSerif-Bold.ttf",
    "C:/Windows/Fonts/georgiab.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSerif-Bold.ttf",
]
_FONT_CANDIDATES_MONO = [
    "C:/Windows/Fonts/ptmono.ttf",
    "C:/Windows/Fonts/PTMono-Regular.ttf",
    "C:/Windows/Fonts/consola.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationMono-Regular.ttf",
]


# Проверяем ВЕСЬ русский алфавит в обоих регистрах. Одной буквы мало —
# шрифт с частичной кириллицей прошёл бы случайно; выборки из двух десятков
# букв тоже мало по той же причине. Полный алфавит стоит лишние миллисекунды
# и снимает вопрос совсем.
_CYRILLIC_PROBES = (
    "АБВГДЕЁЖЗИЙКЛМНОПРСТУФХЦЧШЩЪЫЬЭЮЯ"
    "абвгдеёжзийклмнопрстуфхцчшщъыьэюя"
)

# Эталоны «этого глифа в шрифте нет». Два, а не один: символ из области
# частного использования кое-где определён (иконочные и корпоративные
# шрифты), поэтому рядом стоит заведомо неназначенная кодовая позиция.
# Буква считается нарисованной, только если отличается от ОБОИХ эталонов
# и при этом вообще оставила краску.
_MISSING_PROBES = ("", "􏿽")


def _render_probe(font: ImageFont.FreeTypeFont, ch: str, box: int) -> bytes:
    img = Image.new("L", (box, box), 0)
    ImageDraw.Draw(img).text((0, 0), ch, font=font, fill=255)
    return img.tobytes()


def _renders_cyrillic(font: ImageFont.FreeTypeFont, size: int) -> bool:
    """Умеет ли шрифт РИСОВАТЬ кириллицу, а не просто существовать на диске.

    Проверять наличие глифа через getbbox бесполезно: у «квадрата
    отсутствующего символа» габариты такие же, как у буквы. Поэтому рисуем
    заведомо отсутствующий символ и сравниваем с ним каждую пробу: совпали
    попиксельно — значит вместо буквы нарисовался тот же .notdef.

    Ровно это и случилось на сервере: `load_default()` в Pillow 12 — не
    растровая заглушка, а полноценный FreeType-шрифт БЕЗ кириллицы, и
    карточка ушла владельцу набором пустых квадратов.
    """
    box = max(size * 2, 16)
    try:
        blank = _render_probe(font, " ", box)
        missing = [_render_probe(font, ch, box) for ch in _MISSING_PROBES]
        for ch in _CYRILLIC_PROBES:
            drawn = _render_probe(font, ch, box)
            if drawn in missing:
                return False          # вместо буквы тот же .notdef
            if drawn == blank:
                return False          # буква не оставила краски вообще
    except Exception as exc:  # битый файл шрифта — считаем непригодным
        print(f"[image_card] шрифт не прошёл проверку кириллицы: {exc}")
        return False
    return True


def _load_font(candidates: list[str], size: int) -> ImageFont.FreeTypeFont:
    """Первый шрифт из списка, который РЕАЛЬНО рисует кириллицу.

    Раньше выбор шёл по факту существования файла, а при полном промахе
    брался `load_default()` — и это считалось безопасным запасным вариантом.
    Не считается: на боевом сервере все пути были только для Windows, и
    первая же нарисованная там карточка ушла владельцу пустыми квадратами.
    """
    tried: list[str] = []
    for path in candidates:
        if not Path(path).exists():
            continue
        tried.append(path)
        try:
            font = ImageFont.truetype(path, size)
        except Exception as exc:
            print(f"[image_card] {path} не открылся: {exc}")
            continue
        if _renders_cyrillic(font, size):
            return font
        print(f"[image_card] {path} найден, но не рисует кириллицу — пропускаю")

    # Тихо подставлять шрифт без кириллицы нельзя: битая карточка уходит в
    # канал и выглядит как поломка бота. Падаем громко — вызывающий код
    # (конвейер и кнопка перегенерации) это ловит, и пост уходит текстом
    # без картинки с честной пометкой.
    raise RuntimeError(
        "[image_card] не найден ни один шрифт с кириллицей. "
        f"Проверены пути: {candidates}. Найдены, но непригодны: {tried or 'ни одного'}. "
        "Установите шрифты (например, пакет fonts-dejavu-core) или добавьте путь "
        "в _FONT_CANDIDATES_* в src/image_card.py."
    )


def _wrap_text(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, max_width: int) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        trial = f"{current} {word}".strip()
        bbox = draw.textbbox((0, 0), trial, font=font)
        if bbox[2] - bbox[0] <= max_width or not current:
            current = trial
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def render_card(
    title: str,
    category: str,
    source_name: str,
    output_path: Optional[Path] = None,
    draft_id: str = "draft",
) -> Path:
    """Рендерит карточку поста в PNG, возвращает путь к файлу."""
    img = Image.new("RGB", CARD_SIZE, color=BG_COLOR)
    draw = ImageDraw.Draw(img)

    margin = 80
    content_width = CARD_SIZE[0] - margin * 2

    font_rubric = _load_font(_FONT_CANDIDATES_MONO, 32)
    font_title = _load_font(_FONT_CANDIDATES_SERIF_BOLD, 56)
    font_source = _load_font(_FONT_CANDIDATES_MONO, 26)

    # Верхняя полоса-акцент
    draw.rectangle([(0, 0), (CARD_SIZE[0], 10)], fill=ACCENT_COLOR)

    # Рубрика (золото, капс)
    rubric_text = (category or "КРИМИНАЛЬНЫЕ НОВОСТИ").upper()
    draw.text((margin, margin), rubric_text, font=font_rubric, fill=GOLD_COLOR)

    # Заголовок (перенос строк вручную под ширину карточки)
    lines = _wrap_text(draw, title, font_title, content_width)
    y = margin + 90
    line_height = 68
    max_lines = 8
    for line in lines[:max_lines]:
        draw.text((margin, y), line, font=font_title, fill=TEXT_COLOR)
        y += line_height
    if len(lines) > max_lines:
        draw.text((margin, y), "…", font=font_title, fill=TEXT_COLOR)

    # Нижняя плашка источника
    plate_h = 90
    plate_y0 = CARD_SIZE[1] - plate_h
    draw.rectangle([(0, plate_y0), (CARD_SIZE[0], CARD_SIZE[1])], fill="#15181d")
    draw.rectangle([(0, plate_y0), (8, CARD_SIZE[1])], fill=ACCENT_COLOR)
    source_text = f"Источник: {source_name}" if source_name else "Источник не указан"
    draw.text((margin, plate_y0 + 30), source_text, font=font_source, fill=MUTED_COLOR)

    output_path = output_path or (IMAGES_DIR / f"{draft_id}.png")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, format="PNG")
    return output_path
