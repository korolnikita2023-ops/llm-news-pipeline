"""Роли доступа к боту: owner и admin.

Зачем отдельный модуль. Раньше список тех, кому можно управлять ботом, жил
одной строкой в `.env` (`TELEGRAM_ADMIN_IDS`) и читался ОДИН раз при старте
процесса. Из этого следовали две проблемы:

1. Добавить человека можно было только правкой файла на машине с ботом и
   перезапуском процесса — то есть владелец без доступа к серверу не мог
   этого вообще.
2. Роли не различались. Любой, кто попал в список, мог всё, включая
   изменение самого списка.

Теперь права лежат в таблице `admins` и проверяются на каждом апдейте:

    owner — владелец. Разбирает очередь И управляет составом админов.
            Владелец ровно один, задаётся в .env (TELEGRAM_OWNER_CHAT_ID).
    admin — редактор. approve / reject / edit / regenerate / hold,
            но НЕ управление составом.

Про `.env`. Он остаётся точкой входа только для первого запуска: при
первом старте на пустой базе список `TELEGRAM_ADMIN_IDS` переносится в
таблицу и ставится отметка в `meta`. Дальше источник истины — база. Без
этой отметки удалённый через бота админ воскресал бы при каждом рестарте,
пока его ID остаётся в файле.

Владелец — исключение: он берётся из `.env` при каждом старте, и роль
`owner` в базе всегда приводится к тому, кто указан там. Иначе смена
владельца в файле давала бы двух владельцев, а опечатка в файле — ни
одного (и никто не мог бы управлять составом).
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

from src import storage

ROLE_OWNER = "owner"
ROLE_ADMIN = "admin"

# Отметка «список из .env уже перенесён в базу» (таблица meta).
BOOTSTRAP_KEY = "admins_bootstrapped"


class RoleError(RuntimeError):
    """Операция с составом админов невозможна — с человеческим объяснением."""


# ---------------------------------------------------------------------------
# Разбор .env
# ---------------------------------------------------------------------------


def parse_user_id(raw: Any) -> Optional[int]:
    """ID пользователя Telegram числом или None, если это не ID.

    Отрицательные значения — это ID чатов и каналов, а не людей: положить
    такое в список админов значит завести запись, под которой никто никогда
    не придёт.
    """
    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def env_owner_id(cfg) -> Optional[int]:
    return parse_user_id(cfg.telegram_owner_chat_id)


def env_admin_ids(cfg) -> List[int]:
    """Дополнительные админы из .env, без владельца и без дублей."""
    owner = env_owner_id(cfg)
    out: List[int] = []
    for raw in str(cfg.telegram_admin_ids or "").split(","):
        if not raw.strip():
            continue
        uid = parse_user_id(raw)
        if uid is None or uid == owner or uid in out:
            continue
        out.append(uid)
    return out


def env_labels(cfg) -> Dict[int, str]:
    """Подписи из TELEGRAM_ADMIN_LABELS: «111111111=Alice @alice_example, 222222222=Bob»."""
    out: Dict[int, str] = {}
    for pair in str(getattr(cfg, "telegram_admin_labels", "") or "").split(","):
        if "=" not in pair:
            continue
        raw_id, _, label = pair.partition("=")
        uid = parse_user_id(raw_id)
        if uid is not None and label.strip():
            out[uid] = label.strip()
    return out


# ---------------------------------------------------------------------------
# Схема и первичное наполнение
# ---------------------------------------------------------------------------


def ensure_schema(db_path: Optional[Path] = None) -> None:
    """Создаёт таблицы, если их ещё нет. Идемпотентно.

    Вызывается перед любой работой с ролями: боевая база создана до этой
    таблицы, и бот не должен падать на первой же проверке доступа.
    """
    storage.init_db(db_path)


def bootstrap(cfg, db_path: Optional[Path] = None) -> None:
    """Приводит таблицу в соответствие с .env — ровно в двух местах.

    1. Владелец из .env всегда получает роль owner, и он в базе один.
    2. TELEGRAM_ADMIN_IDS переносится в базу ОДИН раз, на пустой базе.

    Расхождения после переноса не молчат: если в .env появились ID, которых
    нет в базе, об этом печатается предупреждение — иначе владелец правил бы
    файл и не понимал, почему ничего не меняется.
    """
    ensure_schema(db_path)
    owner = env_owner_id(cfg)
    labels = env_labels(cfg)
    now = storage.now_iso()

    with storage.get_conn(db_path) as conn:
        if owner is not None:
            conn.execute(
                "INSERT INTO admins (user_id, role, label, added_by, added_at) "
                "VALUES (?, 'owner', ?, NULL, ?) "
                "ON CONFLICT(user_id) DO UPDATE SET role='owner', "
                "label=COALESCE(admins.label, excluded.label)",
                (owner, labels.get(owner), now),
            )
            # Прежний владелец (если в .env вписали другого) остаётся с
            # доступом, но теряет управление составом. Молча это делать
            # нельзя — человек лишается прав.
            demoted = conn.execute(
                "UPDATE admins SET role='admin' WHERE role='owner' AND user_id<>?",
                (owner,),
            ).rowcount
            if demoted:
                print(
                    f"[roles] владелец в .env сменился — прежних владельцев "
                    f"({demoted}) перевёл в обычные админы"
                )
        else:
            print(
                "[roles] ВНИМАНИЕ: TELEGRAM_OWNER_CHAT_ID не задан или не число — "
                "владелец берётся из базы, если он там уже есть"
            )

        done = conn.execute(
            "SELECT value FROM meta WHERE key=?", (BOOTSTRAP_KEY,)
        ).fetchone()

        if done is None:
            for uid in env_admin_ids(cfg):
                conn.execute(
                    "INSERT INTO admins (user_id, role, label, added_by, added_at) "
                    "VALUES (?, 'admin', ?, ?, ?) ON CONFLICT(user_id) DO NOTHING",
                    (uid, labels.get(uid), owner, now),
                )
            conn.execute(
                "INSERT INTO meta (key, value) VALUES (?, ?) "
                "ON CONFLICT(key) DO NOTHING",
                (BOOTSTRAP_KEY, now),
            )
            return

        known = {r[0] for r in conn.execute("SELECT user_id FROM admins")}
        stale = [uid for uid in env_admin_ids(cfg) if uid not in known]

    if stale:
        print(
            f"[roles] в .env есть ID, которых нет в базе: {stale}. "
            "Состав админов теперь ведётся в боте (кнопка «👥 Администраторы» "
            "у владельца), правка .env на него больше не влияет."
        )


# ---------------------------------------------------------------------------
# Чтение
# ---------------------------------------------------------------------------


def role_of(user_id: Any, db_path: Optional[Path] = None) -> Optional[str]:
    """Роль пользователя или None, если доступа нет."""
    uid = parse_user_id(user_id)
    if uid is None:
        return None
    with storage.get_conn(db_path) as conn:
        try:
            row = conn.execute(
                "SELECT role FROM admins WHERE user_id=?", (uid,)
            ).fetchone()
        except sqlite3.OperationalError:
            # Таблицы ещё нет (старая база) — это не «доступ разрешён».
            return None
    return row[0] if row else None


def is_admin(user_id: Any, db_path: Optional[Path] = None) -> bool:
    """Есть ли доступ к боту вообще. Владелец — тоже админ."""
    return role_of(user_id, db_path) in (ROLE_OWNER, ROLE_ADMIN)


def is_owner(user_id: Any, db_path: Optional[Path] = None) -> bool:
    return role_of(user_id, db_path) == ROLE_OWNER


def allowed_ids(db_path: Optional[Path] = None) -> List[int]:
    with storage.get_conn(db_path) as conn:
        try:
            return [r[0] for r in conn.execute("SELECT user_id FROM admins")]
        except sqlite3.OperationalError:
            return []


def list_admins(db_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Состав: сначала владелец, дальше админы в порядке добавления."""
    with storage.get_conn(db_path) as conn:
        try:
            rows = conn.execute(
                "SELECT user_id, role, label, added_by, added_at FROM admins "
                "ORDER BY CASE role WHEN 'owner' THEN 0 ELSE 1 END, added_at, user_id"
            ).fetchall()
        except sqlite3.OperationalError:
            return []
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Изменение состава — только владелец (проверка роли делается в обработчике)
# ---------------------------------------------------------------------------


def add_admin(
    user_id: Any,
    label: Optional[str] = None,
    added_by: Optional[int] = None,
    db_path: Optional[Path] = None,
) -> Dict[str, Any]:
    """Добавляет редактора. Возвращает запись; отказ — RoleError с текстом.

    Отказ формулируется по-человечески: это сообщение уходит владельцу в
    чат как есть.
    """
    uid = parse_user_id(user_id)
    if uid is None:
        raise RoleError(
            "Это не похоже на ID пользователя Telegram. Нужно положительное "
            "число, например 111111111. Узнать ID: попросите человека "
            "написать боту @userinfobot."
        )

    ensure_schema(db_path)
    existing = role_of(uid, db_path)
    if existing == ROLE_OWNER:
        raise RoleError("Это владелец — у него уже все права.")
    if existing == ROLE_ADMIN:
        raise RoleError("Этот человек уже в списке администраторов.")

    with storage.get_conn(db_path) as conn:
        conn.execute(
            "INSERT INTO admins (user_id, role, label, added_by, added_at) "
            "VALUES (?, 'admin', ?, ?, ?)",
            (uid, (label or "").strip() or None, added_by, storage.now_iso()),
        )
    return {"user_id": uid, "role": ROLE_ADMIN, "label": label}


def remove_admin(
    user_id: Any, db_path: Optional[Path] = None
) -> Dict[str, Any]:
    """Убирает редактора. Владельца убрать нельзя — иначе управлять составом
    станет некому, и бот запрётся изнутри."""
    uid = parse_user_id(user_id)
    if uid is None:
        raise RoleError("Это не похоже на ID пользователя Telegram.")

    role = role_of(uid, db_path)
    if role is None:
        raise RoleError("Такого администратора нет в списке.")
    if role == ROLE_OWNER:
        raise RoleError(
            "Владельца удалить нельзя: управлять составом станет некому. "
            "Сменить владельца можно только в .env с перезапуском бота."
        )

    with storage.get_conn(db_path) as conn:
        conn.execute("DELETE FROM admins WHERE user_id=?", (uid,))
    return {"user_id": uid, "role": role}


def label_of(entry: Dict[str, Any], fallback: Optional[Dict[int, str]] = None) -> str:
    """Как показать админа в списке: подпись из базы, иначе из .env, иначе ID."""
    label = (entry.get("label") or "").strip()
    if not label and fallback:
        label = (fallback.get(entry["user_id"]) or "").strip()
    return label or str(entry["user_id"])
