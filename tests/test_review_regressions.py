"""Регрессии по находкам независимого ревью (27.08.2026).

Каждый тест здесь соответствует конкретному подтверждённому дефекту.
Если он падает — вернулась старая дыра.
"""
from __future__ import annotations

import asyncio
import threading
import time

import pytest

from conftest import FakeBot, FakeCallback, FakeMessage, handlers_of, post_status, seed_post
from config import Config
from src import approval_bot, legal_gate, llm_client, publisher, storage


def make_cfg(*, live: bool = True) -> Config:
    cfg = Config()
    cfg.publish_mode = "live" if live else "draft"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_channel_id = "-100999"
    return cfg


# ---------------------------------------------------------------------------
# C1. Гонка обходила атомарный захват через сброс статуса в обработчике
# ---------------------------------------------------------------------------


def test_concurrent_handler_presses_publish_once(db, images_dir, monkeypatch):
    """Два одновременных нажатия ✅ ЧЕРЕЗ ОБРАБОТЧИК (а не напрямую).

    Дыра была в том, что обработчик безусловно ставил 'approved' перед
    захватом — в том числе поверх 'publishing', выставленного параллельным
    вызовом. Захват выдавался второй раз, и пост уходил в канал дважды.
    """
    seed_post("p1")
    sent, lock = [], threading.Lock()

    def slow_send(cfg, text, image_path=None, source_url=''):
        time.sleep(0.2)
        with lock:
            sent.append(text)
        return True, "ok"

    monkeypatch.setattr(publisher, "send_live_text", slow_send)
    dp = approval_bot.build_dispatcher(make_cfg())
    handler = handlers_of(dp)["approve"]

    def press():
        asyncio.run(handler(FakeCallback("approve:p1")))

    threads = [threading.Thread(target=press) for _ in range(2)]
    threads[0].start()
    time.sleep(0.05)  # второе нажатие приходит, пока идёт отправка
    threads[1].start()
    for t in threads:
        t.join()

    assert len(sent) == 1, f"пост ушёл в канал {len(sent)} раз(а)"


def _force_status(post_id: str, status: str) -> None:
    with storage.get_conn() as conn:
        conn.execute("UPDATE posts SET status=? WHERE post_id=?", (status, post_id))


def test_stale_status_read_cannot_overwrite_publishing(db, images_dir, monkeypatch):
    """То же окно, что в C1, но без гонки на секундомере.

    Прошлая починка проверяла статус в Python и записывала его вторым
    запросом. Между этими двумя запросами параллельное нажатие успевало
    захватить пост, и «approved» затирал захват. Здесь мы подставляем ровно
    это состояние: обработчик видит устаревший 'candidate', а в базе уже
    'publishing'. Пост уходить в канал не должен.
    """
    seed_post("p1")
    _force_status("p1", "publishing")          # захват сделал параллельный вызов
    monkeypatch.setattr(approval_bot, "_status_of", lambda post_id: "candidate")

    sent = []
    monkeypatch.setattr(
        publisher, "send_live_text",
        lambda cfg, text, image_path=None, source_url="": (sent.append(text), (True, "ok"))[1],
    )

    dp = approval_bot.build_dispatcher(make_cfg())
    asyncio.run(handlers_of(dp)["approve"](FakeCallback("approve:p1")))

    assert sent == [], "устаревшее чтение статуса пробило защиту от двойной публикации"
    assert post_status("p1") == "publishing", "захват параллельной отправки затёрт"


def test_edit_cannot_overwrite_publishing(db, images_dir, monkeypatch):
    """Та же дыра в правке текста: она тоже писала статус безусловно."""
    seed_post("p1")
    _force_status("p1", "publishing")
    monkeypatch.setattr(approval_bot, "_status_of", lambda post_id: "candidate")

    sent = []
    monkeypatch.setattr(
        publisher, "send_live_text",
        lambda cfg, text, image_path=None, source_url="": (sent.append(text), (True, "ok"))[1],
    )

    dp = approval_bot.build_dispatcher(make_cfg())
    msg = FakeMessage(text="Новый текст поста.\n\nПо данным BBC.")
    state = _EditState("p1")
    asyncio.run(handlers_of(dp)["msg_on_new_text"](msg, state))

    assert sent == [], "правка пробила защиту от двойной публикации"
    assert post_status("p1") == "publishing"
    assert "публикуется" in msg.answers[-1]


class _EditState:
    """Состояние FSM с уже выбранным постом — как после нажатия ✏️."""

    def __init__(self, post_id: str) -> None:
        self._data = {"editing_post_id": post_id}

    async def get_data(self):
        return self._data

    async def clear(self):
        self._data = {}


# ---------------------------------------------------------------------------
# C2. Гейт пропускал прямые обвинения
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Сергей Петров зарезал соседа и спрятал тело в гараже.",
        "Депутат Иванов брал взятки миллионами.",
        "Он изнасиловал двух школьниц в подъезде.",
        "Педофил работал в школе десять лет.",
        "Мужчина ограбил банк и скрылся.",
        "Она отравила мужа ради наследства.",
    ],
)
def test_direct_accusations_are_blocked(text):
    """Прямое обвинение без оговорки о статусе дела = иск по ст. 152 ГК."""
    assert legal_gate.check_post(text, "https://bbc.com/1").hard_fail is True


@pytest.mark.parametrize(
    "text",
    [
        "По версии следствия, задержанный убил соседа.",
        "Обвиняемый, по данным следствия, брал взятки.",
        "Суд установил, что осуждённый совершил преступление; приговор вступил в силу.",
    ],
)
def test_correct_wording_passes(text):
    """С оговоркой те же сюжеты публиковать можно — иначе гейт бесполезен."""
    assert legal_gate.check_post(text, "https://bbc.com/1").hard_fail is False


def test_convicted_person_may_be_called_criminal():
    """Осуждённого судом человека называть преступником законно. Требование
    оговорки В КАЖДОМ предложении блокировало корректные посты, где приговор
    назван ниже по тексту (реальный случай: пост про Харви Кариньяна)."""
    text = (
        "Показания выжившей стали доказательством против серийного преступника.\n\n"
        "Суд признал Кариньяна виновным и приговорил к пожизненному лишению свободы."
    )
    assert legal_gate.check_post(text, "https://bbc.com/1").hard_fail is False


def test_generic_plural_is_not_an_accusation():
    """«Мошенники выманивали деньги» — описание явления, не обвинение лица."""
    text = "В городе работали мошенники, они выманивали деньги у пенсионеров."
    assert legal_gate.check_post(text, "https://bbc.com/1").hard_fail is False


def test_singular_label_without_verdict_is_blocked():
    """А «Убийца задержан» — уже про конкретного человека без приговора."""
    assert legal_gate.check_post("Убийца задержан.", "https://bbc.com/1").hard_fail is True


def test_qualifier_from_another_sentence_does_not_help():
    """«Убийца задержан. Обвиняемый доставлен в суд.» — оговорка во втором
    предложении не оправдывает обвинение в первом."""
    text = "Убийца задержан. Обвиняемый доставлен в суд."
    assert legal_gate.check_post(text, "https://bbc.com/1").hard_fail is True


# ---------------------------------------------------------------------------
# C3. Гейт был слеп к нерусскому тексту
# ---------------------------------------------------------------------------


def test_english_post_is_blocked():
    """Основной поток источников — англоязычный. Непереписанный текст на
    латинице проходил ВЕСЬ гейт: все паттерны кириллические."""
    text = "Serial killer John Smith murdered five women in Texas last year."
    assert legal_gate.check_post(text, "https://bbc.com/1").hard_fail is True


def test_russian_post_with_latin_names_passes():
    """Латинские имена собственные — норма, блокировать нельзя."""
    text = (
        "В Ливии умер фигурант дела о теракте над Локерби. О смерти сообщили "
        "в авиакомпании Libyan Airlines.\n\nПо данным BBC News."
    )
    assert legal_gate.check_post(text, "https://bbc.com/1").hard_fail is False


# ---------------------------------------------------------------------------
# C4. Повторный прогон воскрешал опубликованный пост
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal", ["published", "rejected", "hold"])
def test_pipeline_rerun_does_not_resurrect(db, terminal):
    """post_id детерминирован в пределах суток, RSS отдаёт те же новости
    часами. Второй прогон за день конфликтовал по ключу и сбрасывал статус
    обратно в candidate — опубликованный пост всплывал в очереди снова."""
    seed_post("p1", status=terminal)

    storage.insert_post(
        post_id="p1", event_id="evt_p1", draft_ru="Перезапись.",
        fmt="A", status="candidate", scores={"average": 9}, manual_only=False,
    )

    assert post_status("p1") == terminal, "финальный статус нельзя перетирать"


def test_pipeline_rerun_updates_working_post(db):
    """А вот пост в работе обновиться должен — иначе сломается регенерация."""
    seed_post("p1", status="candidate")
    storage.insert_post(
        post_id="p1", event_id="evt_p1", draft_ru="Новый.",
        fmt="A", status="rejected", scores={"average": 3}, manual_only=False,
    )
    assert post_status("p1") == "rejected"


# ---------------------------------------------------------------------------
# H1. Посты в статусе approved исчезали из очереди навсегда
# ---------------------------------------------------------------------------


def test_approved_but_unpublished_post_returns_to_queue(db, images_dir, monkeypatch):
    """Telegram вернул 502 → статус approved → очередь показывала только
    candidate → пост пропадал навсегда. Проверяем весь путь целиком."""
    seed_post("p1")
    monkeypatch.setattr(
        publisher, "send_live_text", lambda *a, **k: (False, "Telegram 502")
    )

    ok, _ = approval_bot._publish_post(make_cfg(), "p1")
    assert ok is False
    assert post_status("p1") == "approved"

    bot = FakeBot()
    shown = asyncio.run(approval_bot.send_next_candidate(bot, make_cfg()))
    assert shown is True, "пост обязан вернуться в очередь владельца"


def test_recovered_post_is_visible(db, images_dir):
    """Пост, восстановленный после падения процесса, тоже должен показаться."""
    seed_post("p1")
    with storage.get_conn() as conn:
        conn.execute("UPDATE posts SET status='publishing' WHERE post_id='p1'")

    approval_bot.recover_stuck_posts()

    bot = FakeBot()
    assert asyncio.run(approval_bot.send_next_candidate(bot, make_cfg())) is True


# ---------------------------------------------------------------------------
# H2. Боевой канал стоял значением по умолчанию
# ---------------------------------------------------------------------------


def test_channel_id_has_no_live_default(monkeypatch):
    """Дефолтом был реальный публичный канал: запуск на другой машине с
    PUBLISH_MODE=live и без переменной публиковал бы в реальный канал."""
    monkeypatch.delenv("TELEGRAM_CHANNEL_ID", raising=False)
    assert Config().telegram_channel_id == ""


def test_live_publish_without_channel_stops_loudly(monkeypatch):
    """И тогда защита в publisher обязана сработать, а не промолчать."""
    cfg = make_cfg()
    cfg.telegram_channel_id = ""
    with pytest.raises(SystemExit):
        publisher.send_live_text(cfg, "Текст.\n\nПо данным BBC.", None)


# ---------------------------------------------------------------------------
# H3. Токен бота утекал в лог и в чат владельца
# ---------------------------------------------------------------------------


def test_token_is_redacted_from_errors():
    token = "12:FAKE-TOK"
    err = f"Client error '400' for url 'https://api.telegram.org/bot{token}/sendMessage'"
    out = publisher._redact(err, token)
    assert token not in out
    assert "СКРЫТ" in out


def test_token_redacted_even_if_unknown():
    """Даже если токен не передан — шаблон /botNNN:XXX всё равно вырезается."""
    err = "url 'https://api.telegram.org/bot123456:SECRETVALUE/sendPhoto'"
    assert "SECRETVALUE" not in publisher._redact(err, "")


# ---------------------------------------------------------------------------
# H5/H6. Точность юридических эвристик
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    ["Сергей Иванов получил 10 лет колонии", "Мария Петрова провела 15 лет в бегах"],
)
def test_sentence_length_is_not_an_age(text):
    """«10 лет колонии» — это срок, а не возраст ребёнка."""
    assert legal_gate.check_minors(text) == []


@pytest.mark.parametrize(
    "text",
    [
        "Тело школьницы Марии Ивановой нашли в лесу",
        "Пропала Аня Кузнецова, ученица 6 класса",
        "Двенадцатилетняя Аня Кузнецова не вернулась домой",
    ],
)
def test_minor_without_digits_is_detected(text):
    """Ребёнок в новостях чаще назван словом, а не числом лет."""
    assert legal_gate.check_minors(text) != []


@pytest.mark.parametrize(
    "text",
    [
        "Российские военные расстреляли пленных",
        "Солдаты российской армии обвиняются в мародёрстве",
        "Бойцы ЧВК пытали задержанных",
        "Российские полицейские избили задержанного в отделе",
    ],
)
def test_army_rf_wordings_are_caught(text):
    """Ст. 207.3 ловилась одной формулировкой из пяти."""
    assert legal_gate.check_army_rf(text) is True


def test_neutral_foreign_news_not_flagged_as_army():
    assert legal_gate.check_army_rf("В Зимбабве задержали чиновника") is False


def test_foreign_police_is_not_article_207_3():
    """Полиция есть в любой стране: корейский полицейский не имеет отношения
    к ст. 207.3, и помечать его — значит утопить настоящие флаги в шуме."""
    text = (
        "В Южной Корее арестовали сержанта полиции: по версии следствия, "
        "полицейский прекращал расследования исчезновений."
    )
    assert legal_gate.check_army_rf(text) is False


# ---------------------------------------------------------------------------
# H7. Неизвестная модель отключала лимит расходов
# ---------------------------------------------------------------------------


def test_unknown_model_is_not_free():
    assert llm_client._cost_for("нет-такой-модели", 100_000, 100_000) > 0


# ---------------------------------------------------------------------------
# M2. Стилевой список работал как юридический отказ
# ---------------------------------------------------------------------------


def test_style_words_do_not_block_owner_text(db, images_dir, monkeypatch):
    """«Кроме того» — редакторский штамп, а не нарушение закона. Владелец
    получал отказ «юр. гейт не пропускает» за стилистику."""
    seed_post("p1", draft="Кроме того, суд продлил арест.\n\nПо данным BBC News.")
    sent = []
    monkeypatch.setattr(
        publisher, "send_live_text", lambda cfg, t, i=None, source_url='': (sent.append(t), (True, "ok"))[1]
    )

    ok, status = approval_bot._publish_post(make_cfg(), "p1")

    assert ok is True, f"публикация не должна блокироваться стилем: {status}"


def test_style_words_still_block_machine_text():
    """Но для машинной генерации штамп остаётся жёстким отказом."""
    r = legal_gate.check_post(
        "Кроме того, суд продлил арест.", "https://bbc.com/1", strict_style=True
    )
    assert r.hard_fail is True


# ---------------------------------------------------------------------------
# C9. Протухшее подтверждение нажатия выдавалось за несделанное действие
# ---------------------------------------------------------------------------


class _ExpiredCallback(FakeCallback):
    """Нажатие, подтверждение которого Telegram уже не принимает.

    Живой случай 27.08: владелец не дождался «часиков» на ✅ и нажал второй
    раз. Пост ушёл в канал один раз — защита сработала, — но ответ на второе
    нажатие протух, ловушка ошибок написала «действие не выполнено», и
    человек решил, что публикации не было.
    """

    async def answer(self, text: str = "", **kwargs):
        raise RuntimeError(
            "Telegram server says - Bad Request: query is too old and "
            "response timeout expired or query ID is invalid"
        )


def test_expired_callback_answer_is_not_an_error(db, images_dir, monkeypatch):
    """Просроченное подтверждение — косметика, а не сбой: решение уже в базе."""
    seed_post("p1")
    monkeypatch.setattr(
        publisher, "send_live_text",
        lambda cfg, text, image_path=None, source_url="": (True, "ok"),
    )
    dp = approval_bot.build_dispatcher(make_cfg())

    asyncio.run(handlers_of(dp)["approve"](_ExpiredCallback("approve:p1")))

    assert post_status("p1") == "published", "решение владельца должно быть исполнено"


def test_other_answer_errors_still_surface(db, images_dir, monkeypatch):
    """А настоящий сбой ответа глушить нельзя — иначе вернутся тихие отказы."""

    class Broken(FakeCallback):
        async def answer(self, text: str = "", **kwargs):
            raise RuntimeError("Telegram server says - Forbidden: bot was blocked")

    seed_post("p1")
    monkeypatch.setattr(
        publisher, "send_live_text",
        lambda cfg, text, image_path=None, source_url="": (True, "ok"),
    )
    dp = approval_bot.build_dispatcher(make_cfg())

    with pytest.raises(RuntimeError, match="blocked"):
        asyncio.run(handlers_of(dp)["approve"](Broken("approve:p1")))


def test_press_is_acknowledged_before_publishing(db, images_dir, monkeypatch):
    """Подтверждение уходит ДО отправки, иначе оно и протухает: публикация
    картинки занимает секунды, а Telegram ждёт ответа около пятнадцати."""
    seed_post("p1")
    order = []

    class Recording(FakeCallback):
        async def answer(self, text: str = "", **kwargs):
            order.append("ack")

    def slow_send(cfg, text, image_path=None, source_url=""):
        order.append("publish")
        return True, "ok"

    monkeypatch.setattr(publisher, "send_live_text", slow_send)
    dp = approval_bot.build_dispatcher(make_cfg())

    asyncio.run(handlers_of(dp)["approve"](Recording("approve:p1")))

    assert order[:2] == ["ack", "publish"], f"порядок оказался {order}"
