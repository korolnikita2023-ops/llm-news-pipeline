"""Ограничитель обращений к модели: темп и одновременность.

Зачем. Бесплатный тариф Gemini даёт **15 запросов в минуту на модель** —
это не догадка, это цифра из ответа самого Google:

    quotaId: GenerateRequestsPerMinutePerProjectPerModel-FreeTier
    quotaValue: 15   model: gemini-3.5-flash-lite   retryDelay: 18s

Наш конвейер оценивает новости подряд, по ~0,8 с на вызов, и реальный
прогон 28.08 сделал 13 обращений за 11 секунд. Это темп около 70 запросов
в минуту: в потолок мы упираемся на пятнадцатом вызове, через двенадцать
секунд после старта. Замер это подтвердил — 429 пришёл ровно там.

Суточный объём при этом крошечный (40–115 обращений), то есть тариф нам
подходит целиком, кроме плотности. Её и выравниваем.

Почему через базу, а не через обычный семафор в памяти. Сбор новостей и
бот-редактор — РАЗНЫЕ ПРОЦЕССЫ. Ограничитель внутри одного из них не знает
о вызовах другого, а квота у Google общая на проект. Наложись прогон
конвейера на подготовку поста по кнопке — и оба честно уложились бы в свой
лимит, а Google увидел бы сумму. База у процессов уже общая, в режиме WAL,
с таймаутом ожидания блокировки — она и координирует.

Ограничитель НЕ ускоряет и не замедляет ничего другого: он лишь разводит
обращения во времени. Промпты, оценки, пороги и отбор не затрагиваются.
"""
from __future__ import annotations

import os
import random
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Optional

from src import storage

# Предел берём с запасом от объявленных Google пятнадцати: при счёте на
# стороне сервиса секунды округляются иначе, чем у нас, и упереться ровно
# в потолок — значит время от времени получать 429 на ровном месте.
DEFAULT_RPM = 14

# Окно, за которое считаются обращения. У Google — «в минуту».
WINDOW_S = 60.0

# Сколько ждать освобождения слота, прежде чем сдаться. Ограничитель не
# должен превращаться в бесконечное ожидание: лучше отдать управление
# наверх, там есть свои таймауты и понятные сообщения.
MAX_WAIT_S = 180.0

# Аренда на время одного вызова. Считается от таймаута самого вызова (90 с)
# с большим запасом: если аренда истечёт раньше, чем закончится вызов, место
# перехватит другой процесс — и к модели пойдут два обращения разом, ровно
# то, чего эта очередь и не должна допускать.
#
# Ожидание темпа в аренду НЕ входит: слот в минутном окне занимается ДО
# захвата очереди. Иначе процесс, ждущий полминуты своей очереди по темпу,
# всё это время держал бы второй процесс запертым без всякой пользы.
LEASE_S = 300.0

_OWNER = f"{os.getpid()}"


_SCHEMA = (
    "CREATE TABLE IF NOT EXISTS llm_calls ("
    " id INTEGER PRIMARY KEY AUTOINCREMENT, model TEXT NOT NULL, ts REAL NOT NULL)",
    "CREATE INDEX IF NOT EXISTS idx_llm_calls_model_ts ON llm_calls(model, ts)",
    "CREATE TABLE IF NOT EXISTS llm_lock ("
    " model TEXT PRIMARY KEY, owner TEXT, expires REAL NOT NULL DEFAULT 0)",
)


def _ensure_tables(db_path: Optional[Path] = None) -> None:
    """Создаёт свои таблицы, если их ещё нет.

    Ограничитель не должен зависеть от того, вызвал ли кто-то до него
    init_db. Он подключается к любой базе — временной в тестах, боевой в
    работе — и приводит её в пригодный вид сам.
    """
    with storage.get_conn(db_path) as conn:
        for statement in _SCHEMA:
            conn.execute(statement)


def _now() -> float:
    return time.time()


def _sleep(seconds: float) -> None:
    """Вынесено отдельно, чтобы тесты не ждали реальные секунды."""
    time.sleep(seconds)


# ---------------------------------------------------------------------------
# Темп: не больше N обращений к модели за минуту
# ---------------------------------------------------------------------------


def reserve_slot(model: str, rpm: int = DEFAULT_RPM,
                 db_path: Optional[Path] = None) -> float:
    """Ждёт свободного места в минутном окне и занимает его.

    Возвращает, сколько секунд пришлось ждать (0.0 — ждать не пришлось).

    Место занимается ДО обращения к модели, а не после. Отклонённый по
    квоте запрос Google всё равно считает обращением, поэтому «занял и не
    попал» — правильная сторона ошибки: мы недоиспользуем квоту вместо
    того, чтобы её превысить.
    """
    if rpm <= 0:
        return 0.0

    try:
        _ensure_tables(db_path)
    except Exception as exc:
        # Ограничитель не имеет права быть причиной, по которой вызов не
        # состоялся. Не смог посчитать темп — говорим вслух и пропускаем:
        # дальше сработает retry по ответу Google.
        print(f"[rate_limit] не удалось подготовить учёт темпа ({exc}) — "
              f"иду без ограничения")
        return 0.0

    waited = 0.0
    deadline = _now() + MAX_WAIT_S
    while True:
        now = _now()
        try:
            with storage.get_conn(db_path) as conn:
                conn.execute("DELETE FROM llm_calls WHERE ts < ?", (now - WINDOW_S,))
                row = conn.execute(
                    "SELECT COUNT(*), MIN(ts) FROM llm_calls WHERE model=? AND ts >= ?",
                    (model, now - WINDOW_S),
                ).fetchone()
                used, oldest = int(row[0]), row[1]
                if used < rpm:
                    conn.execute("INSERT INTO llm_calls (model, ts) VALUES (?, ?)",
                                 (model, now))
                    return waited
        except Exception as exc:
            # База занята дольше своего таймаута — редкий, но возможный
            # случай. Ограничитель обещал не быть причиной несостоявшегося
            # вызова, и здесь это обещание тоже действует: говорим вслух и
            # пропускаем. Потолок дальше держит retry по ответу Google.
            print(f"[rate_limit] учёт темпа недоступен ({exc}) — иду без ограничения")
            return waited

        # Место освободится, когда самое старое обращение выпадет из окна.
        free_in = max((oldest or now) + WINDOW_S - now, 0.0) + 0.25
        if now + free_in > deadline:
            print(f"[rate_limit] {model}: место в минутном окне не освободилось "
                  f"за {MAX_WAIT_S:.0f}с — пропускаю ограничитель, дальше решает "
                  f"retry по ответу Google")
            return waited
        print(f"[rate_limit] {model}: {used}/{rpm} за минуту — жду {free_in:.1f}с")
        _sleep(free_in)
        waited += free_in


# ---------------------------------------------------------------------------
# Одновременность: один вызов модели в единицу времени
# ---------------------------------------------------------------------------


@contextmanager
def single_flight(model: str, db_path: Optional[Path] = None):
    """Пропускает к модели по одному вызову за раз — между процессами тоже.

    Нужен ровно для одного случая: прогон конвейера идёт одновременно с
    подготовкой поста по кнопке владельца. Каждый сам по себе укладывается
    в темп, а Google видит их сумму.

    Аренда с истечением: процесс, умерший с захваченным местом, освободит
    его через LEASE_S, а не заблокирует работу навсегда.
    """
    token = f"{_OWNER}:{random.random():.9f}"
    acquired = False
    deadline = _now() + MAX_WAIT_S
    try:
        _ensure_tables(db_path)
    except Exception as exc:
        print(f"[rate_limit] очередь недоступна ({exc}) — иду без неё")
        yield
        return
    try:
        while _now() < deadline:
            now = _now()
            try:
                with storage.get_conn(db_path) as conn:
                    conn.execute(
                        "INSERT INTO llm_lock (model, owner, expires) "
                        "VALUES (?, NULL, 0) ON CONFLICT(model) DO NOTHING",
                        (model,),
                    )
                    taken = conn.execute(
                        "UPDATE llm_lock SET owner=?, expires=? "
                        "WHERE model=? AND (owner IS NULL OR expires < ?)",
                        (token, now + LEASE_S, model, now),
                    ).rowcount
            except Exception as exc:
                print(f"[rate_limit] очередь не отвечает ({exc}) — иду без неё")
                break
            if taken:
                acquired = True
                break
            _sleep(0.2)
        else:
            print(f"[rate_limit] {model}: очередь не освободилась за "
                  f"{MAX_WAIT_S:.0f}с — иду без очереди, чтобы не встать совсем")
        yield
    finally:
        if acquired:
            try:
                with storage.get_conn(db_path) as conn:
                    conn.execute("UPDATE llm_lock SET owner=NULL, expires=0 "
                                 "WHERE model=? AND owner=?", (model, token))
            except Exception as exc:
                # Место освободится само по истечении аренды.
                print(f"[rate_limit] не смог отпустить очередь ({exc}) — "
                      f"освободится через {LEASE_S:.0f}с")


def calls_in_window(model: str, db_path: Optional[Path] = None) -> int:
    """Сколько обращений к модели укладывается в текущее минутное окно."""
    _ensure_tables(db_path)
    with storage.get_conn(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM llm_calls WHERE model=? AND ts >= ?",
            (model, _now() - WINDOW_S),
        ).fetchone()[0]
