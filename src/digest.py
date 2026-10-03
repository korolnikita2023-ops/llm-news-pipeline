"""Короткий список дня — из того, что уже посчитано, без новых вызовов модели.

Смысл: до дорогой стадии владелец должен за несколько секунд понять, какая
история ему интересна. Для этого не нужен переписанный пост — нужны
заголовок источника, откуда он, оценка и за что материал вообще отобран.

Всё это к моменту сборки уже есть и оплачено:

    title, summary, source, url   — из ленты, бесплатно;
    оценка и категория            — из relevance, уже вызванной для отбора;
    сигналы отбора                — из дешёвого префильтра («висяк раскрыт»,
                                    «оплошность, случайность»), вообще без
                                    обращения к сети.

Единственный платный шаг — `translate`: ОДИН дешёвый вызов на весь список
сразу, чтобы владелец читал русские заголовки, а не английские. Порядка
$0.001 за прогон. Это не переписывание поста: ни стиля, ни драматургии,
ни крючков — только «о чём история». Дорогая стадия по-прежнему
запускается лишь для того, что владелец выбрал руками.

Перевод не обязателен: если вызов упал, список сохраняется как есть, на
языке источника. Потерять дайджест целиком из-за неудавшегося перевода
было бы хуже, чем показать его по-английски.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List

from config import LLM_TIER_CHEAP

_WS = re.compile(r"\s+")


def _short(text: str, limit: int) -> str:
    """Обрезает по границе предложения, а не по букве."""
    text = _WS.sub(" ", (text or "").strip())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    for sep in (". ", "! ", "? "):
        pos = cut.rfind(sep)
        if pos > limit // 2:
            return cut[: pos + 1].strip()
    pos = cut.rfind(" ")
    return (cut[:pos] if pos > 0 else cut).strip() + "…"


def build(scored, verdicts_by_hash: Dict[str, Any], limit: int) -> List[dict]:
    """Собирает строки дайджеста.

    `scored` — пары (item, relevance) из relevance.score_and_filter, уже
    отсортированные по убыванию оценки. `verdicts_by_hash` — вердикты
    дешёвого отбора: из них берутся сигналы, за что материал прошёл.
    """
    rows: List[dict] = []
    for item, relevance in scored[:limit]:
        verdict = verdicts_by_hash.get(item.url_hash)
        signals = ", ".join(verdict.reasons) if verdict is not None else ""
        rows.append({
            "url_hash": item.url_hash,
            "source": item.source,
            "source_url": item.url,
            "title": _short(item.title, 200),
            "summary": _short(item.body, 320),
            "score": int(getattr(relevance, "total", 0)),
            "category": getattr(relevance, "category", "") or "",
            "signals": signals,
            # Полный разбор оценки, а не только сумма: после выбора пост
            # записывается с этими же числами. Восстанавливать их делением
            # суммы на пять — значит записать в базу оценки, которых модель
            # не ставила.
            "relevance": json.dumps(relevance.to_dict(), ensure_ascii=False),
        })
    return rows


# Перевод — вспомогательная стадия, и цена ошибки здесь мала: в худшем
# случае владелец читает заголовок на языке источника. Поэтому ошибка
# вызова не роняет прогон, а лишь логируется.
TRANSLATE_STAGE = "digest_translate"


def translate(cfg, rows: List[dict]) -> List[dict]:
    """Один дешёвый вызов на ВЕСЬ список: русский заголовок и суть.

    Именно один, а не по вызову на историю: десять отдельных обращений
    стоили бы вдесятеро и упирались бы в потолок вызовов прогона, ради
    строчки, которую владелец читает секунду.

    Возвращает те же строки; у переведённых заменены `title` и `summary`,
    оригинал сохранён в `title_src`. Непереведённые остаются как были —
    молча пропускать историю из-за неудачного перевода нельзя.
    """
    if not rows:
        return rows

    from src import llm_client

    if cfg.is_mock():
        print("[digest] LLM в mock-режиме — список остаётся на языке источника")
        return rows

    payload = {
        "items": [
            {
                "id": i,
                "title": row.get("title", ""),
                "summary": (row.get("summary") or "")[:600],
                "source": row.get("source", ""),
            }
            for i, row in enumerate(rows, 1)
        ]
    }
    try:
        data = llm_client.generate_json(
            cfg,
            tier=LLM_TIER_CHEAP,
            stage=TRANSLATE_STAGE,
            system_prompt=llm_client.load_prompt("digest_ru.md"),
            user_prompt=json.dumps(payload, ensure_ascii=False),
        )
    except Exception as exc:
        # Дайджест уже собран и оплачен. Ронять его из-за перевода нельзя.
        print(f"[digest] перевод не удался ({exc}) — список остаётся на языке источника")
        return rows

    # Разбор ответа — под той же защитой, что и сам вызов. Модель может
    # вернуть валидный JSON неожиданной формы (null вместо объекта, строку
    # вместо элемента списка): уронить на этом уже собранный и оплаченный
    # дайджест было бы нелепо.
    try:
        by_id = {}
        for entry in (data or {}).get("items") or []:
            if not isinstance(entry, dict):
                continue
            try:
                by_id[int(entry.get("id"))] = entry
            except (TypeError, ValueError):
                continue

        translated = 0
        for i, row in enumerate(rows, 1):
            entry = by_id.get(i)
            if not entry:
                continue
            headline = _WS.sub(" ", str(entry.get("headline") or "").strip())
            summary = _WS.sub(" ", str(entry.get("summary") or "").strip())
            if not headline:
                continue
            row["title_src"] = row.get("title", "")
            row["title"] = headline[:200]
            if summary:
                row["summary"] = summary[:320]
            translated += 1
    except Exception as exc:
        print(f"[digest] ответ перевода не разобран ({exc}) — "
              f"список остаётся на языке источника")
        return rows

    missing = len(rows) - translated
    print(f"[digest] переведено на русский: {translated} из {len(rows)}"
          + (f" (без перевода осталось {missing})" if missing else ""))
    return rows


def format_line(index: int, row: dict) -> str:
    """Одна история в сообщении бота. Коротко и без украшательств."""
    head = f"{index}. [{row['score']}/50] {row['source']}"
    if row.get("category"):
        head += f" · {row['category']}"
    parts = [head, row["title"]]
    if row.get("summary"):
        parts.append(row["summary"])
    if row.get("signals"):
        parts.append(f"↳ {row['signals']}")
    return "\n".join(parts)
