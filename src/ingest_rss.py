"""RSS ingest (BBC, ТАСС, Интерфакс и др. — RU+EN).

Один упавший фид не должен ронять остальные, но каждый сбой логируется явно.
"""
from __future__ import annotations

import re
import time
from typing import Any, Dict, List

try:
    import feedparser
except ImportError:
    feedparser = None  # type: ignore

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    if not text:
        return ""
    text = _TAG_RE.sub(" ", text)
    text = _WS_RE.sub(" ", text)
    return text.strip()


# Часть государственных лент (fbi.gov, sledcom.ru, dea.gov) отвечает 403 на
# клиент без браузерных заголовков. Тем, кто отдаёт ленту, это не мешает, а
# у остальных снимает лишний отказ. Заголовки честные: имя бота и ссылка на
# канал остаются в строке.
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; NewsBot/1.0)"
    ),
    "Accept": "application/rss+xml, application/atom+xml, application/xml;q=0.9, */*;q=0.7",
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}


def fetch_feed(
    url: str,
    source_label: str | None = None,
    timeout: int = 15,
    retries: int = 0,
    source_kind: str = "",
    source_role: str = "",
) -> List[Dict[str, Any]]:
    """Забирает один RSS-фид. Никогда не бросает исключение наружу — логирует
    и возвращает [] при сбое (сетевом или парсинга), чтобы не убить весь батч.

    `retries` — сколько РАЗ повторить при сбое, а не бесконечно: одна лишняя
    попытка спасает от разовой сетевой икоты, десять превратят прогон в
    зависание. `source_kind`/`source_role` едут вместе с записями до самой
    публикации — по ним потом собирается строка «По данным FBI / BBC».
    """
    if feedparser is None:
        print("[ingest_rss] feedparser не установлен (pip install -r requirements.txt)")
        return []

    attempt = 0
    while True:
        items = _fetch_once(url, source_label, timeout, source_kind, source_role)
        if items or attempt >= max(retries, 0):
            return items
        attempt += 1
        print(f"[ingest_rss] {source_label or url}: пусто, повтор {attempt}/{retries}")
        time.sleep(1.0)


def _fetch_once(
    url: str,
    source_label: str | None,
    timeout: int,
    source_kind: str = "",
    source_role: str = "",
) -> List[Dict[str, Any]]:
    try:
        # Параметр timeout был объявлен в сигнатуре и НИКОГДА не применялся:
        # feedparser берёт socket.getdefaulttimeout() = None, и зависший
        # фид вешал прогон бесконечно, накладывая следующий запуск cron.
        import socket
        _prev = socket.getdefaulttimeout()
        socket.setdefaulttimeout(timeout)
        try:
            parsed = feedparser.parse(url, request_headers=dict(_HEADERS))
        finally:
            socket.setdefaulttimeout(_prev)
    except Exception as exc:
        print(f"[ingest_rss] {url} failed: {exc}")
        return []

    if getattr(parsed, "bozo", 0) and not getattr(parsed, "entries", None):
        print(f"[ingest_rss] {url}: malformed feed, 0 entries ({getattr(parsed, 'bozo_exception', '')})")

    label = source_label or (parsed.feed.get("title") if hasattr(parsed, "feed") else url) or url
    items: List[Dict[str, Any]] = []
    for entry in getattr(parsed, "entries", []) or []:
        try:
            link = entry.get("link") or ""
            if not link:
                continue
            title = _strip_html(entry.get("title") or "")
            summary = _strip_html(entry.get("summary") or entry.get("description") or "")[:800]
            published = entry.get("published") or entry.get("updated") or ""
            items.append(
                {
                    "source": str(label),
                    "title": title,
                    "summary": summary,
                    "url": link,
                    "published_at": published,
                    "lang": "ru" if any(ord(ch) > 1024 for ch in title) else "en",
                    "category": "rss",
                    "source_kind": source_kind,
                    "source_role": source_role,
                }
            )
        except Exception as exc:
            print(f"[ingest_rss] entry parse error in {url}: {exc}")
            continue
    return items


def fetch_sources(sources, timeout: int = 20, retries: int = 1,
                  polite_delay: float = 0.2) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Забирает ленты по реестру `src/sources.py`.

    Возвращает (записи, отчёт по источникам). Отчёт — не украшение: без него
    не видно, какой источник замолчал. Падение любой ленты не мешает
    остальным: ошибка уже поглощена внутри fetch_feed, здесь она попадает
    в отчёт со статусом.
    """
    out: List[Dict[str, Any]] = []
    report: List[Dict[str, Any]] = []
    for src in sources:
        t0 = time.time()
        items = fetch_feed(
            src.url, source_label=src.name, timeout=timeout, retries=retries,
            source_kind=src.kind, source_role=src.role,
        )
        report.append({
            "name": src.name, "role": src.role, "kind": src.kind,
            "count": len(items), "seconds": round(time.time() - t0, 1),
            "status": "ok" if items else "ПУСТО/СБОЙ",
        })
        out.extend(items)
        time.sleep(polite_delay)

    alive = [r for r in report if r["count"] > 0]
    if sources and not alive:
        raise RuntimeError(
            f"[ingest_rss] ВСЕ {len(sources)} источников вернули 0 записей — "
            f"проверь сеть прежде чем считать это 'нет новостей'."
        )
    return out, report


def fetch_all(feeds: List[str], polite_delay: float = 0.2) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    ok_count = 0
    for feed in feeds:
        items = fetch_feed(feed)
        if items:
            ok_count += 1
        out.extend(items)
        time.sleep(polite_delay)  # be polite

    if feeds and ok_count == 0:
        raise RuntimeError(
            f"[ingest_rss] ВСЕ {len(feeds)} RSS-источников вернули 0 записей — "
            f"проверь сеть/URL прежде чем считать это 'нет новостей'."
        )
    return out
