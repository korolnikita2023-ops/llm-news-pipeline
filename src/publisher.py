"""Публикация — Bot API sendPhoto+caption.

DRY-RUN режим по умолчанию: пост пишется markdown-файлом в data/drafts/,
в Telegram НЕ отправляется. Реальная отправка включается ТОЛЬКО явным
PUBLISH_MODE=live в .env — см. .env.example.

NO SILENT FAILURES: если PUBLISH_MODE=live, но TELEGRAM_BOT_TOKEN/CHANNEL_ID
не заданы — падаем громко (SystemExit), а не тихо скатываемся в draft-режим.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from config import Config, DRAFTS_DIR
from src.rewrite import PostDraft

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore

# Лимиты Telegram Bot API. Держим их здесь одной константой, чтобы бот и
# публикация считали длину одинаково и не расходились.
TELEGRAM_CAPTION_LIMIT = 1024
TELEGRAM_TEXT_LIMIT = 4096


def _draft_markdown_path(draft: PostDraft) -> Path:
    date = datetime.now(timezone.utc).date().isoformat()
    return DRAFTS_DIR / f"{date}_{draft.id}.md"


def write_draft_markdown(draft: PostDraft, image_path: Optional[Path] = None) -> Path:
    DRAFTS_DIR.mkdir(parents=True, exist_ok=True)
    path = _draft_markdown_path(draft)

    lines = [
        f"# Черновик поста `{draft.id}` (формат {draft.format})",
        "",
        f"- Статус: **{draft.status}**",
        f"- Категория: {draft.category or '—'}",
        f"- Источник: {draft.source_name} — {draft.source_url or 'НЕТ URL (не должно пройти гейт)'}",
        f"- Manual-only: {'да' if draft.manual_only else 'нет'}",
        f"- Editorial scores: {draft.scores}",
        f"- Картинка: {image_path if image_path else 'не сгенерирована'}",
        "",
        "---",
        "",
        draft.full_text,
        "",
        "---",
        f"_Сгенерировано {datetime.now(timezone.utc).isoformat(timespec='seconds')}. "
        f"DRY-RUN: в Telegram не отправлено, требуется approve владельца._",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")
    return path


def send_live(cfg: Config, draft: PostDraft, image_path: Optional[Path] = None) -> tuple[bool, str]:
    """Реальная отправка в канал через Bot API. Вызывается ТОЛЬКО из
    publish_or_draft, когда PUBLISH_MODE=live и есть токен+channel_id.
    """
    return send_live_text(cfg, draft.full_text, image_path=image_path)


def send_live_text(
    cfg: Config,
    text: str,
    image_path: Optional[Path] = None,
    source_url: str = "",
) -> tuple[bool, str]:
    """Отправка произвольного готового текста поста (используется и пайплайном
    через send_live, и approve-ботом при одобрении кнопкой)."""
    if httpx is None:
        return False, "httpx не установлен"

    if not cfg.telegram_bot_token or not cfg.telegram_channel_id:
        raise SystemExit(
            "[publisher] PUBLISH_MODE=live, но TELEGRAM_BOT_TOKEN/TELEGRAM_CHANNEL_ID "
            "не заданы в .env — публикация невозможна, останавливаюсь (не тихий fallback)."
        )

    # Лимиты Telegram Bot API (проверено по документации 27.08.2026):
    # caption у sendPhoto — 0–1024 символа, text у sendMessage — 1–4096.
    #
    # Раньше здесь стояло `caption = text[:1024]` — пост МОЛЧА обрезался.
    # Для формата B (до 1500 знаков) это отрезало концовку вместе со строкой
    # источника («По данным ...»), а наличие источника — жёсткое требование
    # юридического гейта. Молча публиковать обрезанный пост без источника
    # недопустимо, поэтому: влезает в подпись — шлём фото с подписью; не
    # влезает — шлём фото и следом полный текст отдельным сообщением;
    # не влезает даже в 4096 — громкий отказ, а не обрезка.
    text = (text or "").strip()
    if not text:
        return False, "пустой текст поста — публиковать нечего"
    if len(text) > TELEGRAM_TEXT_LIMIT:
        return False, (
            f"текст поста {len(text)} символов при лимите Telegram "
            f"{TELEGRAM_TEXT_LIMIT} — публикация остановлена, чтобы не "
            f"обрезать пост и не потерять ссылку на источник"
        )

    # Ссылка на исходный материал в строке «По данным ...». Лимиты считаем
    # по ВИДИМОМУ тексту (Telegram считает длину после разбора разметки),
    # поэтому проверки выше остаются верными.
    payload, linked = build_html_with_source_link(text, source_url)
    parse_mode = "HTML" if linked else None
    if not linked:
        payload = text

    has_image = bool(image_path and Path(image_path).exists())
    base = f"https://api.telegram.org/bot{cfg.telegram_bot_token}"
    try:
        if has_image and len(text) <= TELEGRAM_CAPTION_LIMIT:
            with open(image_path, "rb") as f:
                resp = httpx.post(
                    f"{base}/sendPhoto",
                    data={
                        "chat_id": cfg.telegram_channel_id,
                        "caption": payload,
                        **({"parse_mode": parse_mode} if parse_mode else {}),
                    },
                    files={"photo": f},
                    timeout=30,
                )
            resp.raise_for_status()
            return True, "sent"

        if has_image:
            # Длинный пост: карточка без подписи, затем полный текст.
            with open(image_path, "rb") as f:
                resp = httpx.post(
                    f"{base}/sendPhoto",
                    data={"chat_id": cfg.telegram_channel_id},
                    files={"photo": f},
                    timeout=30,
                )
            resp.raise_for_status()

        resp = httpx.post(
            f"{base}/sendMessage",
            data={
                "chat_id": cfg.telegram_channel_id,
                "text": payload,
                **({"parse_mode": parse_mode} if parse_mode else {}),
            },
            timeout=30,
        )
        resp.raise_for_status()
        return True, "sent (фото + текст отдельным сообщением)" if has_image else "sent"
    except Exception as exc:
        # Токен бота стоит прямо в URL, а httpx включает URL в текст ошибки.
        # Эта строка уходит и в лог, и в чат владельца — то есть токен
        # сохранялся бы на серверах Telegram навсегда. Вырезаем.
        return False, f"Telegram send error: {_redact(str(exc), cfg.telegram_bot_token)}"


# ---------------------------------------------------------------------------
# Кликабельная ссылка на источник.
#
# Пост заканчивается строкой «По данным BBC News.» — читатель должен иметь
# возможность открыть исходный материал одним касанием. Название издания в
# тексте пишет модель («BBC News», «издания InForum», «El País»), и оно не
# совпадает с полем source в базе («wctrib.com»), поэтому привязываемся не
# к имени, а к последней строке вида «По данным ...».
#
# Разметка — HTML, а не Markdown: в HTML экранировать нужно ровно три символа
# (&, <, >), тогда как Markdown ломается о _ * [ ] ` ~ | из текста новости, и
# один такой символ в имени или заголовке приводил бы к отказу Telegram.
# ---------------------------------------------------------------------------

_SOURCE_LINE_RE = re.compile(r"^(По данным\s+)(.+?)(\.?)$", re.IGNORECASE)


def _html_escape(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def build_html_with_source_link(text: str, source_url: str) -> tuple[str, bool]:
    """Возвращает (html, ссылка_вставлена).

    Если строку источника найти не удалось или URL пустой/некорректный —
    возвращаем экранированный текст без ссылки. Публикация важнее ссылки:
    молча ломать пост из-за неудачной разметки нельзя.
    """
    escaped = _html_escape(text or "")
    url = (source_url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return escaped, False

    lines = escaped.split("\n")
    for i in range(len(lines) - 1, -1, -1):
        m = _SOURCE_LINE_RE.match(lines[i].strip())
        if not m:
            continue
        prefix, name, dot = m.groups()
        href = _html_escape(url).replace('"', "&quot;")
        lines[i] = f'{prefix}<a href="{href}">{name}</a>{dot}'
        return "\n".join(lines), True
    return escaped, False


def _redact(text: str, token: str) -> str:
    """Убирает токен бота из любого текста, который может попасть в лог
    или в сообщение. Работает и по полному токену, и по шаблону в URL."""
    out = text
    if token:
        out = out.replace(token, "***ТОКЕН-СКРЫТ***")
    return re.sub(r"/bot\d+:[A-Za-z0-9_\-]+", "/bot***ТОКЕН-СКРЫТ***", out)


def publish_or_draft(cfg: Config, draft: PostDraft, image_path: Optional[Path] = None) -> tuple[Path, Optional[str]]:
    """Главная точка входа для этого модуля. Всегда пишет markdown-черновик
    (для истории/аудита), и ДОПОЛНИТЕЛЬНО отправляет в канал, если
    PUBLISH_MODE=live.

    Возвращает (путь_к_markdown, telegram_status_или_None).
    """
    md_path = write_draft_markdown(draft, image_path=image_path)

    if not cfg.is_live_publish():
        return md_path, None

    sent, status = send_live(cfg, draft, image_path=image_path)
    if not sent:
        # Публикация не удалась — громкий лог, черновик уже сохранён на диске.
        print(f"[publisher] ОШИБКА публикации live: {status}")
    return md_path, status
