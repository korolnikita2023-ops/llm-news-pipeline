"""Слоты публикации: единственное место, где живут часы и часовой пояс.

Правила, которые здесь соблюдаются:

* в базе время ВСЕГДА в UTC — сравнивать строки с разными смещениями нельзя;
* часы задаются в местной зоне владельца (`SCHEDULE_TZ`) и берутся из
  конфига, а не хардкодятся по коду;
* просроченный слот публикуется, только если опоздание в пределах запаса
  (`SCHEDULE_GRACE_MINUTES`). Больше — пост возвращается владельцу на
  решение, а не уходит в канал глубокой ночью.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Tuple
from zoneinfo import ZoneInfo

MORNING = "morning"
DAY = "day"
EVENING = "evening"

# Порядок важен: по нему идёт перебор слотов за сутки.
SLOTS = (MORNING, DAY, EVENING)
SLOT_TITLES = {MORNING: "🌅 Утро", DAY: "☀️ День", EVENING: "🌙 Вечер"}


class ScheduleConfigError(RuntimeError):
    """Расписание настроено неверно. Останавливаемся громко."""


def _tz(cfg) -> ZoneInfo:
    """Зона владельца. Опечатка — остановка, а не тихий откат на UTC.

    Молчаливый откат был бы худшим из возможных исходов: пост выходил бы
    на три часа мимо слота, и понять это можно было бы только по времени
    сообщений в канале. Опечатку в .env лучше увидеть сразу.
    """
    try:
        return ZoneInfo(cfg.schedule_tz)
    except Exception as exc:
        raise ScheduleConfigError(
            f"SCHEDULE_TZ={cfg.schedule_tz!r} — такой зоны нет ({exc}). "
            f"Ожидается имя из базы IANA, например Europe/Moscow."
        ) from exc


def _slot_time(cfg, slot: str) -> Tuple[int, int]:
    """Час и минута слота. Проверяются диапазоны, а не только формат.

    Без проверки строка вроде «25:00» разбиралась успешно, а падала уже
    в datetime.replace — то есть внутри обработчика нажатия, где владелец
    видел бы только «что-то пошло не так».
    """
    raw = {
        MORNING: cfg.morning_slot,
        DAY: getattr(cfg, "day_slot", "14:00"),
        EVENING: cfg.evening_slot,
    }.get(slot)
    if raw is None:
        raise ScheduleConfigError(
            f"неизвестный слот {slot!r}; известны: {', '.join(SLOTS)}")
    try:
        hh, mm = raw.split(":")
        hour, minute = int(hh), int(mm)
    except Exception as exc:
        raise ScheduleConfigError(
            f"время слота {slot} задано как {raw!r} — ожидается ЧЧ:ММ ({exc})"
        ) from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise ScheduleConfigError(
            f"время слота {slot} задано как {raw!r} — часы 0–23, минуты 0–59"
        )
    return hour, minute


def next_slot_utc(cfg, slot: str, now: datetime | None = None) -> str:
    """Ближайшее наступление слота, в UTC ISO.

    Если сегодняшнее время слота уже прошло — берётся завтрашнее.
    """
    tz = _tz(cfg)
    now_local = (now or datetime.now(timezone.utc)).astimezone(tz)
    hour, minute = _slot_time(cfg, slot)
    target = now_local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if target <= now_local:
        target += timedelta(days=1)

    # Сравнение выше идёт по местному времени, а хранится и сравнивается
    # потом — UTC. В зоне с переводом часов это расходится: при осеннем
    # повторе часа местное «позже» может дать UTC-момент в прошлом, и
    # таймер опубликовал бы пост немедленно. Досчитываем по UTC.
    now_utc = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    while target.astimezone(timezone.utc) <= now_utc:
        target += timedelta(days=1)
    return target.astimezone(timezone.utc).isoformat(timespec="seconds")


def describe(cfg, publish_at_utc: str) -> str:
    """Человеческая подпись времени в зоне владельца."""
    try:
        moment = datetime.fromisoformat(publish_at_utc).astimezone(_tz(cfg))
    except Exception:
        return publish_at_utc or "—"
    # Не strftime: на Windows формат прогоняется через локаль C, и русская
    # «в» внутри шаблона роняет вызов UnicodeEncodeError. Собираем руками.
    return f"{moment.day:02d}.{moment.month:02d} в {moment.hour:02d}:{moment.minute:02d}"


def is_too_late(cfg, publish_at_utc: str, now: datetime | None = None) -> bool:
    """Опоздание больше допустимого — публиковать нельзя.

    Таймер мог не работать (перезагрузка, сбой), и слот прошёл часы назад.
    Публиковать «утренний» пост ночью нельзя, поэтому такой пост
    возвращается владельцу на решение.
    """
    try:
        due = datetime.fromisoformat(publish_at_utc)
    except Exception:
        return True
    # Секунды, а не микросекунды: при нулевом запасе пост, наступивший
    # ровно в 08:30:00, иначе считался бы просроченным уже в 08:30:00.001.
    moment = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    late = moment - due
    return late > timedelta(minutes=max(cfg.schedule_grace_minutes, 0))


def day_bounds_utc(cfg, now: datetime | None = None) -> Tuple[str, str]:
    """Границы текущих суток владельца, выраженные в UTC.

    Суточный потолок выборов — обещание человеку («не больше двух в день»),
    поэтому «день» здесь его местный, а не UTC: для Москвы граница сдвинута
    на три часа, и по UTC-дате вечерний выбор попал бы уже в завтра.
    """
    tz = _tz(cfg)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    return (start.astimezone(timezone.utc).isoformat(timespec="seconds"),
            end.astimezone(timezone.utc).isoformat(timespec="seconds"))


def now_utc_iso() -> str:
    """Единый формат времени для сравнения со `publish_at` в базе."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _check_slots_differ(cfg) -> None:
    """Два слота в одно время — почти наверняка опечатка.

    Один слот, один пост: совпавшее время означало бы, что второй пост
    некуда поставить, и система молча публиковала бы меньше обещанного.
    """
    times = {}
    for slot in SLOTS:
        moment = _slot_time(cfg, slot)
        if moment in times:
            raise ScheduleConfigError(
                f"слоты {times[moment]} и {slot} назначены на одно время "
                f"{moment[0]:02d}:{moment[1]:02d} — проверьте настройки")
        times[moment] = slot


def slot_time_today_utc(cfg, slot: str, now: datetime | None = None) -> str:
    """Момент СЕГОДНЯШНЕГО слота в UTC, даже если он уже прошёл.

    `next_slot_utc` всегда смотрит вперёд и на прошедшем слоте перепрыгивает
    на завтра. Автоматическому режиму нужно знать именно сегодняшний момент:
    по нему видно, наступило ли время готовить пост.
    """
    tz = _tz(cfg)
    local = (now or datetime.now(timezone.utc)).astimezone(tz)
    hour, minute = _slot_time(cfg, slot)
    target = local.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return target.astimezone(timezone.utc).isoformat(timespec="seconds")


def validate(cfg) -> None:
    """Проверяет настройки расписания на старте, а не в момент нажатия.

    Опечатка в зоне или во времени слота иначе всплывёт внутри обработчика
    кнопки: владелец увидит «что-то пошло не так», а причина останется в
    логах. Лучше не подняться вообще и сказать, что именно чинить.
    """
    _tz(cfg)
    for slot in SLOTS:
        _slot_time(cfg, slot)
    _check_slots_differ(cfg)
