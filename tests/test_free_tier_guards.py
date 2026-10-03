"""Защиты под бесплатный тариф Gemini.

Написано по замеру 28.08. Google на 429 сам назвал свои условия:

    quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier
    quotaValue: 15   model: gemini-3.5-flash-lite   retryDelay: 18s

Наш прогон делал 13 обращений за 11 секунд — темп около 70 в минуту, и
429 приходил на пятнадцатом вызове через двенадцать секунд. Суточный объём
при этом мизерный: тариф подходит целиком, кроме плотности.

Здесь проверяются пять защит и то, что они ничего не решают за модель:
ни промптов, ни оценок, ни порогов они не касаются.
"""
from __future__ import annotations

import asyncio
import time

import pytest

from conftest import FakeCallback, handlers_of
from config import Config
from src import llm_client, rate_limit, storage


def make_cfg(*, rpm: int = 14) -> Config:
    cfg = Config()
    cfg.publish_mode = "live"
    cfg.workflow_mode = "digest"
    cfg.llm_rpm_limit = rpm
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = "-100999"
    return cfg


# Настоящий текст ошибки, полученный от Google 28.08 на бесплатном ключе.
REAL_429 = (
    "429 RESOURCE_EXHAUSTED. {'error': {'code': 429, 'message': 'You exceeded "
    "your current quota, please check your plan and billing details. "
    "* Quota exceeded for metric: "
    "generativelanguage.googleapis.com/generate_content_free_tier_requests, "
    "limit: 15, model: gemini-3.5-flash-lite\\nPlease retry in 18.078224056s.', "
    "'status': 'RESOURCE_EXHAUSTED', 'details': [{'@type': "
    "'type.googleapis.com/google.rpc.QuotaFailure', 'violations': [{'quotaId': "
    "'GenerateRequestsPerMinutePerProjectPerModel-FreeTier', 'quotaValue': '15'}]}, "
    "{'@type': 'type.googleapis.com/google.rpc.RetryInfo', 'retryDelay': '18s'}]}}"
)


# ---------------------------------------------------------------------------
# 1. Темп: не больше N обращений в минуту
# ---------------------------------------------------------------------------


def test_throttle_lets_the_first_calls_through_without_waiting(db, monkeypatch):
    """Пока места в окне есть, ограничитель не задерживает ничего."""
    slept = []
    monkeypatch.setattr(rate_limit, "_sleep", slept.append)

    for _ in range(14):
        assert rate_limit.reserve_slot("m", rpm=14) == 0.0
    assert slept == []
    assert rate_limit.calls_in_window("m") == 14


def test_throttle_waits_when_the_minute_is_full(db, monkeypatch):
    """Пятнадцатый вызов ждёт, пока первый выпадет из окна.

    Ровно тут прежний код получал 429: за одиннадцать секунд успевал
    сделать тринадцать обращений и упирался в потолок.
    """
    slept = []
    now = [1000.0]

    def fake_sleep(seconds):
        # Сон двигает часы — иначе ожидание никогда бы не кончилось.
        slept.append(seconds)
        now[0] += seconds

    monkeypatch.setattr(rate_limit, "_sleep", fake_sleep)
    monkeypatch.setattr(rate_limit, "_now", lambda: now[0])

    for i in range(14):
        now[0] = 1000.0 + i * 0.8          # темп ~75 запросов в минуту
        rate_limit.reserve_slot("m", rpm=14)
    assert slept == []

    now[0] = 1011.0                         # прошло одиннадцать секунд
    waited = rate_limit.reserve_slot("m", rpm=14)
    assert slept, "пятнадцатый вызов должен был подождать"
    # Ждём до момента, когда первое обращение выпадет из минутного окна:
    # оно было в 1000.0, окно шестьдесят секунд, сейчас 1011.
    assert 48 < waited < 51, (waited, slept)


def test_throttle_counts_per_model(db, monkeypatch):
    """Квота у Google на модель, значит и счёт на модель."""
    monkeypatch.setattr(rate_limit, "_sleep", lambda s: None)
    for _ in range(14):
        rate_limit.reserve_slot("lite", rpm=14)
    assert rate_limit.calls_in_window("lite") == 14
    assert rate_limit.calls_in_window("mid") == 0
    assert rate_limit.reserve_slot("mid", rpm=14) == 0.0


def test_throttle_can_be_switched_off(db):
    """Ноль выключает ограничитель — на платном тарифе он не нужен."""
    for _ in range(50):
        assert rate_limit.reserve_slot("m", rpm=0) == 0.0
    assert rate_limit.calls_in_window("m") == 0


def test_slot_is_taken_before_the_call_not_after(db, monkeypatch):
    """Отклонённый по квоте запрос Google всё равно считает обращением.

    Поэтому место занимается до вызова: лучше недоиспользовать квоту, чем
    превысить её.
    """
    monkeypatch.setattr(rate_limit, "_sleep", lambda s: None)
    rate_limit.reserve_slot("m", rpm=14)
    assert rate_limit.calls_in_window("m") == 1


def test_broken_limiter_never_blocks_the_call(db, monkeypatch):
    """Ограничитель не имеет права быть причиной несостоявшегося вызова."""
    def boom(*a, **k):
        raise RuntimeError("база недоступна")

    monkeypatch.setattr(rate_limit, "_ensure_tables", boom)
    assert rate_limit.reserve_slot("m", rpm=14) == 0.0

    with rate_limit.single_flight("m"):
        pass        # не должно бросить


# ---------------------------------------------------------------------------
# 2. Одновременность: один вызов к модели за раз
# ---------------------------------------------------------------------------


def test_single_flight_blocks_a_second_holder(db, monkeypatch):
    """Второй желающий ждёт, пока первый не отпустит.

    Это про наложение прогона конвейера на подготовку поста в боте: они
    разные процессы, а квота у Google одна.
    """
    monkeypatch.setattr(rate_limit, "_sleep", lambda s: None)
    with rate_limit.single_flight("m"):
        with storage.get_conn() as c:
            owner = c.execute("SELECT owner FROM llm_lock WHERE model='m'").fetchone()[0]
        assert owner, "место должно быть занято"
    with storage.get_conn() as c:
        owner = c.execute("SELECT owner FROM llm_lock WHERE model='m'").fetchone()[0]
    assert owner is None, "место должно освободиться"


def test_expired_lease_is_taken_over(db, monkeypatch):
    """Упавший процесс не блокирует очередь навсегда."""
    monkeypatch.setattr(rate_limit, "_sleep", lambda s: None)
    with storage.get_conn() as c:
        c.execute("INSERT INTO llm_lock (model, owner, expires) VALUES "
                  "('m', 'мёртвый процесс', ?)", (time.time() - 1,))
    with rate_limit.single_flight("m"):
        with storage.get_conn() as c:
            owner = c.execute("SELECT owner FROM llm_lock WHERE model='m'").fetchone()[0]
        assert owner != "мёртвый процесс"


def test_live_lease_is_not_stolen(db, monkeypatch):
    """Живую аренду перехватывать нельзя."""
    monkeypatch.setattr(rate_limit, "_sleep", lambda s: None)
    monkeypatch.setattr(rate_limit, "MAX_WAIT_S", 0.5)
    with storage.get_conn() as c:
        c.execute("INSERT INTO llm_lock (model, owner, expires) VALUES "
                  "('m', 'живой', ?)", (time.time() + 300,))
    with rate_limit.single_flight("m"):
        with storage.get_conn() as c:
            owner = c.execute("SELECT owner FROM llm_lock WHERE model='m'").fetchone()[0]
        assert owner == "живой", "чужая живая аренда не должна перехватываться"


# ---------------------------------------------------------------------------
# 3. Слушаем retryDelay Google вместо своей короткой паузы
# ---------------------------------------------------------------------------


def test_retry_delay_is_read_from_the_real_google_error():
    """Из настоящего ответа Google достаётся именно его число."""
    assert llm_client._retry_after_from(Exception(REAL_429)) == pytest.approx(18.08, abs=0.01)


@pytest.mark.parametrize("text,expected", [
    ("{'retryDelay': '31s'}", 31.0),
    ("Please retry in 7.5s.", 7.5),
    ("503 ServiceUnavailable", 0.0),
    ("совсем без чисел", 0.0),
])
def test_retry_delay_parsing(text, expected):
    assert llm_client._retry_after_from(Exception(text)) == expected


def test_absurd_retry_delay_is_capped():
    """Ответ с огромной задержкой не должен подвешивать прогон."""
    assert llm_client._retry_after_from(
        Exception("retry in 99999s")) == llm_client.MAX_RETRY_AFTER_S


def test_backoff_obeys_google_instead_of_its_own_formula():
    """Раньше мы ждали 1 и 2 секунды там, где сервис просил 18.

    Все три попытки укладывались в три секунды, то есть отказ был
    гарантирован ещё до первого повтора.
    """
    delays = [llm_client._delay_for(1, 18.078) for _ in range(200)]
    assert min(delays) > 14, "ждём около просьбы провайдера, а не одну секунду"
    assert max(delays) < 22


def test_backoff_has_jitter():
    """Без разброса параллельные клиенты просыпаются в одну секунду."""
    delays = {round(llm_client._delay_for(2, 0.0), 4) for _ in range(50)}
    assert len(delays) > 10, "задержка должна разниться от вызова к вызову"
    assert all(1.5 < d < 2.5 for d in delays), delays


def test_retry_uses_the_asked_delay_end_to_end(monkeypatch):
    """Проверка целиком: транзиентная ошибка с retryDelay → пауза около 18с."""
    slept = []
    monkeypatch.setattr(llm_client, "_sleep", slept.append)
    calls = []

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) < 2:
            raise llm_client.LLMTransientError(REAL_429)
        return llm_client.ProviderResult(text="{}", input_tokens=1, output_tokens=1)

    llm_client._call_with_retry(flaky)
    assert len(slept) == 1
    assert 14 < slept[0] < 22, slept


def test_exhausted_retries_raise_unavailable_not_bad_answer(monkeypatch):
    """После всех повторов это «провайдер молчит», а не «ответ негодный».

    Различие нужно ровно в одном месте — суточной норме подготовок.
    """
    monkeypatch.setattr(llm_client, "_sleep", lambda s: None)

    def always_fails(*a, **k):
        raise llm_client.LLMTransientError(REAL_429)

    with pytest.raises(llm_client.LLMUnavailableError) as exc:
        llm_client._call_with_retry(always_fails)
    assert exc.value.retry_after == pytest.approx(18.08, abs=0.01)
    # Наследование сохранено: старый код, ловящий LLMResponseError, работает.
    assert isinstance(exc.value, llm_client.LLMResponseError)


# ---------------------------------------------------------------------------
# 4. Временный отказ API не расходует суточную норму подготовок
# ---------------------------------------------------------------------------


def _seed_digest(n: int = 2):
    rows = [{"url_hash": f"h{i}", "source": "bbc.com",
             "source_url": f"https://e.com/{i}", "title": f"История {i}",
             "summary": "Суть.", "score": 20 - i, "category": "crime",
             "signals": "серийность"} for i in range(1, n + 1)]
    storage.save_digest("run1", rows)
    return storage.digest_pool()


def test_api_outage_does_not_consume_a_daily_attempt(db, images_dir, monkeypatch):
    """Провайдер молчит — модель ничего не решала, работы не было."""
    from src import approval_bot, schedule

    rows = _seed_digest(2)

    def unavailable(cfg, item_row):
        raise llm_client.LLMUnavailableError("не ответил после 3 попыток",
                                             retry_after=18.0)

    monkeypatch.setattr(approval_bot, "_generate_from_digest", unavailable)
    cfg = make_cfg()
    dp = approval_bot.build_dispatcher(cfg)
    call = FakeCallback(f"pick:{rows[0]['id']}")
    asyncio.run(handlers_of(dp)["pick"](call))

    attempts, successes = storage.count_selections_today(*schedule.day_bounds_utc(cfg))
    assert (attempts, successes) == (0, 0), "отказ API не должен расходовать норму"
    assert storage.digest_item(rows[0]["id"])["chosen"] == 0, "история вернулась"

    text = call.message.answers[-1]
    assert "НЕ засчитана" in text
    assert "18" in text, "владельцу называем, сколько ждать"


def test_api_outage_is_still_recorded_in_the_journal(db, images_dir, monkeypatch):
    """Не считается — не значит «не записывается». Сбои надо видеть."""
    from src import approval_bot

    rows = _seed_digest(1)
    monkeypatch.setattr(
        approval_bot, "_generate_from_digest",
        lambda cfg, row: (_ for _ in ()).throw(
            llm_client.LLMUnavailableError("молчит")))
    dp = approval_bot.build_dispatcher(make_cfg())
    asyncio.run(handlers_of(dp)["pick"](FakeCallback(f"pick:{rows[0]['id']}")))

    with storage.get_conn() as c:
        outcome = c.execute(
            "SELECT outcome FROM selection_attempts ORDER BY id DESC LIMIT 1"
        ).fetchone()[0]
    assert outcome == storage.ATTEMPT_API_ERROR


def test_a_real_rejection_still_consumes_an_attempt(db, images_dir, monkeypatch):
    """Гейт или редактор забраковали — попытка была, и она в счёт идёт."""
    from src import approval_bot, schedule

    rows = _seed_digest(1)
    monkeypatch.setattr(approval_bot, "_generate_from_digest",
                        lambda cfg, row: {"post_id": None, "outcome": "editorial",
                                          "average": 7.5, "threshold": 8.6,
                                          "weakest": [], "issues": []})
    cfg = make_cfg()
    dp = approval_bot.build_dispatcher(cfg)
    asyncio.run(handlers_of(dp)["pick"](FakeCallback(f"pick:{rows[0]['id']}")))

    attempts, _ = storage.count_selections_today(*schedule.day_bounds_utc(cfg))
    assert attempts == 1


def test_other_crashes_still_consume_an_attempt(db, images_dir, monkeypatch):
    """Послабление — только для молчания провайдера, не для любой ошибки."""
    from src import approval_bot, schedule

    rows = _seed_digest(1)
    monkeypatch.setattr(
        approval_bot, "_generate_from_digest",
        lambda cfg, row: (_ for _ in ()).throw(ValueError("ошибка в нашем коде")))
    cfg = make_cfg()
    dp = approval_bot.build_dispatcher(cfg)
    asyncio.run(handlers_of(dp)["pick"](FakeCallback(f"pick:{rows[0]['id']}")))

    attempts, _ = storage.count_selections_today(*schedule.day_bounds_utc(cfg))
    assert attempts == 1


# ---------------------------------------------------------------------------
# 5. Ограничитель ничего не решает за модель
# ---------------------------------------------------------------------------


def test_guards_do_not_touch_prompts_or_scoring():
    """Защиты выбирают момент вызова, а не его содержание."""
    import inspect

    source = inspect.getsource(rate_limit)
    for forbidden in ("prompt", "score", "relevance", "legal", "threshold"):
        assert forbidden not in source.lower(), forbidden


def test_throttle_wraps_the_provider_call(monkeypatch):
    """Ограничитель стоит вокруг обращения к провайдеру, а не мимо него."""
    import inspect

    source = inspect.getsource(llm_client._throttled_call)
    assert "single_flight" in source
    assert "reserve_slot" in source
    assert "_call_llm" in source

    retry_source = inspect.getsource(llm_client.generate_json)
    assert "_throttled_call" in retry_source, "вызов должен идти через ограничитель"


# ---------------------------------------------------------------------------
# Находки независимого ревью 28.08.2026
# ---------------------------------------------------------------------------


def test_lease_outlives_the_longest_possible_call():
    """Аренда должна пережить самый долгий вызов.

    Истеки она раньше — место перехватит другой процесс, и к модели пойдут
    два обращения разом. Ровно то, чего очередь и не должна допускать.
    """
    assert rate_limit.LEASE_S > llm_client.LLM_CALL_TIMEOUT_S * 2


def test_throttle_waits_outside_the_queue(monkeypatch):
    """Ожидание темпа не должно держать очередь запертой.

    Иначе процесс, ждущий полминуты своего места по темпу, всё это время
    блокировал бы второй — без всякой пользы, они и так делят один счётчик.
    """
    import inspect

    source = inspect.getsource(llm_client._throttled_call)
    reserve_at = source.index("reserve_slot")
    queue_at = source.index("single_flight(model)")
    assert reserve_at < queue_at, "темп сначала, очередь потом"


def test_zero_limit_bypasses_everything(monkeypatch):
    """Ноль выключает ограничитель целиком: ни пауз, ни очереди, ни базы."""
    touched = []
    monkeypatch.setattr(rate_limit, "reserve_slot",
                        lambda *a, **k: touched.append("slot"))
    monkeypatch.setattr(rate_limit, "single_flight",
                        lambda *a, **k: touched.append("queue"))
    monkeypatch.setattr(llm_client, "_call_llm",
                        lambda *a, **k: llm_client.ProviderResult("{}", 1, 1))

    cfg = make_cfg(rpm=0)
    llm_client._throttled_call(cfg, "gemini", "m", "sys", "user")
    assert touched == [], "при нуле ограничитель не должен участвовать вовсе"


def test_jitter_never_shortens_the_asked_delay():
    """Ждать меньше, чем просит Google, бессмысленно — второй 429 придёт точно."""
    asked = 18.078
    delays = [llm_client._delay_for(1, asked) for _ in range(300)]
    assert min(delays) >= asked, min(delays)
    assert max(delays) <= asked * 1.2 + 0.001
    assert len(set(round(d, 4) for d in delays)) > 10, "разброс всё-таки нужен"


def test_database_trouble_does_not_block_the_call(db, monkeypatch):
    """Занятая база — не повод не сходить к модели.

    Прежде ошибка SQLite летела наверх как обычное исключение, и бот
    списывал за неё платную попытку владельца.
    """
    calls = []

    class Boom:
        def __enter__(self):
            raise RuntimeError("database is locked")

        def __exit__(self, *a):
            return False

    monkeypatch.setattr(rate_limit, "_ensure_tables", lambda db_path=None: None)
    monkeypatch.setattr(storage, "get_conn", lambda *a, **k: Boom())
    assert rate_limit.reserve_slot("m", rpm=14) == 0.0
    assert calls == []


def test_api_outages_are_bounded(db, images_dir, monkeypatch):
    """Срывы провайдера норму не расходуют, но и бесконечными не бывают.

    При затяжном сбое кнопку можно было бы жать без счёта, а частичный
    сбой — переписывание прошло, оценка не ответила — успевает потратить
    настоящие деньги.
    """
    from src import approval_bot, schedule

    rows = _seed_digest(8)
    monkeypatch.setattr(
        approval_bot, "_generate_from_digest",
        lambda cfg, row: (_ for _ in ()).throw(
            llm_client.LLMUnavailableError("молчит")))
    cfg = make_cfg()
    cfg.max_preparation_attempts_per_day = 2      # значит потолок срывов 4
    dp = approval_bot.build_dispatcher(cfg)
    pick = handlers_of(dp)["pick"]

    answers = []
    for row in rows:
        call = FakeCallback(f"pick:{row['id']}")
        asyncio.run(pick(call))
        answers.append(call.message.answers[-1] if call.message.answers else "")

    day = schedule.day_bounds_utc(cfg)
    assert storage.count_api_errors_today(*day) == 4, "срывы должны упереться в потолок"
    attempts, _ = storage.count_selections_today(*day)
    assert attempts == 0, "норма подготовок при этом не тронута"
    assert any("бесконечно" in a for a in answers), answers
