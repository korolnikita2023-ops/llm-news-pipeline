"""Роли доступа: владелец и редактор (S23–S34).

Главный риск этого куска — не «кнопка не работает», а тихое повышение прав:
фильтр диспетчера пускает в бота ВСЕХ админов и роли не различает, поэтому
проверка «владелец ли» обязана стоять внутри каждого обработчика управления
составом. Ниже это проверяется прямым вызовом обработчиков от лица редактора.

Второй риск — воскрешение удалённого. Пока список админов был в .env,
удаление через бота было бы отменено первым же перезапуском процесса.
"""
from __future__ import annotations

import asyncio

import pytest

from conftest import FakeCallback, FakeMessage, handlers_of
from config import Config
from src import approval_bot, roles

OWNER = 777
EDITOR = 111
STRANGER = 999


def make_cfg(owner: str | int = OWNER, admins: str = "") -> Config:
    cfg = Config()
    cfg.publish_mode = "live"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = str(owner)
    cfg.telegram_channel_id = "-100999"
    # Config() читает настоящий .env — состав оттуда сделал бы тесты
    # зависимыми от машины разработчика.
    cfg.telegram_admin_ids = admins
    cfg.telegram_admin_labels = ""
    return cfg


class FakeState:
    """Минимальный FSMContext: обработчики только ставят и чистят состояние."""

    def __init__(self) -> None:
        self.state = None
        self.data: dict = {}

    async def set_state(self, state) -> None:
        self.state = state

    async def clear(self) -> None:
        self.state = None
        self.data = {}

    async def update_data(self, **kw) -> None:
        self.data.update(kw)

    async def get_data(self) -> dict:
        return self.data


def build(cfg=None):
    return approval_bot.build_dispatcher(cfg or make_cfg())


def filter_of(dp, kind: str = "message"):
    handler = getattr(dp, kind)._handler
    return handler.filters[0].callback


def event_from(user_id: int):
    from types import SimpleNamespace

    return SimpleNamespace(from_user=SimpleNamespace(id=user_id))


# ---------------------------------------------------------------------------
# S23. Перенос состава из .env в базу
# ---------------------------------------------------------------------------


def test_owner_from_env_gets_owner_role(db):
    roles.bootstrap(make_cfg(OWNER, admins="111,222"))
    assert roles.role_of(OWNER) == roles.ROLE_OWNER


def test_env_admins_become_editors(db):
    roles.bootstrap(make_cfg(OWNER, admins="111,222"))
    assert roles.role_of(111) == roles.ROLE_ADMIN
    assert roles.role_of(222) == roles.ROLE_ADMIN


def test_owner_counts_as_admin_for_access(db):
    roles.bootstrap(make_cfg())
    assert roles.is_admin(OWNER) is True
    assert roles.is_owner(OWNER) is True


def test_stranger_has_no_role(db):
    roles.bootstrap(make_cfg(OWNER, admins="111"))
    assert roles.role_of(STRANGER) is None
    assert roles.is_admin(STRANGER) is False


def test_removed_editor_does_not_come_back_after_restart(db):
    """Ключевое свойство: .env переносится в базу ОДИН раз.

    Иначе удаление редактора отменялось бы первым же перезапуском бота,
    пока его ID остаётся в файле, — а править файл владелец не может.
    """
    cfg = make_cfg(OWNER, admins="111")
    roles.bootstrap(cfg)
    roles.remove_admin(111)

    roles.bootstrap(cfg)  # рестарт процесса

    assert roles.role_of(111) is None


def test_owner_change_in_env_demotes_previous_owner(db):
    """Владелец в базе ровно один: иначе смена владельца в .env оставила бы
    двух человек с правом менять состав."""
    roles.bootstrap(make_cfg(OWNER))
    roles.bootstrap(make_cfg(555))

    assert roles.role_of(555) == roles.ROLE_OWNER
    assert roles.role_of(OWNER) == roles.ROLE_ADMIN, "прежний владелец не должен терять доступ"


def test_bootstrap_is_idempotent(db):
    cfg = make_cfg(OWNER, admins="111")
    roles.bootstrap(cfg)
    roles.bootstrap(cfg)
    assert len(roles.list_admins()) == 2


# ---------------------------------------------------------------------------
# S24. Доступ проверяется в базе на каждом апдейте, без перезапуска
# ---------------------------------------------------------------------------


def test_added_editor_gets_access_without_restart(db):
    dp = build(make_cfg(OWNER))
    check = filter_of(dp)
    assert bool(check(event_from(EDITOR))) is False

    roles.add_admin(EDITOR, label="Редактор", added_by=OWNER)

    assert bool(check(event_from(EDITOR))) is True, (
        "права читаются из базы на каждом апдейте — перезапуск не нужен"
    )


def test_removed_editor_loses_access_immediately(db):
    dp = build(make_cfg(OWNER, admins="111"))
    check = filter_of(dp)
    assert bool(check(event_from(EDITOR))) is True

    roles.remove_admin(EDITOR)

    assert bool(check(event_from(EDITOR))) is False


def test_removed_editor_cannot_press_buttons(db):
    """Самое опасное: у редактора кнопки публикации остаются в переписке."""
    dp = build(make_cfg(OWNER, admins="111"))
    roles.remove_admin(EDITOR)
    assert bool(filter_of(dp, "callback_query")(event_from(EDITOR))) is False


def test_access_falls_back_to_env_when_db_unreadable(db, monkeypatch, capsys):
    """База недоступна — владельца нельзя запереть снаружи молча."""
    dp = build(make_cfg(OWNER, admins="111"))
    check = filter_of(dp)

    def boom(*a, **kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(roles, "is_admin", boom)

    assert bool(check(event_from(EDITOR))) is True
    assert bool(check(event_from(STRANGER))) is False
    assert "по .env" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# S25. Редактор НЕ управляет составом — проверка внутри обработчиков
# ---------------------------------------------------------------------------


def test_editor_cannot_add_admin_by_command(db):
    """Фильтр диспетчера редактора пропускает — значит, роль обязана
    проверяться внутри обработчика."""
    dp = build(make_cfg(OWNER, admins="111"))
    msg = FakeMessage(text="/addadmin 222 Чужой", user_id=EDITOR)

    asyncio.run(handlers_of(dp)["msg_cmd_addadmin"](msg, FakeState()))

    assert roles.role_of(222) is None, "редактор не должен уметь заводить админов"
    assert "владелец" in msg.answers[-1].lower()


def test_editor_cannot_remove_admin_by_command(db):
    dp = build(make_cfg(OWNER, admins="111,222"))
    msg = FakeMessage(text="/deladmin 222", user_id=EDITOR)

    asyncio.run(handlers_of(dp)["msg_cmd_deladmin"](msg))

    assert roles.role_of(222) == roles.ROLE_ADMIN, "состав не должен измениться"


def test_editor_cannot_remove_admin_by_button(db):
    """Кнопки живут в переписке вечно — старое сообщение владельца могли
    переслать редактору."""
    dp = build(make_cfg(OWNER, admins="111,222"))
    call = FakeCallback("admin_del:222", user_id=EDITOR)

    asyncio.run(handlers_of(dp)["admin_del"](call))

    assert roles.role_of(222) == roles.ROLE_ADMIN


def test_editor_cannot_open_add_dialog(db):
    dp = build(make_cfg(OWNER, admins="111"))
    call = FakeCallback("admin_add", user_id=EDITOR)
    state = FakeState()

    asyncio.run(handlers_of(dp)["admin_add"](call, state))

    assert state.state is None, "редактору нельзя даже открывать диалог добавления"


def test_role_is_rechecked_when_state_already_set(db):
    """Между нажатием кнопки и ответом права могли отобрать: состояние FSM
    живёт в памяти процесса и само по себе прав не даёт."""
    dp = build(make_cfg(OWNER, admins="111"))
    msg = FakeMessage(text="222 Чужой", user_id=EDITOR)
    state = FakeState()
    asyncio.run(state.set_state(approval_bot.AdminStates.waiting_for_new_admin))

    asyncio.run(handlers_of(dp)["msg_on_new_admin_input"](msg, state))

    assert roles.role_of(222) is None
    assert state.state is None


def test_editor_still_reviews_queue(db, images_dir):
    """Отобрали только управление составом. Разбор очереди у редактора
    остаётся — иначе роль бессмысленна."""
    from conftest import post_status, seed_post

    seed_post("p1")
    dp = build(make_cfg(OWNER, admins="111"))
    call = FakeCallback("reject:p1", user_id=EDITOR)

    asyncio.run(handlers_of(dp)["reject"](call))

    assert post_status("p1") == "rejected"


# ---------------------------------------------------------------------------
# S26. Владелец управляет составом через бота
# ---------------------------------------------------------------------------


def test_owner_adds_editor_by_command(db):
    dp = build(make_cfg(OWNER))
    msg = FakeMessage(text="/addadmin 222 Bob", user_id=OWNER)

    asyncio.run(handlers_of(dp)["msg_cmd_addadmin"](msg, FakeState()))

    assert roles.role_of(222) == roles.ROLE_ADMIN
    entry = [a for a in roles.list_admins() if a["user_id"] == 222][0]
    assert entry["label"] == "Bob"
    assert entry["added_by"] == OWNER
    assert "/start" in msg.answers[-1], "человеку надо сказать написать боту первым"


def test_owner_adds_editor_by_forwarded_message(db):
    """Чтобы не спрашивать ID: владелец пересылает сообщение человека."""
    from types import SimpleNamespace

    dp = build(make_cfg(OWNER))
    forwarded = SimpleNamespace(id=222, first_name="Bob", last_name="Г.")
    msg = FakeMessage(text="переслано", user_id=OWNER, forward_from=forwarded)

    asyncio.run(handlers_of(dp)["msg_on_new_admin_input"](msg, FakeState()))

    assert roles.role_of(222) == roles.ROLE_ADMIN
    assert [a for a in roles.list_admins() if a["user_id"] == 222][0]["label"] == "Bob Г."


def test_owner_removes_editor_by_button(db):
    dp = build(make_cfg(OWNER, admins="222"))
    call = FakeCallback("admin_del:222", user_id=OWNER)

    asyncio.run(handlers_of(dp)["admin_del"](call))

    assert roles.role_of(222) is None


def test_owner_cannot_remove_himself(db):
    """Иначе бот запирается изнутри: управлять составом станет некому."""
    dp = build(make_cfg(OWNER, admins="111"))
    msg = FakeMessage(text=f"/deladmin {OWNER}", user_id=OWNER)

    asyncio.run(handlers_of(dp)["msg_cmd_deladmin"](msg))

    assert roles.role_of(OWNER) == roles.ROLE_OWNER
    assert "владельца" in msg.answers[-1].lower()


def test_command_typed_in_add_dialog_is_not_eaten(db):
    """Диалог добавления стоит в цепочке раньше /review. Без исключения
    команд владелец, передумавший на полпути, получил бы «неверный ID /review»
    вместо очереди."""
    dp = build(make_cfg(OWNER))
    handler = [
        h for h in dp.message.handlers
        if h.callback.__name__ == "on_new_admin_input"
    ][0]
    magic = [f.magic for f in handler.filters if f.magic is not None]

    from types import SimpleNamespace

    assert magic, "у обработчика должен быть фильтр, отсекающий команды"
    assert not any(m.resolve(SimpleNamespace(text="/review")) for m in magic)
    assert all(m.resolve(SimpleNamespace(text="111111111")) for m in magic)


def test_add_dialog_opens_without_arguments(db):
    dp = build(make_cfg(OWNER))
    msg = FakeMessage(text="/addadmin", user_id=OWNER)
    state = FakeState()

    asyncio.run(handlers_of(dp)["msg_cmd_addadmin"](msg, state))

    assert state.state == approval_bot.AdminStates.waiting_for_new_admin


# ---------------------------------------------------------------------------
# S27. Мусор на входе не ломает состав
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("bad", ["не-число", "-100999", "0", "  ", "12.5", "@alice_example"])
def test_garbage_id_is_rejected_with_explanation(db, bad):
    """Отрицательные ID — это чаты и каналы, а не люди: такая запись
    навсегда осталась бы мёртвой строкой в списке."""
    roles.bootstrap(make_cfg(OWNER))
    before = len(roles.list_admins())

    with pytest.raises(roles.RoleError):
        roles.add_admin(bad, added_by=OWNER)

    assert len(roles.list_admins()) == before


def test_adding_existing_editor_says_so(db):
    roles.bootstrap(make_cfg(OWNER, admins="222"))
    with pytest.raises(roles.RoleError, match="уже в списке"):
        roles.add_admin(222, added_by=OWNER)


def test_adding_owner_says_so(db):
    roles.bootstrap(make_cfg(OWNER))
    with pytest.raises(roles.RoleError, match="владелец"):
        roles.add_admin(OWNER, added_by=OWNER)


def test_removing_unknown_says_so(db):
    roles.bootstrap(make_cfg(OWNER))
    with pytest.raises(roles.RoleError, match="нет в списке"):
        roles.remove_admin(424242)


def test_bad_id_in_handler_does_not_crash(db):
    dp = build(make_cfg(OWNER))
    msg = FakeMessage(text="/addadmin @alice_example", user_id=OWNER)

    asyncio.run(handlers_of(dp)["msg_cmd_addadmin"](msg, FakeState()))

    assert msg.answers, "бот обязан ответить, а не промолчать"
    assert "ID" in msg.answers[-1]


# ---------------------------------------------------------------------------
# S28. Экран «Администраторы» показывает роли
# ---------------------------------------------------------------------------


def test_admins_screen_shows_roles(db):
    dp = build(make_cfg(OWNER, admins="111"))
    msg = FakeMessage(text=approval_bot.BTN_ADMINS, user_id=OWNER)

    asyncio.run(handlers_of(dp)["msg_btn_admins"](msg))

    out = msg.answers[-1]
    assert "владелец" in out
    assert "редактор" in out
    assert "← это вы" in out


def test_owner_sees_management_buttons(db):
    dp = build(make_cfg(OWNER, admins="111"))
    msg = FakeMessage(text=approval_bot.BTN_ADMINS, user_id=OWNER)

    asyncio.run(handlers_of(dp)["msg_btn_admins"](msg))

    kb = msg.markups[-1]
    labels = [b.text for row in kb.inline_keyboard for b in row]
    assert any("Добавить" in t for t in labels)
    assert any("Убрать" in t for t in labels)


def test_editor_sees_no_management_buttons(db):
    dp = build(make_cfg(OWNER, admins="111"))
    msg = FakeMessage(text=approval_bot.BTN_ADMINS, user_id=EDITOR)

    asyncio.run(handlers_of(dp)["msg_btn_admins"](msg))

    kb = msg.markups[-1]
    assert not hasattr(kb, "inline_keyboard"), "редактору не показываем управление составом"


def test_owner_help_mentions_management(db):
    dp = build(make_cfg(OWNER, admins="111"))
    owner_msg = FakeMessage(text="/start", user_id=OWNER)
    editor_msg = FakeMessage(text="/start", user_id=EDITOR)

    asyncio.run(handlers_of(dp)["msg_cmd_start"](owner_msg))
    asyncio.run(handlers_of(dp)["msg_cmd_start"](editor_msg))

    assert "/addadmin" in owner_msg.answers[-1]
    assert "/addadmin" not in editor_msg.answers[-1], (
        "редактору не показываем команды, которые ему всё равно откажут"
    )
