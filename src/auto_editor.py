"""Автоматическая редакция: система сама выбирает, что выйдет в канал.

Что здесь НЕ появляется: ни нового отбора, ни нового скоринга, ни второго
пути публикации. Берётся уже проверенное — пул дайджеста, отсортированный
по существующей оценке, та же подготовка `_process_item`, тот же юридический
гейт, та же редакторская оценка, та же карточка, то же расписание и та же
единственная точка публикации. Меняется одно: решение принимает не человек.

Главное правило — **потолок, а не план**. Три поста в сутки это МАКСИМУМ.
Если критериям отвечает одна история, выйдет одна. Если ни одной — не выйдет
ничего, и это нормальный день, а не сбой. Добивать норму слабыми новостями
нельзя: канал живёт качеством, а не количеством.

Как это работает по времени. Слоты заданы настройками (по умолчанию 09:00,
14:00 и 20:00 по Москве). Незадолго до каждого — `AUTO_LEAD_MINUTES` — система
берёт лучшую историю из АКТУАЛЬНОГО пула и готовит из неё пост. Именно
незадолго, а не заранее всем скопом: так дневной и вечерний посты получают
свежие новости, собранные уже после утреннего выпуска.

Качество. С 30.08.2026 действует ranked-модель: оценка редактора РАНЖИРУЕТ,
а не служит абсолютным барьером. >= MIN_EDITORIAL_AVG — отличный кандидат;
>= AUTO_EDITORIAL_FLOOR — приемлемый добор (в канал уходит лучшее доступное
за день, а не тишина); ниже floor — отказ, берётся следующая история
рейтинга. Пограничная полоса ручного режима (там взвешивает человек) тут
по-прежнему не действует — у AUTO своя, отдельно настраиваемая планка.
Юридический гейт одинаково строг во всех режимах.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from src import schedule as slots
from src import storage


def _now() -> datetime:
    return datetime.now(timezone.utc)


def published_today(cfg, db_path=None) -> int:
    """Сколько публикаций этих суток уже занято в этом канале.

    Считаются и вышедшие, и ещё только назначенные на слот: назначенный
    пост — это решение, которое почти наверняка станет публикацией. Считать
    один вышедший означало бы разрешить подготовить лишний пост поверх уже
    стоящих в расписании.
    """
    day_start, day_end = slots.day_bounds_utc(cfg)
    with storage.get_conn(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) FROM posts "
            "WHERE COALESCE(channel_id,'') IN ('', ?) AND ("
            "      (status='published' AND created_at >= ? AND created_at < ?)"
            "   OR (status IN ('scheduled','publishing') "
            "       AND publish_at >= ? AND publish_at < ?))",
            (cfg.telegram_channel_id, day_start, day_end, day_start, day_end),
        ).fetchone()[0]


def slot_state(cfg, now: Optional[datetime] = None, db_path=None) -> List[Dict[str, Any]]:
    """Что сейчас с каждым слотом суток: время, занятость, пора ли готовить."""
    moment = now or _now()
    lead = timedelta(minutes=max(cfg.auto_lead_minutes, 0))

    with storage.get_conn(db_path) as conn:
        taken = {
            r[0]: {"post_id": r[1], "status": r[2]}
            for r in conn.execute(
                "SELECT slot, post_id, status FROM posts "
                "WHERE status IN ('scheduled','publishing','published') "
                "AND publish_at >= ? AND publish_at < ?",
                slots.day_bounds_utc(cfg),
            )
        }

    out: List[Dict[str, Any]] = []
    for slot in slots.SLOTS:
        at_iso = slots.slot_time_today_utc(cfg, slot, moment)
        at = datetime.fromisoformat(at_iso)
        out.append({
            "slot": slot,
            "title": slots.SLOT_TITLES[slot],
            "at_utc": at_iso,
            "at_local": slots.describe(cfg, at_iso),
            "filled": taken.get(slot),
            # Пора готовить: время слота на подходе, но ещё не безнадёжно
            # просрочено — просроченный слот забирает publish_due по своим
            # правилам, и подсовывать ему свежий пост уже поздно.
            "due": (at - lead) <= moment < (at + timedelta(
                minutes=max(cfg.schedule_grace_minutes, 0))),
            "passed": moment >= at,
        })
    return out


def _fresh_enough(cfg, row: dict, now: Optional[datetime] = None) -> bool:
    """История ещё не протухла для публикации.

    Защита от того, чтобы накопившаяся очередь разом заняла все слоты
    вчерашними новостями. Сводка суточной давности — уже не новость.
    """
    hours = max(cfg.auto_freshness_hours, 0)
    if hours <= 0:
        return True
    try:
        created = datetime.fromisoformat(row["created_at"])
    except (TypeError, ValueError, KeyError):
        return False
    if created.tzinfo is None:
        # Наши записи всегда со смещением, но правленная руками строка может
        # его потерять. Считаем такую отметку временем UTC, а не роняем весь
        # подбор на вычитании несовместимых дат.
        created = created.replace(tzinfo=timezone.utc)
    return (now or _now()) - created <= timedelta(hours=hours)


def eligibility(cfg, now: Optional[datetime] = None,
                db_path=None) -> tuple[List[dict], Dict[str, int]]:
    """Истории-претенденты (сильнейшая первой) плюс разбор, куда делись
    остальные: pool → минус «уже пробовали сегодня» → минус протухшие.

    Разбор нужен диагностике: postmortem 30.08 показал, что «дайджест
    видит одну историю, AUTO — ноль» без этих цифр выглядит как молчаливый
    отказ, и причину приходится собирать по базе руками.
    """
    pool = storage.digest_pool(cfg.telegram_channel_id, None, db_path)
    # История, уже не прошедшая проверки сегодня, к следующему слоту снова
    # оказалась бы первой — и деньги уходили бы на один и тот же материал,
    # пока не кончатся суточные попытки.
    failed = storage.failed_item_ids_today(*slots.day_bounds_utc(cfg), db_path)
    out: List[dict] = []
    n_failed = n_stale = 0
    for r in pool:
        if r["id"] in failed:
            n_failed += 1
            continue
        if not _fresh_enough(cfg, r, now):
            n_stale += 1
            continue
        out.append(r)
    return out, {"pool": len(pool), "failed_today": n_failed,
                 "stale": n_stale, "eligible": len(out)}


def candidates(cfg, now: Optional[datetime] = None, db_path=None) -> List[dict]:
    """Истории-претенденты, сильнейшая первой.

    Это тот же пул, что видит владелец на экране дайджеста: канальная
    защита от повторов, отклонённое им, уже взятое в работу — всё уже
    учтено там. Здесь добавляется окно свежести и «уже пробовали сегодня».
    """
    return eligibility(cfg, now, db_path)[0]


def fill_due_slots(cfg, now: Optional[datetime] = None, db_path=None,
                   dry_run: bool = False) -> Dict[str, Any]:
    """Готовит и ставит в расписание посты для наступающих слотов.

    Возвращает отчёт: что назначено, что отбраковано и почему. При
    `dry_run` не тратит ни копейки и ничего не пишет — только показывает,
    что сделал бы.
    """
    moment = now or _now()
    report: Dict[str, Any] = {"assigned": [], "rejected": [], "skipped": [],
                              "attempts_used": 0, "dry_run": dry_run}

    day = slots.day_bounds_utc(cfg)
    done = published_today(cfg, db_path)
    if cfg.auto_max_posts_per_day > 0 and done >= cfg.auto_max_posts_per_day:
        report["skipped"].append(
            f"дневная норма выбрана: {done} из {cfg.auto_max_posts_per_day}")
        return report

    states = slot_state(cfg, moment, db_path)
    pending = [st for st in states if st["due"] and not st["filled"]]
    if not pending:
        report["skipped"].append("подходящих слотов сейчас нет")
        return report

    pool, diag = eligibility(cfg, moment, db_path)
    # Диагностика попадает в отчёт ВСЕГДА, когда слот был в окне: тик,
    # которому нечего готовить, обязан оставить в журнале причину в цифрах,
    # а не промолчать (postmortem 30.08).
    report["diag"] = diag
    if not pool:
        report["skipped"].append(
            f"в пуле нет свежих историй, отвечающих критериям "
            f"(pool={diag['pool']} failed_today={diag['failed_today']} "
            f"stale={diag['stale']})")
        return report

    for state in pending:
        if cfg.auto_max_posts_per_day > 0 and done + len(report["assigned"]) \
                >= cfg.auto_max_posts_per_day:
            report["skipped"].append("дневная норма исчерпана")
            break

        assigned = False
        while pool and not assigned:
            attempts, _ = storage.count_selections_today(*day, db_path)
            if (cfg.max_preparation_attempts_per_day > 0
                    and attempts >= cfg.max_preparation_attempts_per_day):
                report["skipped"].append(
                    f"платные попытки на сегодня исчерпаны "
                    f"({attempts} из {cfg.max_preparation_attempts_per_day})")
                return report

            story = pool.pop(0)
            if dry_run:
                report["assigned"].append({
                    "slot": state["slot"], "title": state["title"],
                    "at_local": state["at_local"], "story": story,
                    "note": "проверки не запускались — это разбор без затрат",
                })
                assigned = True
                break

            outcome = _prepare(cfg, story, day, report)
            if outcome is None:
                continue
            post_id = outcome["post_id"]
            if storage.schedule_post(post_id, state["at_utc"], state["slot"], db_path):
                report["assigned"].append({
                    "slot": state["slot"], "title": state["title"],
                    "at_local": state["at_local"], "story": story,
                    "post_id": post_id, "average": outcome.get("average"),
                    "auto_floor": bool(outcome.get("auto_floor")),
                })
                assigned = True
            else:
                # Слот заняли параллельно — пост остаётся кандидатом, а не
                # публикуется мимо расписания.
                report["skipped"].append(
                    f"{state['title']}: слот заняли, пост {post_id} остался в очереди")
                assigned = True

        if not assigned and not pool:
            report["skipped"].append(
                f"{state['title']}: подходящих историй не нашлось")

    return report


def _prepare(cfg, story: dict, day, report: Dict[str, Any]) -> Optional[dict]:
    """Проводит историю через подготовку. None — не прошла, берём следующую.

    `borderline_ok=False`: пограничная полоса ручного режима здесь не
    действует — у неё нет человека, который взвесит. Вместо неё своя
    политика `auto_floor` (с 30.08.2026): оценка от
    AUTO_EDITORIAL_FLOOR до порога — приемлемый добор, публикуется как
    лучшее доступное; на редакторскую неудачу — максимум одна
    дополнительная генерация, фиксируется лучший вариант. Юридический
    гейт при этом не ослаблен ни на йоту.
    """
    from src import approval_bot

    attempt = storage.start_selection_attempt(
        story["id"], *day, cfg.auto_max_posts_per_day,
        cfg.max_preparation_attempts_per_day,
        max_api_errors=cfg.max_preparation_attempts_per_day * 2,
    )
    if not isinstance(attempt, int):
        report["skipped"].append(f"«{story['title'][:60]}»: {attempt}")
        return None

    report["attempts_used"] += 1
    try:
        outcome = approval_bot._generate_from_digest(
            cfg, dict(story), borderline_ok=False,
            auto_floor=cfg.auto_editorial_floor)
    except Exception as exc:
        from src.llm_client import LLMUnavailableError

        unavailable = isinstance(exc, LLMUnavailableError)
        storage.release_digest_item(story["id"])
        storage.finish_selection_attempt(
            attempt, None, storage.ATTEMPT_API_ERROR if unavailable else "error")
        report["rejected"].append({
            "title": story["title"],
            "why": "провайдер не ответил" if unavailable else f"сбой: {exc}",
        })
        return None

    post_id = outcome.get("post_id")
    if not post_id:
        storage.release_digest_item(story["id"])
        paid = bool(outcome.get("paid_work", True))
        storage.finish_selection_attempt(
            attempt, None, "rejected" if paid else storage.ATTEMPT_NO_WORK)
        report["rejected"].append({
            "title": story["title"],
            "why": _why(outcome),
            "average": outcome.get("average"),
        })
        return None

    storage.finish_selection_attempt(attempt, post_id, "ok", item_id=story["id"])
    return outcome


def _why(outcome: dict) -> str:
    kind = outcome.get("outcome")
    if kind == "legal_gate":
        issues = outcome.get("issues") or []
        return "юридический гейт" + (f": {str(issues[0])[:120]}" if issues else "")
    if kind == "editorial":
        avg, threshold = outcome.get("average"), outcome.get("threshold")
        if avg is not None:
            return f"редакторская оценка {avg} при пороге {threshold}"
        return "редакторская оценка ниже порога"
    if kind == "skipped":
        return "по этой истории уже есть пост"
    return f"подготовка не дошла до поста ({kind})"


def format_report(cfg, report: Dict[str, Any]) -> str:
    """Отчёт словами — для лога и для сообщения владельцу."""
    lines: List[str] = []
    for a in report.get("assigned", []):
        story = a["story"]
        avg = f" · оценка {a['average']}" if a.get("average") else ""
        # Добор по floor виден в отчёте сразу: по этой пометке владелец
        # калибрует AUTO_EDITORIAL_FLOOR, не залезая в базу.
        if a.get("auto_floor"):
            avg += " · добор по floor"
        lines.append(f"{a['title']} · {a['at_local']}{avg}")
        lines.append(f"  «{story['title'][:70]}» [{story['score']}/50]")
    for r in report.get("rejected", []):
        lines.append(f"отбраковано: «{r['title'][:60]}» — {r['why']}")
    for sk in report.get("skipped", []):
        lines.append(f"пропуск: {sk}")
    return "\n".join(lines) or "делать нечего"
