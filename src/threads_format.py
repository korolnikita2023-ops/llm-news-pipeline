"""Готовый пост Telegram → пост Threads. Без модели, без новых фактов.

Почему без LLM. Настоящие тексты канала — 444–490 знаков при лимите Threads
в 500, то есть подавляющее большинство постов проходит вообще без правки.
Сокращать надо только формат B (до 1500 знаков, примерно каждый пятый пост),
и для этого достаточно отбросить хвостовые абзацы. Звать модель ради этого
значит платить за возможность выдумать факт там, где выдумывать нечего.

Что делаем:
  1. Отделяем хвостовую строку атрибуции («По данным NCA.») — она вернётся
     на место последней, её нельзя потерять при обрезке.
  2. Набираем абзацы с начала, пока помещаются целиком. Style v1 ставит
     крючок в первый абзац, поэтому начало и есть самая сильная часть.
  3. Возвращаем атрибуцию, ссылку на источник вешаем отдельным вложением.
  4. Прогоняем УЖЕ СОКРАЩЁННЫЙ текст через тот же юридический гейт.

Пункт 4 — не перестраховка. Style v1 разносит утверждение и оговорку по
разным абзацам: «Задержан подозреваемый…» в первом, «по версии следствия» во
втором. Отрезав второй абзац, мы превратили бы осторожную формулировку в
утверждение о виновности конкретного человека — ровно то, за что отвечают
ст. 152 ГК и ст. 128.1 УК. Гейт здесь работает как код, а не как совет:
не пропустил — пост в Threads не уходит вовсе.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import List, Optional

# Хвостовая строка атрибуции: «По данным X.», «По информации Y.»
_ATTRIBUTION = re.compile(r"^(По данным|По информации|Источник)\b.*$", re.IGNORECASE)

# Запас к лимиту. Документация Meta говорит про эмодзи «считаются по UTF-8»
# и молчит о кириллице, поэтому запас изначально был 20 знаков — и зря резал
# последний абзац у постов длиной под 500.
#
# Проверено на боевом аккаунте 29.08.2026: контейнер с 499 знаками кириллицы
# (997 байт UTF-8) принят. Значит лимит считается в ЗНАКАХ, и большой запас
# не нужен. Оставляем маленький — на случай, если Meta когда-нибудь начнёт
# считать иначе; терять из-за этого целый абзац больше не приходится.
SAFETY_MARGIN = 5

SKIP_TOO_LONG = "too_long"
SKIP_LEGAL = "legal_hard_fail"
SKIP_EMPTY = "empty"


@dataclass
class ThreadsPost:
    text: str
    link_url: str


@dataclass
class FormatResult:
    post: Optional[ThreadsPost]
    skip_reason: str = ""
    detail: str = ""


def _split_paragraphs(text: str) -> List[str]:
    return [p.strip() for p in re.split(r"\n\s*\n", text or "") if p.strip()]


def build(draft: str, source_url: str = "", limit: int = 500,
          check_legal=None, case_status: str = "") -> FormatResult:
    """Собирает пост Threads из одобренного текста Telegram.

    `check_legal` — вызываемое как `check_legal(text, source_url)`,
    возвращающее объект с `hard_fail` и `issues`. Передаётся снаружи, чтобы
    модуль оставался проверяемым без сети и без остального конвейера.
    """
    paragraphs = _split_paragraphs(draft)
    if not paragraphs:
        return FormatResult(None, SKIP_EMPTY, "текст поста пуст")

    # Атрибуция живёт последней строкой и обязана уцелеть при обрезке.
    attribution = ""
    if _ATTRIBUTION.match(paragraphs[-1]):
        attribution = paragraphs.pop().strip()

    if not paragraphs:
        return FormatResult(None, SKIP_EMPTY, "кроме строки источника ничего нет")

    budget = limit - SAFETY_MARGIN
    if attribution:
        budget -= len(attribution) + 2   # плюс пустая строка перед ней

    kept: List[str] = []
    used = 0
    for para in paragraphs:
        extra = len(para) + (2 if kept else 0)
        if used + extra > budget:
            break
        kept.append(para)
        used += extra

    if not kept:
        # Даже первый абзац не влезает. Резать его по словам нельзя:
        # оборванная на середине фраза о деле — это не «короче», это другое
        # утверждение. Такой пост в Threads не идёт.
        return FormatResult(
            None, SKIP_TOO_LONG,
            f"первый абзац {len(paragraphs[0])} знаков не помещается в {budget}",
        )

    text = "\n\n".join(kept)
    if attribution:
        text = f"{text}\n\n{attribution}"

    if len(text) > limit:
        return FormatResult(None, SKIP_TOO_LONG,
                            f"собранный текст {len(text)} знаков при лимите {limit}")

    if check_legal is not None:
        verdict = check_legal(text, source_url)
        if getattr(verdict, "hard_fail", False):
            issues = "; ".join(getattr(verdict, "issues", []) or [])
            return FormatResult(None, SKIP_LEGAL, issues[:400])

    return FormatResult(ThreadsPost(text=text, link_url=source_url or ""))
