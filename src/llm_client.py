"""Единый мультипровайдерный LLM-клиент.

Провайдеры: **gemini** (google-genai SDK) и **openai** (openai SDK) — оба
поддержаны и переключаются через `LLM_PROVIDER` в `.env`. **anthropic**
присутствует в коде как опциональный третий вариант, но НЕ используется по
умолчанию. Если явно поставить `LLM_PROVIDER=anthropic`, модуль работает, но
громко предупреждает в лог на каждый вызов.

Этот модуль отвечает за:
  - выбор провайдера/модели по уровню (`config.LLM_TIER_CHEAP`/`_MID`);
  - JSON-режим: строгий парсинг ответа + извлечение JSON из markdown-фенса,
    1 retry с явным требованием "ТОЛЬКО валидный JSON" при невалидном ответе;
  - retry с экспоненциальным backoff (3 попытки) на сетевые/5xx/429 ошибки;
  - учёт usage (input/output tokens) и запись РЕАЛЬНОЙ стоимости в cost_log
    (`storage.log_cost`) на каждый вызов провайдера, не только раз за run;
  - дневной лимит расходов (`Config.daily_cost_limit_usd`) — `LLMBudgetExceeded`
    поднимается ДО обращения к провайдеру, если бюджет уже исчерпан
    (NO SILENT FAILURES).

Mock-режим (`cfg.is_mock()`) НЕ проходит через этот модуль вообще —
relevance.py/rewrite.py/scorer.py сами решают mock/live веткой ДО вызова
`generate_json`, поэтому `generate_json` считает, что если её вызвали — значит,
реальный LLM нужен взаправду, и падает громко, если это не так.
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, Dict, Optional

import config
from config import Config
from src import storage

# Максимальное время ОДНОГО обращения к модели. Больше — считаем, что
# провайдер завис: лучше упасть с понятной ошибкой и уйти в retry, чем
# держать поток бесконечно.
# Потолок длины ответа модели. Без него стоимость вызова ничем не
# ограничена сверху: резерв бюджета считается по оценке в 300–1200 токенов,
# а модель вправе ответить сколько угодно. Наши ответы — компактный JSON,
# 2048 токенов хватает с запасом даже длинному посту.
LLM_MAX_OUTPUT_TOKENS = 2048

LLM_CALL_TIMEOUT_S = 90.0

# ---------------------------------------------------------------------------
# Исключения — иерархия, чтобы вызывающий код (relevance/rewrite/scorer/main)
# мог различать "бюджет кончился" от "провайдер сломался" при желании, но по
# умолчанию оба варианта не глотаются нигде и падают наверх до main.py.
# ---------------------------------------------------------------------------


class LLMError(RuntimeError):
    """Базовый класс всех ошибок llm_client."""


class LLMBudgetExceeded(LLMError):
    """Дневной лимит расходов (Config.daily_cost_limit_usd) уже исчерпан."""


class LLMTransientError(LLMError):
    """Сетевая/5xx/429 ошибка провайдера — кандидат на retry."""


class LLMResponseError(LLMError):
    """Провайдер ответил, но ответ непригоден (невалидный JSON после retry,
    неизвестный провайдер, не установлен SDK, не-retryable ошибка API и т.п.),
    либо retry-бюджет исчерпан. NO SILENT FAILURES: это исключение всегда
    поднимается наверх, никакого молчаливого fallback на mock здесь нет.
    """


# ---------------------------------------------------------------------------
# Промпты — общая загрузка для relevance.py/rewrite.py/scorer.py.
# ---------------------------------------------------------------------------


@lru_cache(maxsize=None)
def load_prompt(filename: str) -> str:
    path = config.PROMPTS_DIR / filename
    if not path.exists():
        raise LLMResponseError(
            f"Промпт {path} не найден — без него LLM-вызов невозможен "
            f"(проверь каталог prompts/)."
        )
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Результат вызова провайдера — единый формат для gemini/openai/anthropic.
# ---------------------------------------------------------------------------


@dataclass
class ProviderResult:
    text: str
    input_tokens: int
    output_tokens: int


# ---------------------------------------------------------------------------
# JSON-парсинг ответа модели: прямой json.loads, иначе — вытащить JSON из
# markdown-фенса (```json ... ``` или ``` ... ```), иначе — вытащить первый
# сбалансированный {...} блок из текста.
# ---------------------------------------------------------------------------

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL | re.IGNORECASE)


def _find_balanced_json(text: str) -> Optional[str]:
    start = text.find("{")
    if start == -1:
        return None
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _extract_json(text: str) -> Dict[str, Any]:
    """Достаёт JSON-объект из ответа модели. Пробует по очереди:
    1) весь текст как JSON;
    2) содержимое markdown-фенса ```json ... ```;
    3) первый сбалансированный {...} блок где-либо в тексте.
    Поднимает ValueError, если ни один вариант не распарсился — вызывающий
    код (generate_json) решает, делать ли retry с более строгим промптом.
    """
    if text is None:
        raise ValueError("пустой ответ модели (None)")

    candidate = text.strip()
    if not candidate:
        raise ValueError("пустой ответ модели")

    try:
        parsed = json.loads(candidate)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    fence_match = _FENCE_RE.search(candidate)
    if fence_match:
        try:
            parsed = json.loads(fence_match.group(1))
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    balanced = _find_balanced_json(candidate)
    if balanced:
        try:
            parsed = json.loads(balanced)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise ValueError(f"не удалось извлечь валидный JSON-объект из ответа: {candidate[:200]!r}")


class LLMUnavailableError(LLMResponseError):
    """Провайдер не ответил: квота, 5xx, таймаут — после всех повторов.

    Отличать это от «модель ответила, но ответ непригоден» нужно ровно в
    одном месте: суточный лимит подготовок. Молчание провайдера не должно
    расходовать попытку владельца — модель ничего не решала, работы не
    было. Денежные лимиты при этом никуда не деваются.
    """

    def __init__(self, message: str, retry_after=None):
        super().__init__(message)
        self.retry_after = retry_after


# Google в ответе 429 прямо называет, сколько ждать:
#     "Please retry in 18.078224056s"   и   {'retryDelay': '18s'}
# Прежний backoff ждал секунду и две и сдавался, то есть все три попытки
# укладывались в три секунды там, где сервис просил восемнадцать. Теперь
# слушаем его: он знает про свою квоту больше нас.
_RETRY_AFTER_PATTERNS = (
    re.compile(r"retry in\s+([0-9.]+)\s*s", re.IGNORECASE),
    re.compile(r"retryDelay[^0-9]{0,6}([0-9.]+)\s*s", re.IGNORECASE),
)

# Верхняя граница ожидания по просьбе провайдера. Без неё ответ с большой
# задержкой подвесил бы прогон на неопределённое время.
MAX_RETRY_AFTER_S = 90.0


def _retry_after_from(exc) -> float:
    """Сколько провайдер просит подождать. 0.0 — не сказал."""
    text = str(exc)
    for pattern in _RETRY_AFTER_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        try:
            return min(float(m.group(1)), MAX_RETRY_AFTER_S)
        except (TypeError, ValueError):
            continue
    return 0.0


# ---------------------------------------------------------------------------
# Retry с экспоненциальным backoff — 3 попытки, задержки 1с/2с/4с.
# `_sleep` вынесен отдельной функцией, чтобы тесты могли подменить её и не
# ждать реальные секунды.
# ---------------------------------------------------------------------------

MAX_ATTEMPTS = 3
_BASE_DELAY_SECONDS = 1.0


def _sleep(seconds: float) -> None:
    time.sleep(seconds)


def _delay_for(attempt: int, asked: float) -> float:
    """Пауза перед следующей попыткой.

    Просьба провайдера важнее нашей формулы: при 429 он называет точное
    время, когда освободится квота, и ждать меньше бессмысленно — второй
    отказ придёт гарантированно.

    Джиттер нужен, когда параллельных клиентов несколько: без него все
    просыпаются в одну и ту же секунду и снова упираются друг в друга.
    Разброс ±20% этого достаточно.
    """
    if asked > 0:
        # Провайдер назвал точное время, когда освободится квота. Ждать
        # МЕНЬШЕ бессмысленно: второй отказ придёт гарантированно. Поэтому
        # разброс здесь только вверх — вниз от просьбы не уходим.
        return asked * random.uniform(1.0, 1.2)
    base = _BASE_DELAY_SECONDS * (2 ** (attempt - 1))
    return max(base * random.uniform(0.8, 1.2), 0.1)


def _throttled_call(cfg, provider: str, model: str, system_prompt: str,
                    user_prompt: str) -> ProviderResult:
    """Обращение к провайдеру, разведённое во времени с остальными.

    Два ограничения, оба между процессами (конвейер и бот — разные
    процессы, а квота у Google одна на проект):

    * не больше LLM_RPM_LIMIT обращений к модели за минуту;
    * не больше одного обращения к модели одновременно.

    Ограничитель ничего не решает про содержание: он только выбирает
    момент. Промпты, оценки и пороги его не касаются.
    """
    from src import rate_limit

    rpm = getattr(cfg, "llm_rpm_limit", rate_limit.DEFAULT_RPM)
    if rpm <= 0:
        # Ноль выключает ограничитель ЦЕЛИКОМ — и темп, и очередь. На
        # платном тарифе он не нужен, и поведение должно быть в точности
        # прежним: никаких пауз, никакой сериализации, никаких обращений
        # к базе на пути вызова.
        return _call_llm(cfg, provider, model, system_prompt, user_prompt)

    # Порядок важен. Сначала ждём свой момент по темпу, и только потом
    # занимаем очередь: ожидание темпа может длиться десятки секунд, и
    # держать всё это время второй процесс запертым незачем — он всё равно
    # упрётся в тот же общий счётчик минутного окна.
    waited = rate_limit.reserve_slot(model, rpm)
    if waited > 0:
        print(f"[llm_client] {model}: подождал {waited:.1f}с ради темпа "
              f"{rpm} запросов/мин")
    with rate_limit.single_flight(model):
        return _call_llm(cfg, provider, model, system_prompt, user_prompt)


def _call_with_retry(fn, *args, **kwargs) -> ProviderResult:
    last_exc: Optional[Exception] = None
    last_asked = 0.0
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            return fn(*args, **kwargs)
        except LLMTransientError as exc:
            last_exc = exc
            last_asked = _retry_after_from(exc)
            if attempt < MAX_ATTEMPTS:
                delay = _delay_for(attempt, last_asked)
                asked_note = (f", провайдер просит {last_asked:.0f}с"
                              if last_asked > 0 else "")
                print(
                    f"[llm_client] попытка {attempt}/{MAX_ATTEMPTS} не удалась "
                    f"({exc}){asked_note} — retry через {delay:.1f}с."
                )
                _sleep(delay)
            else:
                raise LLMUnavailableError(
                    f"LLM-провайдер не ответил после {MAX_ATTEMPTS} попыток: {exc}",
                    retry_after=last_asked or None,
                ) from exc
    # Недостижимо (цикл либо возвращает, либо поднимает исключение), но
    # оставлено ради явности вместо неявного None.
    raise LLMUnavailableError(
        f"LLM-провайдер не ответил после {MAX_ATTEMPTS} попыток: {last_exc}",
        retry_after=last_asked or None,
    )


# ---------------------------------------------------------------------------
# Провайдер-специфичные вызовы. Каждый: (а) поднимает LLMTransientError на
# сетевые/5xx/429 ошибки — годится для retry; (б) поднимает LLMResponseError
# на всё остальное (не установлен SDK, неверный ключ, 4xx кроме 429 и т.п.) —
# retry по этим ошибкам бессмысленен.
# ---------------------------------------------------------------------------

_RETRYABLE_STATUS_CODES = {429, 500, 502, 503, 504}


def _is_retryable_status(status_code: Optional[int]) -> bool:
    return status_code is not None and status_code in _RETRYABLE_STATUS_CODES


def _call_openai(cfg: Config, model: str, system_prompt: str, user_prompt: str) -> ProviderResult:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise LLMResponseError(
            "Пакет openai не установлен — pip install -r requirements.txt"
        ) from exc

    # Без явного таймаута зависший ответ провайдера блокирует поток
    # навсегда: конвейер стоит, а по кнопке 🔁 владелец бесконечно
    # видит «Переписываю…». Ретраи и backoff тут уже есть, а верхней
    # границы времени одного вызова не было.
    client = OpenAI(api_key=cfg.openai_api_key, timeout=LLM_CALL_TIMEOUT_S)
    try:
        resp = client.chat.completions.create(
            model=model,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            max_completion_tokens=LLM_MAX_OUTPUT_TOKENS,
            response_format={"type": "json_object"},
        )
    except Exception as exc:  # noqa: BLE001 — узкий re-raise ниже по типу
        status_code = getattr(exc, "status_code", None)
        cls_name = type(exc).__name__
        if _is_retryable_status(status_code) or cls_name in {
            "RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError",
        }:
            raise LLMTransientError(f"OpenAI {cls_name}: {exc}") from exc
        raise LLMResponseError(f"OpenAI API ошибка ({cls_name}): {exc}") from exc

    choice = resp.choices[0] if resp.choices else None
    text = (choice.message.content if choice and choice.message else "") or ""
    usage = getattr(resp, "usage", None)
    input_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    return ProviderResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)


def _call_gemini(cfg: Config, model: str, system_prompt: str, user_prompt: str) -> ProviderResult:
    try:
        from google import genai
        from google.genai import types
    except ImportError as exc:
        raise LLMResponseError(
            "Пакет google-genai не установлен — pip install -r requirements.txt"
        ) from exc

    # См. комментарий в _call_openai: верхняя граница времени вызова.
    client = genai.Client(
        api_key=cfg.gemini_api_key,
        http_options=types.HttpOptions(timeout=int(LLM_CALL_TIMEOUT_S * 1000)),
    )
    try:
        resp = client.models.generate_content(
            model=model,
            contents=user_prompt,
            config=types.GenerateContentConfig(
                system_instruction=system_prompt,
                response_mime_type="application/json",
                max_output_tokens=LLM_MAX_OUTPUT_TOKENS,
            ),
        )
    except Exception as exc:  # noqa: BLE001
        status_code = getattr(exc, "status_code", None) or getattr(exc, "code", None)
        cls_name = type(exc).__name__
        if _is_retryable_status(status_code) or cls_name in {
            "ServerError", "APIError", "DeadlineExceededError", "ResourceExhausted",
        }:
            raise LLMTransientError(f"Gemini {cls_name}: {exc}") from exc
        raise LLMResponseError(f"Gemini API ошибка ({cls_name}): {exc}") from exc

    text = getattr(resp, "text", None) or ""
    usage = getattr(resp, "usage_metadata", None)
    input_tokens = int(getattr(usage, "prompt_token_count", 0) or 0)
    output_tokens = int(getattr(usage, "candidates_token_count", 0) or 0)
    return ProviderResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)


def _call_anthropic(cfg: Config, model: str, system_prompt: str, user_prompt: str) -> ProviderResult:
    """Опциональный третий провайдер — НЕ используется по умолчанию нигде в
    коде (см. модульный docstring).
    """
    try:
        import anthropic
    except ImportError as exc:
        raise LLMResponseError(
            "Пакет anthropic не установлен — pip install -r requirements.txt"
        ) from exc

    client = anthropic.Anthropic(api_key=cfg.anthropic_api_key)
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            system=system_prompt,
            messages=[{"role": "user", "content": user_prompt}],
        )
    except Exception as exc:  # noqa: BLE001
        status_code = getattr(exc, "status_code", None)
        cls_name = type(exc).__name__
        if _is_retryable_status(status_code) or cls_name in {
            "RateLimitError", "APIConnectionError", "APITimeoutError", "InternalServerError", "OverloadedError",
        }:
            raise LLMTransientError(f"Anthropic {cls_name}: {exc}") from exc
        raise LLMResponseError(f"Anthropic API ошибка ({cls_name}): {exc}") from exc

    text = "".join(
        block.text for block in (resp.content or []) if getattr(block, "type", "") == "text"
    )
    usage = getattr(resp, "usage", None)
    input_tokens = int(getattr(usage, "input_tokens", 0) or 0)
    output_tokens = int(getattr(usage, "output_tokens", 0) or 0)
    return ProviderResult(text=text, input_tokens=input_tokens, output_tokens=output_tokens)


_PROVIDER_CALLERS = {
    "openai": _call_openai,
    "gemini": _call_gemini,
    "anthropic": _call_anthropic,
}


def _call_llm(cfg: Config, provider: str, model: str, system_prompt: str, user_prompt: str) -> ProviderResult:
    caller = _PROVIDER_CALLERS.get(provider)
    if caller is None:
        raise LLMResponseError(
            f"Неизвестный LLM_PROVIDER={provider!r}. Поддерживаются: gemini, openai "
            f"(anthropic — опционально, не по умолчанию)."
        )
    if provider == "anthropic":
        print(
            "[llm_client] WARNING: LLM_PROVIDER=anthropic — не провайдер по "
            "умолчанию. Используется, потому что явно задан в .env — "
            "решение осознанное."
        )
    return caller(cfg, model, system_prompt, user_prompt)


# ---------------------------------------------------------------------------
# Стоимость и cost_log.
# ---------------------------------------------------------------------------


def _cost_for(model: str, input_tokens: int, output_tokens: int) -> float:
    prices = config.LLM_PRICES_PER_1M.get(model)
    if not prices:
        # Раньше здесь возвращался 0.0: неизвестная модель = «бесплатно».
        # Это молча отключало дневной лимит расходов ЦЕЛИКОМ — в cost_log
        # писались нули, get_today_cost() навсегда оставался 0, и лимит не
        # срабатывал никогда. Достаточно было указать в .env ID модели,
        # которого нет в таблице цен.
        # Теперь считаем по ВЕРХНЕЙ известной цене: лимит сработает раньше
        # времени (это безопасно и заметно), а не позже (это дорого и тихо).
        worst = max(
            (p for p in config.LLM_PRICES_PER_1M.values()),
            key=lambda p: p["input"] + p["output"],
            default={"input": 15.0, "output": 75.0},
        )
        print(
            f"[llm_client] WARNING: нет цены для модели {model!r} в "
            f"config.LLM_PRICES_PER_1M — считаю по самому дорогому известному "
            f"тарифу (${worst['input']}/${worst['output']} за 1М токенов), "
            f"чтобы дневной лимит не оказался отключён. Впиши реальный тариф."
        )
        prices = worst
    return (input_tokens / 1_000_000) * prices["input"] + (output_tokens / 1_000_000) * prices["output"]


def _check_budget(cfg: Config, db_path=None) -> None:
    """Единственная проверка бюджета перед обращением к провайдеру.

    Здесь проверяются ОБА потолка. Месячный раньше стоял только в начале
    прогона конвейера: при остатке в один цент прогон стартовал, а дальше
    каждый вызов смотрел лишь на сутки — и месячный предел проезжали. Мимо
    него же шла кнопка «перегенерить» в боте: конвейер уже остановлен по
    месяцу, а бот продолжает платить.

    Бюджет проекта задан в МЕСЯЦ. Дневной лимит — аварийный предохранитель
    внутри месяца, а не норма расхода.
    """
    month_limit = getattr(cfg, "monthly_cost_limit_usd", 0.0)
    if month_limit > 0:
        month_cost = storage.get_month_cost(db_path)
        if month_cost >= month_limit:
            raise LLMBudgetExceeded(
                f"Месячный предел расходов исчерпан (${month_cost:.4f} >= "
                f"${month_limit:.2f}) — новый вызов провайдера заблокирован ДО "
                f"обращения к сети. Это абсолютный потолок бюджета проекта "
                f"(см. .env MONTHLY_COST_LIMIT_USD)."
            )

    today_cost = storage.get_today_cost(db_path)
    if today_cost >= cfg.daily_cost_limit_usd:
        raise LLMBudgetExceeded(
            f"Дневной лимит LLM-расходов исчерпан (${today_cost:.4f} >= "
            f"${cfg.daily_cost_limit_usd:.2f}) — новый вызов провайдера заблокирован "
            f"ДО обращения к сети (NO SILENT FAILURES, см. .env DAILY_COST_LIMIT_USD)."
        )


# ---------------------------------------------------------------------------
# Главная точка входа.
# ---------------------------------------------------------------------------


def generate_json(
    cfg: Config,
    tier: str,
    stage: str,
    system_prompt: str,
    user_prompt: str,
    db_path=None,
) -> Dict[str, Any]:
    """Вызывает LLM-провайдера cfg.llm_provider на уровне `tier`
    (config.LLM_TIER_CHEAP/_MID), парсит JSON-ответ, логирует реальную
    стоимость в cost_log на КАЖДЫЙ фактический вызов провайдера (включая
    вызов при JSON-retry).

    `stage` — метка для cost_log.stage (например "relevance"/"rewrite"/"scorer").

    Поднимает:
      - LLMBudgetExceeded — если дневной лимит уже исчерпан (до сетевого вызова);
      - LLMResponseError — после исчерпания retry на сетевые ошибки, или если
        JSON невалиден дважды подряд, или провайдер/SDK недоступны.
    Ничего не глотает и не откатывается на mock молча — вызывающий код решает,
    что делать с исключением (в этом проекте — не решает, оно падает наверх
    до main.py).
    """
    if cfg.is_mock():
        raise LLMResponseError(
            "generate_json() вызван при LLM_PROVIDER=mock/без ключа — это ошибка "
            "вызывающего кода: relevance.py/rewrite.py/scorer.py должны были "
            "уйти в mock-ветку ДО этого вызова (cfg.is_mock())."
        )

    provider = cfg.llm_provider
    model = cfg.model_for_tier(tier)

    _check_budget(cfg, db_path)

    result = _call_with_retry(_throttled_call, cfg, provider, model,
                              system_prompt, user_prompt)
    storage.log_cost(
        stage=stage, provider=provider, model=model,
        usd_cost=_cost_for(model, result.input_tokens, result.output_tokens),
        db_path=db_path,
    )

    try:
        return _extract_json(result.text)
    except ValueError as first_exc:
        print(
            f"[llm_client] [{stage}] ответ модели не распарсился как JSON "
            f"({first_exc}) — повторяю запрос с явным требованием JSON."
        )
        strict_prompt = (
            f"{user_prompt}\n\n"
            f"ВАЖНО: предыдущий ответ не был валидным JSON. Верни ТОЛЬКО "
            f"валидный JSON-объект, без markdown-фенса, без пояснений до или после."
        )
        _check_budget(cfg, db_path)
        result2 = _call_with_retry(_throttled_call, cfg, provider, model,
                                   system_prompt, strict_prompt)
        storage.log_cost(
            stage=stage, provider=provider, model=model,
            usd_cost=_cost_for(model, result2.input_tokens, result2.output_tokens),
            db_path=db_path,
        )
        try:
            return _extract_json(result2.text)
        except ValueError as second_exc:
            raise LLMResponseError(
                f"[{stage}] LLM вернул невалидный JSON дважды подряд "
                f"(провайдер={provider}, модель={model}): {second_exc}"
            ) from second_exc
