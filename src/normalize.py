"""Нормализация сырых новостей из разных источников (GDELT/RSS/sample) в единую
схему `NewsItem`, совпадающую с таблицей `raw_items`:
url_hash, source, title, body, published_at, fetched_at, lang.

NO SILENT FAILURES: элемент без title или без валидного url_hash отбрасывается
с явным логом, но не роняет весь батч (одна плохая запись не должна убивать
прогон — источники, особенно RSS/агрегаторы, регулярно отдают мусор).
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List


@dataclass
class NewsItem:
    url_hash: str
    source: str
    title: str
    body: str
    url: str
    published_at: str
    fetched_at: str
    lang: str = "ru"
    category: str = ""
    sample: bool = False
    # Происхождение: official (правоохранители) | media (издание) и
    # discovery (здесь нашли сюжет) | verification (здесь подтверждаем).
    # Нужно до самой публикации — из этого собирается строка источника
    # под постом, и по этому же полю префильтр даёт вес официальным лентам.
    source_kind: str = ""
    source_role: str = ""

    # Заполняется на последующих стадиях пайплайна (не часть схемы raw_items,
    # но удобно нести дальше вместе с объектом).
    relevance: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "url_hash": self.url_hash,
            "source": self.source,
            "title": self.title,
            "body": self.body,
            "url": self.url,
            "published_at": self.published_at,
            "fetched_at": self.fetched_at,
            "lang": self.lang,
            "category": self.category,
            "sample": self.sample,
            "source_kind": self.source_kind,
            "source_role": self.source_role,
        }


def _hash_url(url: str, fallback_seed: str = "") -> str:
    basis = url or fallback_seed
    return hashlib.sha1(basis.encode("utf-8", errors="ignore")).hexdigest()[:16]


# Трекинг-параметры источников (utm_*, at_* у BBC, fbclid и т.п.) в публикацию
# не идут: ссылка в посте должна быть чистой, а дедуп по URL — устойчивым к ним.
_TRACKING_PARAM_PREFIXES = ("utm_", "at_", "ns_", "cmp", "ito", "ref_")
_TRACKING_PARAMS = {"fbclid", "gclid", "yclid", "ref", "source", "ocid", "ftag"}


def clean_url(url: str) -> str:
    """Убирает трекинг-параметры из URL, сохраняя значимые query-параметры."""
    if not url or "?" not in url:
        return url
    from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode

    parts = urlsplit(url)
    kept = [
        (k, v)
        for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_PARAMS
        and not k.lower().startswith(_TRACKING_PARAM_PREFIXES)
    ]
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(kept), ""))


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_raw(raw: Dict[str, Any], default_source: str = "unknown") -> NewsItem | None:
    """Превращает один сырой dict (из sample_news.json / GDELT / RSS-парсера)
    в NewsItem. Возвращает None и печатает предупреждение, если запись негодная
    (нет заголовка) — это осознанная деградация, не тихий сбой: вызывающий код
    (normalize_batch) считает и логирует, сколько записей отброшено.
    """
    title = (raw.get("title") or "").strip()
    if not title:
        print(f"[normalize] пропуск записи без заголовка: {raw.get('url') or raw.get('id')}")
        return None

    url = clean_url((raw.get("url") or "").strip())
    body = (raw.get("summary") or raw.get("body") or "").strip()
    seed = raw.get("id") or title
    url_hash = _hash_url(url, fallback_seed=seed)

    return NewsItem(
        url_hash=url_hash,
        source=(raw.get("source") or default_source).strip(),
        title=title,
        body=body,
        url=url,
        published_at=(raw.get("published_at") or "").strip(),
        fetched_at=now_iso(),
        lang=(raw.get("lang") or "ru").strip().lower(),
        category=(raw.get("category") or "").strip(),
        sample=bool(raw.get("sample", False)),
        source_kind=(raw.get("source_kind") or "").strip(),
        source_role=(raw.get("source_role") or "").strip(),
    )


def normalize_batch(raw_items: List[Dict[str, Any]], default_source: str = "unknown") -> List[NewsItem]:
    out: List[NewsItem] = []
    dropped = 0
    for raw in raw_items:
        item = normalize_raw(raw, default_source=default_source)
        if item is None:
            dropped += 1
            continue
        out.append(item)
    if dropped:
        print(f"[normalize] отброшено записей без заголовка: {dropped}")
    return out
