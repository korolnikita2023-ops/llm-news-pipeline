"""Когда будет следующий сбор новостей — по фактическому расписанию.

Расписание живёт в systemd-таймере `news-bot-run.timer` и больше
нигде: дублировать его в конфиге бота нельзя, иначе два источника правды
однажды разойдутся, и бот начнёт называть время, в которое ничего не
происходит.

Поэтому время берётся у самого systemd:

    systemctl show news-bot-run.timer -p NextElapseUSecRealtime

Он уже учёл и оба срабатывания в сутки, и `RandomizedDelaySec`, и смену
расписания, если владелец её сделает. Разбирать `OnCalendar` руками не
нужно и не хочется: у календарных выражений systemd своя семантика, и
свой парсер здесь был бы источником расхождений.

Ответ ЛЮБОЙ проблемы — `None`: на ноутбуке без systemd, при выключенном
таймере, при недоступной команде. Бот в этом случае говорит общими
словами, но не выдумывает время и не падает.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from datetime import datetime, timezone
from typing import Optional

NEWS_TIMER = "news-bot-run.timer"

# Вызов не должен подвешивать обработчик кнопки. systemctl отвечает
# мгновенно; две секунды — с большим запасом.
_TIMEOUT_S = 2.0

# systemd отдаёт микросекунды с эпохи. Ноль означает «следующего
# срабатывания нет»: таймер остановлен или отключён.
_USEC_RE = re.compile(r"^(\d+)$")


def _ask_systemd(prop: str, unit: str) -> Optional[str]:
    if not shutil.which("systemctl"):
        return None
    try:
        out = subprocess.run(
            ["systemctl", "show", unit, "-p", prop, "--value"],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return (out.stdout or "").strip()


def next_run_utc(unit: str = NEWS_TIMER) -> Optional[datetime]:
    """Ближайшее срабатывание таймера сбора новостей, в UTC.

    None — расписание неизвестно: нет systemd, таймер выключен или
    остановлен. Врать про время в этом случае нельзя.
    """
    raw = _ask_systemd("NextElapseUSecRealtime", unit)
    if not raw:
        return None

    # systemd отдаёт либо число микросекунд, либо уже готовую дату —
    # это зависит от версии. Разбираем оба вида.
    m = _USEC_RE.match(raw)
    if m:
        usec = int(m.group(1))
        if usec <= 0:
            return None
        try:
            return datetime.fromtimestamp(usec / 1_000_000, tz=timezone.utc)
        except (OverflowError, OSError, ValueError):
            # Бессмысленно большое число — расписание считаем неизвестным,
            # как и при любой другой невнятице.
            return None

    # systemd 255 отдаёт «Fri 2026-08-28 07:03:36 CEST» — с СОКРАЩЁННЫМ
    # именем зоны. Разобрать его питоном нельзя: strptime %Z смещение не
    # восстанавливает, а подстановка текущего смещения сервера врёт на час
    # по другую сторону перевода часов. Переводит сама система — её база
    # часовых поясов знает и CEST, и CET.
    return _to_utc_via_date(raw)


def _to_utc_via_date(stamp: str) -> Optional[datetime]:
    """Переводит строку времени systemd в UTC силами системной утилиты date.

    Строка приходит от systemd и передаётся отдельным аргументом (shell не
    используется), поэтому подставить туда команду нельзя.
    """
    if not shutil.which("date"):
        return None
    try:
        out = subprocess.run(
            ["date", "-d", stamp, "+%s"],
            capture_output=True, text=True, timeout=_TIMEOUT_S, check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    text = (out.stdout or "").strip()
    if not text.lstrip("-").isdigit():
        return None
    try:
        return datetime.fromtimestamp(int(text), tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None


def _humanize(delta_seconds: int) -> str:
    """«через 2 ч 15 мин» — без секунд и без «0 ч»."""
    if delta_seconds < 60:
        return "меньше минуты"
    minutes = delta_seconds // 60
    hours, minutes = divmod(minutes, 60)
    if hours >= 24:
        days, hours = divmod(hours, 24)
        return f"через {days} дн {hours} ч" if hours else f"через {days} дн"
    if hours:
        return f"через {hours} ч {minutes} мин" if minutes else f"через {hours} ч"
    return f"через {minutes} мин"


# Названия зон по-русски. Зона задаётся настройкой SCHEDULE_TZ, поэтому
# писать «по Москве» в тексте нельзя: сменив настройку, владелец получил бы
# верное время с неверной подписью.
_ZONE_LABELS = {
    "Europe/Moscow": "по Москве",
    "Europe/Kaliningrad": "по Калининграду",
    "Asia/Yekaterinburg": "по Екатеринбургу",
    "Asia/Novosibirsk": "по Новосибирску",
    "UTC": "UTC",
}


def zone_label(cfg) -> str:
    """Подпись часового пояса. Незнакомая зона называется как есть."""
    tz = (getattr(cfg, "schedule_tz", "") or "").strip()
    return _ZONE_LABELS.get(tz, f"по зоне {tz}" if tz else "")


def describe_next_run(cfg, now: datetime | None = None,
                      unit: str = NEWS_TIMER) -> Optional[str]:
    """Человеческая фраза о следующем сборе — или None, если он неизвестен.

    Время показывается в зоне владельца (`SCHEDULE_TZ`), а не в зоне
    сервера: сервер и владелец могут жить в разных зонах, и «08:03» без
    оговорки означало бы для них разные моменты.
    """
    when = next_run_utc(unit)
    if when is None:
        return None

    from src import schedule as slots

    local = when.astimezone(slots._tz(cfg))
    moment = (now or datetime.now(timezone.utc)).astimezone(slots._tz(cfg))

    if local.date() == moment.date():
        day = "сегодня"
    elif (local.date() - moment.date()).days == 1:
        day = "завтра"
    else:
        day = f"{local.day:02d}.{local.month:02d}"

    left = int((when - (now or datetime.now(timezone.utc))).total_seconds())
    tail = f" ({_humanize(left)})" if left > 0 else ""
    return f"{day} в {local.hour:02d}:{local.minute:02d} {zone_label(cfg)}{tail}"
