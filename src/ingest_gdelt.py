"""GDELT DOC 2.0 ingest.

$0, лимит вежливости — 1 запрос/5 сек (соблюдается в fetch_all через time.sleep).

NO SILENT FAILURES для ЭТОГО модуля означает: сбой ОДНОГО запроса (один query)
логируется явно и не прерывает остальные запросы батча (источник может быть
временно недоступен) — но если ВСЕ запросы упали, вызывающий код (main.py)
обязан узнать об этом через непустой список ошибок и явно сообщить пользователю,
а не тихо продолжить с пустым списком новостей.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Tuple

from config import GDELT_DOC_ENDPOINT, GDELT_QUERIES

try:
    import httpx
except ImportError:
    httpx = None  # type: ignore


def fetch_query(query: str, max_records: int = 50, timeout: float = 15.0) -> Tuple[List[Dict[str, Any]], str | None]:
    """Один запрос к GDELT DOC 2.0 API. Возвращает (items, error).

    error is None при успехе. При ошибке items == [] и error содержит текст —
    вызывающий код решает, насколько это критично.
    """
    if httpx is None:
        return [], "httpx не установлен (pip install -r requirements.txt)"

    params = {
        "query": query,
        "mode": "artlist",
        "maxrecords": str(max_records),
        "format": "json",
        "sort": "datedesc",
    }
    try:
        resp = httpx.get(GDELT_DOC_ENDPOINT, params=params, timeout=timeout)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # сеть/парсинг — не роняем весь батч
        return [], f"GDELT query '{query}' failed: {exc}"

    articles = data.get("articles", []) if isinstance(data, dict) else []
    out: List[Dict[str, Any]] = []
    for art in articles:
        out.append(
            {
                "source": art.get("domain") or "GDELT",
                "title": art.get("title") or "",
                "summary": "",  # GDELT DOC artlist не отдаёт summary, только title+url
                "url": art.get("url") or "",
                "published_at": art.get("seendate") or "",
                "lang": "en" if (art.get("language") or "english").lower().startswith("en") else "ru",
                "category": "gdelt",
            }
        )
    return out, None


def fetch_all(queries: List[str] | None = None, max_records: int = 50, polite_delay: float = 5.0) -> List[Dict[str, Any]]:
    """Прогоняет все запросы из GDELT_QUERIES (или переданные), собирает результаты.

    Печатает явный лог по каждому запросу (успех/ошибка) — источник, который
    молчит, не должен быть незаметен.
    """
    queries = queries or GDELT_QUERIES
    out: List[Dict[str, Any]] = []
    errors: List[str] = []
    for i, q in enumerate(queries):
        items, error = fetch_query(q, max_records=max_records)
        if error:
            print(f"[ingest_gdelt] WARNING: {error}")
            errors.append(error)
        else:
            print(f"[ingest_gdelt] query='{q}': {len(items)} статей")
        out.extend(items)
        if i < len(queries) - 1:
            time.sleep(polite_delay)

    if errors and len(errors) == len(queries):
        # Все запросы упали — это не частичная деградация, а полный отказ источника.
        raise RuntimeError(
            f"[ingest_gdelt] ВСЕ {len(queries)} запросов к GDELT провалились: {errors}"
        )
    return out
