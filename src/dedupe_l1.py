"""L1-дедупликация: MinHash/LSH по title+первый абзац.

Отсекает точные дубли (по url_hash) и перепечатки/близкие пересказы одной и
той же новости разными источниками — CPU-only, без LLM и без эмбеддингов.
L2 (инкрементальная кластеризация по эмбеддингам, окно 48ч) — явно НЕ входит
в MVP.
"""
from __future__ import annotations

import re
from typing import List, Set

try:
    from datasketch import MinHash, MinHashLSH
except ImportError:
    MinHash = None  # type: ignore
    MinHashLSH = None  # type: ignore

from src.normalize import NewsItem

_TOKEN_RE = re.compile(r"[a-zа-яё0-9]+", re.IGNORECASE)
NUM_PERM = 128


def _tokens(text: str) -> Set[str]:
    return {t for t in _TOKEN_RE.findall((text or "").lower()) if len(t) > 2}


def _first_paragraph(body: str) -> str:
    if not body:
        return ""
    return body.split("\n\n")[0][:500]


def _make_minhash(item: NewsItem):
    tokens = _tokens(item.title) | _tokens(_first_paragraph(item.body))
    m = MinHash(num_perm=NUM_PERM)
    for tok in tokens:
        m.update(tok.encode("utf8"))
    return m


def dedupe(items: List[NewsItem], threshold: float = 0.75) -> List[NewsItem]:
    """Возвращает items без точных и near-duplicate записей.

    Порядок: сначала точные дубли по url_hash, затем MinHash/LSH по
    title+первый абзац с порогом `threshold` (Jaccard-оценка).

    Если datasketch не установлен — падаем громко (это не деградация "источник
    недоступен", это отсутствие обязательной зависимости пайплайна).
    """
    if MinHash is None or MinHashLSH is None:
        raise SystemExit(
            "[dedupe_l1] Missing dependency: datasketch. Run: pip install -r requirements.txt"
        )

    # 1. точные дубли по url_hash
    seen_hashes: Set[str] = set()
    unique: List[NewsItem] = []
    exact_dupes = 0
    for item in items:
        if item.url_hash in seen_hashes:
            exact_dupes += 1
            continue
        seen_hashes.add(item.url_hash)
        unique.append(item)

    # 2. near-duplicates через LSH
    lsh = MinHashLSH(threshold=threshold, num_perm=NUM_PERM)
    kept: List[NewsItem] = []
    near_dupes = 0
    for item in unique:
        mh = _make_minhash(item)
        candidates = lsh.query(mh)
        if candidates:
            near_dupes += 1
            continue
        lsh.insert(item.url_hash, mh)
        kept.append(item)

    print(
        f"[dedupe_l1] raw={len(items)} → exact_dupes={exact_dupes} → "
        f"near_dupes={near_dupes} → kept={len(kept)}"
    )
    return kept


# --- Дедуп между источниками на финальном отборе ----------------------------
#
# MinHash выше работает по общему батчу всех лент, то есть уже кросс-
# источниковый. Но официальный пресс-релиз и обзор издания об одном деле
# написаны разными словами, и порог 0.75 такую пару может не поймать.
# Снижать порог нельзя — склеит разные дела.
#
# Поэтому вторая проверка, высокоточная и только среди УЖЕ отобранных тем
# (их максимум два): совпадение редких слов плюс совпадение чисел. Цена
# ошибки здесь мала — в худшем случае одним кандидатом в день меньше, — и
# каждое склеивание печатается в лог.

_RARE_MIN_LEN = 5
# Слова длиной пять букв «редкими» не являются: в криминальных лентах
# «sentenced», «trafficking», «убийство» встречаются в каждом втором
# заголовке. Совпадение по ним — не признак одного дела, а признак жанра,
# и именно на этом правило склеивало разные дела. Поэтому жанровая лексика
# и служебные слова из расчёта исключены: остаются имена, места, названия
# организаций — то, что действительно повторяется только в одном сюжете.
_STOP = {
    # служебное
    "после", "также", "более", "менее", "которые", "который", "может",
    "сообщает", "сообщил", "заявил", "около", "через", "между", "прошлом",
    "после", "своей", "своих", "этого", "этому", "когда", "потом",
    "after", "about", "which", "their", "these", "those", "there", "where",
    "including", "according", "following", "against", "during", "before",
    "while", "another", "other", "first", "second", "third", "under", "over",
    # жанровая лексика — общая для половины ленты
    "убийство", "убийства", "убийстве", "задержан", "задержали", "полиция",
    "полиции", "следствие", "следствия", "уголовное", "уголовного", "дело",
    "приговор", "приговорил", "приговорён", "осуждён", "осуждена", "суда",
    "мошенничество", "мошенники", "похищение", "расследование", "обвинение",
    "police", "court", "courts", "office", "attorney", "prosecutors",
    "sentenced", "sentence", "convicted", "conviction", "charged", "charges",
    "arrested", "arrest", "arrests", "guilty", "pleads", "pleaded", "indicted",
    "trafficking", "traffickers", "smuggling", "fraud", "scheme", "network",
    "operation", "investigation", "criminal", "crime", "murder", "homicide",
    "prison", "years", "month", "months", "federal", "justice", "department",
    "announced", "announces", "authorities", "suspects", "suspect", "defendant",
    "defendants", "victim", "victims", "million", "billion",
}


def _significant(item: NewsItem) -> tuple[set, set]:
    """Редкие слова и числа заголовка с первым абзацем."""
    text = f"{item.title} {_first_paragraph(item.body)}".lower()
    words = {
        t for t in _TOKEN_RE.findall(text)
        if len(t) >= _RARE_MIN_LEN and not t.isdigit() and t not in _STOP
    }
    numbers = {t for t in _TOKEN_RE.findall(text) if t.isdigit() and len(t) >= 2}
    return words, numbers


def same_story(a: NewsItem, b: NewsItem) -> bool:
    """Одно ли это дело, рассказанное двумя источниками."""
    if a.url and a.url == b.url:
        return True
    words_a, nums_a = _significant(a)
    words_b, nums_b = _significant(b)
    if not words_a or not words_b:
        return False

    shared_words = words_a & words_b
    union = words_a | words_b
    jaccard = len(shared_words) / len(union) if union else 0.0
    shared_numbers = nums_a & nums_b

    # Два независимых условия, оба намеренно строгие. Ошибка здесь стоит
    # владельцу поста в день, поэтому склеиваем только при уверенности:
    #   — тексты пересекаются больше чем наполовину; либо
    #   — совпадает и заметная доля значимых слов, и конкретные числа
    #     (имя фигуранта, город, сумма, число задержанных).
    # Прежнее правило «три общих слова длиной от пяти букв плюс одно число»
    # ловило любые два дела о наркотрафике в Калифорнии.
    return jaccard >= 0.5 or (
        jaccard >= 0.35 and len(shared_words) >= 4 and len(shared_numbers) >= 1
    )


def drop_cross_source_repeats(scored: List) -> List:
    """Убирает из отобранных тем повторы одного дела из разных источников.

    На вход — список пар (item, relevance), отсортированный по убыванию
    оценки: остаётся тот источник, чью версию редактор оценил выше.
    """
    kept: List = []
    for pair in scored:
        item = pair[0]
        twin = next((k for k in kept if same_story(k[0], item)), None)
        if twin is not None:
            print(
                f"[dedupe_l1] тот же сюжет из другого источника — пропускаю: "
                f"{item.source} «{item.title[:60]}» ≈ {twin[0].source} "
                f"«{twin[0].title[:60]}»"
            )
            continue
        kept.append(pair)
    return kept
