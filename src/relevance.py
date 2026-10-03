"""Relevance-фильтр: дешёвая LLM-оценка по 5 осям
(curiosity/emotion/unusual/shareable/ru_fit) + red_flags.

JSON-контракт промпта — prompts/relevance_judge.md. В mock-режиме (нет
LLM-ключа или LLM_PROVIDER=mock) считает детерминированную keyword-based
оценку — так пайплайн работает end-to-end в dry-run без сети (требование "MVP запускается на sample-данных").
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from config import Config, CRIME_KEYWORDS_RU, CRIME_KEYWORDS_EN, LOW_VALUE_KEYWORDS_RU, LLM_TIER_CHEAP
from src import llm_client
from src.normalize import NewsItem

# Сколько текста новости уходит в промпт (см. комментарий в _llm_score).
PROMPT_BODY_LIMIT = 2000


@dataclass
class RelevanceResult:
    relevant: bool
    curiosity: int
    emotion: int
    unusual: int
    shareable: int
    ru_fit: int
    category: str
    red_flags: List[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return self.curiosity + self.emotion + self.unusual + self.shareable + self.ru_fit

    def to_dict(self) -> Dict[str, Any]:
        return {
            "relevant": self.relevant,
            "curiosity": self.curiosity,
            "emotion": self.emotion,
            "unusual": self.unusual,
            "shareable": self.shareable,
            "ru_fit": self.ru_fit,
            "category": self.category,
            "red_flags": self.red_flags,
            "total": self.total,
        }


def _blob(item: NewsItem) -> str:
    return f"{item.title} {item.body}".lower()


def _mock_score(item: NewsItem, min_sum: int = 20) -> RelevanceResult:
    """Keyword-based mock,
    адаптирован под RU true-crime оси вместо crypto-релевантности.

    Порог `relevant` приходит из конфига: раньше здесь жило своё число 20,
    и смена MIN_RELEVANCE_SUM не меняла поведение dry-run — mock и боевой
    контур отбирали по-разному.
    """
    blob = _blob(item)
    ru_hits = sum(1 for kw in CRIME_KEYWORDS_RU if kw in blob)
    en_hits = sum(1 for kw in CRIME_KEYWORDS_EN if kw in blob)
    hits = ru_hits + en_hits
    low_value = any(kw in blob for kw in LOW_VALUE_KEYWORDS_RU)

    if hits == 0:
        return RelevanceResult(
            relevant=False, curiosity=0, emotion=0, unusual=0, shareable=0, ru_fit=0,
            category="не по теме", red_flags=["нет crime-ключевых слов"],
        )

    base = min(8, 3 + hits)
    curiosity = base
    emotion = max(1, base - 1)
    unusual = base if "необычн" in blob or "курьёз" in (item.category or "") else max(1, base - 2)
    shareable = base
    ru_fit = base if item.lang == "ru" else max(2, base - 2)

    red_flags: List[str] = []
    if not item.url:
        red_flags.append("нет source_url")
    if low_value:
        red_flags.append("бытовуха без сюжета — низкая ценность")
        curiosity = max(0, curiosity - 3)
        shareable = max(0, shareable - 3)

    total = curiosity + emotion + unusual + shareable + ru_fit
    relevant = total >= min_sum and not low_value

    category = item.category or ("мировое дело" if item.lang == "en" else "российское дело")

    return RelevanceResult(
        relevant=relevant,
        curiosity=curiosity, emotion=emotion, unusual=unusual,
        shareable=shareable, ru_fit=ru_fit,
        category=category, red_flags=red_flags,
    )


def _clamp10(value: Any) -> int:
    try:
        return max(0, min(10, int(value)))
    except (TypeError, ValueError):
        return 0


def _llm_score(cfg: Config, item: NewsItem) -> RelevanceResult:
    """Реальный LLM-вызов по prompts/relevance_judge.md (CHEAP-уровень модели,
    см. config.py). Парсинг JSON-ответа, retry, cost-tracking — всё в
    src/llm_client.py.
    """
    system_prompt = llm_client.load_prompt("relevance_judge.md")
    user_payload = {
        "title": item.title,
        # Длина входа ограничена: резерв бюджета считается по оценке
        # размера запроса, и один аномально длинный текст ленты ломал бы
        # этот расчёт. Для оценки релевантности хватает первых абзацев.
        "summary": (item.body or "")[:PROMPT_BODY_LIMIT],
        "source": item.source,
        "url": item.url,
        "published_at": item.published_at,
        "lang": item.lang,
    }
    data = llm_client.generate_json(
        cfg,
        tier=LLM_TIER_CHEAP,
        stage="relevance",
        system_prompt=system_prompt,
        user_prompt=json.dumps(user_payload, ensure_ascii=False),
    )

    return RelevanceResult(
        relevant=bool(data.get("relevant", False)),
        curiosity=_clamp10(data.get("curiosity", 0)),
        emotion=_clamp10(data.get("emotion", 0)),
        unusual=_clamp10(data.get("unusual", 0)),
        shareable=_clamp10(data.get("shareable", 0)),
        ru_fit=_clamp10(data.get("ru_fit", 0)),
        category=str(data.get("category") or "не по теме"),
        red_flags=[str(f) for f in (data.get("red_flags") or [])],
    )


def score_item(cfg: Config, item: NewsItem) -> RelevanceResult:
    if cfg.is_mock():
        return _mock_score(item, min_sum=cfg.min_relevance_sum)
    return _llm_score(cfg, item)


def score_and_filter(
    cfg: Config,
    items: List[NewsItem],
    on_scored=None,
    on_failed=None,
    dedupe_sorted=None,
    top_n: int | None = None,
) -> List[tuple[NewsItem, RelevanceResult]]:
    """Скорит все items, отбрасывает нерелевантные и те, что ниже
    MIN_RELEVANCE_SUM, сортирует по total desc, обрезает до MAX_TOPICS_PER_RUN.

    Два необязательных сигнала наружу — через них main ведёт журнал
    виденного, а сам этот модуль остаётся без знания о базе:

    `on_scored(item)` — оценка прошла. Помечать в журнале можно.
    `on_failed(item)` — оценка упала, элемент дальше не пойдёт. Решение
        «помечать или нет» принимает вызывающий: деньги за элемент могли
        уже списаться (модель ответила, но не валидным JSON), а могли и
        нет (лимит сработал до обращения к провайдеру).

    Почему поштучно, а не всей пачкой заранее: оценка может оборваться на
    середине, и непроверенные новости пропали бы молча — ровно так однажды
    ушло 137 штук.
    """
    scored: List[tuple[NewsItem, RelevanceResult]] = []
    for item in items:
        try:
            result = score_item(cfg, item)
        except Exception:
            if on_failed is not None:
                on_failed(item)
            raise
        item.relevance = result.to_dict()
        if on_scored is not None:
            on_scored(item)
        passed = result.relevant and result.total >= cfg.min_relevance_sum
        # Калибровка порога: без этого лога распределение оценок не видно и
        # MIN_RELEVANCE_SUM приходится подбирать вслепую.
        print(
            f"[relevance] {'PASS' if passed else 'skip'} total={result.total:>2} "
            f"(cur={result.curiosity} emo={result.emotion} unu={result.unusual} "
            f"shr={result.shareable} ru={result.ru_fit}) "
            f"cat={result.category or '-'} · {item.title[:70]}"
        )
        if passed:
            scored.append((item, result))

    scored.sort(key=lambda pair: pair[1].total, reverse=True)
    # Склейка повторов одного дела применяется ДО обрезки. Иначе дубль
    # занимает второй слот дня, потом отбрасывается — и третья, уже
    # уникальная тема на освободившееся место не подставляется.
    if dedupe_sorted is not None:
        scored = dedupe_sorted(scored)
    # `top_n` нужен режиму дайджеста: там из этого же, уже оплаченного,
    # списка берётся десятка на выбор владельцу. Обрезка до двух — это
    # правило дорогой стадии, а не свойство оценки.
    return scored[: top_n if top_n is not None else cfg.max_topics_per_run]
