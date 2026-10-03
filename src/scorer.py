"""Editorial scorer: 6 измерений 0-10 +
hard-fail slop-list, отдельно от юридического гейта (тон/качество, не право).

Промпт — prompts/editorial_scorer.md. Ниже порога MIN_EDITORIAL_AVG →
regenerate (main.py, макс MAX_REGENERATE_ATTEMPTS раз), потом reject.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Dict, List

from config import Config, LLM_TIER_CHEAP
from src import llm_client
from src.legal_gate import check_slop
from src.rewrite import PostDraft


@dataclass
class ScoreResult:
    tone_match: int
    news_value: int
    engagement: int
    originality: int
    factual_safety: int
    legal_safety: int
    hard_fail: bool
    issues: List[str] = field(default_factory=list)
    suggested_fix: str = ""

    @property
    def average(self) -> float:
        vals = [
            self.tone_match, self.news_value, self.engagement,
            self.originality, self.factual_safety, self.legal_safety,
        ]
        return round(sum(vals) / len(vals), 2)

    # Порог берётся из настроек (MIN_EDITORIAL_AVG). Раньше здесь стояло
    # число 7.0, а настройка в .env не читалась вообще — владелец крутил
    # ручку, ни к чему не подключённую.
    min_average: float = 7.0

    @property
    def decision(self) -> str:
        if self.hard_fail:
            return "reject"
        if self.average >= self.min_average and self.legal_safety >= 6:
            return "approve"
        if self.average >= 5.0:
            return "revise"
        return "reject"

    def to_dict(self) -> Dict[str, Any]:
        return {
            "tone_match": self.tone_match,
            "news_value": self.news_value,
            "engagement": self.engagement,
            "originality": self.originality,
            "factual_safety": self.factual_safety,
            "legal_safety": self.legal_safety,
            "average": self.average,
            "hard_fail": self.hard_fail,
            "decision": self.decision,
            "issues": self.issues,
            "suggested_fix": self.suggested_fix,
        }


_MAX_LEN_A = 700
_MAX_LEN_B = 1500


def _mock_score(draft: PostDraft) -> ScoreResult:
    issues: List[str] = []
    hard_fail = False

    slop_hits = check_slop(draft.full_text)
    if slop_hits:
        issues.append(f"slop-list: {', '.join(slop_hits[:5])}")
        hard_fail = True

    max_len = _MAX_LEN_B if draft.format == "B" else _MAX_LEN_A
    length = len(draft.full_text)
    if length > max_len:
        issues.append(f"превышена длина формата {draft.format}: {length} > {max_len}")
        hard_fail = True

    if not draft.source_url:
        issues.append("нет source_url")
        hard_fail = True

    has_source_phrase = any(
        marker in draft.full_text.lower()
        for marker in ("по данным", "сообщает", "суд установил", "по версии следствия")
    )
    news_value = 8 if has_source_phrase else 4
    tone_match = 7 if not slop_hits else 2
    engagement = 7 if len(draft.hook) <= 120 and draft.hook else 4
    originality = 6  # mock не умеет оценивать оригинальность против корпуса — нейтральная оценка
    factual_safety = 8 if has_source_phrase else 5
    legal_safety = 8 if not hard_fail else 2

    suggested_fix = ""
    if not hard_fail and (tone_match < 7 or news_value < 7):
        suggested_fix = "Добавить более конкретную атрибуцию источника в теле поста."

    return ScoreResult(
        tone_match=tone_match, news_value=news_value, engagement=engagement,
        originality=originality, factual_safety=factual_safety, legal_safety=legal_safety,
        hard_fail=hard_fail, issues=issues, suggested_fix=suggested_fix,
    )


def _clamp10(value: Any) -> int:
    try:
        return max(0, min(10, int(value)))
    except (TypeError, ValueError):
        return 0


def _llm_score(cfg: Config, draft: PostDraft) -> ScoreResult:
    """Реальный LLM-вызов по prompts/editorial_scorer.md (CHEAP-уровень
    модели, см. config.py). Парсинг JSON-ответа, retry, cost-tracking — в
    src/llm_client.py.

    Slop-list дополнительно проверяется КОДОМ (check_slop), а не только
    LLM-ответом — это тот же backstop, что и в _mock_score: LLM-оценке
    юридических/тональных нарушений не доверяем полностью, финальное
    решение о hard_fail всегда усиливается кодом.
    """
    system_prompt = llm_client.load_prompt("editorial_scorer.md")
    user_payload = {
        "hook": draft.hook,
        "full_text": draft.full_text,
        "source_url": draft.source_url,
        "format": draft.format,
        "category": draft.category,
    }
    data = llm_client.generate_json(
        cfg,
        tier=LLM_TIER_CHEAP,
        stage="scorer",
        system_prompt=system_prompt,
        user_prompt=json.dumps(user_payload, ensure_ascii=False),
    )

    issues = [str(i) for i in (data.get("issues") or [])]
    hard_fail = bool(data.get("hard_fail", False))

    slop_hits = check_slop(draft.full_text)
    if slop_hits and not hard_fail:
        hard_fail = True
        issues.append(f"slop-list (код, не пойман LLM-скорером): {', '.join(slop_hits[:5])}")

    return ScoreResult(
        tone_match=_clamp10(data.get("tone_match", 0)),
        news_value=_clamp10(data.get("news_value", 0)),
        engagement=_clamp10(data.get("engagement", 0)),
        originality=_clamp10(data.get("originality", 0)),
        factual_safety=_clamp10(data.get("factual_safety", 0)),
        legal_safety=_clamp10(data.get("legal_safety", 0)),
        hard_fail=hard_fail,
        issues=issues,
        suggested_fix=str(data.get("suggested_fix") or ""),
    )


def score_draft(cfg: Config, draft: PostDraft) -> ScoreResult:
    result = _mock_score(draft) if cfg.is_mock() else _llm_score(cfg, draft)
    # Порог из настроек прокидываем в результат — иначе MIN_EDITORIAL_AVG
    # остаётся мёртвой ручкой, а решение принимается по числу в коде.
    result.min_average = cfg.min_editorial_avg
    draft.scores = result.to_dict()
    return result
