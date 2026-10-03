"""Rewrite RU: хук → факты → деталь-крючок → источник,
формат A (400-700 знаков, ~80% постов) или формат B (до 1500, топ-истории).

Промпт — prompts/rewriter_ru.md (JSON-контракт).
В mock-режиме (LLM_PROVIDER=mock или нет ключа) собирает пост шаблонно из
полей NewsItem — этого достаточно, чтобы пайплайн прогонялся end-to-end в
dry-run, но НЕ является образцом голоса канала (реальная LLM-интеграция
обязательна перед боевым запуском).
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict

from config import Config, LLM_TIER_MID
from src import llm_client
from src.normalize import NewsItem


@dataclass
class PostDraft:
    id: str
    source_item_hash: str
    source_url: str
    source_name: str
    category: str
    format: str  # "A" | "B"
    hook: str
    body: str
    detail_hook: str
    source_line: str
    full_text: str
    # Короткий заголовок для карточки-картинки. По умолчанию пуст: старый
    # код и тесты, создающие черновик без него, продолжают работать —
    # на карточку тогда идёт запасной вариант из хука.
    headline: str = ""
    manual_only: bool = False
    regenerate_count: int = 0
    scores: Dict[str, Any] = field(default_factory=dict)
    status: str = "candidate"  # candidate|approved|rejected|published

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "source_item_hash": self.source_item_hash,
            "source_url": self.source_url,
            "source_name": self.source_name,
            "category": self.category,
            "format": self.format,
            "headline": self.headline,
            "full_text": self.full_text,
            "manual_only": self.manual_only,
            "regenerate_count": self.regenerate_count,
            "scores": self.scores,
            "status": self.status,
        }


def _draft_id(item: NewsItem, attempt: int, channel_id: str = "") -> str:
    """Идентификатор черновика: детерминирован, но различает каналы.

    В основе хеш новости, номер попытки и дата — повтор в тот же день даёт
    тот же идентификатор, и это правильно: одна работа, одна запись.

    Канал добавлен потому, что одну и ту же историю могут готовить для
    разных каналов в один день. Так было при переходе с песочницы на боевой:
    без канала новая подготовка сталкивалась с записью песочницы, и либо
    затирала её (теряя историю), либо не могла записаться вовсе.
    """
    basis = (f"{item.url_hash}:{attempt}:"
             f"{datetime.now(timezone.utc).date().isoformat()}:{channel_id}")
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:12]


# Поле category у новости заполняет ingest — там лежит ТИП ИСТОЧНИКА («rss»,
# «gdelt»), а не категория дела. Оно перекрывало настоящую категорию из
# relevance, и на карточке в рубрике всегда стояло «RSS» вместо «курьёз»
# или «громкое дело».
_INGEST_MARKERS = {"rss", "gdelt", "newsdata", "sample", ""}


def _pick_category(item: NewsItem) -> str:
    from_relevance = (item.relevance or {}).get("category", "")
    if from_relevance:
        return from_relevance
    raw = (item.category or "").strip()
    return "" if raw.lower() in _INGEST_MARKERS else raw


def _choose_format(item: NewsItem) -> str:
    # ASSUMPTION: формат B не чаще 1 раза на 4-5 постов.
    # Эвристика для MVP: длинные/насыщенные новости с явным "контекстом дела" -> B.
    total = item.relevance.get("total", 0) if item.relevance else 0
    return "B" if total >= 40 and len(item.body) > 400 else "A"


def _mock_hook(item: NewsItem) -> str:
    title = item.title.strip()
    return title if len(title) <= 120 else title[:117] + "…"


def _mock_body(item: NewsItem) -> str:
    body = item.body.strip()
    return body if body else "Подробности уточняются по мере поступления официальной информации."


def _mock_detail_hook(item: NewsItem) -> str:
    return "Деталь для проверки редактором: уточнить самый конкретный факт из источника (цифра, предмет, цитата)."


def _mock_source_line(item: NewsItem) -> str:
    return f"По данным {item.source}." if item.source else "Источник не указан — публикация запрещена."


# LLM иногда смешивает алфавиты внутри русского слова («сall-центр»: «с»
# кириллическая, остальное латиница). Чиним гомоглифы по контексту слова.
_LAT_TO_CYR = str.maketrans("aceopxyABCEHKMOPTXacek", "асеорхуАВСЕНКМОРТХасек")
_CYR_TO_LAT = str.maketrans("асеорхуАВСЕНКМОРТХ", "aceopxyABCEHKMOPTX")
_CYR_RE = re.compile(r"[а-яёА-ЯЁ]")
_LAT_RE = re.compile(r"[a-zA-Z]")


def _fix_mixed_alphabet(text: str) -> str:
    """Внутри одного слова не должно быть смеси кириллицы и латиницы-гомоглифов.
    Слово чинится в сторону преобладающего алфавита; слова без гомоглифной
    неоднозначности (напр. «iPhone», «GPS-трекер») не трогаем."""

    def fix_word(word: str) -> str:
        cyr = len(_CYR_RE.findall(word))
        lat = len(_LAT_RE.findall(word))
        if not cyr or not lat:
            return word
        if cyr >= lat:
            fixed = word.translate(_LAT_TO_CYR)
            return fixed if not _LAT_RE.search(fixed) else word
        fixed = word.translate(_CYR_TO_LAT)
        return fixed if not _CYR_RE.search(fixed) else word

    # Части через дефис чиним по отдельности: «сall-центры» = «сall» + «центры».
    return re.sub(r"\w+", lambda m: fix_word(m.group(0)), text, flags=re.UNICODE)


# ---------------------------------------------------------------------------
# Заголовок для карточки-картинки.
#
# Это ОТДЕЛЬНОЕ поле, а не обрезанный хук: на карточке текст читают за секунду
# в ленте, и предложение на 120 знаков там нечитаемо. Модель генерирует его
# сама (см. prompts/rewriter_ru.md), здесь — только страховка на случай, если
# она вернула мусор, слишком длинный вариант или ничего.
# ---------------------------------------------------------------------------

HEADLINE_MAX_CHARS = 45


def _clean_headline(raw: str) -> str:
    """Приводит заголовок к виду, пригодному для печати на карточке."""
    text = " ".join((raw or "").split())
    text = text.strip(" \"«»'`")
    text = text.rstrip(".")
    if len(text) > HEADLINE_MAX_CHARS:
        return ""  # слишком длинный — лучше собрать из хука, чем резать по букве
    return text


def _headline_from_hook(hook: str) -> str:
    """Запасной заголовок, если модель не дала своего.

    Берём первую грамматически цельную часть хука (до запятой/тире/двоеточия),
    а не механическую обрезку по количеству символов: обрубок на середине
    слова на картинке выглядит как брак.
    """
    text = " ".join((hook or "").split()).rstrip(".")
    if not text:
        return "Без заголовка"
    for sep in ("—", ":", ";", ","):
        if sep in text:
            head = text.split(sep, 1)[0].strip()
            if 10 <= len(head) <= HEADLINE_MAX_CHARS:
                return head
    if len(text) <= HEADLINE_MAX_CHARS:
        return text
    words: list[str] = []
    for word in text.split():
        if len(" ".join(words + [word])) > HEADLINE_MAX_CHARS:
            break
        words.append(word)
    return " ".join(words) or text[:HEADLINE_MAX_CHARS]


def _assemble_full_text(hook: str, body: str, detail_hook: str, source_line: str) -> str:
    # Без «**»: публикация идёт без parse_mode (иначе символы _ * [ ] из
    # текста новости ломали бы разметку и Telegram отклонял сообщение),
    # поэтому звёздочки ушли бы в канал как обычные символы.
    parts = [hook, "", body, "", detail_hook, "", source_line]
    return _fix_mixed_alphabet("\n".join(p for p in parts if p is not None))


def _mock_rewrite(item: NewsItem, fmt: str) -> PostDraft:
    hook = _mock_hook(item)
    body = _mock_body(item)
    detail_hook = _mock_detail_hook(item)
    source_line = _mock_source_line(item)
    full_text = _assemble_full_text(hook, body, detail_hook, source_line)

    return PostDraft(
        id=_draft_id(item, 0),
        source_item_hash=item.url_hash,
        source_url=item.url,
        source_name=item.source,
        category=_pick_category(item),
        format=fmt,
        headline=_headline_from_hook(hook),
        hook=hook,
        body=body,
        detail_hook=detail_hook,
        source_line=source_line,
        full_text=full_text,
    )


def _llm_rewrite(cfg: Config, item: NewsItem, fmt: str,
                 case_status: str | None = None) -> PostDraft:
    """Реальная генерация через LLM (MID-уровень модели, см. config.py) по
    prompts/rewriter_ru.md. Парсинг JSON-ответа, retry, cost-tracking — в
    src/llm_client.py.
    """
    system_prompt = llm_client.load_prompt("rewriter_ru.md")
    category = _pick_category(item)
    user_payload = {
        "title": item.title,
        # Ограничение длины входа — см. relevance.PROMPT_BODY_LIMIT: резерв
        # бюджета прогона считается по оценке размера запроса.
        "summary": (item.body or "")[:4000],
        "source_name": item.source,
        "source_url": item.url,
        "category": category,
        "lang": item.lang,
        "format": fmt,
    }
    if case_status:
        # Статус, прочитанный из источника, и точная русская формулировка
        # под него. Модель иначе угадывает: по делу с приговором пишет
        # «по версии следствия», по делу без задержанных — «преступник».
        # Каждая такая ошибка — отказ гейта и оплаченная перегенерация.
        from src import case_status as case_status_mod

        user_payload["case_status"] = case_status
        user_payload["case_status_ru"] = case_status_mod.title(case_status)
        user_payload["legal_wording"] = case_status_mod.guidance(case_status)
    data = llm_client.generate_json(
        cfg,
        tier=LLM_TIER_MID,
        stage="rewrite",
        system_prompt=system_prompt,
        user_prompt=json.dumps(user_payload, ensure_ascii=False),
    )

    if data.get("rejected_no_source"):
        raise llm_client.LLMResponseError(
            "LLM отказался писать пост: нет source_url "
            "(см. prompts/rewriter_ru.md, п.6 юридических правил)."
        )

    headline = _clean_headline(str(data.get("headline", "")))
    hook = str(data.get("hook", "")).strip()
    body = str(data.get("body", "")).strip()
    detail_hook = str(data.get("detail_hook", "")).strip()
    source_line = str(data.get("source_line", "")).strip()
    full_text = str(data.get("full_text", "")).strip() or _assemble_full_text(hook, body, detail_hook, source_line)
    fmt_out = str(data.get("format") or fmt)

    return PostDraft(
        id=_draft_id(item, 0),
        source_item_hash=item.url_hash,
        source_url=item.url,
        source_name=item.source,
        category=category,
        format=fmt_out,
        headline=headline or _headline_from_hook(hook),
        hook=hook,
        body=body,
        detail_hook=detail_hook,
        source_line=source_line,
        full_text=full_text,
    )


def generate_draft(cfg: Config, item: NewsItem, attempt: int = 0,
                   case_status: str | None = None) -> PostDraft:
    fmt = _choose_format(item)
    draft = (_mock_rewrite(item, fmt) if cfg.is_mock()
             else _llm_rewrite(cfg, item, fmt, case_status))
    draft.id = _draft_id(item, attempt, getattr(cfg, "telegram_channel_id", "") or "")
    draft.regenerate_count = attempt
    return draft
