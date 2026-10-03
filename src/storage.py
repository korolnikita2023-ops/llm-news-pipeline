"""SQLite-хранилище. sqlite3 — стандартная библиотека,
отдельно не устанавливается. Схема:

    raw_items(url_hash PK, source, title, body, published_at, fetched_at, lang)
    events(event_id PK, centroid_embedding, first_seen, best_item, n_sources)
    posts(post_id PK, event_id FK, draft_ru, format, status, scores, erid_needed, created_at)
    metrics(metric_id PK, post_id FK, views, forwards, ts)
    cost_log(id PK, ts, stage, provider, model, usd_cost)  — доп. таблица,
        нужна для cost-tracker'а (NO SILENT FAILURES: дневной лимит
        расходов LLM не должен превышаться тихо).
    admins(user_id PK, role, label, added_by, added_at)  — права доступа к
        боту; работа с таблицей — в src/roles.py.
    meta(key PK, value)  — служебные отметки (перенос админов из .env).

MVP не реализует L2-кластеризацию (embedding-центроиды) — events заполняется
упрощённо, 1 событие = 1 принятая новость, n_sources=1, centroid_embedding=NULL.
Настоящая кластеризация мультиисточников не реализована.
"""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator

from config import DB_PATH, ensure_dirs

SCHEMA = """
CREATE TABLE IF NOT EXISTS raw_items (
    url_hash TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    title TEXT NOT NULL,
    body TEXT,
    url TEXT,
    published_at TEXT,
    fetched_at TEXT NOT NULL,
    lang TEXT,
    category TEXT,
    sample INTEGER DEFAULT 0,
    source_kind TEXT,
    source_role TEXT
);

CREATE TABLE IF NOT EXISTS events (
    event_id TEXT PRIMARY KEY,
    centroid_embedding BLOB,
    first_seen TEXT NOT NULL,
    best_item TEXT NOT NULL,
    n_sources INTEGER DEFAULT 1,
    FOREIGN KEY (best_item) REFERENCES raw_items(url_hash)
);

CREATE TABLE IF NOT EXISTS posts (
    post_id TEXT PRIMARY KEY,
    event_id TEXT,
    draft_ru TEXT NOT NULL,
    format TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'candidate',
    scores TEXT,
    erid_needed INTEGER DEFAULT 0,
    manual_only INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    FOREIGN KEY (event_id) REFERENCES events(event_id)
);

CREATE TABLE IF NOT EXISTS metrics (
    metric_id INTEGER PRIMARY KEY AUTOINCREMENT,
    post_id TEXT NOT NULL,
    views INTEGER DEFAULT 0,
    forwards INTEGER DEFAULT 0,
    ts TEXT NOT NULL,
    FOREIGN KEY (post_id) REFERENCES posts(post_id)
);

-- Все новости, которые конвейер УЖЕ ВИДЕЛ, включая отсеянные.
-- raw_items хранит только прошедшие фильтр релевантности, поэтому
-- отсеянные оценивались платно снова и снова: в боевой базе накопилось
-- 676 вызовов relevance на 18 уникальных сохранённых новостей.
CREATE TABLE IF NOT EXISTS seen_items (
    url_hash TEXT PRIMARY KEY,
    first_seen TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS cost_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    stage TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT,
    usd_cost REAL NOT NULL DEFAULT 0
);

-- Права доступа к боту. До этого список админов жил в .env и читался ОДИН
-- раз при старте процесса: добавить человека можно было только правкой
-- файла с перезапуском, а роли не различались вообще — любой, кто попал в
-- список, мог всё. Теперь права здесь, роль проверяется на каждом апдейте.
-- role: 'owner' (владелец, управляет составом) | 'admin' (разбирает очередь).
CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY,
    role TEXT NOT NULL,
    label TEXT,
    added_by INTEGER,
    added_at TEXT NOT NULL
);

-- Короткий список дня: то, что конвейер отобрал и оценил, но НЕ переписывал.
-- Дорогая генерация (rewrite + карточка + редактор) запускается только для
-- того, что владелец выбрал сам, — иначе платим за десять постов, из которых
-- нужны два. Строки живут сутки и архивируются: невыбранное на следующий
-- день не переносится, новости к утру устаревают.
CREATE TABLE IF NOT EXISTS digest_items (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    url_hash TEXT NOT NULL,
    source TEXT,
    source_url TEXT,
    title TEXT NOT NULL,
    summary TEXT,
    score INTEGER DEFAULT 0,
    category TEXT,
    signals TEXT,            -- за что отобран: маркеры дешёвого отбора
    relevance TEXT,          -- полный разбор оценки (JSON), чтобы не восстанавливать
    title_src TEXT,          -- заголовок на языке источника, до перевода
    created_at TEXT NOT NULL,
    chosen INTEGER DEFAULT 0,
    chosen_at TEXT,          -- когда владелец отправил в генерацию
    archived INTEGER DEFAULT 0,
    post_id TEXT             -- заполняется после генерации
);

-- Журнал платных попыток подготовки поста. Одна строка — одно обращение к
-- дорогой стадии, независимо от исхода.
--
-- Отдельная таблица, а не счётчик в digest_items, по простой причине:
-- повторный выбор той же истории переписал бы отметку времени в её строке,
-- и вторая оплаченная попытка не была бы видна вовсе. Потолок попыток
-- обходился бы повтором одной и той же неудачной новости.
CREATE TABLE IF NOT EXISTS selection_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest_item_id INTEGER NOT NULL,
    url_hash TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,        -- когда попытка завершилась (успехом или отказом)
    post_id TEXT,            -- заполнен только у попытки, давшей пост
    outcome TEXT             -- ok | rejected | error; NULL = идёт прямо сейчас
);

CREATE INDEX IF NOT EXISTS idx_attempts_started ON selection_attempts(started_at);
CREATE INDEX IF NOT EXISTS idx_attempts_finished ON selection_attempts(finished_at);

-- Учёт обращений к модели: сколько их было за последнюю минуту.
-- Таблица общая для ВСЕХ процессов — конвейер и бот живут отдельно, а квота
-- у Google одна на проект. Ограничитель внутри одного процесса не увидел бы
-- вызовов другого, и наложение прогона на подготовку поста давало бы
-- суммарный всплеск. Старые строки удаляются на каждом обращении.
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,
    ts REAL NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_llm_calls_model_ts ON llm_calls(model, ts);


-- Очередь на одновременность: к модели идёт один вызов за раз.
-- Аренда с истечением, чтобы упавший процесс не заблокировал работу.
CREATE TABLE IF NOT EXISTS llm_lock (
    model TEXT PRIMARY KEY,
    owner TEXT,
    expires REAL NOT NULL DEFAULT 0
);

-- Служебные отметки. Пока одна: перенос списка админов из .env выполнен.
-- Без неё удалённый через бота админ воскресал бы при каждом рестарте,
-- пока его ID остаётся в .env.
CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT
);

-- Очередь зеркалирования в сторонние площадки. Telegram — первичный канал,
-- всё здесь — независимый downstream: отказ площадки не влияет на Telegram.
--
-- PRIMARY KEY (post_id, platform) — и есть гарантия «один пост Telegram →
-- максимум один пост в Threads». Не флаг в коде, который можно обойти
-- гонкой двух процессов, а ограничение самой базы.
CREATE TABLE IF NOT EXISTS crossposts (
    post_id TEXT NOT NULL,
    platform TEXT NOT NULL,
    -- pending  — ждёт отправки
    -- sending  — отправка идёт прямо сейчас (захвачено воркером)
    -- done     — опубликовано, remote_post_id заполнен
    -- failed   — временный отказ, вернётся по next_retry_at
    -- dead     — попытки исчерпаны, руками
    -- skipped  — сознательно не публикуем (см. skip_reason)
    -- unknown  — исход отправки не установлен, требуется человек
    status TEXT NOT NULL DEFAULT 'pending',
    attempts INTEGER NOT NULL DEFAULT 0,
    -- Когда работу взяли в отправку. Именно по этому времени, а НЕ по
    -- created_at, определяется брошенная работа: старая работа, взятая
    -- секунду назад, иначе тут же считалась бы протухшей, и второй воркер
    -- отправил бы второй пост поверх первого.
    sending_since TEXT,
    -- Была ли уже попытка публикации контейнера. После обрыва на этом шаге
    -- повторять вслепую нельзя: пост мог уже появиться в ленте.
    publish_attempted_at TEXT,
    -- Текст, которым пост уйдёт. Пишется при первой попытке и больше не
    -- пересчитывается: иначе повтор после сбоя мог бы опубликовать не то,
    -- что готовилось, и сверить постфактум было бы нечем.
    payload_text TEXT,
    link_url TEXT,
    container_id TEXT,
    remote_post_id TEXT,
    skip_reason TEXT,
    last_error TEXT,
    next_retry_at TEXT,
    created_at TEXT NOT NULL,
    published_at TEXT,
    PRIMARY KEY (post_id, platform)
);

CREATE INDEX IF NOT EXISTS idx_crossposts_due
    ON crossposts(platform, status, next_retry_at);

-- Здоровье внешней интеграции. Секретов здесь нет и быть не должно:
-- токен живёт только в .env.
CREATE TABLE IF NOT EXISTS integration_state (
    platform TEXT PRIMARY KEY,
    -- Момент включения. Раньше него не зеркалится ничто: включение
    -- интеграции не должно вывалить в ленту всё, что канал публиковал
    -- прежде.
    enabled_at TEXT,
    last_probe_at TEXT,
    token_expires_at TEXT,
    token_refreshed_at TEXT,
    degraded INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    last_notified_at TEXT,
    updated_at TEXT
);

"""


@contextmanager
def get_conn(db_path: Path | None = None) -> Iterator[sqlite3.Connection]:
    ensure_dirs()
    # timeout: конвейер (main.py) и бот работают с одной базой одновременно.
    # По умолчанию sqlite ждёт освобождения блокировки 5 секунд и падает с
    # "database is locked" — при записи пачки постов этого мало.
    conn = sqlite3.connect(str(db_path or DB_PATH), timeout=30)
    # WAL: читатели не блокируют писателя и наоборот — бот может читать
    # очередь, пока конвейер пишет новые посты.
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
    except sqlite3.Error:
        pass  # на сетевых ФС WAL может быть недоступен — это не повод падать
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


# Колонки, добавленные после того, как боевая база уже жила. CREATE TABLE
# IF NOT EXISTS их не добавит — существующая таблица останется старой,
# поэтому догоняем ALTER'ом. Только добавление и только с NULL по умолчанию:
# старые строки остаются на месте, откат на предыдущий коммит кода читает
# такую базу без единой правки.
_ADDED_COLUMNS = {
    "raw_items": {
        "source_kind": "TEXT",
        "source_role": "TEXT",
    },
    # Расписание живёт в самом посте, отдельной таблицы очереди не нужно.
    # publish_at ВСЕГДА в UTC: слот считается в местной зоне владельца, но
    # сравнивать строки с разными смещениями нельзя.
    "posts": {
        "publish_at": "TEXT",
        "slot": "TEXT",
        # Кто именно забраковал пост. Один статус 'rejected' означал и
        # «владелец нажал ❌», и «не прошло юридический гейт», и «слабая
        # редакторская оценка» — по экрану статуса различить было нельзя.
        "reject_reason": "TEXT",
        # Заголовок карточки. В расписании владелец видел первую строку
        # текста, а не название материала.
        "headline": "TEXT",
        # Канал, для которого пост сделан. До боевого запуска это была
        # песочница, и такие посты не должны мешать готовить те же истории
        # заново — уже для боевого канала. Пустое значение (записи старше
        # этой колонки) считается текущим каналом, чтобы ничего не пропало
        # между выкладкой кода и переносом старых строк.
        "channel_id": "TEXT",
    },
    "digest_items": {
        "chosen_at": "TEXT",
        "relevance": "TEXT",
        "title_src": "TEXT",
    },
    "selection_attempts": {
        "finished_at": "TEXT",
    },
}


def _migrate(conn) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        for name, decl in columns.items():
            if name in have:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
                print(f"[storage] миграция: {table}.{name} добавлена")
            except sqlite3.OperationalError as exc:
                # Проверка наличия колонки и ALTER — два отдельных запроса, и
                # между ними успевает вклиниться второй процесс: бот и
                # конвейер стартуют независимо и оба зовут init_db. Тогда
                # второй получает «duplicate column name» на колонке, которая
                # уже есть. Это не ошибка миграции, это гонка двух стартов.
                if "duplicate column" not in str(exc).lower():
                    raise
                print(f"[storage] миграция: {table}.{name} уже добавлена другим процессом")

    # Индексы, зависящие от добавленных колонок, создаются ПОСЛЕ миграции:
    # на чистой базе колонки ещё нет в CREATE TABLE, и индекс в базовой
    # схеме падал бы с «no such column».
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_posts_event_channel "
        "ON posts(event_id, channel_id, status)"
    )


def init_db(db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def insert_raw_item(item_dict: Dict[str, Any], db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO raw_items (url_hash, source, title, body, url, published_at, fetched_at,
                                   lang, category, sample, source_kind, source_role)
            VALUES (:url_hash, :source, :title, :body, :url, :published_at, :fetched_at,
                    :lang, :category, :sample, :source_kind, :source_role)
            ON CONFLICT(url_hash) DO NOTHING
            """,
            {
                "source_kind": "", "source_role": "",   # значения по умолчанию
                **item_dict,
                "sample": int(item_dict.get("sample", False)),
            },
        )


def insert_event(event_id: str, best_item_hash: str, n_sources: int = 1, db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            """
            INSERT INTO events (event_id, centroid_embedding, first_seen, best_item, n_sources)
            VALUES (?, NULL, ?, ?, ?)
            ON CONFLICT(event_id) DO NOTHING
            """,
            (event_id, now_iso(), best_item_hash, n_sources),
        )


# Кто забраковал пост. Хранится в posts.reject_reason.
# Статусы, при которых пост считается живым: по такому сюжету второй раз
# работать не надо.
ACTIVE_STATUSES = "('candidate','approved','publishing','published','hold','scheduled')"

REJECT_BY_USER = "user"
REJECT_BY_LEGAL = "legal_gate"
REJECT_BY_EDITOR = "editorial"
REJECT_BY_ERROR = "error"

# Исход попытки, который НЕ расходует суточный лимит подготовок. Провайдер
# не ответил — модель ничего не решала, работы не было, и наказывать за это
# владельца нечем. Денежные лимиты (прогон, сутки, месяц) при этом остаются
# на месте: они и защищают бюджет.
ATTEMPT_API_ERROR = "api_error"

# Исход «работы не было»: по сюжету уже есть пост, к модели не обращались,
# деньги не тратили. В суточный счёт подготовок такая попытка не идёт —
# наказывать владельца за нажатие, которое ничего не стоило, не за что.
ATTEMPT_NO_WORK = "no_work"

# Человеческие названия для экрана статуса: внутренние значения владельцу
# показывать незачем.
REJECT_LABELS = {
    REJECT_BY_USER: "отклонил я",
    REJECT_BY_LEGAL: "не прошло юридический гейт",
    REJECT_BY_EDITOR: "слабая редакторская оценка",
    REJECT_BY_ERROR: "сбой при подготовке",
}


def insert_post(
    post_id: str,
    event_id: str,
    draft_ru: str,
    fmt: str,
    status: str,
    scores: Dict[str, Any],
    manual_only: bool = False,
    erid_needed: bool = False,
    reject_reason: str | None = None,
    headline: str | None = None,
    channel_id: str | None = None,
    supersede_auto_reject: bool = False,
    db_path: Path | None = None,
) -> bool:
    """Записывает пост. Возвращает True, если запись действительно легла.

    Идентификатор черновика детерминирован (url_hash + номер попытки), и
    повторная подготовка той же истории даёт ТОТ ЖЕ post_id. Защита от
    перезаписи существует не зря — она бережёт решения владельца и уже
    опубликованное, — но у неё была цена: повторная подготовка не могла
    записать ни новый отказ, ни новый УСПЕШНЫЙ пост. Он молча пропадал, а
    бот, глядя на старую запись, говорил владельцу «что-то сорвалось».

    `supersede_auto_reject` разрешает заменить только АВТОМАТИЧЕСКИЙ отказ
    (гейт, редакторская оценка, сбой подготовки). Отказ владельца, нажавшего
    ❌, а также published, hold, scheduled и publishing не затрагиваются
    никогда: это его решения, и переписывать их нельзя.

    Возвращаемое значение важно: нулевой результат означает, что запись НЕ
    легла, и считать такой исход успехом нельзя.
    """
    # Базовое условие не меняется ни на йоту — оно и раньше берегло решения.
    # Разрешение добавляется РЯДОМ, отдельным слагаемым, и только на один
    # случай: запись со статусом 'rejected', забракованная автоматикой.
    # Ни candidate, ни approved, ни тем более published/hold/scheduled в это
    # разрешение не входят: одобренный владельцем пост перезаписывать
    # нельзя, даже если он сам попросил подготовить историю ещё раз.
    # 'approved' — это нажатое владельцем ✅: пост одобрен и ждёт публикации.
    # В защите его не было, и повторный прогон по тому же сюжету мог молча
    # подменить одобренный текст. Добавлено сюда же: перезаписывать решения
    # человека нельзя ни при каких условиях.
    base = ("posts.status NOT IN "
            "('published','rejected','hold','publishing','scheduled','approved')")
    where = base
    if supersede_auto_reject:
        where = (f"{base} OR (posts.status = 'rejected' "
                 f"AND COALESCE(posts.reject_reason,'') <> '{REJECT_BY_USER}')")

    # Обновляется ВЕСЬ черновик, а не только статус. Иначе повторная
    # подготовка сделала бы кандидатом СТАРЫЙ текст — тот самый, который
    # раньше не пропустил юридический гейт, — приклеив к нему новый статус
    # и свежие оценки. Хуже способа получить в канале забракованный текст
    # придумать трудно.
    with get_conn(db_path) as conn:
        cur = conn.execute(
            f"""
            INSERT INTO posts (post_id, event_id, draft_ru, format, status, scores,
                               erid_needed, manual_only, reject_reason, headline,
                               channel_id, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(post_id) DO UPDATE SET
                draft_ru=excluded.draft_ru, format=excluded.format,
                status=excluded.status, scores=excluded.scores,
                erid_needed=excluded.erid_needed, manual_only=excluded.manual_only,
                reject_reason=excluded.reject_reason, headline=excluded.headline,
                channel_id=excluded.channel_id, created_at=excluded.created_at
            WHERE {where}
            """,
            (post_id, event_id, draft_ru, fmt, status, json.dumps(scores, ensure_ascii=False),
             int(erid_needed), int(manual_only), reject_reason, headline,
             channel_id, now_iso()),
        )
        return cur.rowcount > 0


def update_post_status(post_id: str, status: str, reject_reason: str | None = None,
                       db_path: Path | None = None) -> None:
    """Меняет статус. Для отказа записывает и его причину.

    Причина нужна, потому что 'rejected' сам по себе не отличает решение
    владельца от отказа гейта, — на экране статуса это выглядело одинаково.
    """
    with get_conn(db_path) as conn:
        if reject_reason is None:
            conn.execute("UPDATE posts SET status=? WHERE post_id=?", (status, post_id))
        else:
            conn.execute("UPDATE posts SET status=?, reject_reason=? WHERE post_id=?",
                         (status, reject_reason, post_id))


def insert_metric(post_id: str, views: int, forwards: int, db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO metrics (post_id, views, forwards, ts) VALUES (?, ?, ?, ?)",
            (post_id, views, forwards, now_iso()),
        )


def log_cost(stage: str, provider: str, model: str, usd_cost: float, db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO cost_log (ts, stage, provider, model, usd_cost) VALUES (?, ?, ?, ?, ?)",
            (now_iso(), stage, provider, model, usd_cost),
        )


def get_today_cost(db_path: Path | None = None) -> float:
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(usd_cost), 0) AS total FROM cost_log WHERE ts LIKE ?",
            (f"{today}%",),
        ).fetchone()
        return float(row["total"]) if row else 0.0


def has_active_post_for_event(event_id: str, channel_id: str | None = None,
                              db_path: Path | None = None) -> bool:
    """Есть ли уже живой пост по этому сюжету.

    Один исходный материал не должен давать двух кандидатов. В очереди
    владельца такое уже случилось: попугаи и Локерби пришли дважды —
    прогон 26-го и прогон 27-го числа сделали по посту из одной и той же
    статьи BBC. Журнал виденного это закрывает, но проверка на самой
    записи надёжнее: она не зависит от того, отработал ли журнал.
    """
    return active_post_for_event(event_id, channel_id, db_path) is not None


# Условие «этот пост относится к текущему каналу». Пустое значение —
# записи старше колонки: считаем их своими, чтобы между выкладкой кода и
# переносом старых строк ничего не потерялось из очереди.
# Пустая строка — то же самое, что отсутствие значения. Иначе запись с ''
# исчезла бы из очереди, а опубликованная — вернулась бы в пул.
_SAME_CHANNEL = "(COALESCE(posts.channel_id,'') IN ('', ?))"


def active_post_for_event(event_id: str, channel_id: str | None = None,
                          db_path: Path | None = None) -> dict | None:
    """Живой пост по сюжету В ЭТОМ КАНАЛЕ — сам, а не просто «есть ли он».

    Канал важен. До боевого запуска посты делались для песочницы, и они не
    должны мешать подготовить те же истории заново — уже для настоящего
    канала. При этом настоящая публикация в боевой канал по-прежнему
    закрывает сюжет: второй раз одно и то же не предлагаем.

    Вызывающему нужно не только знать, что работать не надо, но и сказать
    владельцу человеческим языком, ЧТО именно уже есть.
    """
    sql = "SELECT post_id, status FROM posts WHERE event_id=? AND status IN " + ACTIVE_STATUSES
    params: list = [event_id]
    if channel_id is not None:
        sql += " AND " + _SAME_CHANNEL
        params.append(channel_id)
    sql += " ORDER BY created_at DESC LIMIT 1"
    with get_conn(db_path) as conn:
        row = conn.execute(sql, params).fetchone()
    return dict(row) if row else None


def mark_seen(url_hashes, db_path: Path | None = None) -> None:
    """Отмечает новости как виденные — ДО платной оценки релевантности."""
    now = now_iso()
    with get_conn(db_path) as conn:
        conn.executemany(
            "INSERT INTO seen_items (url_hash, first_seen) VALUES (?, ?) "
            "ON CONFLICT(url_hash) DO NOTHING",
            [(h, now) for h in url_hashes],
        )


def purge_old_seen(days: int = 30, db_path: Path | None = None) -> int:
    """Чистит журнал виденного: новость старше месяца всё равно не всплывёт
    в лентах, а таблица не должна расти бесконечно."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    with get_conn(db_path) as conn:
        return conn.execute(
            "DELETE FROM seen_items WHERE first_seen < ?", (cutoff,)
        ).rowcount


def known_url_hashes(db_path: Path | None = None) -> set:
    """Хэши новостей, которые конвейер уже обрабатывал.

    RSS отдаёт одни и те же записи часами, а дедуп работал только внутри
    одного батча — каждый прогон заново платно оценивал те же новости
    (в боевой базе: 676 вызовов relevance на 18 уникальных новостей) и мог
    снова создать кандидата по уже разобранному сюжету.
    """
    with get_conn(db_path) as conn:
        try:
            return {
                r[0]
                for r in conn.execute(
                    "SELECT url_hash FROM seen_items "
                    "UNION SELECT url_hash FROM raw_items"
                )
            }
        except sqlite3.OperationalError:
            # Таблиц ещё нет — база не инициализирована (например, прогон
            # разведки на чистой машине). Это «ничего не видели», а не сбой.
            return set()


def count_candidates_created_today(db_path: Path | None = None) -> int:
    """Сколько кандидатов создано за сегодня — для суточного потолка."""
    today = datetime.now(timezone.utc).date().isoformat()
    with get_conn(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM posts WHERE created_at LIKE ? "
            "AND status NOT IN ('rejected')",
            (f"{today}%",),
        ).fetchone()[0]


def get_cost_summary(db_path: Path | None = None) -> Dict[str, Any]:
    """Сводка расходов на модель по нашему собственному учёту.

    Почему считаем сами: Gemini API не отдаёт ни баланс, ни потраченную
    сумму — обычному ключу из AI Studio такие эндпоинты недоступны (проверено
    по документации на 27.08.2026). Единственный надёжный
    источник — `usage_metadata` в ответе на каждый вызов: токены умножаются
    на тариф и пишутся в cost_log. Здесь эти записи агрегируются.
    """
    today = datetime.now(timezone.utc).date().isoformat()
    month = today[:7]
    with get_conn(db_path) as conn:
        def total(where: str, param) -> float:
            row = conn.execute(
                f"SELECT COALESCE(SUM(usd_cost), 0) AS s FROM cost_log WHERE {where}",
                (param,),
            ).fetchone()
            return float(row["s"] or 0)

        by_stage = {
            r["stage"]: (float(r["s"]), int(r["n"]))
            for r in conn.execute(
                "SELECT stage, SUM(usd_cost) AS s, COUNT(*) AS n FROM cost_log "
                "WHERE ts LIKE ? GROUP BY stage",
                (f"{month}%",),
            )
        }
        calls_today = conn.execute(
            "SELECT COUNT(*) AS n FROM cost_log WHERE ts LIKE ?", (f"{today}%",)
        ).fetchone()["n"]
        first = conn.execute("SELECT MIN(ts) AS t FROM cost_log").fetchone()["t"]
        models = [
            r["model"]
            for r in conn.execute(
                "SELECT DISTINCT model FROM cost_log WHERE model IS NOT NULL AND model<>'-'"
            )
        ]
        all_time = conn.execute(
            "SELECT COALESCE(SUM(usd_cost), 0) AS s FROM cost_log"
        ).fetchone()["s"]
        # Суммы считаем ВНУТРИ блока: соединение закрывается на выходе из
        # контекста, и обращение к нему снаружи падало с «closed database».
        today_sum = total("ts LIKE ?", f"{today}%")
        month_sum = total("ts LIKE ?", f"{month}%")

    return {
        "today": today_sum,
        "month": month_sum,
        "all_time": float(all_time or 0),
        "calls_today": int(calls_today or 0),
        "by_stage": by_stage,
        "since": first,
        "models": models,
    }


def count_posts_by_status(db_path: Path | None = None) -> Dict[str, int]:
    with get_conn(db_path) as conn:
        rows = conn.execute("SELECT status, COUNT(*) AS n FROM posts GROUP BY status").fetchall()
        return {row["status"]: row["n"] for row in rows}


def count_pending_posts(db_path: Path | None = None) -> int:
    """Сколько постов ждут решения человека.

    Не то же самое, что суточный потолок: тот считает созданное за сегодня,
    а этот — накопившееся. Очередь, которую владелец не успевает разбирать,
    не нужно пополнять новыми дорогими постами.
    """
    with get_conn(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM posts WHERE status IN ('candidate','approved')"
        ).fetchone()[0]


def get_month_cost(db_path: Path | None = None) -> float:
    """Расходы на модель за текущий календарный месяц.

    Бюджет проекта задан в месяц, а не в сутки: дневной лимит — только
    аварийный предохранитель. Считается по тем же записям cost_log, что и
    дневной; второго независимого учёта расходов в проекте нет.
    """
    month = datetime.now(timezone.utc).date().isoformat()[:7]
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT COALESCE(SUM(usd_cost), 0) AS total FROM cost_log WHERE ts LIKE ?",
            (f"{month}%",),
        ).fetchone()
        return float(row["total"]) if row else 0.0


# ---------------------------------------------------------------------------
# Короткий список дня (дайджест)
# ---------------------------------------------------------------------------


def backfill_selection_attempts(db_path: Path | None = None) -> int:
    """Переносит прежние выборы в журнал попыток. Выполняется один раз.

    До появления журнала выбор фиксировался прямо в строке дайджеста. На
    обновлённой базе журнал был бы пуст, суточные счётчики обнулились бы,
    и в день обновления оба потолка обошлись бы сами собой.

    Идемпотентность — по отметке в `meta`: повторный запуск ничего не
    задваивает.
    """
    with get_conn(db_path) as conn:
        done = conn.execute(
            "SELECT 1 FROM meta WHERE key='attempts_backfilled'"
        ).fetchone()
        if done:
            return 0
        moved = conn.execute(
            "INSERT INTO selection_attempts "
            "  (digest_item_id, url_hash, started_at, finished_at, post_id, outcome) "
            "SELECT id, url_hash, chosen_at, chosen_at, post_id, "
            "       CASE WHEN post_id IS NULL THEN 'rejected' ELSE 'ok' END "
            "FROM digest_items WHERE chosen_at IS NOT NULL",
        ).rowcount
        conn.execute("INSERT OR REPLACE INTO meta (key, value) VALUES "
                     "('attempts_backfilled', ?)", (now_iso(),))
    if moved:
        print(f"[storage] перенесено в журнал попыток: {moved} прежних выбор(ов)")
    return moved


def save_digest(run_id: str, rows: list, db_path: Path | None = None) -> int:
    """Добавляет новые истории в пул. Возвращает, сколько добавилось.

    Раньше каждый сбор выбрасывал весь прежний список: было пять историй,
    прошёл сбор — осталась одна, и четыре ещё живые темы исчезали у
    владельца из-под рук. Теперь список — пул: новое добавляется, старое
    остаётся доступным, пока не устареет по сроку или не будет разобрано.

    Повторы не копятся: история, уже лежащая в пуле, второй раз не
    добавляется. Одна и та же новость приходит из лент много прогонов
    подряд, и без этой проверки список забился бы копиями.
    """
    now = now_iso()
    added = 0
    with get_conn(db_path) as conn:
        known = {r[0] for r in conn.execute(
            "SELECT url_hash FROM digest_items WHERE archived=0")}
        fresh = []
        for r in rows:
            # Проверяем и против пула, и против уже отобранных: один сюжет
            # может прийти дважды внутри одной пачки, и тогда в пуле
            # оказались бы две одинаковые строки с двумя кнопками.
            h = r.get("url_hash")
            if h in known:
                continue
            known.add(h)
            fresh.append(r)
        if fresh:
            conn.executemany(
                "INSERT INTO digest_items (run_id, url_hash, source, source_url, title, "
                "summary, score, category, signals, relevance, title_src, created_at) "
                "VALUES (:run_id, :url_hash, :source, :source_url, :title, :summary, "
                ":score, :category, :signals, :relevance, :title_src, :created_at)",
                [{"relevance": None, "title_src": None, **r,
                  "run_id": run_id, "created_at": now} for r in fresh],
            )
            added = len(fresh)
    skipped = len(rows) - added
    print(f"[storage] в пул добавлено {added} истор(ий)"
          + (f", уже были в пуле: {skipped}" if skipped else ""))
    return added


def archive_stale_digest(ttl_hours: int, db_path: Path | None = None) -> int:
    """Помечает архивными строки старше срока жизни.

    Единственный механизм, который не даёт пулу расти бесконечно. Историю
    суточной давности предлагать уже поздно: она либо разобрана, либо
    перестала быть новостью.
    """
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(hours=ttl_hours)).isoformat()
    with get_conn(db_path) as conn:
        return conn.execute(
            "UPDATE digest_items SET archived=1 WHERE archived=0 AND created_at < ?",
            (cutoff,),
        ).rowcount


def digest_pool(channel_id: str | None = None, limit: int | None = None,
                db_path: Path | None = None) -> list:
    """Актуальный пул историй: всё живое, из чего есть что выбрать.

    Из пула выпадает то, чему там не место:

    * взятое в работу (`chosen`) — оно уже готовится или готово;
    * устаревшее по сроку (`archived`) — им занимается archive_stale_digest;
    * то, по чему в ЭТОМ канале уже есть живой пост: кандидат, запас,
      расписание, публикация. Пост, сделанный для песочницы, сюда не
      считается — истории, готовившиеся до боевого запуска, снова доступны;
    * то, что владелец сам отклонил кнопкой ❌. Раз сказал «мимо» — второй
      раз предлагать невежливо.

    Порядок — по оценке, при равной оценке свежее выше. `limit` даёт TOP-N.
    """
    sql = [
        "SELECT d.* FROM digest_items d",
        "WHERE d.archived = 0 AND d.chosen = 0",
        "  AND NOT EXISTS (",
        "        SELECT 1 FROM posts p",
        "        WHERE p.event_id = 'evt_' || d.url_hash",
        "          AND (p.status IN " + ACTIVE_STATUSES,
        "               OR (p.status = 'rejected'",
        "                   AND COALESCE(p.reject_reason,'') = '" + REJECT_BY_USER + "'))",
    ]
    params: list = []
    if channel_id is not None:
        sql.append("          AND COALESCE(p.channel_id,'') IN ('', ?)")
        params.append(channel_id)
    sql.append("  )")
    sql.append("ORDER BY d.score DESC, d.created_at DESC, d.id DESC")
    if limit and limit > 0:
        sql.append("LIMIT ?")
        params.append(limit)

    with get_conn(db_path) as conn:
        rows = conn.execute("\n".join(sql), params).fetchall()
    return [dict(r) for r in rows]





def digest_for_run(run_id: str, db_path: Path | None = None) -> list:
    """Строки дайджеста ИМЕННО этого прогона.

    Рассылке нужен свой список, а не «последний вообще»: прогон, не нашедший
    ничего нового, иначе разослал бы вчерашний список под отчётом сегодняшних
    цифр, а два перекрывающихся прогона перепутали бы результаты.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM digest_items WHERE run_id=? AND archived=0 "
            "ORDER BY score DESC, id ASC", (run_id,),
        ).fetchall()
    return [dict(r) for r in rows]


def digest_item(item_id: int, db_path: Path | None = None) -> dict | None:
    with get_conn(db_path) as conn:
        row = conn.execute("SELECT * FROM digest_items WHERE id=?", (item_id,)).fetchone()
    return dict(row) if row else None


# Отказы, которые start_selection_attempt возвращает вместо id попытки.
TAKEN = "taken"                    # историю уже взяли
POSTS_EXHAUSTED = "posts_done"     # дневная норма готовых постов выбрана
ATTEMPTS_EXHAUSTED = "tries_done"  # платные попытки на сегодня кончились


API_OUTAGE_EXHAUSTED = "api_done"   # срывы по вине провайдера тоже не бесконечны


def start_selection_attempt(item_id: int, day_start_utc: str, day_end_utc: str,
                            max_success: int, max_attempts: int,
                            max_api_errors: int = 0,
                            db_path: Path | None = None):
    """Занимает историю и открывает запись о платной попытке — одной транзакцией.

    Возвращает id попытки (int) либо строку-причину отказа: TAKEN,
    POSTS_EXHAUSTED, ATTEMPTS_EXHAUSTED.

    Оба суточных потолка проверяются ЗДЕСЬ, а не в обработчике. Снаружи
    между «посчитал» и «занял» помещается второе нажатие: два быстрых
    выбора разных историй оба видели один и тот же остаток, оба проходили
    проверку, и владелец получал на пост больше нормы. Внутри одной
    транзакции SQLite такого окна нет.

    Незавершённые попытки (`outcome IS NULL`) считаются будущими успехами:
    они прямо сейчас идут к посту, и слот под них уже занят. Иначе
    резервация не защищала бы от одновременных нажатий вовсе.

    `max_api_errors` — отдельный потолок на попытки, сорванные молчанием
    провайдера. Они не расходуют дневную норму подготовок (модель ничего
    не решала), но и бесконечными быть не должны: при затяжном сбое можно
    было бы жать кнопку без счёта, а частичный сбой — переписывание прошло,
    оценка не ответила — успевает потратить настоящие деньги. Ноль
    отключает этот потолок.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT url_hash FROM digest_items WHERE id=? AND chosen=0 AND archived=0",
            (item_id,),
        ).fetchone()
        if row is None:
            return TAKEN

        # Плейсхолдеры повторяются, а не нумеруются: sqlite3 считает «?1»
        # ИМЕНОВАННЫМ параметром, а с кортежем это уже даёт предупреждение
        # и с Python 3.14 станет ошибкой.
        counts = conn.execute(
            "SELECT "
            " (SELECT COUNT(*) FROM selection_attempts "
            "   WHERE started_at >= ? AND started_at < ? "
            "     AND COALESCE(outcome, '') NOT IN ('api_error', 'no_work')), "
            " (SELECT COUNT(*) FROM selection_attempts "
            "   WHERE post_id IS NOT NULL "
            "     AND COALESCE(finished_at, started_at) >= ? "
            "     AND COALESCE(finished_at, started_at) < ?), "
            " (SELECT COUNT(*) FROM selection_attempts WHERE outcome IS NULL), "
            " (SELECT COUNT(*) FROM selection_attempts "
            "   WHERE outcome = 'api_error' AND started_at >= ? AND started_at < ?)",
            (day_start_utc, day_end_utc, day_start_utc, day_end_utc,
             day_start_utc, day_end_utc),
        ).fetchone()
        attempts, successes = int(counts[0]), int(counts[1])
        in_flight, api_errors = int(counts[2]), int(counts[3])

        if max_success > 0 and successes + in_flight >= max_success:
            return POSTS_EXHAUSTED
        if max_attempts > 0 and attempts >= max_attempts:
            return ATTEMPTS_EXHAUSTED
        if max_api_errors > 0 and api_errors >= max_api_errors:
            return API_OUTAGE_EXHAUSTED

        claimed = conn.execute(
            "UPDATE digest_items SET chosen=1, chosen_at=? "
            "WHERE id=? AND chosen=0 AND archived=0",
            (now_iso(), item_id),
        ).rowcount
        if not claimed:
            return TAKEN
        cur = conn.execute(
            "INSERT INTO selection_attempts (digest_item_id, url_hash, started_at) "
            "VALUES (?, ?, ?)",
            (item_id, row[0], now_iso()),
        )
        return int(cur.lastrowid)


def finish_selection_attempt(attempt_id: int, post_id: str | None, outcome: str,
                             item_id: int | None = None,
                             db_path: Path | None = None) -> None:
    """Закрывает попытку и привязывает пост к истории — одной транзакцией.

    Раздельные записи давали расхождение: пост уже привязан к истории, а
    журнал попыток об этом не знает. Успех не засчитывался, и владелец мог
    подготовить постов больше нормы.

    Строка попытки остаётся в журнале навсегда: за неё заплачено.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE selection_attempts SET post_id=?, outcome=?, finished_at=? WHERE id=?",
            (post_id, outcome, now_iso(), attempt_id),
        )
        if item_id is not None and post_id:
            conn.execute("UPDATE digest_items SET post_id=? WHERE id=?",
                         (post_id, item_id))


def close_orphan_attempts(db_path: Path | None = None) -> int:
    """Закрывает попытки, оборванные вместе с процессом.

    Незавершённая попытка занимает слот готового поста (она считается
    будущим успехом). Пережив рестарт, она заняла бы его навсегда: работа
    мертва, а слот числится за ней. Вызывается при старте бота, когда
    прошлый процесс заведомо не работает.

    Отметка времени попытки сохраняется: деньги за неё потрачены, и в
    суточном счёте расходов она обязана остаться.
    """
    with get_conn(db_path) as conn:
        return conn.execute(
            "UPDATE selection_attempts SET outcome='interrupted', finished_at=? "
            "WHERE outcome IS NULL",
            (now_iso(),),
        ).rowcount


def release_digest_item(item_id: int, db_path: Path | None = None) -> None:
    """Возврат выбора: генерация не удалась, история снова доступна.

    Журнал попыток при этом не трогается: деньги за попытку заплачены,
    и в суточном счёте расходов она обязана остаться.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE digest_items SET chosen=0, post_id=NULL WHERE id=?",
            (item_id,),
        )


def link_digest_post(item_id: int, post_id: str, db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute("UPDATE digest_items SET post_id=? WHERE id=?", (post_id, item_id))


def count_selections_today(day_start_utc: str, day_end_utc: str,
                           db_path: Path | None = None) -> tuple:
    """Сколько за сутки владельца было ПОПЫТОК и сколько из них дали пост.

    Возвращает (попытки, успехи).

    Считается по времени ВЫБОРА, а не по времени создания дайджеста:
    дайджест живёт сутки и переживает полночь, поэтому по `created_at`
    сегодняшние выборы из вчерашнего списка не считались бы вовсе — и
    суточный потолок обходился бы сам собой.

    Попытки считаются по журналу `selection_attempts`, а не по отметке в
    самой истории: повторный выбор той же новости переписал бы отметку, и
    вторая оплаченная попытка исчезла бы из счёта — потолок обходился бы
    повтором одной и той же неудачной истории.

    Попытки с исходами `api_error` и `no_work` в счёт НЕ идут: в первом
    случае провайдер не ответил, во втором по сюжету уже был пост и к
    модели не обращались вовсе. Ни там, ни там модель ничего не решала и
    деньги не тратились. Строки в журнале остаются — по ним видно, сколько
    раз подводил API и сколько нажатий пришлось на уже готовые истории.

    Успех — это попытка, доведённая до поста в очереди (`post_id`
    заполнен). Провал гейта или редакторской оценки в успехи не идёт и
    дневной слот готовых постов не расходует.

    Попытки считаются по времени НАЧАЛА, успехи — по времени ЗАВЕРШЕНИЯ:
    выбор, сделанный в 23:59 и давший пост в 00:01, относится к новым
    суткам. Иначе за первые минуты дня можно было получить три поста —
    один вчерашней попыткой и два сегодняшними.

    Границы суток приходят снаружи уже посчитанными в зоне владельца:
    здесь не место знанию о часовом поясе, а «сегодня» по UTC для Москвы
    сдвинуто на три часа.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT "
            " (SELECT COUNT(*) FROM selection_attempts "
            "   WHERE started_at >= ? AND started_at < ? "
            "     AND COALESCE(outcome, '') NOT IN ('api_error', 'no_work')), "
            " (SELECT COUNT(*) FROM selection_attempts "
            "   WHERE post_id IS NOT NULL "
            "     AND COALESCE(finished_at, started_at) >= ? "
            "     AND COALESCE(finished_at, started_at) < ?)",
            (day_start_utc, day_end_utc, day_start_utc, day_end_utc),
        ).fetchone()
    return int(row[0]), int(row[1])


# ---------------------------------------------------------------------------
# Расписание публикаций
# ---------------------------------------------------------------------------


def schedule_post(post_id: str, publish_at_utc: str, slot: str,
                  db_path: Path | None = None) -> bool:
    """Ставит пост в слот. Только из рабочего состояния и только в свободный.

    Один слот — один пост: иначе «утро» превращается в пачку публикаций
    подряд.
    """
    with get_conn(db_path) as conn:
        taken = conn.execute(
            "SELECT 1 FROM posts WHERE status='scheduled' AND slot=? AND publish_at=? "
            "AND post_id<>? LIMIT 1",
            (slot, publish_at_utc, post_id),
        ).fetchone()
        if taken:
            return False
        return conn.execute(
            "UPDATE posts SET status='scheduled', publish_at=?, slot=? "
            "WHERE post_id=? AND status IN ('candidate','approved','hold')",
            (publish_at_utc, slot, post_id),
        ).rowcount == 1


def unschedule_post(post_id: str, db_path: Path | None = None) -> bool:
    """Снимает с расписания обратно в очередь на решение."""
    with get_conn(db_path) as conn:
        return conn.execute(
            "UPDATE posts SET status='candidate', publish_at=NULL, slot=NULL "
            "WHERE post_id=? AND status='scheduled'",
            (post_id,),
        ).rowcount == 1


def finish_publishing(post_id: str, sent: bool, db_path: Path | None = None,
                      crosspost_to: str = "") -> None:
    """Завершает отправку одним запросом: статус и расписание вместе.

    Раздельные «поставить approved» и «снять расписание» открывали окно:
    между ними владелец успевал нажать 🌅, пост получал status='scheduled'
    с временем, а следующая строка обнуляла время — оставался запланированный
    пост без слота. Такой не виден ни таймеру (он требует publish_at),
    ни очереди на решение: пост пропадал молча.

    Условие `status='publishing'` держит переход в границах собственного
    захвата: чужой статус этот вызов не перепишет.
    """
    # Перевод поста в 'published' идёт ОТДЕЛЬНОЙ, ничем не отягощённой
    # транзакцией. Соблазн записать сюда же строку очереди зеркала велик —
    # это закрыло бы щель, в которой убитый процесс теряет зеркало. Но цена
    # ошибки несимметрична: сбой общей транзакции откатил бы и статус, пост
    # остался бы в 'publishing' после того, как уже ушёл в канал, и таймер
    # отправил бы его второй раз. Потерянное зеркало — неприятность,
    # задвоенный пост в канале — брак, который видят подписчики.
    #
    # Щель закрывается иначе: воркер сам подбирает свежие опубликованные
    # посты без строки очереди (см. reconcile_crossposts).
    with get_conn(db_path) as conn:
        if sent:
            conn.execute(
                "UPDATE posts SET status='published' WHERE post_id=? AND status='publishing'",
                (post_id,),
            )
        else:
            conn.execute(
                "UPDATE posts SET status='approved', publish_at=NULL, slot=NULL "
                "WHERE post_id=? AND status='publishing'",
                (post_id,),
            )


def release_stale_digest_claims(db_path: Path | None = None) -> int:  # noqa: D401
    """Возвращает в список истории, выбор которых оборвался на генерации.

    Между отметкой «выбрана» и появлением поста бот может умереть
    (рестарт службы, падение). История осталась бы выбранной без поста:
    в дайджесте она видна, но нажать на неё уже нельзя. Вызывается при
    старте бота, когда прошлая генерация заведомо мертва.

    Отметка времени выбора НЕ снимается: попытка была и деньги за неё
    могли уйти, поэтому суточный потолок её по-прежнему считает.
    """
    with get_conn(db_path) as conn:
        return conn.execute(
            "UPDATE digest_items SET chosen=0 WHERE chosen=1 AND post_id IS NULL"
        ).rowcount


def clear_schedule(post_id: str, db_path: Path | None = None) -> None:
    """Убирает время и слот у поста, который перестал быть запланированным.

    Иначе в карточке остаётся подпись «выйдет тогда-то» у поста, который
    уже вернулся в очередь после неудачной отправки.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE posts SET publish_at=NULL, slot=NULL WHERE post_id=? AND status<>'published'",
            (post_id,),
        )


def scheduled_posts(db_path: Path | None = None) -> list:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT post_id, slot, publish_at, headline, "
            "       substr(draft_ru,1,160) AS preview "
            "FROM posts WHERE status='scheduled' ORDER BY publish_at ASC"
        ).fetchall()
    return [dict(r) for r in rows]


def failed_item_ids_today(day_start_utc: str, day_end_utc: str,
                          db_path: Path | None = None) -> set:
    """Истории, которые сегодня уже не прошли подготовку.

    Нужны автоматическому режиму. Он берёт из пула сильнейшую историю, и
    если та не прошла проверки, к следующему слоту она снова оказывается
    первой — деньги уходили бы на один и тот же материал, пока не кончатся
    суточные попытки. Один отказ за сутки на историю достаточно.

    Владельца это не касается: он выбирает вручную и вправе попробовать
    ещё раз.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT DISTINCT digest_item_id FROM selection_attempts "
            "WHERE started_at >= ? AND started_at < ? "
            "AND COALESCE(outcome,'') IN ('rejected','error')",
            (day_start_utc, day_end_utc),
        ).fetchall()
    return {r[0] for r in rows}


def count_api_errors_today(day_start_utc: str, day_end_utc: str,
                           db_path: Path | None = None) -> int:
    """Сколько раз за сутки подготовку сорвал сам провайдер."""
    with get_conn(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM selection_attempts "
            "WHERE outcome = ? AND started_at >= ? AND started_at < ?",
            (ATTEMPT_API_ERROR, day_start_utc, day_end_utc),
        ).fetchone()[0]


def mark_event_post_failed(event_id: str, db_path: Path | None = None) -> int:
    """Помечает пост по сюжету как отклонённый из-за сбоя подготовки.

    Отдельная причина нужна, чтобы технический сбой не попадал в статистику
    как решение юридического гейта или редакторская оценка: это разные
    вещи, и реагировать на них надо по-разному.
    """
    with get_conn(db_path) as conn:
        return conn.execute(
            "UPDATE posts SET status='rejected', reject_reason=? "
            "WHERE event_id=? AND status NOT IN ('published','publishing','scheduled')",
            (REJECT_BY_ERROR, event_id),
        ).rowcount


def rejected_by_reason(db_path: Path | None = None) -> dict:
    """Сколько постов забраковано и кем. Пустая причина — записи до 28.08."""
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT COALESCE(reject_reason, ''), COUNT(*) FROM posts "
            "WHERE status='rejected' GROUP BY COALESCE(reject_reason, '')"
        ).fetchall()
    return {r[0]: r[1] for r in rows}


def due_scheduled_posts(now_utc_iso: str, db_path: Path | None = None) -> list:
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT post_id, slot, publish_at FROM posts "
            "WHERE status='scheduled' AND publish_at IS NOT NULL AND publish_at <= ? "
            "ORDER BY publish_at ASC",
            (now_utc_iso,),
        ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Очередь зеркалирования
# ---------------------------------------------------------------------------

def enqueue_crosspost(post_id: str, platform: str, db_path: Path | None = None) -> bool:
    """Ставит пост в очередь зеркалирования. True — строка появилась.

    Отдельно от `finish_publishing` намеренно: сбой здесь не имеет права
    тронуть статус поста в Telegram.
    """
    try:
        with get_conn(db_path) as conn:
            return conn.execute(
                "INSERT OR IGNORE INTO crossposts (post_id, platform, status, created_at) "
                "VALUES (?, ?, 'pending', ?)",
                (post_id, platform, now_iso()),
            ).rowcount > 0
    except sqlite3.Error as exc:
        print(f"[storage] пост {post_id} опубликован, но в очередь {platform} "
              f"не попал: {exc}")
        return False


def reconcile_crossposts(platform: str, since: str, channel_id: str,
                         db_path: Path | None = None) -> int:
    """Подбирает опубликованные посты, для которых строки очереди нет.

    Закрывает щель между отправкой в Telegram и постановкой в очередь: если
    процесс умер ровно между ними, пост остался бы без зеркала навсегда.

    Задним числом ничего не выкладывает: `since` — момент включения
    интеграции, раньше него не берётся ничего. Старые посты канала так и
    остаются неотзеркаленными, как и требовалось.
    """
    with get_conn(db_path) as conn:
        rows = conn.execute(
            "SELECT p.post_id FROM posts p LEFT JOIN crossposts c "
            "  ON c.post_id = p.post_id AND c.platform = ? "
            "WHERE p.status='published' AND c.post_id IS NULL "
            "  AND p.created_at >= ? AND COALESCE(p.channel_id,'') IN ('', ?)",
            (platform, since, channel_id),
        ).fetchall()
        added = 0
        for row in rows:
            added += conn.execute(
                "INSERT OR IGNORE INTO crossposts (post_id, platform, status, created_at) "
                "VALUES (?, ?, 'pending', ?)",
                (row[0], platform, now_iso()),
            ).rowcount
    return added


def mark_publish_attempted(post_id: str, platform: str,
                           db_path: Path | None = None) -> None:
    """Отмечает, что публикация контейнера начата.

    Нужно, чтобы после обрыва отличить «мы даже не пробовали» от «пробовали,
    исход неизвестен». Во втором случае повторять вслепую нельзя.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE crossposts SET publish_attempted_at=? WHERE post_id=? AND platform=?",
            (now_iso(), post_id, platform),
        )


def claim_crosspost(platform: str, now: str, db_path: Path | None = None) -> dict | None:
    """Захватывает одну работу к отправке. None — брать нечего.

    Захват атомарный: `UPDATE ... WHERE status IN (...)` выполняется одним
    запросом, поэтому одну и ту же работу не получат двое. Транзакция
    закрывается ДО обращения к сети — держать запись открытой, пока мы ждём
    ответа Meta, значит подвесить единственного писателя SQLite и через него
    задеть публикацию в Telegram.
    """
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT post_id FROM crossposts WHERE platform=? AND status IN ('pending','failed') "
            "AND (next_retry_at IS NULL OR next_retry_at<=?) ORDER BY created_at LIMIT 1",
            (platform, now),
        ).fetchone()
        if row is None:
            return None
        claimed = conn.execute(
            "UPDATE crossposts SET status='sending', attempts=attempts+1, sending_since=? "
            "WHERE post_id=? AND platform=? AND status IN ('pending','failed')",
            (now_iso(), row[0], platform),
        ).rowcount
        if not claimed:
            return None
        got = conn.execute(
            "SELECT post_id, platform, status, attempts, payload_text, link_url, "
            "container_id, remote_post_id, publish_attempted_at "
            "FROM crossposts WHERE post_id=? AND platform=?",
            (row[0], platform),
        ).fetchone()
    return dict(got) if got else None


def save_crosspost_payload(post_id: str, platform: str, text: str, link_url: str,
                           db_path: Path | None = None) -> None:
    """Фиксирует текст, которым пост уйдёт, до первого обращения к сети.

    Повтор после сбоя обязан отправить ровно то, что готовилось: пересчёт
    на каждой попытке означал бы, что опубликованное и проверенное — разные
    тексты, и сверить постфактум нечем.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE crossposts SET payload_text=?, link_url=? WHERE post_id=? AND platform=?",
            (text, link_url, post_id, platform),
        )


def save_crosspost_container(post_id: str, platform: str, container_id: str,
                             db_path: Path | None = None) -> None:
    """Запоминает созданный контейнер СРАЗУ, до попытки его опубликовать.

    Если процесс умрёт между созданием и публикацией, повтор не создаст
    второй контейнер: он возьмёт этот и сначала спросит у Meta его статус.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE crossposts SET container_id=?, publish_attempted_at=NULL "
            "WHERE post_id=? AND platform=?",
            (container_id, post_id, platform),
        )


def finish_crosspost(post_id: str, platform: str, remote_post_id: str,
                     db_path: Path | None = None) -> None:
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE crossposts SET status='done', remote_post_id=?, published_at=?, "
            "last_error=NULL, next_retry_at=NULL WHERE post_id=? AND platform=?",
            (remote_post_id, now_iso(), post_id, platform),
        )


def fail_crosspost(post_id: str, platform: str, error: str, next_retry_at: str | None,
                   terminal: bool = False, db_path: Path | None = None) -> None:
    """Откладывает повтор или закрывает работу насовсем.

    `terminal` ставит 'dead': попытки исчерпаны или отказ неисправим. Такая
    работа больше не берётся — долбить Meta бесконечно нельзя.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE crossposts SET status=?, last_error=?, next_retry_at=? "
            "WHERE post_id=? AND platform=?",
            ("dead" if terminal else "failed", error[:500], next_retry_at, post_id, platform),
        )


def unknown_crosspost(post_id: str, platform: str, detail: str,
                      db_path: Path | None = None) -> None:
    """Исход отправки не установлен — работа замораживается до человека.

    Возникает, если ответ Meta не разобрался или статус контейнера не
    читается. Слепой повтор здесь опаснее простоя: он способен выложить
    второй пост поверх уже опубликованного.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE crossposts SET status='unknown', last_error=?, next_retry_at=NULL "
            "WHERE post_id=? AND platform=?",
            (detail[:500], post_id, platform),
        )


def skip_crosspost(post_id: str, platform: str, reason: str, detail: str = "",
                   db_path: Path | None = None) -> None:
    """Сознательный отказ от публикации — не ошибка, но и не тишина.

    Повода два: текст не удалось уложить в лимит по границам абзацев и
    юридический гейт не пропустил укороченный текст. И то и другое обязано
    быть видно владельцу на экране статуса, иначе пост молча не доедет.
    """
    with get_conn(db_path) as conn:
        conn.execute(
            "UPDATE crossposts SET status='skipped', skip_reason=?, last_error=?, "
            "next_retry_at=NULL WHERE post_id=? AND platform=?",
            (reason, detail[:500], post_id, platform),
        )


def recover_stuck_crossposts(platform: str, older_than: str,
                             db_path: Path | None = None) -> int:
    """Возвращает в очередь работы, застрявшие в 'sending'.

    В этот статус работа попадает на время обращения к Meta. Убитый в этот
    момент процесс оставил бы её невидимой навсегда. Контейнер при этом
    сохранён, поэтому повтор не создаст второй пост: он сверит статус
    контейнера у Meta прежде чем что-то публиковать.
    """
    with get_conn(db_path) as conn:
        return conn.execute(
            "UPDATE crossposts SET status='pending' WHERE platform=? AND status='sending' "
            "AND COALESCE(sending_since, created_at) <= ?",
            (platform, older_than),
        ).rowcount


def crosspost_row(post_id: str, platform: str, db_path: Path | None = None) -> dict | None:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM crossposts WHERE post_id=? AND platform=?", (post_id, platform)
        ).fetchone()
    return dict(row) if row else None


def crosspost_counts(platform: str, day_start: str, day_end: str,
                     db_path: Path | None = None) -> dict:
    """Сводка для экрана статуса: сколько сегодня ушло, сколько ждёт."""
    with get_conn(db_path) as conn:
        today = dict(conn.execute(
            "SELECT COUNT(*) AS done, MAX(published_at) AS last FROM crossposts "
            "WHERE platform=? AND status='done' AND published_at>=? AND published_at<?",
            (platform, day_start, day_end),
        ).fetchone())
        queued = conn.execute(
            "SELECT COUNT(*) FROM crossposts WHERE platform=? "
            "AND status IN ('pending','sending','failed')", (platform,),
        ).fetchone()[0]
        stuck = conn.execute(
            "SELECT COUNT(*) FROM crossposts WHERE platform=? AND status IN ('dead','unknown')",
            (platform,),
        ).fetchone()[0]
        skipped = conn.execute(
            "SELECT COUNT(*) FROM crossposts WHERE platform=? AND status='skipped'",
            (platform,),
        ).fetchone()[0]
        last_err = conn.execute(
            "SELECT last_error FROM crossposts WHERE platform=? AND last_error IS NOT NULL "
            "ORDER BY COALESCE(published_at, created_at) DESC LIMIT 1", (platform,),
        ).fetchone()
    return {"done_today": today["done"], "last_at": today["last"], "queued": queued,
            "stuck": stuck, "skipped": skipped,
            "last_error": last_err[0] if last_err else None}


def integration_get(platform: str, db_path: Path | None = None) -> dict:
    with get_conn(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM integration_state WHERE platform=?", (platform,)
        ).fetchone()
    return dict(row) if row else {}


def integration_set(platform: str, db_path: Path | None = None, **fields) -> None:
    """Обновляет здоровье интеграции. Секреты сюда не пишутся.

    Список полей закрыт намеренно: опечатка в имени иначе создала бы
    колонку-призрак, а токен, случайно переданный сюда, попал бы в базу.
    """
    if not fields:
        return
    allowed = {"enabled_at", "last_probe_at", "token_expires_at",
               "token_refreshed_at", "degraded", "last_error", "last_notified_at"}
    bad = set(fields) - allowed
    if bad:
        raise ValueError(f"integration_state: недопустимые поля {sorted(bad)}")
    fields["updated_at"] = now_iso()
    cols = ", ".join(fields)
    marks = ", ".join("?" for _ in fields)
    upd = ", ".join(f"{k}=excluded.{k}" for k in fields)
    with get_conn(db_path) as conn:
        conn.execute(
            f"INSERT INTO integration_state (platform, {cols}) VALUES (?, {marks}) "
            f"ON CONFLICT(platform) DO UPDATE SET {upd}",
            (platform, *fields.values()),
        )
