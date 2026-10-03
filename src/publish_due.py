"""Публикация постов, у которых наступил слот. Конечный прогон.

Почему отдельный процесс, а не таймер внутри бота: планировщика внутри
бота в этом проекте нет намеренно — расписание
живёт снаружи, в systemd. Бот остаётся тем, чем был: долгоживущим
обработчиком нажатий.

Публикация идёт через ту же и единственную точку `_publish_post`: свой
путь отправки в канал здесь не появляется, иначе юридический гейт и
защита от двойной публикации перестают быть обязательными.

Просроченный слот: опоздание в пределах `SCHEDULE_GRACE_MINUTES`
публикуется как есть, большее — снимается с расписания и возвращается
владельцу. Утренний пост, ушедший в канал в три часа ночи из-за того,
что сервер лежал, — хуже, чем не ушедший.
"""
from __future__ import annotations

import sys

from config import get_config
from src import schedule, storage


def _fill_slots(cfg) -> None:
    """Готовит посты к наступающим слотам. Ошибку наверх не поднимает.

    Публикация уже назначенного важнее подготовки нового: сбой здесь не
    должен помешать выйти тому, что стоит в расписании.
    """
    from src import auto_editor

    try:
        report = auto_editor.fill_due_slots(cfg)
    except Exception as exc:
        print(f"[publish_due] автоподбор сорвался ({type(exc).__name__}: {exc}) — "
              f"публикация назначенного продолжается", file=sys.stderr)
        return

    if report.get("assigned") or report.get("rejected"):
        print("[auto] " + auto_editor.format_report(cfg, report).replace("\n", "\n[auto] "))
    elif report.get("diag") is not None:
        # Слот был в окне подготовки, но готовить оказалось нечего. Молчать
        # нельзя: 30.08 вечерний слот прошёл пустым, и по журналу было не
        # видно, что AUTO вообще смотрел (пул был, но весь «уже пробовали
        # сегодня»). Одна строка в журнал сервера, в Telegram не идёт.
        d = report["diag"]
        why = "; ".join(report.get("skipped", [])) or "готовить нечего"
        print(f"[auto] pool={d['pool']} failed_today={d['failed_today']} "
              f"stale={d['stale']} eligible={d['eligible']} — {why}")


def run() -> int:
    cfg = get_config()
    try:
        schedule.validate(cfg)
    except schedule.ScheduleConfigError as exc:
        print(f"[publish_due] СТОП: {exc}", file=sys.stderr)
        return 1
    storage.init_db()

    # В автоматическом режиме этот же тик заодно наполняет наступающие
    # слоты. Отдельного таймера и второго пути публикации не появляется:
    # ставится всё в то же расписание, публикует всё та же _publish_post.
    if cfg.workflow_mode == "auto":
        _fill_slots(cfg)

    now = schedule.now_utc_iso()
    due = storage.due_scheduled_posts(now)
    if not due:
        print(f"[publish_due] {now}: к публикации ничего не готово")
        return 0

    # Импорт здесь, а не наверху: модуль бота тянет aiogram, и незачем
    # поднимать его, когда публиковать нечего.
    from src.approval_bot import _publish_post

    published = overdue = failed = 0
    for row in due:
        post_id = row["post_id"]
        if schedule.is_too_late(cfg, row["publish_at"]):
            storage.unschedule_post(post_id)
            overdue += 1
            print(
                f"[publish_due] {post_id}: слот {row['publish_at']} просрочен больше "
                f"{cfg.schedule_grace_minutes} мин — снят с расписания, ждёт решения владельца"
            )
            continue

        # require_status: за секунды между чтением списка и этой строкой
        # владелец мог снять пост со слота. Публикуем только то, что всё
        # ещё запланировано.
        sent, status = _publish_post(cfg, post_id, require_status="scheduled")
        if sent:
            published += 1
            print(f"[publish_due] {post_id}: опубликован ({row['slot']})")
        else:
            failed += 1
            # Громко: молчаливый пропуск здесь означал бы, что владелец
            # считает пост вышедшим, а его в канале нет.
            print(f"[publish_due] {post_id}: НЕ опубликован — {status}", file=sys.stderr)

    print(
        f"[publish_due] итог: опубликовано {published}, просрочено {overdue}, "
        f"не удалось {failed}"
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(run())
