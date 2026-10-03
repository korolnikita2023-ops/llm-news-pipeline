"""Approve-бот.

aiogram 3, long-polling, отдельный процесс от main.py. Шлёт кандидата (один
пост за раз, не стеной) владельцу в личный чат с кнопками:
[✅ Пост] [✏️ Править] [🔁 Перегенерить] [❌ Мимо] [⏸ В запас]

Правка: следующее сообщение владельца после нажатия "✏️ Править"
воспринимается как новый текст поста (FSM-состояние).

Запуск (отдельно от пайплайна): `python -m src.approval_bot`
Требует TELEGRAM_BOT_TOKEN и TELEGRAM_OWNER_CHAT_ID в .env — без них падает
громко при старте (NO SILENT FAILURES), а не молча ничего не делает.

MVP: только ручной approve/reject/regenerate/edit-цикл на кандидатах из
SQLite (`posts.status = 'candidate'`). Autopublish по накопленной точности
(≥95% на 300+ решений) здесь НЕ реализовано.
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Optional

from config import Config, get_config
from src.publisher import TELEGRAM_CAPTION_LIMIT, TELEGRAM_TEXT_LIMIT
from src import roles, storage

# см. src/main.py — та же причина (Windows-консоль не в UTF-8 по умолчанию).
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass

try:
    from aiogram import Bot, Dispatcher, F
    from aiogram.filters import Command
    from aiogram.fsm.context import FSMContext
    from aiogram.fsm.state import State, StatesGroup
    from aiogram.fsm.storage.memory import MemoryStorage
    from aiogram.types import (
        BotCommand,
        CallbackQuery,
        InlineKeyboardButton,
        InlineKeyboardMarkup,
        KeyboardButton,
        Message,
        MenuButtonCommands,
        ReplyKeyboardMarkup,
    )
except ImportError:
    # aiogram не установлен. Модуль всё равно должен ИМПОРТИРОВАТЬСЯ без падения
    # (main.py его не трогает, но `python -m src.approval_bot` должен дать
    # понятную ошибку в main(), а не трейсбек на импорте) — поэтому минимальные
    # заглушки для имён, использованных на уровне модуля ниже.
    Bot = None  # type: ignore

    class StatesGroup:  # type: ignore
        pass

    class State:  # type: ignore
        pass


class LLMBudgetExceededError(RuntimeError):
    """Дневной лимит расходов на модель исчерпан — платные вызовы запрещены."""


class EditStates(StatesGroup):
    waiting_for_new_text = State()


class AdminStates(StatesGroup):
    """Владелец добавляет редактора: следующее сообщение — ID или пересланное
    сообщение того, кого добавляем."""

    waiting_for_new_admin = State()


# ---------------------------------------------------------------------------
# Постоянное меню владельца.
#
# Инлайн-кнопки живут только под конкретным постом, а slash-команды нужно
# помнить. Reply-клавиатура висит всегда — основные действия доступны в один
# тап, без запоминания команд.
# ---------------------------------------------------------------------------

BTN_NEXT = "📰 Следующий пост"
BTN_STATUS = "📊 Статус"
BTN_ADMINS = "👥 Администраторы"
BTN_DIGEST = "🗞 Дайджест"
BTN_SCHEDULED = "🗓 Запланировано"


def _main_menu() -> "ReplyKeyboardMarkup":
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_DIGEST), KeyboardButton(text=BTN_SCHEDULED)],
            [KeyboardButton(text=BTN_NEXT)],
            [KeyboardButton(text=BTN_STATUS), KeyboardButton(text=BTN_ADMINS)],
        ],
        resize_keyboard=True,          # компактная, не на пол-экрана
        is_persistent=True,            # не прячется после нажатия
        input_field_placeholder="Выберите действие или напишите текст правки",
    )


BOT_COMMANDS = [
    ("review", "Следующий пост на утверждение"),
    ("queue", "Что в очереди"),
    ("budget", "Расходы на модель"),
    ("digest", "Короткий список дня"),
    ("scheduled", "Запланированные публикации"),
    ("admins", "Кто имеет доступ к боту"),
    ("start", "Меню и справка"),
]


async def setup_bot_menu(bot: "Bot") -> bool:
    """Регистрирует список команд и кнопку «Меню» рядом с полем ввода.

    Это стандартный интерфейс Telegram: команды видны по нажатию «/» и в
    кнопке меню, без запоминания. Сбой регистрации не должен мешать боту
    работать — меню это удобство, а не условие запуска.
    """
    try:
        await bot.set_my_commands(
            [BotCommand(command=c, description=d) for c, d in BOT_COMMANDS]
        )
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
        return True
    except Exception as exc:
        print(f"[approval_bot] меню Telegram не зарегистрировано: {exc}")
        return False


def _digest_screen(cfg: Config) -> tuple[str, "InlineKeyboardMarkup | None"]:
    """Короткий список дня: что конвейер нашёл, но ещё не переписывал.

    Дорогая генерация запускается только для выбранного — поэтому здесь
    заголовок источника, оценка и сигналы отбора, а не готовый пост.
    """
    from src import digest, schedule, storage as st

    # Срок жизни списка соблюдается здесь, а не только в конвейере: день
    # без релевантных новостей заканчивается ранним выходом, и вчерашний
    # дайджест иначе пережил бы свой срок и остался бы доступен для
    # дорогой генерации по протухшей новости.
    st.archive_stale_digest(cfg.digest_ttl_hours)
    rows = st.digest_pool(cfg.telegram_channel_id, cfg.digest_size)
    if not rows:
        return (
            "🗞 Дайджест пуст.\n\nБлижайший сбор новостей наполнит его — "
            "или конвейер работает в прежнем режиме (WORKFLOW_MODE=legacy), "
            "и посты приходят сразу кнопкой «📰 Следующий пост».",
            None,
        )

    attempts, successes = st.count_selections_today(*schedule.day_bounds_utc(cfg))
    posts_left = max(cfg.max_successful_selections_per_day - successes, 0)
    tries_left = max(cfg.max_preparation_attempts_per_day - attempts, 0)
    # В AUTO список обязан показывать доступность глазами АВТОМАТИКИ: 30.08
    # экран говорил «1 история», а AUTO видел ноль — история уже провалила
    # сегодняшнюю подготовку и до полуночи не берётся. Владелец при этом
    # вправе выбрать её вручную, поэтому строка помечается, а не прячется.
    failed_today: set = set()
    head_count = f"🗞 Дайджест дня — {len(rows)} историй."
    if cfg.workflow_mode == "auto":
        from src import auto_editor

        failed_today = st.failed_item_ids_today(*schedule.day_bounds_utc(cfg))
        # Счётчик — тем же источником истины, что и сам AUTO: candidates()
        # смотрит весь пул, а на экране только top-N строк. Считать по
        # видимым строкам значило бы показать «0», когда одиннадцатая
        # история пула прекрасно доступна (нашёл Codex).
        head_count = (f"🗞 Дайджест дня — {len(rows)} историй, "
                      f"доступно AUTO сейчас: {len(auto_editor.candidates(cfg))}.")
    lines = [
        head_count,
        f"Готовых постов за сегодня: {successes} из "
        f"{cfg.max_successful_selections_per_day}, можно ещё {posts_left}.",
        f"Платных попыток осталось: {tries_left} из "
        f"{cfg.max_preparation_attempts_per_day}.",
        "",
    ]
    buttons: list[list] = []
    row_buttons: list = []
    for i, row in enumerate(rows, 1):
        mark = " ✅ выбрана" if row["chosen"] else ""
        if not mark and row["id"] in failed_today:
            mark = " ⚪ уже пробовали сегодня — AUTO возьмёт снова завтра"
        lines.append(digest.format_line(i, row) + mark)
        lines.append("")
        if not row["chosen"]:
            row_buttons.append(
                InlineKeyboardButton(text=str(i), callback_data=f"pick:{row['id']}")
            )
            if len(row_buttons) == 5:
                buttons.append(row_buttons)
                row_buttons = []
    if row_buttons:
        buttons.append(row_buttons)

    lines.append("Нажмите номер, чтобы отправить историю в работу.")
    keyboard = InlineKeyboardMarkup(inline_keyboard=buttons) if buttons else None
    return "\n".join(lines)[:TELEGRAM_TEXT_LIMIT], keyboard


def _status_screen(cfg: Config, next_run: str | None = None) -> str:
    """Что происходит и что делать дальше — одним сообщением.

    Порядок строк — по убыванию полезности: сначала то, что требует
    действия сейчас, потом ближайшие события, потом остаток лимитов, и
    только в конце накопленная статистика.

    `next_run` приходит готовой строкой, а не считается здесь: за ней
    стоит вызов systemctl, а он блокирующий. Из обработчика кнопки его
    зовут через отдельный поток, иначе зависший systemctl задержал бы
    обработку ВСЕХ сообщений бота на пару секунд.
    """
    from src import schedule as slots, storage as st

    counts = st.count_posts_by_status()
    cost = st.get_cost_summary()
    attempts, successes = st.count_selections_today(*slots.day_bounds_utc(cfg))
    digest_rows = st.digest_pool(cfg.telegram_channel_id, cfg.digest_size)
    free_digest = sum(1 for r in digest_rows if not r["chosen"])

    lines = ["📊 Статус", ""]

    auto = cfg.workflow_mode == "auto"
    if auto:
        # В автоматическом режиме первым делом видно, что происходит без
        # участия человека и сколько уже вышло.
        from src import auto_editor

        lines.append("🤖 Режим: AUTO — система выбирает и публикует сама")
        try:
            done = auto_editor.published_today(cfg)
            lines.append(f"Опубликовано сегодня: {done} из "
                         f"{cfg.auto_max_posts_per_day} (потолок, не план)")
            for st in auto_editor.slot_state(cfg):
                if st["filled"]:
                    mark = ("опубликован" if st["filled"]["status"] == "published"
                            else f"назначен {st['filled']['post_id']}")
                    lines.append(f"  {st['title']} · {st['at_local']} — {mark}")
                elif st["passed"]:
                    lines.append(f"  {st['title']} · {st['at_local']} — прошёл пустым")
                else:
                    lines.append(f"  {st['title']} · {st['at_local']} — ждёт подбора")
        except Exception as exc:
            lines.append(f"  состояние слотов не прочиталось: {exc}")
        lines.append("")

    if cfg.threads_enabled:
        lines += _threads_lines(cfg)

    # --- что требует внимания прямо сейчас
    lines.append(f"Ждут решения: {_candidates_left(cfg.telegram_channel_id)}")
    if free_digest:
        line = f"В дайджесте не разобрано: {free_digest} из {len(digest_rows)}"
        if auto:
            # Глазами автоматики: часть неразобранного могла уже провалить
            # сегодняшнюю подготовку — AUTO возьмёт её не раньше завтра.
            try:
                from src import auto_editor

                line += f" · доступно AUTO сейчас: {len(auto_editor.candidates(cfg))}"
            except Exception as exc:
                print(f"[approval_bot] статус: не удалось посчитать "
                      f"доступное AUTO ({exc}) — показываю без этой цифры")
        lines.append(line)
    lines.append(f"В запасе: {counts.get('hold', 0)}")

    # --- ближайшие события
    lines.append("")
    scheduled = st.scheduled_posts()
    if scheduled:
        nearest = scheduled[0]
        title = slots.SLOT_TITLES.get(nearest["slot"], nearest["slot"] or "—")
        head = " ".join((nearest.get("headline") or "").split())[:60]
        lines.append(f"Ближайшая публикация: {title} · "
                     f"{slots.describe(cfg, nearest['publish_at'])}")
        if head:
            lines.append(f"  «{head}»")
        if len(scheduled) > 1:
            lines.append(f"  всего в расписании: {len(scheduled)}")
    else:
        lines.append("Ничего не запланировано")

    lines.append(f"Следующий сбор новостей: {next_run}" if next_run
                 else "Следующий сбор новостей: расписание недоступно")

    # --- сколько осталось сегодня
    lines.append("")
    lines.append(f"Сегодня: постов {successes} из "
                 f"{cfg.max_successful_selections_per_day} · "
                 f"попыток {attempts} из {cfg.max_preparation_attempts_per_day}")
    lines.append(f"Потрачено: ${cost['today']:.4f} из "
                 f"${cfg.daily_cost_limit_usd:.2f} · за месяц ${cost['month']:.4f}")

    # --- накопленное, мелким шрифтом
    lines.append("")
    lines.append(f"Всего опубликовано: {counts.get('published', 0)}")
    rejected = st.rejected_by_reason()
    total_rejected = sum(rejected.values())
    if total_rejected:
        lines.append(f"Забраковано за всё время: {total_rejected}")
        for reason, n in sorted(rejected.items(), key=lambda kv: -kv[1]):
            # Пустая причина бывает только у записей, сделанных до того, как
            # её начали сохранять. Писать про них «причина не записана» —
            # значит намекать на сбой там, где его не было.
            label = st.REJECT_LABELS.get(reason) or "решения до 28.08, причина не сохранялась"
            lines.append(f"  {label}: {n}")

    lines.append("")
    lines.append("Подробнее о расходах: /budget")
    return "\n".join(lines)


def _schedule_keyboard(post_id: str, borderline: bool = False) -> "InlineKeyboardMarkup":
    rows = [
        [
            InlineKeyboardButton(text="🌅 Утро", callback_data=f"slot:morning:{post_id}"),
            InlineKeyboardButton(text="🌙 Вечер", callback_data=f"slot:evening:{post_id}"),
        ],
        [
            InlineKeyboardButton(text="🚀 Опубликовать сейчас", callback_data=f"pubnow:{post_id}"),
            InlineKeyboardButton(text="❌ Отклонить", callback_data=f"reject:{post_id}"),
        ],
    ]
    if borderline:
        # У пограничного поста выбор шире: его показали именно потому, что
        # оценка спорная, — значит переписать заново должно быть в один тап.
        rows.insert(1, [InlineKeyboardButton(
            text="🔁 Перегенерировать", callback_data=f"regen:{post_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def _scheduled_screen(cfg: Config) -> tuple[str, "InlineKeyboardMarkup | None"]:
    from src import schedule, storage as st

    rows = st.scheduled_posts()
    if not rows:
        return "🗓 Запланированных публикаций нет.", None

    lines = ["🗓 Ближайшие публикации:", ""]
    buttons: list[list] = []
    for row in rows:
        when = schedule.describe(cfg, row["publish_at"])
        title = schedule.SLOT_TITLES.get(row["slot"], row["slot"] or "—")
        # Заголовок карточки — то, чем пост назван. Раньше здесь стояла
        # первая строка текста, и владелец в расписании видел обрывок
        # предложения вместо названия материала.
        headline = " ".join((row.get("headline") or "").split())
        preview = " ".join((row["preview"] or "").split())[:110]
        lines.append(f"{title} · {when}")
        if headline:
            lines.append(f"«{headline}»")
        lines.append(f"{preview}…")
        lines.append(f"`{row['post_id']}`\n")
        buttons.append([
            InlineKeyboardButton(text=f"👁 {when}", callback_data=f"showsched:{row['post_id']}"),
            InlineKeyboardButton(text="🚀 Сейчас", callback_data=f"pubnow:{row['post_id']}"),
            InlineKeyboardButton(text="✖️ Снять", callback_data=f"unsched:{row['post_id']}"),
        ])
    return "\n".join(lines)[:TELEGRAM_TEXT_LIMIT], InlineKeyboardMarkup(inline_keyboard=buttons)


def _generate_from_digest(cfg: Config, item_row: dict,
                          borderline_ok: bool | None = None,
                          auto_floor: float | None = None) -> dict:
    """Проводит выбранную историю через существующие проверенные стадии.

    Ничего своего здесь нет: вызывается тот же `_process_item`, что и в
    конвейере, — переписывание Style v1, юридический гейт, редакторская
    оценка, карточка, запись кандидата.

    Возвращает разбор исхода, а не голый post_id: раньше при отказе
    возвращался None, и бот мог сказать владельцу только «не прошло гейт
    ИЛИ оценку, причина в логах». Человека отправляли читать логи сервера
    из-за отказа, причина которого уже была посчитана.

        {"post_id": str|None, "outcome": "ok"|"borderline"|"legal_gate"
                                         |"editorial"|"lost",
         "average": float|None, "threshold": float, "weakest": [(имя, балл)],
         "issues": [...]}
    """

    from src import main as pipeline
    from src.normalize import NewsItem

    # Неизвестный исход считаем ОПЛАЧЕННЫМ. Это сюда попасть не должно, но
    # если попадёт — безопаснее списать попытку, чем открыть путь к
    # бесплатным повторам: деньги защищать важнее, чем экономить владельцу
    # одно нажатие.
    fail = {"post_id": None, "outcome": "lost", "average": None,
            "threshold": cfg.min_editorial_avg, "weakest": [], "issues": [],
            "paid_work": True}

    with storage.get_conn() as conn:
        raw = conn.execute(
            "SELECT * FROM raw_items WHERE url_hash=?", (item_row["url_hash"],)
        ).fetchone()
    if raw is None:
        return fail

    data = dict(raw)
    item = NewsItem(
        url_hash=data["url_hash"], source=data["source"], title=data["title"],
        body=data["body"] or "", url=data["url"] or "",
        published_at=data["published_at"] or "", fetched_at=data["fetched_at"],
        lang=data["lang"] or "ru", category=data["category"] or "",
        sample=bool(data["sample"]),
        source_kind=data.get("source_kind") or "", source_role=data.get("source_role") or "",
    )
    relevance = _relevance_from_digest(item_row)

    stats = {"approved": 0, "manual_only": 0, "hard_rejected": 0, "regenerated": 0}
    # borderline_ok: в ручном режиме пост с пограничной оценкой не
    # выбрасывается, а показывается владельцу с пометкой. Второго пути
    # публикации при этом не возникает — пост остаётся обычным кандидатом.
    #
    # supersede_auto_reject: владелец сознательно берёт историю ещё раз.
    # Идентификатор черновика детерминирован, и без этого разрешения новый
    # результат — хоть отказ, хоть успешный пост — не смог бы лечь поверх
    # автоматического отказа прошлой попытки. Решение самого владельца
    # (нажатое ❌) не затрагивается никогда.
    # Пограничная полоса 8.0–8.59 придумана для случая, когда спорную
    # оценку взвешивает человек. В автоматическом режиме взвешивать некому,
    # поэтому там строгий порог — вызывающий говорит об этом явно.
    if borderline_ok is None:
        borderline_ok = cfg.workflow_mode == "digest"
    # auto_floor — политика автоматической редакции: передаётся только
    # auto_editor'ом. Ручной выбор владельца идёт со старым поведением.
    pipeline._process_item(cfg, item, relevance, stats,
                           borderline_ok=borderline_ok,
                           supersede_auto_reject=True,
                           auto_floor=auto_floor)

    # Исход берём у конвейера — он только что его посчитал и знает точно.
    # Раньше здесь стоял запрос «самый свежий пост по этому сюжету», и он
    # читал ЧУЖУЮ запись, оставшуюся от прошлой подготовки: конвейер
    # получил отказ редактора, а владелец видел «что-то сорвалось».
    outcome = stats.get("outcome")
    if not outcome:
        return fail

    kind = outcome.get("kind")
    post_id = outcome.get("post_id")
    scores = outcome.get("scores") or {}

    if kind == "skipped":
        # Работы не было: к модели не обращались, платить не за что.
        return {
            "post_id": None, "outcome": "skipped", "average": None,
            "threshold": cfg.min_editorial_avg, "weakest": [], "issues": [],
            "paid_work": False, "post_status": outcome.get("post_status"),
        }

    axes = [("тон", "tone_match"), ("новостная ценность", "news_value"),
            ("вовлечение", "engagement"), ("оригинальность", "originality"),
            ("фактическая безопасность", "factual_safety"),
            ("юридическая безопасность", "legal_safety")]
    present = [(name, scores[key]) for name, key in axes if key in scores]
    weakest = sorted(present, key=lambda kv: kv[1])[:2]

    out = {
        "post_id": post_id,
        "average": scores.get("average"),
        # Пометка добора обязана долетать до ежедневного отчёта: владелец
        # калибрует floor по тому, сколько постов вышло добором, и лазить
        # за этим в базу он не должен.
        "auto_floor": bool(scores.get("auto_floor")),
        "threshold": cfg.min_editorial_avg,
        "weakest": weakest,
        # Для отказа гейта берём его претензии, для редакторского — замечания
        # скорера: это разные списки, и путать их нельзя.
        "issues": (outcome.get("gate_issues")
                   if kind == storage.REJECT_BY_LEGAL
                   else scores.get("issues")) or [],
        "paid_work": bool(outcome.get("paid_work", True)),
    }
    if kind == "ok":
        out["outcome"] = "borderline" if outcome.get("borderline") else "ok"
    elif kind == storage.REJECT_BY_LEGAL:
        out["outcome"] = "legal_gate"
    elif kind == storage.REJECT_BY_EDITOR:
        out["outcome"] = "editorial"
    elif kind == "not_saved":
        out["outcome"] = "not_saved"
    else:
        out["outcome"] = "lost"
    return out


def _candidate_keyboard(post_id: str) -> "InlineKeyboardMarkup":
    buttons = [
        [
            InlineKeyboardButton(text="✅ Пост", callback_data=f"approve:{post_id}"),
            InlineKeyboardButton(text="✏️ Править", callback_data=f"edit:{post_id}"),
        ],
        [
            InlineKeyboardButton(text="🔁 Перегенерить", callback_data=f"regen:{post_id}"),
            InlineKeyboardButton(text="❌ Мимо", callback_data=f"reject:{post_id}"),
        ],
        [InlineKeyboardButton(text="⏸ В запас", callback_data=f"hold:{post_id}")],
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def _next_candidate(
    db_path: Optional[Path] = None,
    after: Optional[str] = None,
    channel_id: Optional[str] = None,
) -> Optional[dict]:
    """Следующий пост на решение.

    `after` — post_id уже показанного поста. Без него возвращается самый
    старый нерешённый. Это и есть правильная семантика очереди, но кнопка
    «Следующий пост» с ней вела себя неожиданно: пока владелец не нажал
    ✅/❌/⏸, повторное нажатие приносило ТОТ ЖЕ пост, и выглядело это как
    зависание. Теперь кнопка листает дальше по очереди, а в конце
    возвращается к началу.
    """
    fields = "post_id, draft_ru, format, scores, manual_only, created_at"
    # Только посты ТЕКУЩЕГО канала. Черновики, сделанные до боевого запуска
    # для песочницы, в боевой очереди не место: нажатие ✅ отправило бы в
    # настоящий канал материал, подготовленный для проверки. Пустое значение
    # канала — записи старше этой колонки, их считаем своими.
    where = "status IN ('candidate','approved')"
    chan: list = []
    if channel_id is not None:
        where += " AND (COALESCE(channel_id,'') IN ('', ?))"
        chan = [channel_id]

    with storage.get_conn(db_path) as conn:
        if after:
            prev = conn.execute(
                "SELECT created_at FROM posts WHERE post_id=?", (after,)
            ).fetchone()
            if prev:
                row = conn.execute(
                    f"SELECT {fields} FROM posts WHERE {where} "
                    "AND (created_at > ? OR (created_at = ? AND post_id > ?)) "
                    "ORDER BY created_at ASC, post_id ASC LIMIT 1",
                    (*chan, prev[0], prev[0], after),
                ).fetchone()
                if row:
                    return dict(row)
                # Дошли до конца очереди — начинаем сначала.
        row = conn.execute(
            f"SELECT {fields} FROM posts WHERE {where} "
            "ORDER BY created_at ASC, post_id ASC LIMIT 1",
            chan,
        ).fetchone()
        return dict(row) if row else None


def _candidates_left(channel_id: Optional[str] = None,
                     db_path: Optional[Path] = None) -> int:
    """Сколько постов ждут решения В ЭТОМ канале."""
    sql = "SELECT COUNT(*) FROM posts WHERE status IN ('candidate','approved')"
    params: list = []
    if channel_id is not None:
        sql += " AND (COALESCE(channel_id,'') IN ('', ?))"
        params.append(channel_id)
    with storage.get_conn(db_path) as conn:
        return conn.execute(sql, params).fetchone()[0]


def _case_status_of(post_id: str, db_path: Optional[Path] = None) -> Optional[str]:
    """Судебный статус дела по исходной новости этого поста.

    Нужен всем проверкам гейта внутри бота, а не только конвейеру. Без него
    правка владельца по делу с приговором отклонялась бы с требованием
    написать «по версии следствия» — то есть ровно там, где это неверно.

    Источник недоступен (запись потёрта, база старая) — возвращаем None, и
    гейт работает в самом строгом режиме. Это правильная сторона ошибки.
    """
    from src import case_status as case_status_mod

    item = _load_news_item(post_id, db_path)
    if item is None:
        return None
    return case_status_mod.detect(item.title, item.body)


def _load_news_item(post_id: str, db_path: Optional[Path] = None):
    """Достаёт исходную новость по посту: posts.event_id → events.best_item
    → raw_items. Нужна для НАСТОЯЩЕЙ перегенерации (кнопка 🔁): без исходника
    переписать пост невозможно.
    """
    from src.normalize import NewsItem

    with storage.get_conn(db_path) as conn:
        row = conn.execute(
            """
            SELECT r.* FROM posts p
            JOIN events e ON e.event_id = p.event_id
            JOIN raw_items r ON r.url_hash = e.best_item
            WHERE p.post_id = ?
            """,
            (post_id,),
        ).fetchone()
    if row is None:
        return None
    d = dict(row)
    return NewsItem(
        url_hash=d["url_hash"], source=d["source"], title=d["title"],
        body=d["body"], url=d["url"], published_at=d["published_at"],
        fetched_at=d["fetched_at"], lang=d["lang"] or "ru",
        category=d["category"] or "", sample=bool(d["sample"]),
    )


def _threads_lines(cfg: Config) -> list:
    """Состояние зеркала в Threads для экрана статуса.

    Своей секции у Threads нет намеренно: это распространение, а не
    редакция. Владельцу нужно видеть три вещи — жива ли связь, сколько
    ушло сегодня и не застряло ли что-то.
    """
    from src import schedule

    try:
        state = storage.integration_get("threads")
        counts = storage.crosspost_counts("threads", *schedule.day_bounds_utc(cfg))
    except Exception as exc:
        return [f"Threads: состояние не прочиталось ({exc})", ""]

    if state.get("degraded"):
        head = "Threads: ⚠️ требуется авторизация"
    else:
        head = "Threads: ✅ подключён"

    out = [head, f"  Сегодня опубликовано: {counts['done_today']}"]
    if counts["queued"]:
        out.append(f"  В очереди: {counts['queued']}")
    if counts["stuck"]:
        out.append(f"  Застряло, нужен разбор: {counts['stuck']}")
    if counts["skipped"]:
        out.append(f"  Сознательно пропущено: {counts['skipped']}")
    if counts["last_at"]:
        out.append(f"  Последняя публикация: {schedule.describe(cfg, counts['last_at'])}")

    expires = state.get("token_expires_at")
    if expires:
        out.append(f"  Токен действует до: {schedule.describe(cfg, expires)}")
    if counts["last_error"]:
        out.append(f"  Последняя ошибка: {str(counts['last_error'])[:120]}")
    out.append("")
    return out


def _publish_post(cfg: Config, post_id: str,
                  require_status: Optional[str] = None) -> tuple[bool, str]:
    """Единственная точка публикации в канал. Используется и кнопкой ✅,
    и ручной правкой — раньше правка молча оставляла пост лежать в базе.

    Юридический гейт прогоняется ПЕРЕД отправкой ещё раз, в том числе по
    тексту, который владелец написал руками: гейт — это код, и он не должен
    обходиться просто потому, что текст набрал человек.
    """
    from config import IMAGES_DIR
    from src import legal_gate, publisher

    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT draft_ru, status, channel_id FROM posts WHERE post_id=?",
            (post_id,),
        ).fetchone()
    if row is None:
        return False, "пост не найден в базе"

    # Пост, подготовленный для другого канала, в этот канал не уходит.
    # Кнопки живут в переписке вечно: ✅ под черновиком времён песочницы
    # иначе отправила бы в настоящий канал материал, сделанный для проверки.
    post_channel = (row[2] or "").strip()
    if post_channel and post_channel != (cfg.telegram_channel_id or "").strip():
        return False, (
            f"пост готовился для другого канала ({post_channel}) — "
            f"в текущий он не отправляется"
        )

    # Защита от двойной публикации: Telegram доставляет нажатие кнопки
    # повторно при плохой связи, и владелец сам может ткнуть ✅ дважды.
    if row[1] == "published":
        return False, "этот пост уже опубликован"

    # Кнопки живут в переписке вечно. Нажатие ✅ на старом сообщении поста,
    # который владелец уже отклонил, публиковало бы в канал именно то, что
    # он забраковал. Отклонение — осознанное решение, отменять его случайным
    # тапом по старому сообщению нельзя.
    if row[1] == "rejected":
        return False, (
            "пост был отклонён — публикация из старого сообщения отменена. "
            "Если решение изменилось, верните пост в очередь вручную"
        )

    text = (row[0] or "").strip()
    if not text:
        return False, "текст поста пустой — публиковать нечего"

    # strict_style=False: редакторские штампы — не юридическое нарушение.
    # Иначе правка владельца отклонялась с формулировкой «юр. гейт не
    # пропускает: кроме того», и человек не понимал, что нарушил.
    gate = legal_gate.check_post(text, _source_url_of(post_id), strict_style=False,
                                 case_status=_case_status_of(post_id))
    if gate.hard_fail:
        return False, f"юр. гейт не пропускает: {'; '.join(gate.issues)}"

    if not cfg.is_live_publish():
        return False, "PUBLISH_MODE не live — публикация выключена в настройках"

    # Атомарный захват поста перед отправкой. Проверки статуса выше
    # недостаточно: два одновременных нажатия ✅ успевают прочитать статус
    # до того, как первое его запишет, и пост уходит в канал дважды.
    # UPDATE ... WHERE status NOT IN (...) выполняется атомарно, поэтому
    # захват получает ровно один вызов.
    with storage.get_conn() as conn:
        if require_status:
            # Захват по ожидаемому статусу нужен таймеру: между тем, как он
            # прочитал список наступивших слотов, и этой строкой владелец
            # успевает нажать «✖️ Снять». Без условия пост всё равно уйдёт
            # в канал — ровно тот, который человек только что отменил.
            claimed = conn.execute(
                "UPDATE posts SET status='publishing' WHERE post_id=? AND status=?",
                (post_id, require_status),
            ).rowcount
        else:
            claimed = conn.execute(
                "UPDATE posts SET status='publishing' WHERE post_id=? "
                "AND status NOT IN ('published','publishing')",
                (post_id,),
            ).rowcount
    if not claimed:
        if require_status:
            return False, (
                f"пост уже не в статусе {require_status!r} — решение изменилось, "
                f"публикация по расписанию отменена"
            )
        return False, "публикация этого поста уже идёт"

    image_path = IMAGES_DIR / f"{post_id}.png"
    try:
        sent, status = publisher.send_live_text(
            cfg, text, image_path if image_path.exists() else None,
            source_url=_source_url_of(post_id),
        )
    except Exception as exc:  # сеть/таймаут — пост не должен застрять в 'publishing'
        storage.finish_publishing(post_id, sent=False)
        # Текст ошибки уходит владельцу в Telegram и в журнал. У httpx в нём
        # лежит полный URL, а в URL Bot API стоит токен — вычищаем.
        from src.publisher import _redact

        return False, f"ошибка отправки: {_redact(str(exc), cfg.telegram_bot_token)}"

    # Возвращаем пост в очередь на публикацию, если отправка не удалась, —
    # иначе он навсегда останется в промежуточном статусе и потеряется.
    # Статус и расписание меняются одним запросом: слот, отработавший вхолостую,
    # держать нельзя — иначе таймер будет бить в ту же стену каждые пять минут.
    storage.finish_publishing(post_id, sent=sent)

    # Зеркало в Threads. Отдельным вызовом ПОСЛЕ того, как статус поста уже
    # зафиксирован: сбой постановки в очередь не имеет права откатить
    # 'published' — пост, ушедший в канал и оставшийся в 'publishing',
    # таймер отправил бы второй раз.
    #
    # Обращения к сети здесь нет и быть не должно: Telegram не ждёт Meta.
    # Отправкой занимается отдельный разовый воркер. Если процесс умрёт
    # ровно здесь, воркер подберёт пост сам (storage.reconcile_crossposts).
    if sent and cfg.threads_enabled:
        try:
            storage.enqueue_crosspost(post_id, "threads")
        except Exception as exc:
            print(f"[approval_bot] пост {post_id} опубликован, зеркало в Threads "
                  f"не поставлено в очередь: {type(exc).__name__}")
    return sent, status


def recover_stuck_posts() -> int:
    """Возвращает в работу посты, застрявшие в статусе 'publishing'.

    В этот статус пост попадает на время отправки в Telegram. Если процесс
    убили ровно в этот момент (рестарт сервера, обрыв), пост остался бы
    висеть невидимым для очереди навсегда. Вызывается при старте бота.

    Возможен редкий случай: сообщение всё же ушло в канал перед падением —
    тогда пост вернётся в очередь и владелец увидит его ещё раз. Это лучше,
    чем потерять пост молча: лишний показ владелец отклонит одной кнопкой.
    """
    with storage.get_conn() as conn:
        return conn.execute(
            "UPDATE posts SET status='approved', publish_at=NULL, slot=NULL "
            "WHERE status='publishing'"
        ).rowcount


# Статусы, из которых пост можно вернуть в работу. 'publishing' и 'published'
# сюда не входят намеренно: первый значит «отправка идёт прямо сейчас»,
# второй — «уже в канале».
WORKABLE_STATUSES = ("candidate", "hold", "approved")


def _reset_to_approved(post_id: str) -> int:
    """Переводит пост в 'approved' — но только из рабочего статуса, и атомарно.

    Раньше здесь стояла пара «прочитать статус → записать статус». Между
    этими двумя запросами параллельное нажатие ✅ успевало захватить пост
    ('publishing'), и второе нажатие затирало захват словом 'approved' —
    после чего захват выдавался ВТОРОЙ раз и пост уходил в канал дважды.
    Ровно эта дыра однажды уже чинилась, но условие проверялось в Python,
    а не в SQL, и окно осталось: на четырёхъядерном сервере тест ловил его
    примерно раз из пяти прогонов, на ноутбуке — никогда.

    Одним UPDATE с условием в WHERE окна нет: SQLite выполняет его целиком.
    """
    with storage.get_conn() as conn:
        placeholders = ",".join("?" * len(WORKABLE_STATUSES))
        return conn.execute(
            f"UPDATE posts SET status='approved' WHERE post_id=? "
            f"AND status IN ({placeholders})",
            (post_id, *WORKABLE_STATUSES),
        ).rowcount


def _status_of(post_id: str) -> Optional[str]:
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM posts WHERE post_id=?", (post_id,)
        ).fetchone()
    return row[0] if row else None


def _source_url_of(post_id: str) -> str:
    item = _load_news_item(post_id)
    return item.url if item else ""


# Какой пост владелец видел последним. Нужно, чтобы кнопка «Следующий пост»
# листала очередь дальше, а не приносила один и тот же пост, пока по нему
# не принято решение. Живёт в памяти процесса: после рестарта показ просто
# начнётся с начала очереди — терять тут нечего.
_last_shown: dict[str, str] = {}


async def send_next_candidate(
    bot: "Bot", cfg: Config, advance: bool = False, chat_id=None
) -> bool:
    """Присылает пост на решение.

    chat_id — кому слать. По умолчанию владельцу, но обработчики передают
    чат того админа, который запросил пост: иначе при нескольких админах
    второй нажимал кнопку, а пост уходил первому.

    advance=True — листать дальше от последнего показанного (кнопка
    «Следующий пост»). advance=False — самый старый нерешённый (после
    принятого решения и по команде /review).
    """
    chat = chat_id if chat_id is not None else cfg.telegram_owner_chat_id
    key = str(chat)
    candidate = _next_candidate(
        after=_last_shown.get(key) if advance else None,
        channel_id=cfg.telegram_channel_id,
    )
    if not candidate:
        return False
    _last_shown[key] = candidate["post_id"]

    from config import IMAGES_DIR
    from aiogram.types import FSInputFile

    manual_flag = " ⚠️ ТОЛЬКО РУЧНАЯ МОДЕРАЦИЯ (юр. гейт)" if candidate["manual_only"] else ""
    left = _candidates_left(cfg.telegram_channel_id)
    try:
        avg = json.loads(candidate["scores"]).get("average", "?")
    except Exception:
        avg = "?"

    # Предупреждения юр. гейта раньше вычислялись и никуда не шли: публикация
    # смотрела только на hard_fail, а issues (несовершеннолетний рядом с
    # именем, ст. 207.3, отсутствие статуса дела) владелец не видел вообще.
    # Теперь они прямо в сообщении — решение принимает человек, но с фактами.
    from src import legal_gate

    gate = legal_gate.check_post(
        candidate["draft_ru"], _source_url_of(candidate["post_id"]),
        case_status=_case_status_of(candidate["post_id"]),
    )
    warn = ""
    if gate.issues:
        warn = "\n⚠️ " + "\n⚠️ ".join(i[:200] for i in gate.issues)

    text = (
        f"{candidate['draft_ru']}\n\n"
        f"— — —\n"
        f"оценка {avg}/10 · формат {candidate['format']} · "
        f"`{candidate['post_id']}`{manual_flag}\n"
        f"в очереди ещё: {max(left - 1, 0)}{warn}"
    )

    # Владелец должен видеть РОВНО то, что уйдёт в канал, — вместе с карточкой.
    # Раньше на одобрение шёл голый текст, и битую карточку заметить было негде.
    #
    # Подпись под фото у Telegram — максимум 1024 символа. Длинный пост
    # (формат B) в неё не помещается, и отправка упала бы с ошибкой, а
    # владелец просто не увидел бы кандидата. Поэтому длинные посты уходят
    # карточкой без подписи + текстом с кнопками отдельным сообщением.
    image_path = IMAGES_DIR / f"{candidate['post_id']}.png"
    keyboard = _candidate_keyboard(candidate["post_id"])

    if image_path.exists() and len(text) <= TELEGRAM_CAPTION_LIMIT:
        await bot.send_photo(
            chat_id=chat, photo=FSInputFile(image_path), caption=text,
            reply_markup=keyboard, parse_mode=None,
        )
        return True

    if image_path.exists():
        await bot.send_photo(
            chat_id=chat, photo=FSInputFile(image_path), parse_mode=None
        )
    else:
        text += "\n⚠️ карточка не сгенерирована — уйдёт текстом"

    await bot.send_message(
        chat_id=chat, text=text[:TELEGRAM_TEXT_LIMIT],
        reply_markup=keyboard, parse_mode=None,
    )
    return True


def _explain_outcome(cfg: Config, title: str, outcome: dict,
                     attempts_used: int) -> str:
    """Почему пост не вышел — словами, которые понятны без логов.

    Владелец не должен открывать журнал сервера, чтобы узнать, что
    случилось с историей, за подготовку которой он заплатил.
    """
    left = max(cfg.max_preparation_attempts_per_day - attempts_used, 0)
    paid = bool(outcome.get("paid_work", True))
    head = ("ℹ️ " if not paid else "❌ Не пошло: ") + f"«{title[:80]}»"
    tail = (f"История снова доступна в дайджесте.\n"
            f"Сегодня: постов {{posts}} из {cfg.max_successful_selections_per_day} · "
            f"попыток осталось {left} из {cfg.max_preparation_attempts_per_day}.")

    kind = outcome.get("outcome")
    if kind == "legal_gate":
        body = ["Причина: юридический гейт не пропустил формулировки."]
        for issue in (outcome.get("issues") or [])[:2]:
            body.append(f"  {str(issue)[:180]}")
        body.append("Переписать безопаснее не удалось за отведённые попытки.")
    elif kind == "editorial":
        avg = outcome.get("average")
        threshold = outcome.get("threshold")
        if avg is not None:
            gap = round(float(threshold) - float(avg), 2)
            body = [f"Причина: редакторская оценка {avg} при пороге {threshold} — "
                    f"не хватило {gap}."]
        else:
            body = ["Причина: редакторская оценка ниже порога."]
        weak = outcome.get("weakest") or []
        if weak:
            body.append("Слабые места: "
                        + ", ".join(f"{name} {value}" for name, value in weak) + ".")
        body.append("Юридических претензий нет.")
    elif kind == "skipped":
        human = {
            "candidate": "уже лежит в очереди и ждёт вашего решения",
            "approved": "уже одобрен и ждёт публикации",
            "hold": "лежит в запасе",
            "scheduled": "уже стоит в расписании",
            "published": "уже опубликован",
            "publishing": "прямо сейчас публикуется",
        }.get(outcome.get("post_status") or "", "уже есть в работе")
        body = [f"По этой истории пост {human}.",
                "Второй раз её готовить незачем — поэтому ни времени, ни "
                "денег не потрачено."]
    elif kind == "not_saved":
        body = ["Пост подготовлен, но записать его не удалось: пока шла "
                "работа, по этому сюжету появилось решение, которое "
                "перезаписывать нельзя.",
                "Скорее всего, историю параллельно взяли ещё раз."]
    else:
        body = ["Причина: подготовка не дошла до готового поста.",
                "Ни гейт, ни редакторская оценка её не браковали — сорвалось "
                "раньше. Это повод посмотреть, что с конвейером."]

    if not paid:
        # Без платной работы незачем и пугать владельца остатком лимитов.
        return head + "\n" + "\n".join(body) + "\n\nПопытка не засчитана."
    return head + "\n" + "\n".join(body) + "\n\n" + tail


def _relevance_from_digest(item_row: dict):
    """Восстанавливает оценку релевантности, сохранённую при сборке дайджеста.

    Разбор оценки уже оплачен на стадии отбора и лежит в строке дайджеста
    целиком. Если по какой-то причине его нет (строка от старой версии),
    сумма раскладывается поровну — приблизительно, но честно помечено в
    red_flags, чтобы потом не принять эти числа за оценку модели.
    """
    import json as _json

    from src.relevance import RelevanceResult

    raw = item_row.get("relevance")
    if raw:
        try:
            d = _json.loads(raw)
            return RelevanceResult(
                relevant=True,
                curiosity=int(d.get("curiosity", 0)), emotion=int(d.get("emotion", 0)),
                unusual=int(d.get("unusual", 0)), shareable=int(d.get("shareable", 0)),
                ru_fit=int(d.get("ru_fit", 0)),
                category=d.get("category") or item_row.get("category") or "",
                red_flags=list(d.get("red_flags") or []),
            )
        except Exception as exc:
            print(f"[approval_bot] разбор оценки из дайджеста не прочитан: {exc}")

    total = int(item_row.get("score") or 0)
    even, rest = divmod(total, 5)
    return RelevanceResult(
        relevant=True, curiosity=even, emotion=even, unusual=even, shareable=even,
        ru_fit=even + rest, category=item_row.get("category") or "",
        red_flags=["оценка восстановлена из суммы"],
    )


async def _send_post_preview(bot: "Bot", cfg: Config, post_id: str, chat_id) -> None:
    """Показывает готовый пост с кнопками планирования.

    Отдельно от `send_next_candidate`: тот берёт следующего по очереди, а
    здесь нужен конкретный пост — только что подготовленный или выбранный
    в расписании.
    """
    from config import IMAGES_DIR
    from aiogram.types import FSInputFile

    import json as _json

    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT draft_ru, status, publish_at, slot, scores FROM posts WHERE post_id=?",
            (post_id,),
        ).fetchone()
    if row is None:
        await bot.send_message(chat_id=chat_id, text=f"Пост `{post_id}` не найден.")
        return

    from src import schedule

    try:
        scores = _json.loads(row[4] or "{}")
    except Exception:
        scores = {}
    borderline = bool(scores.get("borderline"))

    # Пометка идёт ПЕРЕД текстом: если она окажется под постом, её увидят
    # уже после того, как решение принято.
    warning = ""
    if borderline:
        warning = (
            f"⚠️ Пограничная редакторская оценка: {scores.get('average')} / 10 "
            f"(обычный порог {cfg.min_editorial_avg}).\n"
            f"Юридический гейт пройден. Решение за вами: принять, "
            f"переписать заново или отклонить.\n\n"
        )

    tail = f"\n\n— — —\n`{post_id}` · статус: {row[1]}"
    if row[1] == "scheduled" and row[2]:
        tail += f" · выйдет {schedule.describe(cfg, row[2])}"
    text = warning + (row[0] or "") + tail

    image = IMAGES_DIR / f"{post_id}.png"
    keyboard = _schedule_keyboard(post_id, borderline=borderline)
    if image.exists() and len(text) <= TELEGRAM_CAPTION_LIMIT:
        await bot.send_photo(chat_id=chat_id, photo=FSInputFile(image), caption=text,
                             reply_markup=keyboard, parse_mode=None)
        return
    if image.exists():
        await bot.send_photo(chat_id=chat_id, photo=FSInputFile(image), parse_mode=None)
    else:
        text += "\n⚠️ карточка не сгенерирована — уйдёт текстом"
    await bot.send_message(chat_id=chat_id, text=text[:TELEGRAM_TEXT_LIMIT],
                           reply_markup=keyboard, parse_mode=None)


def _regenerate(cfg: Config, post_id: str, item) -> Optional[str]:
    """Переписывает пост заново из исходной новости и обновляет его в базе.

    Проходит те же ворота, что и конвейер: юридический гейт по готовому
    тексту + редакторская оценка. Не прошёл — возвращаем None, пост
    отклоняется (не подсовываем владельцу заведомо negodный вариант).
    Возвращает новый текст при успехе.
    """
    import json as _json

    from config import IMAGES_DIR
    from src import legal_gate
    from src.image_card import render_card
    from src.rewrite import generate_draft
    from src.scorer import score_draft

    # Дневной лимит расходов проверялся только в конвейере. Кнопку 🔁
    # можно жать сколько угодно, и каждое нажатие — платный вызов модели:
    # без этой проверки лимит обходился в один клик.
    month = storage.get_month_cost()
    if cfg.monthly_cost_limit_usd > 0 and month >= cfg.monthly_cost_limit_usd:
        raise LLMBudgetExceededError(
            f"месячный предел расходов исчерпан (${month:.2f} из "
            f"${cfg.monthly_cost_limit_usd:.2f}) — перегенерация остановлена "
            f"до следующего месяца"
        )
    today = storage.get_today_cost()
    if today >= cfg.daily_cost_limit_usd:
        raise LLMBudgetExceededError(
            f"дневной лимит расходов исчерпан (${today:.2f} из "
            f"${cfg.daily_cost_limit_usd:.2f}) — перегенерация остановлена"
        )

    # Тот же судебный статус, что и в конвейере: без него перегенерация по
    # делу с приговором снова подбирала бы формулировку вслепую, а каждая
    # попытка платная.
    from src import case_status as case_status_mod

    status = case_status_mod.detect(item.title, item.body)
    draft = generate_draft(cfg, item, attempt=1, case_status=status)

    gate = legal_gate.check_post(draft.full_text, draft.source_url,
                                 case_status=_case_status_of(post_id))
    if gate.hard_fail:
        print(f"[approval_bot] regen {post_id}: юр. гейт — {gate.issues}")
        return None

    score = score_draft(cfg, draft)
    if score.decision == "reject":
        print(f"[approval_bot] regen {post_id}: редактор отклонил, avg={score.average}")
        return None

    # Пометка о пограничной оценке пересчитывается заново. Без этого
    # переписанный текст, оставшийся ниже порога, приезжал бы владельцу как
    # обычный: предупреждение исчезало, а причина его — нет.
    scores = score.to_dict()
    if score.average < cfg.min_editorial_avg:
        scores["borderline"] = True

    with storage.get_conn() as conn:
        conn.execute(
            "UPDATE posts SET draft_ru=?, scores=?, headline=?, status='candidate' "
            "WHERE post_id=?",
            (draft.full_text, _json.dumps(scores, ensure_ascii=False),
             getattr(draft, "headline", None) or None, post_id),
        )

    # Карточка рисуется по новому заголовку и кладётся под ИСХОДНЫМ post_id —
    # публикация ищет картинку именно по нему.
    try:
        new_card = render_card(
            title=getattr(draft, "headline", "") or draft.hook,
            category=draft.category,
            source_name=draft.source_name, draft_id=draft.id,
        )
        target = IMAGES_DIR / f"{post_id}.png"
        if Path(new_card) != target:
            target.write_bytes(Path(new_card).read_bytes())
    except Exception as exc:
        # Старую карточку надо УБРАТЬ, а не оставить. Иначе владелец получает
        # новый текст со старой картинкой — с прежним заголовком, а то и с
        # прежним дефектом, — и считает, что перегенерация не сработала.
        # Без файла карточки бот честно шлёт пост текстом и пишет об этом.
        print(f"[approval_bot] regen {post_id}: карточка не перерисована: {exc}")
        stale = IMAGES_DIR / f"{post_id}.png"
        try:
            stale.unlink(missing_ok=True)
            print(f"[approval_bot] regen {post_id}: устаревшая карточка убрана, пост уйдёт текстом")
        except OSError as unlink_exc:
            print(f"[approval_bot] regen {post_id}: не удалось убрать карточку: {unlink_exc}")

    return draft.full_text


async def _ack(call: "CallbackQuery", text: str = "") -> None:
    """Гасит «часики» на кнопке. Ответить на нажатие нужно в первые ~15 секунд,
    иначе Telegram отвечает «query is too old».

    Это подтверждение — чистая косметика: решение владельца к этому моменту
    уже записано в базу, а пост уже отправлен или уже отклонён. Поэтому
    просроченное подтверждение НЕ должно выглядеть как сбой: раньше оно
    поднимало исключение, ловушка ошибок писала владельцу «действие не
    выполнено» — и человек думал, что пост не ушёл, хотя он был в канале.
    """
    try:
        await call.answer(text)
    except Exception as exc:
        if "query is too old" in str(exc) or "query ID is invalid" in str(exc):
            print(f"[approval_bot] подтверждение нажатия опоздало (не ошибка): {exc}")
            return
        raise


async def _mark(call: "CallbackQuery", suffix: str) -> None:
    """Дописывает пометку к сообщению кандидата. Кандидат приходит фотографией
    с подписью, а не текстом, — у фото `call.message.text` пустой и edit_text
    падает. Поэтому правим подпись, если она есть.
    """
    try:
        if call.message.caption is not None:
            await call.message.edit_caption(
                caption=call.message.caption + suffix, parse_mode=None
            )
        else:
            await call.message.edit_text(
                (call.message.text or "") + suffix, parse_mode=None
            )
    except Exception as exc:  # правка сообщения — не повод терять решение владельца
        print(f"[approval_bot] не удалось обновить сообщение: {exc}")


def _admin_labels(cfg: Config) -> dict:
    """Человеческие подписи для ID из TELEGRAM_ADMIN_LABELS.

    Формат: «111111111=Alice @alice_example, 222222222=Bob». Нужно только
    для экрана «Администраторы»: без подписей там был бы список голых
    чисел, по которым непонятно, кто есть кто. Подпись, заданная при
    добавлении через бота, лежит в базе и имеет приоритет над этой.
    """
    return roles.env_labels(cfg)


def _owner_id(cfg: Config) -> Optional[int]:
    """ID владельца числом. В .env он строкой, а Telegram отдаёт int."""
    try:
        return int(str(cfg.telegram_owner_chat_id).strip())
    except (TypeError, ValueError):
        return None


def _uid_of(event) -> Optional[int]:
    """ID отправителя апдейта. У сообщения и у нажатия кнопки он лежит
    одинаково, поэтому проверка доступа для обоих одна."""
    user = getattr(event, "from_user", None)
    return getattr(user, "id", None)


def _access_filter(cfg: Config):
    """Фильтр «пускать ли в бота вообще». Читает права из БАЗЫ на каждом
    апдейте, а не из .env при старте: добавленный через бота редактор
    работает сразу, а удалённый теряет доступ мгновенно, без перезапуска.

    ВАЖНО: этот фильтр НЕ различает ролей — он про доступ к боту как
    таковому. Проверка «владелец ли» обязана стоять ВНУТРИ обработчиков
    управления составом, иначе редактор сможет вызвать команду владельца.

    Если база вдруг недоступна, доступ считается по .env — с громкой
    записью в лог. Молча запирать владельца снаружи нельзя.
    """

    def check(event) -> bool:
        uid = _uid_of(event)
        if uid is None:
            return False
        try:
            return roles.is_admin(uid)
        except Exception as exc:
            print(
                f"[approval_bot] права из базы не прочитались ({exc}) — "
                f"проверяю по .env"
            )
            return uid in cfg.admin_ids()

    return check


def _is_owner(cfg: Config, user_id: Optional[int]) -> bool:
    """Владелец ли. При недоступной базе откатываемся на .env — но только
    для владельца из файла: повышать кого-то ещё в этот момент нельзя."""
    if user_id is None:
        return False
    try:
        return roles.is_owner(user_id)
    except Exception as exc:
        print(f"[approval_bot] роль из базы не прочиталась ({exc}) — проверяю по .env")
        return user_id == _owner_id(cfg)


DENY_NOT_OWNER = (
    "Составом администраторов управляет только владелец бота.\n"
    "Вам доступен разбор очереди: ✅ / ✏️ / 🔁 / ❌ / ⏸."
)


def _admins_screen(cfg: Config, me: Optional[int]) -> tuple[str, "InlineKeyboardMarkup | None"]:
    """Экран «Администраторы»: состав с ролями + кнопки управления владельцу."""
    entries = roles.list_admins()
    fallback = _admin_labels(cfg)
    rows = []
    for entry in entries:
        badge = "👑 владелец" if entry["role"] == roles.ROLE_OWNER else "редактор"
        mark = "  ← это вы" if entry["user_id"] == me else ""
        rows.append(
            f"  {roles.label_of(entry, fallback)} ({entry['user_id']}) — {badge}{mark}"
        )
    listing = "\n".join(rows) if rows else "  никого — доступ открыт всем!"

    owner_hint = (
        "\n\nВы владелец: можете добавлять и удалять редакторов кнопками ниже\n"
        "или командами /addadmin <ID> [имя] и /deladmin <ID>."
        if _is_owner(cfg, me)
        else "\n\nСоставом управляет владелец."
    )

    text = (
        "👥 Администраторы бота\n\n"
        + listing
        + "\n\n"
        f"Канал публикации: {cfg.telegram_channel_id or 'не задан'}\n"
        f"Режим: {'боевой' if cfg.is_live_publish() else 'публикация выключена'}\n\n"
        "Решения общие: пост, разобранный одним админом, исчезает из "
        "очереди у остальных. Опубликовать один пост дважды нельзя."
        + owner_hint
    )

    if not _is_owner(cfg, me):
        return text, None

    buttons = [[InlineKeyboardButton(text="➕ Добавить редактора", callback_data="admin_add")]]
    for entry in entries:
        if entry["role"] == roles.ROLE_OWNER:
            continue
        buttons.append(
            [
                InlineKeyboardButton(
                    text=f"➖ Убрать {roles.label_of(entry, fallback)}",
                    callback_data=f"admin_del:{entry['user_id']}",
                )
            ]
        )
    return text, InlineKeyboardMarkup(inline_keyboard=buttons)


def build_dispatcher(cfg: Config) -> "Dispatcher":
    dp = Dispatcher(storage=MemoryStorage())

    # Бот управляет публикациями в канал — посторонний не должен иметь к нему
    # доступа вообще. Раньше проверки отправителя не было ни в одном
    # обработчике: любой, кто нашёл бота, мог слать ему команды и заставлять
    # его работать. Фильтр на уровне диспетчера закрывает сразу все
    # обработчики, включая те, что появятся позже.
    try:
        roles.bootstrap(cfg)
        admins = roles.allowed_ids()
    except Exception as exc:
        print(f"[approval_bot] таблица прав недоступна ({exc}) — беру список из .env")
        admins = cfg.admin_ids()

    if admins:
        # Раньше доступ был только у одного владельца. Теперь список: любой
        # из админов может разбирать очередь. Решения общие — база одна, а
        # атомарный захват поста не даст двум админам опубликовать один и
        # тот же пост дважды.
        #
        # Список читается из базы на КАЖДОМ апдейте (см. _access_filter), а
        # не фиксируется здесь: иначе добавленный через бота редактор ждал
        # бы перезапуска процесса.
        check = _access_filter(cfg)
        dp.message.filter(check)
        dp.callback_query.filter(check)
        print(f"[approval_bot] админов с доступом: {len(admins)} — {admins}")
    else:
        print(
            "[approval_bot] ВНИМАНИЕ: не задан ни один админ "
            "(TELEGRAM_OWNER_CHAT_ID / TELEGRAM_ADMIN_IDS) — "
            "фильтр НЕ включён, бот отвечает кому угодно."
        )

    @dp.errors()
    async def on_error(event) -> bool:
        """Единая ловушка необработанных исключений.

        Без неё падение внутри обработчика уходило в лог aiogram, а владелец
        видел только то, что кнопка «не сработала», и не понимал почему.
        NO SILENT FAILURES: пишем трейс в лог процесса и говорим владельцу.
        """
        import traceback

        exc = getattr(event, "exception", None)
        print(f"[approval_bot] НЕОБРАБОТАННАЯ ОШИБКА: {exc!r}")
        traceback.print_exception(type(exc), exc, getattr(exc, "__traceback__", None))

        try:
            update = getattr(event, "update", None)
            chat_id = cfg.telegram_owner_chat_id
            bot = getattr(update, "bot", None)
            if bot is not None and chat_id:
                await bot.send_message(
                    chat_id=chat_id,
                    text=(
                        "⚠️ Внутренняя ошибка бота.\n"
                        f"{type(exc).__name__}: {str(exc)[:300]}\n\n"
                        "Выполнено ли действие — видно по пометке под постом "
                        "и по каналу: часть шага могла пройти. Раньше здесь "
                        "стояло «действие не выполнено», и это вводило в "
                        "заблуждение — пост бывал уже опубликован.\n"
                        "Очередь и статусы покажет «📊 Статус», следующего "
                        "кандидата — /review."
                    ),
                    parse_mode=None,
                )
        except Exception as notify_exc:
            print(f"[approval_bot] не удалось уведомить владельца: {notify_exc}")
        return True

    @dp.message(Command("start", "help"))
    async def cmd_start(message: Message) -> None:
        """Раньше бот на /start молчал — выглядело как «бот сломался»."""
        left = _candidates_left(cfg.telegram_channel_id)
        me = message.from_user.id if message.from_user else None
        owner_block = (
            "/addadmin <ID> [имя] — добавить редактора\n"
            "/deladmin <ID> — убрать редактора\n"
            if _is_owner(cfg, me)
            else ""
        )
        await message.answer(
            "Редактор новостного бота.\n\n"
            "Внизу — постоянное меню, команды помнить не нужно:\n"
            f"{BTN_DIGEST} · {BTN_SCHEDULED}\n"
            f"{BTN_NEXT} · {BTN_STATUS} · {BTN_ADMINS}\n\n"
            "Команды (то же самое):\n"
            "/digest — короткий список дня, выбрать историю в работу\n"
            "/scheduled — что стоит в расписании\n"
            "/review — следующий пост на утверждение\n"
            "/queue — что в очереди\n"
            "/budget — расходы на модель и остаток лимита\n"
            "/admins — кто имеет доступ к боту\n"
            + owner_block
            + "\n"
            "Под каждым постом кнопки:\n"
            "✅ Пост — опубликовать в канал\n"
            "✏️ Править — прислать свой текст следующим сообщением\n"
            "🔁 Перегенерить — переписать заново\n"
            "❌ Мимо — отклонить\n"
            "⏸ В запас — отложить на потом\n\n"
            f"Сейчас в очереди: {left}",
            reply_markup=_main_menu(),
        )

    @dp.message(Command("queue"))
    async def cmd_queue(message: Message) -> None:
        counts = storage.count_posts_by_status()
        lines = [f"{k}: {v}" for k, v in sorted(counts.items())]
        await message.answer(
            "Очередь:\n" + ("\n".join(lines) if lines else "пусто")
        )

    @dp.message(Command("budget", "cost"))
    async def cmd_budget(message: Message) -> None:
        """Расходы на модель.

        ВАЖНО про «баланс»: Gemini API не отдаёт ни остаток средств, ни
        потраченную сумму — обычному ключу из AI Studio такие эндпоинты
        недоступны, а Cloud Billing требует отдельного сервисного аккаунта
        с ролями биллинга. Поэтому показываем СВОЙ учёт: он считается по
        `usage_metadata` каждого ответа модели (реальные токены × тариф) и
        по нему же работает дневной лимит.
        """
        s = storage.get_cost_summary()
        limit = cfg.daily_cost_limit_usd
        left = max(limit - s["today"], 0)
        used_pct = int(min(s["today"] / limit * 100, 100)) if limit else 0
        bar_len = 10
        filled = int(bar_len * used_pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)

        stages = "\n".join(
            f"  {name}: ${cost:.4f} ({n} вызовов)"
            for name, (cost, n) in sorted(
                s["by_stage"].items(), key=lambda kv: -kv[1][0]
            )
        ) or "  за этот месяц вызовов не было"

        forecast = s["month"] / max(int(storage.now_iso()[8:10]), 1) * 30

        await message.answer(
            f"💸 Расходы на модель\n\n"
            f"Сегодня:      ${s['today']:.4f}  ({s['calls_today']} вызовов)\n"
            f"Дневной лимит: ${limit:.2f}\n"
            f"{bar} {used_pct}%  ·  осталось ${left:.4f}\n\n"
            f"За месяц:     ${s['month']:.4f}\n"
            f"Прогноз/мес:  ~${forecast:.2f}\n"
            f"За всё время: ${s['all_time']:.4f}\n\n"
            f"По этапам за месяц:\n{stages}\n\n"
            f"Модели: {', '.join(s['models']) or '—'}\n"
            f"Учёт с: {(s['since'] or '—')[:10]}\n\n"
            f"ℹ️ Это наш собственный подсчёт по токенам каждого ответа.\n"
            f"Остаток средств на счёте Google через API недоступен — его\n"
            f"видно только в AI Studio → Dashboard → Usage."
        )

    @dp.message(F.text == BTN_NEXT)
    async def btn_next(message: Message) -> None:
        """Кнопка «Следующий пост»: листает очередь дальше."""
        sent = await send_next_candidate(
            message.bot, cfg, advance=True, chat_id=message.chat.id
        )
        if not sent:
            # Общая фраза «придёт после ближайшего сбора» бесполезна:
            # расписание живёт в systemd, и помнить его владелец не обязан.
            from src import news_schedule

            when = await asyncio.to_thread(news_schedule.describe_next_run, cfg)
            free = storage.digest_pool(cfg.telegram_channel_id, cfg.digest_size)
            lines = ["Новых постов пока нет."]
            if when:
                lines.append(f"Следующий сбор новостей: {when}.")
            else:
                lines.append("Расписание сбора сейчас недоступно — "
                             "проверьте таймер на сервере.")
            if free:
                lines.append("")
                lines.append(f"В дайджесте ждут {len(free)} истор(ий) — "
                             f"можно взять любую кнопкой «{BTN_DIGEST}».")
            await message.answer("\n".join(lines), reply_markup=_main_menu())

    @dp.message(F.text == BTN_STATUS)
    async def btn_status(message: Message) -> None:
        """Кнопка «Статус».

        Раньше здесь печаталась сырая сводка по статусам базы —
        «hold: 9, published: 8, rejected: 7». Владельцу это не говорило
        ничего: непонятно ни что делать дальше, ни когда придут новые
        материалы, ни кто забраковал отклонённое.
        """
        from src import news_schedule

        next_run = await asyncio.to_thread(news_schedule.describe_next_run, cfg)
        await message.answer(_status_screen(cfg, next_run), reply_markup=_main_menu())

    @dp.message(F.text == BTN_ADMINS)
    @dp.message(Command("admins"))
    async def btn_admins(message: Message) -> None:
        """Кнопка «Администраторы» — кто имеет доступ к боту, с какой ролью и
        куда идёт публикация. Владелец видит здесь же кнопки управления
        составом; редактор — только список.

        Роль читается из базы: экран у владельца и у редактора разный.
        """
        me = message.from_user.id if message.from_user else None
        text, keyboard = _admins_screen(cfg, me)
        await message.answer(text, reply_markup=keyboard or _main_menu())

    @dp.message(Command("addadmin"))
    async def cmd_addadmin(message: Message, state: "FSMContext") -> None:
        """/addadmin <ID> [имя] — добавить редактора.

        Проверка роли стоит ЗДЕСЬ, а не в фильтре диспетчера: фильтр даёт
        доступ «в бота вообще» и роли не различает, поэтому без этой строки
        команду владельца мог бы выполнить любой редактор.
        """
        me = message.from_user.id if message.from_user else None
        if not _is_owner(cfg, me):
            await message.answer(DENY_NOT_OWNER, reply_markup=_main_menu())
            return

        args = (message.text or "").split(maxsplit=2)
        if len(args) < 2:
            await state.set_state(AdminStates.waiting_for_new_admin)
            await message.answer(
                "Пришлите ID нового редактора следующим сообщением "
                "(можно с именем: «111111111 Alice») или перешлите сюда "
                "любое его сообщение.\n\n"
                "Свой ID человек узнаёт у @userinfobot."
            )
            return

        await state.clear()  # команда с аргументами отменяет незаконченный ввод
        await _do_add_admin(message, me, args[1], args[2] if len(args) > 2 else None)

    @dp.message(Command("deladmin"))
    async def cmd_deladmin(message: Message) -> None:
        """/deladmin <ID> — убрать редактора. Только владелец."""
        me = message.from_user.id if message.from_user else None
        if not _is_owner(cfg, me):
            await message.answer(DENY_NOT_OWNER, reply_markup=_main_menu())
            return

        args = (message.text or "").split(maxsplit=1)
        if len(args) < 2:
            await message.answer(
                "Нужен ID: /deladmin 111111111. Список — «👥 Администраторы».",
                reply_markup=_main_menu(),
            )
            return
        await _do_remove_admin(message, args[1].strip())

    async def _do_add_admin(message: Message, actor, raw_id, label) -> None:
        try:
            added = roles.add_admin(raw_id, label=label, added_by=actor)
        except roles.RoleError as exc:
            await message.answer(str(exc), reply_markup=_main_menu())
            return

        name = (label or "").strip() or added["user_id"]
        await message.answer(
            f"✅ {name} ({added['user_id']}) — редактор. Доступ уже работает, "
            "перезапускать бота не нужно.\n\n"
            "⚠️ Telegram не разрешает боту написать человеку первым: "
            "попросите его самого отправить боту /start.",
            reply_markup=_main_menu(),
        )

    async def _do_remove_admin(message: Message, raw_id) -> None:
        try:
            removed = roles.remove_admin(raw_id)
        except roles.RoleError as exc:
            await message.answer(str(exc), reply_markup=_main_menu())
            return
        await message.answer(
            f"✅ {removed['user_id']} больше не редактор — доступ закрыт сразу.",
            reply_markup=_main_menu(),
        )

    @dp.callback_query(F.data == "admin_add")
    async def cb_admin_add(call: CallbackQuery, state: "FSMContext") -> None:
        """Кнопка «➕ Добавить редактора». Роль проверяется здесь же:
        кнопки живут в переписке вечно, и старое сообщение владельца могло
        быть переслано кому угодно."""
        me = call.from_user.id if getattr(call, "from_user", None) else None
        if not _is_owner(cfg, me):
            await call.answer("Только владелец", show_alert=True)
            await call.message.answer(DENY_NOT_OWNER)
            return

        await state.set_state(AdminStates.waiting_for_new_admin)
        await call.message.answer(
            "Пришлите ID нового редактора следующим сообщением "
            "(можно с именем: «111111111 Alice») или перешлите сюда "
            "любое его сообщение.\n\n"
            "Свой ID человек узнаёт у @userinfobot."
        )
        await call.answer()

    @dp.callback_query(F.data.startswith("admin_del:"))
    async def cb_admin_del(call: CallbackQuery) -> None:
        me = call.from_user.id if getattr(call, "from_user", None) else None
        if not _is_owner(cfg, me):
            await call.answer("Только владелец", show_alert=True)
            await call.message.answer(DENY_NOT_OWNER)
            return

        raw_id = call.data.split(":", 1)[1]
        try:
            removed = roles.remove_admin(raw_id)
        except roles.RoleError as exc:
            await call.answer("Не удалось")
            await call.message.answer(str(exc))
            return

        await call.answer("Удалён")
        await call.message.answer(
            f"✅ {removed['user_id']} больше не редактор — доступ закрыт сразу."
        )
        text, keyboard = _admins_screen(cfg, me)
        await call.message.answer(text, reply_markup=keyboard or _main_menu())

    @dp.message(AdminStates.waiting_for_new_admin, ~F.text.startswith("/"))
    async def on_new_admin_input(message: Message, state: "FSMContext") -> None:
        """Ответ на «➕ Добавить редактора»: ID числом или пересланное
        сообщение будущего редактора.

        Команды исключены фильтром: этот обработчик стоит в цепочке раньше
        `/review`, и без исключения набранная в диалоге команда уходила бы
        в «добавить редактора с ID /review» вместо выполнения.

        Роль проверяется ещё раз: состояние живёт в памяти процесса, и между
        нажатием кнопки и ответом владельца права могли смениться.
        """
        me = message.from_user.id if message.from_user else None
        if not _is_owner(cfg, me):
            await state.clear()
            await message.answer(DENY_NOT_OWNER, reply_markup=_main_menu())
            return

        forwarded = getattr(message, "forward_from", None)
        if forwarded is not None:
            await state.clear()
            label = " ".join(
                p for p in (
                    getattr(forwarded, "first_name", ""),
                    getattr(forwarded, "last_name", ""),
                ) if p
            )
            await _do_add_admin(message, me, forwarded.id, label or None)
            return

        raw = (message.text or "").strip()
        if not raw:
            await state.clear()
            await message.answer(
                "Это не ID. Если пересылали сообщение — у человека закрыта "
                "пересылка, попросите его ID у @userinfobot и пришлите числом: "
                "/addadmin 111111111",
                reply_markup=_main_menu(),
            )
            return

        parts = raw.split(maxsplit=1)
        await state.clear()
        await _do_add_admin(message, me, parts[0], parts[1] if len(parts) > 1 else None)

    @dp.message(F.text == BTN_DIGEST)
    @dp.message(Command("digest"))
    async def btn_digest(message: Message) -> None:
        text, keyboard = _digest_screen(cfg)
        await message.answer(text, reply_markup=keyboard or _main_menu())

    @dp.message(F.text == BTN_SCHEDULED)
    @dp.message(Command("scheduled"))
    async def btn_scheduled(message: Message) -> None:
        text, keyboard = _scheduled_screen(cfg)
        await message.answer(text, reply_markup=keyboard or _main_menu())

    @dp.callback_query(F.data.startswith("pick:"))
    async def cb_pick(call: CallbackQuery) -> None:
        """Владелец выбрал историю из дайджеста.

        Порядок важен: сначала суточные потолки и атомарный захват, и только
        потом дорогая генерация. Иначе двойной тап или лишний выбор
        оплачиваются моделью, а очередь кандидатов на этот момент ещё пуста
        и ни от чего не защищает.

        Потолков два, и они про разное. Готовых постов — сколько владелец
        хочет получать в день. Попыток — сколько раз за день допустимо
        заплатить за подготовку, включая те, что забраковал гейт. Провал
        не съедает слот готового поста, но и повторять его бесконечно
        нельзя.
        """
        item_id = int(call.data.split(":", 1)[1])

        from src import schedule

        storage.archive_stale_digest(cfg.digest_ttl_hours)
        row = storage.digest_item(item_id)
        if row is None:
            await _ack(call, "Не найдено")
            return

        attempts, successes = storage.count_selections_today(
            *schedule.day_bounds_utc(cfg))

        # Оба потолка проверяются ВНУТРИ захвата, одной транзакцией. Снаружи
        # между «посчитал» и «занял» помещается второе нажатие, и два
        # быстрых выбора проходили проверку по одному и тому же остатку.
        day_start, day_end = schedule.day_bounds_utc(cfg)
        attempt_id = storage.start_selection_attempt(
            item_id, day_start, day_end,
            cfg.max_successful_selections_per_day,
            cfg.max_preparation_attempts_per_day,
            # Срывы по вине провайдера норму подготовок не расходуют, но
            # и бесконечными быть не должны: при затяжном сбое кнопку
            # можно было бы жать без счёта.
            max_api_errors=cfg.max_preparation_attempts_per_day * 2,
        )

        if attempt_id == storage.POSTS_EXHAUSTED:
            await _ack(call, "На сегодня хватит")
            await call.message.answer(
                f"Дневная норма готовых постов выбрана: "
                f"{cfg.max_successful_selections_per_day}.\n"
                f"Настройка: MAX_SUCCESSFUL_SELECTIONS_PER_DAY."
            )
            return
        if attempt_id == storage.API_OUTAGE_EXHAUSTED:
            await _ack(call, "Провайдер недоступен")
            await call.message.answer(
                "Модель сегодня уже несколько раз не ответила, и попытки "
                "перестали засчитываться в норму — но и бесконечно "
                "перезапрашивать её нельзя.\n"
                "Подождите, пока сервис восстановится: следующий сбор "
                "новостей всё равно принесёт свежий список."
            )
            return
        if attempt_id == storage.ATTEMPTS_EXHAUSTED:
            await _ack(call, "Попытки исчерпаны")
            await call.message.answer(
                f"За сегодня израсходованы все "
                f"{cfg.max_preparation_attempts_per_day} платных попыток "
                f"подготовки, готовых постов из них: {successes}.\n"
                f"Это защита от бесконечных повторов на историях, которые не "
                f"проходят гейт. Настройка: MAX_PREPARATION_ATTEMPTS_PER_DAY."
            )
            return
        if not isinstance(attempt_id, int):
            await _ack(call, "Уже выбрана")
            return

        await _ack(call, "Готовлю пост…")
        await call.message.answer(
            f"Беру в работу: {row['title'][:90]}\n"
            f"Переписываю, проверяю юридически, рисую карточку — это займёт полминуты."
        )

        try:
            outcome = await asyncio.to_thread(_generate_from_digest, cfg, row)
        except Exception as exc:
            from src.llm_client import LLMUnavailableError

            # Провайдер не ответил — это не решение модели и не работа.
            # Наказывать владельца дневной нормой подготовок не за что:
            # запись о попытке остаётся в журнале, но в счёт не идёт.
            # Деньги при этом защищены прежними лимитами прогона, суток и
            # месяца — они не менялись.
            unavailable = isinstance(exc, LLMUnavailableError)
            storage.release_digest_item(item_id)
            storage.finish_selection_attempt(
                attempt_id, None,
                storage.ATTEMPT_API_ERROR if unavailable else "error",
            )
            # Если запись поста успела появиться до сбоя, помечаем причину:
            # иначе в статистике такой отказ выглядел бы как решение гейта.
            storage.mark_event_post_failed(f"evt_{row['url_hash']}")
            print(f"[approval_bot] выбор {item_id}: генерация упала "
                  f"({'провайдер недоступен' if unavailable else 'сбой'}): {exc}")

            if unavailable:
                wait = getattr(exc, "retry_after", None)
                when = (f" Провайдер просит подождать {wait:.0f}с."
                        if wait else "")
                await call.message.answer(
                    f"⏳ Не пошло: «{row['title'][:80]}»\n"
                    f"Причина: модель сейчас недоступна — она не ответила "
                    f"после всех повторов.{when}\n\n"
                    f"Попытка НЕ засчитана: история снова в дайджесте, "
                    f"дневная норма не тронута. Попробуйте через минуту."
                )
            else:
                await call.message.answer(
                    f"❌ Не пошло: «{row['title'][:80]}»\n"
                    f"Причина: сбой при подготовке — {exc}\n\n"
                    f"История снова доступна в дайджесте."
                )
            return

        post_id = outcome.get("post_id")

        if not post_id:
            storage.release_digest_item(item_id)
            # Пропуск без обращения к модели попытку не расходует: платить
            # было не за что. Настоящий отказ гейта или редактора —
            # расходует, там работа была и деньги ушли.
            paid = bool(outcome.get("paid_work", True))
            storage.finish_selection_attempt(
                attempt_id, None,
                "rejected" if paid else storage.ATTEMPT_NO_WORK,
            )
            await call.message.answer(
                _explain_outcome(cfg, row["title"], outcome,
                                 attempts + (1 if paid else 0))
                .replace("{posts}", str(successes))
            )
            return

        # Привязка поста и закрытие попытки — одной транзакцией: иначе
        # успех мог не попасть в счёт, и владелец получил бы постов больше
        # нормы.
        storage.finish_selection_attempt(attempt_id, post_id, "ok", item_id=item_id)
        await _send_post_preview(call.bot, cfg, post_id, call.message.chat.id)

    @dp.callback_query(F.data.startswith("slot:"))
    async def cb_slot(call: CallbackQuery) -> None:
        from src import schedule

        _, slot, post_id = call.data.split(":", 2)
        publish_at = schedule.next_slot_utc(cfg, slot)
        if storage.schedule_post(post_id, publish_at, slot):
            await _ack(call, "Запланировано")
            await call.message.answer(
                f"{schedule.SLOT_TITLES.get(slot, slot)} · выйдет "
                f"{schedule.describe(cfg, publish_at)}.\n"
                f"Снять или опубликовать раньше — экран «{BTN_SCHEDULED}»."
            )
        else:
            await _ack(call, "Слот занят")
            await call.message.answer(
                "На это время уже стоит другой пост — один слот, один пост. "
                f"Свободные слоты видно в «{BTN_SCHEDULED}»."
            )

    @dp.callback_query(F.data.startswith("pubnow:"))
    async def cb_publish_now(call: CallbackQuery) -> None:
        post_id = call.data.split(":", 1)[1]
        await _ack(call, "Публикую…")
        sent, status = await asyncio.to_thread(_publish_post, cfg, post_id)
        await call.message.answer(
            "📤 Опубликовано в канал." if sent else f"⚠️ В канал не ушло: {status}"
        )

    @dp.callback_query(F.data.startswith("unsched:"))
    async def cb_unschedule(call: CallbackQuery) -> None:
        post_id = call.data.split(":", 1)[1]
        if storage.unschedule_post(post_id):
            await _ack(call, "Снято")
            await call.message.answer("Пост снят с расписания и вернулся в очередь.")
        else:
            await _ack(call, "Уже не в расписании")

    @dp.callback_query(F.data.startswith("showsched:"))
    async def cb_show_scheduled(call: CallbackQuery) -> None:
        post_id = call.data.split(":", 1)[1]
        await _ack(call)
        await _send_post_preview(call.bot, cfg, post_id, call.message.chat.id)

    @dp.message(Command("review"))
    async def cmd_review(message: Message) -> None:
        sent = await send_next_candidate(message.bot, cfg, chat_id=message.chat.id)
        if not sent:
            await message.answer("Кандидатов на модерацию нет.")

    async def _refuse_if_scheduled(call: CallbackQuery, post_id: str, action: str) -> bool:
        """Старые кнопки под постом живут в переписке вечно.

        Запланированный пост они трогать не должны: ✅ опубликовал бы его
        немедленно мимо слота, 🔁 вернул бы в очередь и потерял расписание.
        Для запланированного есть свой экран «🗓 Запланировано».
        """
        if _status_of(post_id) != "scheduled":
            return False
        await _ack(call, "Пост запланирован")
        await call.message.answer(
            f"Этот пост уже стоит в расписании, поэтому «{action}» не применяю.\n"
            f"Снять с расписания или опубликовать сейчас — экран «{BTN_SCHEDULED}»."
        )
        return True

    @dp.callback_query(F.data.startswith("approve:"))
    async def cb_approve(call: CallbackQuery) -> None:
        post_id = call.data.split(":", 1)[1]
        if await _refuse_if_scheduled(call, post_id, "✅ Пост"):
            return
        # Статус перетираем ТОЛЬКО из рабочих состояний. Иначе нажатие ✅ на
        # старом сообщении затирало бы 'published' (ломая защиту от дублей)
        # или 'rejected' (публикуя то, что владелец уже забраковал) — проверки
        # в _publish_post до них просто не доходили.
        _reset_to_approved(post_id)

        # Подтверждаем нажатие СРАЗУ, до отправки. Публикация — это загрузка
        # картинки в канал, секунды, а иногда и больше десяти; ответ на
        # нажатие Telegram ждёт около пятнадцати. Владелец, не дождавшийся
        # «часиков», жал ✅ второй раз: пост защищён от дубля и уходил один
        # раз, но подтверждение второго нажатия успевало протухнуть, и бот
        # рапортовал внутреннюю ошибку о посте, который на самом деле
        # опубликован (случай 27.08, пост f90a3da84e68).
        await _ack(call, "Публикую…")

        sent, status = await asyncio.to_thread(_publish_post, cfg, post_id)
        if sent:
            note = "\n\n✅ ОДОБРЕНО · 📤 опубликовано в канал"
        else:
            note = f"\n\n✅ ОДОБРЕНО · ⚠️ в канал не ушло: {status}"
            print(f"[approval_bot] публикация {post_id} не удалась: {status}")

        await _mark(call, note)
        await send_next_candidate(call.bot, cfg, chat_id=call.message.chat.id)

    @dp.callback_query(F.data.startswith("reject:"))
    async def cb_reject(call: CallbackQuery) -> None:
        post_id = call.data.split(":", 1)[1]
        # ❌ намеренно НЕ блокируется у запланированного поста: и под свежим
        # превью, и под старым сообщением это одно и то же однозначное
        # решение — не публиковать. Просто сначала снимаем со слота.
        if _status_of(post_id) == "scheduled":
            storage.unschedule_post(post_id)
        storage.update_post_status(post_id, "rejected",
                                   reject_reason=storage.REJECT_BY_USER)
        await _mark(call, "\n\n❌ ОТКЛОНЕНО")
        await _ack(call, "Отклонено")
        await send_next_candidate(call.bot, cfg, chat_id=call.message.chat.id)

    @dp.callback_query(F.data.startswith("hold:"))
    async def cb_hold(call: CallbackQuery) -> None:
        post_id = call.data.split(":", 1)[1]
        if await _refuse_if_scheduled(call, post_id, "⏸ В запас"):
            return
        storage.update_post_status(post_id, "hold")
        await _mark(call, "\n\n⏸ В ЗАПАСЕ")
        await _ack(call, "Отложено")
        await send_next_candidate(call.bot, cfg, chat_id=call.message.chat.id)

    @dp.callback_query(F.data.startswith("regen:"))
    async def cb_regen(call: CallbackQuery) -> None:
        """НАСТОЯЩАЯ перегенерация прямо здесь.

        Раньше кнопка только ставила статус `needs_regenerate` и обещала,
        что перепишет main.py при следующем прогоне. Но main.py обрабатывает
        свежие новости и на такие посты не смотрит вообще — пост оставался
        лежать мёртвым навсегда (так и случилось с постом про Мексику от
        13.08). Теперь переписываем сразу и присылаем новый вариант.
        """
        post_id = call.data.split(":", 1)[1]
        if await _refuse_if_scheduled(call, post_id, "🔁 Перегенерить"):
            return

        # Переписывать уже опубликованный пост нельзя: в канале останется
        # старый текст, а в базе появится новый, и они молча разъедутся —
        # потом невозможно понять, что именно видели читатели.
        if _status_of(post_id) == "published":
            await call.answer("Пост уже опубликован")
            await call.message.answer(
                "Этот пост уже в канале — перегенерация его там не заменит. "
                "Отредактируйте сообщение прямо в канале."
            )
            return

        await call.answer("Переписываю…")
        await _mark(call, "\n\n🔁 переписываю…")

        item = _load_news_item(post_id)
        if item is None:
            await call.message.answer(
                f"Не нашёл исходную новость для `{post_id}` — переписать не из чего."
            )
            return

        try:
            new_draft = await asyncio.to_thread(_regenerate, cfg, post_id, item)
        except Exception as exc:
            print(f"[approval_bot] перегенерация {post_id} упала: {exc}")
            await call.message.answer(f"Перегенерация не удалась: {exc}")
            return

        if new_draft is None:
            storage.update_post_status(post_id, "rejected")
            await call.message.answer(
                "Новый вариант не прошёл юридический гейт или редакторскую "
                "оценку. Пост отклонён — исходник для него слабый."
            )
            await send_next_candidate(call.bot, cfg, chat_id=call.message.chat.id)
            return

        await send_next_candidate(call.bot, cfg, chat_id=call.message.chat.id)

    @dp.callback_query(F.data.startswith("edit:"))
    async def cb_edit(call: CallbackQuery, state: "FSMContext") -> None:
        post_id = call.data.split(":", 1)[1]
        await state.update_data(editing_post_id=post_id)
        await state.set_state(EditStates.waiting_for_new_text)
        await call.message.answer(f"Пришли новый текст для `{post_id}` следующим сообщением.")
        await call.answer()

    @dp.message(EditStates.waiting_for_new_text)
    async def on_new_text(message: Message, state: "FSMContext") -> None:
        data = await state.get_data()
        post_id = data.get("editing_post_id")
        if not post_id:
            await message.answer("Не понял, какой пост правим — начни с /review.")
            return
        await state.clear()

        new_text = (message.text or "").strip()
        if not new_text:
            await message.answer("Пустой текст — правка не принята. /review для продолжения.")
            return

        # Правка уже опубликованного поста НЕ должна отправлять его в канал
        # второй раз. Раньше UPDATE безусловно ставил статус 'approved',
        # затирая 'published', и защита от дублей переставала работать:
        # правка после публикации давала второе сообщение в канале.
        current = _status_of(post_id)
        if current is None:
            await message.answer(f"Пост `{post_id}` не найден. /review для продолжения.")
            return
        if current == "published":
            await message.answer(
                "Этот пост уже опубликован — исправленный текст в канал повторно "
                "не уйдёт. Отредактируйте сообщение прямо в канале."
            )
            return

        # Условие в WHERE, а не проверка выше по коду: между чтением статуса
        # и этой записью параллельное нажатие ✅ успевает захватить пост, и
        # безусловный UPDATE затирал бы захват — пост ушёл бы в канал дважды.
        with storage.get_conn() as conn:
            placeholders = ",".join("?" * len(WORKABLE_STATUSES))
            updated = conn.execute(
                f"UPDATE posts SET draft_ru=?, status='approved' WHERE post_id=? "
                f"AND status IN ({placeholders})",
                (new_text, post_id, *WORKABLE_STATUSES),
            ).rowcount
        if not updated:
            await message.answer(
                "Пост прямо сейчас публикуется или уже опубликован — правка "
                "не применена, чтобы он не ушёл в канал дважды. "
                "Отредактируйте сообщение прямо в канале."
            )
            return

        # Раньше правка на этом и заканчивалась: пост помечался одобренным и
        # молча оставался в базе, в канал не уходил ничего. Теперь правка
        # публикует так же, как кнопка ✅.
        sent, status = await asyncio.to_thread(_publish_post, cfg, post_id)
        if sent:
            await message.answer("Текст обновлён. 📤 Опубликовано в канал.")
        else:
            await message.answer(f"Текст обновлён, но в канал не ушло: {status}")
        await send_next_candidate(message.bot, cfg, chat_id=message.chat.id)

    @dp.message()
    async def on_unhandled_message(message: Message) -> None:
        """Последний обработчик: ловит всё, что не подошло остальным.

        Без него бот МОЛЧАЛ в двух реальных ситуациях: (1) владелец нажал
        ✏️, бота перезапустили — состояние правки живёт в памяти и теряется,
        присланный текст не матчит ничего; (2) прислано фото/стикер/голосовое
        вместо текста. Молчащий бот выглядит сломанным.
        """
        await message.answer(
            "Не понял сообщение. Если правили пост — состояние сбросилось "
            "(например, бота перезапускали), начните заново: /review.\n"
            "Команды: /review, /queue, /help"
        )

    return dp


def main() -> int:
    cfg = get_config()
    if Bot is None:
        print("[approval_bot] ОШИБКА: aiogram не установлен. pip install -r requirements.txt")
        return 1
    if not cfg.telegram_bot_token or not cfg.telegram_owner_chat_id:
        print(
            "[approval_bot] ОШИБКА: TELEGRAM_BOT_TOKEN и/или TELEGRAM_OWNER_CHAT_ID "
            "не заданы в .env. Approve-бот без них работать не может — останавливаюсь."
        )
        return 1

    # Настройки расписания проверяются до запуска: неверная зона или время
    # слота должны останавливать службу с понятным текстом, а не всплывать
    # внутри нажатия кнопки.
    from src import schedule as _schedule

    try:
        _schedule.validate(cfg)
    except _schedule.ScheduleConfigError as exc:
        print(f"[approval_bot] СТОП: {exc}")
        return 1

    storage.init_db()
    storage.backfill_selection_attempts()
    recovered = recover_stuck_posts()
    if recovered:
        print(
            f"[approval_bot] восстановлено после нештатной остановки: "
            f"{recovered} пост(ов) вернулись из статуса 'publishing' в 'approved'"
        )

    # Выбор, оборвавшийся на генерации, возвращаем в дайджест: прошлый
    # процесс мёртв, значит и генерация, которую он вёл, уже не закончится.
    orphans = storage.close_orphan_attempts()
    if orphans:
        print(
            f"[approval_bot] закрыто оборванных попыток подготовки: {orphans} — "
            f"иначе они навсегда держали бы за собой дневные слоты постов"
        )

    released = storage.release_stale_digest_claims()
    if released:
        print(
            f"[approval_bot] возвращено в дайджест после обрыва: {released} "
            f"истор(ия/ии) — генерация не довела их до поста"
        )

    async def run() -> None:
        bot = Bot(token=cfg.telegram_bot_token)
        dp = build_dispatcher(cfg)
        await setup_bot_menu(bot)
        print("[approval_bot] Запущен. /review в личном чате — прислать следующего кандидата.")
        # drop_pending_updates: пока бот лежал, Telegram копил нажатия кнопок.
        # Без этого при старте они доставляются пачкой и ИСПОЛНЯЮТСЯ —
        # владелец получает публикации, которых уже не ждёт.
        await dp.start_polling(bot, drop_pending_updates=True)

    asyncio.run(run())
    return 0


if __name__ == "__main__":
    sys.exit(main())
