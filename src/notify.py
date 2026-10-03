"""Дайджест приходит владельцу сам, сразу после сбора новостей.

Раньше владелец должен был помнить про кнопку «🗞 Дайджест» и нажимать её.
Теперь конвейер, собрав список, сам отправляет его редакции.

Три решения, объясняющие устройство модуля.

**Отправляет конвейер, а не бот.** Планировщика внутри бота в этом проекте
нет намеренно: расписание живёт снаружи, в systemd.
Дайджест собирается внутри разового прогона сбора — он же его и отправляет,
никакого нового расписания не появляется.

**Отправка синхронная, через тот же httpx, что и публикация в канал.**
Конвейер — обычный синхронный скрипт; тащить в него асинхронный слой ради
одного сообщения незачем.

**Недоступность Telegram не проваливает сбор.** Новости собраны, оценены и
сохранены — это главная работа прогона, и она сделана. Неотправленное
сообщение печатается в лог громко, но код возврата не меняет: иначе systemd
считал бы прогон неудачным из-за того, что у владельца в этот момент не
было связи.

Получатели берутся из таблицы прав (`roles.list_admins`), а не из настроек:
состав редакции меняется из бота, и второго списка быть не должно.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

import httpx

from src import roles, storage

# Telegram режет сообщение на 4096 знаках. Десять историй с описаниями в
# один лимит не влезают, поэтому список бьётся на части, а кнопки уходят с
# последней. Порог с запасом: подписи и служебные строки тоже занимают место.
CHUNK_LIMIT = 3500

SEND_TIMEOUT_S = 20.0


def _safe(exc: Exception, cfg) -> str:
    """Текст ошибки без токена — на случай, если он всё же куда-то просочился."""
    text = f"{type(exc).__name__}: {exc}"[:200]
    token = getattr(cfg, "telegram_bot_token", "") or ""
    return text.replace(token, "<токен скрыт>") if token else text


def _sent_key(run_id: str, chat_id: Any) -> str:
    return f"digest_sent:{run_id}:{chat_id}"


def already_sent(run_id: str, chat_id: Any, db_path=None) -> bool:
    """Уже отправляли этому адресату этот дайджест?

    Отметка на КАЖДОГО получателя, а не одна на прогон. С одной общей
    отметкой частичная доставка не чинилась бы: первому ушло, второй
    заблокировал бота, повтор прогона уже подавлен — и второй не получил
    список никогда.
    """
    with storage.get_conn(db_path) as conn:
        row = conn.execute("SELECT 1 FROM meta WHERE key=?",
                           (_sent_key(run_id, chat_id),)).fetchone()
    return row is not None


def _claim(run_id: str, chat_id: Any, db_path=None) -> bool:
    """Занимает отправку этому адресату. False — уже занято или отправлено.

    Захват ДО отправки, а не отметка после: два одновременных вызова иначе
    оба увидели бы «не отправляли» и продублировали сообщение. Если отправка
    сорвётся, захват снимается, и повтор возможен.
    """
    with storage.get_conn(db_path) as conn:
        return conn.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO NOTHING",
            (_sent_key(run_id, chat_id), storage.now_iso()),
        ).rowcount == 1


def _release(run_id: str, chat_id: Any, db_path=None) -> None:
    """Снимает захват — отправка не удалась, пусть повтор попробует снова."""
    with storage.get_conn(db_path) as conn:
        conn.execute("DELETE FROM meta WHERE key=?", (_sent_key(run_id, chat_id),))


class SendFailed(RuntimeError):
    """Отправка не удалась. В тексте НЕТ ни URL, ни токена."""


def _send_message(cfg, chat_id: Any, text: str,
                  keyboard: Optional[dict] = None) -> None:
    """Одно сообщение. Исключение поднимается — решает вызывающий.

    Ошибка формируется своя. У httpx в тексте исключения лежит полный URL,
    а в URL Telegram Bot API стоит токен: одна неудачная отправка — и секрет
    в журнале systemd открытым текстом. Наружу уходят только код ответа и
    описание от Telegram.
    """
    data: Dict[str, Any] = {"chat_id": chat_id, "text": text}
    if keyboard:
        data["reply_markup"] = json.dumps(keyboard, ensure_ascii=False)
    try:
        resp = httpx.post(
            f"https://api.telegram.org/bot{cfg.telegram_bot_token}/sendMessage",
            data=data, timeout=SEND_TIMEOUT_S,
        )
    except Exception as exc:
        raise SendFailed(f"сеть недоступна ({type(exc).__name__})") from None

    if resp.status_code >= 400:
        detail = ""
        try:
            detail = str(resp.json().get("description", ""))[:120]
        except Exception:
            detail = ""
        raise SendFailed(f"Telegram ответил {resp.status_code}"
                         + (f": {detail}" if detail else ""))


def _split(lines: List[str]) -> List[str]:
    """Режет список на сообщения, влезающие в лимит Telegram.

    Режем по строкам, а не по знакам: разорванная посреди слова история
    читается плохо, а история целиком — нормально.
    """
    chunks: List[str] = []
    current: List[str] = []
    size = 0
    for line in lines:
        # Одна строка длиннее лимита не разбилась бы вовсе, и Telegram
        # вернул бы 400 на всё сообщение. Такой длины у нас взяться неоткуда
        # (заголовок и описание обрезаны при сборке списка), но полагаться
        # на это нельзя.
        if len(line) > CHUNK_LIMIT:
            line = line[:CHUNK_LIMIT - 1] + "…"
        extra = len(line) + 1
        if current and size + extra > CHUNK_LIMIT:
            chunks.append("\n".join(current))
            current, size = [], 0
        current.append(line)
        size += extra
    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _keyboard(rows: List[dict]) -> Optional[dict]:
    """Кнопки выбора — те же `pick:<id>`, что и на экране дайджеста."""
    buttons: List[List[dict]] = []
    line: List[dict] = []
    for i, row in enumerate(rows, 1):
        if row.get("chosen"):
            continue
        line.append({"text": str(i), "callback_data": f"pick:{row['id']}"})
        if len(line) == 5:
            buttons.append(line)
            line = []
    if line:
        buttons.append(line)
    return {"inline_keyboard": buttons} if buttons else None


def _slot_lines(cfg) -> List[str]:
    """Что уже назначено на сегодняшние слоты."""
    from src import auto_editor

    try:
        states = auto_editor.slot_state(cfg)
        done = auto_editor.published_today(cfg)
    except Exception as exc:
        print(f"[notify] состояние слотов не прочиталось ({exc})")
        return []

    lines = [f"Опубликовано сегодня: {done} из {cfg.auto_max_posts_per_day} "
             f"(это потолок, а не план)."]
    for st in states:
        if st["filled"]:
            mark = "опубликован" if st["filled"]["status"] == "published" else "назначен"
            lines.append(f"  {st['title']} · {st['at_local']} — {mark}")
        elif st["passed"]:
            lines.append(f"  {st['title']} · {st['at_local']} — прошёл пустым")
        else:
            lines.append(f"  {st['title']} · {st['at_local']} — ждёт подбора")
    lines.append("")
    return lines


def build_digest_message(cfg, rows: List[dict], report: Dict[str, Any]) -> List[str]:
    """Текст рассылки. Ровно та же информация, что на экране в боте, плюс
    короткая сводка прогона: сколько собрано, сколько оценено, когда следующий.
    """
    from src import digest, news_schedule, schedule as slots

    attempts, successes = storage.count_selections_today(*slots.day_bounds_utc(cfg))
    posts_left = max(cfg.max_successful_selections_per_day - successes, 0)
    tries_left = max(cfg.max_preparation_attempts_per_day - attempts, 0)

    auto = getattr(cfg, "workflow_mode", "") == "auto"
    head = []
    if auto:
        # В автоматическом режиме это отчёт, а не задание: от владельца
        # действий не требуется, и он должен понимать это с первой строки.
        head.append("🤖 AUTO: система сама выберет публикации")
    head += [
        f"🗞 Свежий дайджест — {report.get('collected_at', '')}",
        f"Собрано {report.get('raw', 0)} записей, оценено "
        f"{report.get('scored', 0)}, в список вошло {len(rows)}.",
        "",
    ]
    head += _slot_lines(cfg) if auto else []
    # Рассылка показывает то же, что и экран: историю, уже провалившую
    # сегодняшнюю подготовку, AUTO до полуночи не возьмёт — без пометки
    # список создавал бы впечатление, что она доступна (postmortem 30.08).
    failed_today: set = set()
    if auto:
        try:
            from src import schedule as slots

            failed_today = storage.failed_item_ids_today(*slots.day_bounds_utc(cfg))
        except Exception as exc:
            print(f"[notify] не удалось прочитать провалы дня ({exc}) — "
                  f"рассылаю без пометок")
    body: List[str] = []
    for i, row in enumerate(rows, 1):
        mark = " ✅ уже выбрана" if row.get("chosen") else ""
        if not mark and row.get("id") in failed_today:
            mark = " ⚪ уже пробовали сегодня"
        body.append(digest.format_line(i, row) + mark)
        body.append("")

    next_run = news_schedule.describe_next_run(cfg)
    tail = [
        f"Сегодня можно ещё: постов {posts_left} из "
        f"{cfg.max_successful_selections_per_day}, платных попыток "
        f"{tries_left} из {cfg.max_preparation_attempts_per_day}.",
        f"Следующий сбор: {next_run}." if next_run else "",
        "",
        ("Действий от вас не требуется." if auto
         else "Нажмите номер, чтобы отправить историю в работу."),
    ]
    return _split(head + body + [t for t in tail if t])


def build_empty_message(cfg, report: Dict[str, Any]) -> str:
    from src import news_schedule

    next_run = news_schedule.describe_next_run(cfg)
    lines = []
    if getattr(cfg, "workflow_mode", "") == "auto":
        lines.append("🤖 AUTO: система сама выберет публикации")
    lines += [
        f"🗞 Свежий сбор завершён — {report.get('collected_at', '')}",
        f"Собрано {report.get('raw', 0)} записей, новых историй для дайджеста нет.",
    ]
    if next_run:
        lines.append(f"Следующий сбор: {next_run}.")
    return "\n".join(lines)


def send_digest(cfg, run_id: str, rows: List[dict], report: Dict[str, Any],
                db_path=None) -> int:
    """Рассылает дайджест редакции. Возвращает число адресатов, которым дошло.

    НИКОГДА не поднимает исключение наружу: сбор новостей уже сделан, и
    провалить его из-за недоступного Telegram было бы неверно.
    """
    try:
        admins = roles.list_admins(db_path)
    except Exception as exc:
        print(f"[notify] не удалось прочитать состав редакции ({exc}) — "
              f"дайджест не разослан")
        return 0

    if not admins:
        print("[notify] в редакции никого нет — рассылать некому")
        return 0

    if rows:
        chunks = build_digest_message(cfg, rows, report)
        # В автоматическом режиме кнопки выбора не нужны: решение принимает
        # система, а кнопка, которой не надо пользоваться, только сбивает.
        keyboard = None if getattr(cfg, "workflow_mode", "") == "auto" else _keyboard(rows)
    else:
        chunks = [build_empty_message(cfg, report)]
        keyboard = None

    delivered = 0
    for admin in admins:
        chat_id = admin.get("user_id")
        if chat_id is None:
            continue
        if not _claim(run_id, chat_id, db_path):
            print(f"[notify] {chat_id}: этот дайджест уже отправляли, пропускаю")
            continue
        try:
            for i, chunk in enumerate(chunks):
                last = i == len(chunks) - 1
                _send_message(cfg, chat_id, chunk, keyboard if last else None)
            delivered += 1
        except Exception as exc:
            _release(run_id, chat_id, db_path)
            # Один недоступный адресат не должен мешать остальным и уж тем
            # более валить прогон.
            print(f"[notify] {chat_id}: не доставлено — {_safe(exc, cfg)}")

    print(f"[notify] дайджест разослан: {delivered} из {len(admins)}")
    return delivered
