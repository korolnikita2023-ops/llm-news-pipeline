"""Зеркалирование в Threads.

Главное, что здесь защищается, — правило владельца №0: **Telegram никогда
не зависит от Threads**. Отказ Meta, протухший токен, обрыв связи, кривой
ответ — ничто из этого не имеет права заблокировать публикацию в канале,
откатить её или задвоить.

Второе по важности — «один пост Telegram → максимум один пост в Threads»
при любом стечении обстоятельств: повторный прогон воркера, перезапуск
между постановкой в очередь и отправкой, обрыв ровно в момент публикации.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from config import Config
from src import crosspost_due, storage, threads_api, threads_format

PLATFORM = "threads"
PRODUCTION = "-100111"


def make_cfg(**over) -> Config:
    cfg = Config()
    cfg.publish_mode = "live"
    cfg.workflow_mode = "auto"
    cfg.telegram_bot_token = "42:TEST"
    cfg.telegram_owner_chat_id = "777"
    cfg.telegram_admin_ids = ""
    cfg.telegram_admin_labels = ""
    cfg.telegram_channel_id = PRODUCTION
    cfg.schedule_tz = "Europe/Moscow"
    cfg.threads_enabled = True
    cfg.threads_user_id = "17841400000000000"
    cfg.threads_access_token = "TH-SECRET-1"
    cfg.threads_text_limit = 500
    cfg.threads_max_attempts = 6
    cfg.threads_refresh_before_days = 15
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


@pytest.fixture(autouse=True)
def _news_source(monkeypatch):
    """Источник и статус дела берутся из привязанной новости.

    В боевом коде их отдаёт `_load_news_item`, здесь новостей нет. Без
    источника юридический гейт честно заваливает любой текст, и все
    проверки ниже упирались бы в это, а не в то, что проверяют.
    """
    monkeypatch.setattr("src.approval_bot._source_url_of", lambda pid: "https://x.ru/1")
    monkeypatch.setattr("src.approval_bot._case_status_of", lambda pid: "")


def publish_and_queue(post_id: str) -> None:
    """Как в бою: сначала фиксируется публикация, отдельно — очередь зеркала."""
    storage.finish_publishing(post_id, sent=True)
    storage.enqueue_crosspost(post_id, PLATFORM)


def add_post(post_id: str, text: str = "", status: str = "publishing") -> None:
    with storage.get_conn() as c:
        c.execute(
            "INSERT INTO posts (post_id, event_id, draft_ru, format, status, created_at, "
            "channel_id) VALUES (?,?,?,?,?,?,?)",
            (post_id, f"ev-{post_id}", text or "Текст поста.\n\nПо данным СК.",
             "A", status, storage.now_iso(), PRODUCTION),
        )


class FakeClient:
    """Подставной Threads. Считает вызовы — на этом ловятся дубли."""

    def __init__(self, *, create_exc=None, publish_exc=None, state_exc=None,
                 profile_exc=None, container_status="FINISHED"):
        self.created = []
        self.published = []
        self.state_calls = []
        self.profile_calls = 0
        self.create_exc = create_exc
        self.publish_exc = publish_exc
        self.state_exc = state_exc
        # По умолчанию профиль недоступен: сломанная интеграция должна
        # оставаться сломанной, пока проверка связи не пройдёт. Явный
        # profile_exc=False означает «связь есть».
        self.profile_exc = (threads_api.ThreadsError("HTTP 401: доступ не восстановлен",
                                                     auth=True)
                            if profile_exc is None else profile_exc)
        self.container_status = container_status

    def profile(self):
        self.profile_calls += 1
        if self.profile_exc:
            raise self.profile_exc
        return {"id": "1", "username": "example_channel"}

    def create_text_container(self, text, link_url=""):
        if self.create_exc:
            raise self.create_exc
        self.created.append((text, link_url))
        return f"cont-{len(self.created)}"

    def publish_container(self, container_id):
        if self.publish_exc:
            raise self.publish_exc
        self.published.append(container_id)
        return f"remote-{len(self.published)}"

    def container_state(self, container_id):
        self.state_calls.append(container_id)
        if self.state_exc:
            raise self.state_exc
        return threads_api.ContainerState(status=self.container_status)


# ---------------------------------------------------------------------------
# Telegram не зависит от Threads
# ---------------------------------------------------------------------------


def test_publishing_is_recorded_before_the_mirror(db):
    """Статус поста фиксируется отдельно от очереди зеркала.

    Порядок именно такой, потому что цена ошибок несимметрична: сбой
    постановки в очередь стоит потерянного зеркала, а откат статуса — поста,
    ушедшего в канал дважды.
    """
    add_post("p1")
    publish_and_queue("p1")

    with storage.get_conn() as c:
        assert c.execute("SELECT status FROM posts WHERE post_id='p1'").fetchone()[0] \
            == "published"
    job = storage.crosspost_row("p1", PLATFORM)
    assert job["status"] == "pending"


def test_failed_telegram_send_never_queues_threads(db):
    """Не ушло в Telegram — в Threads тем более нечего зеркалить."""
    add_post("p2")
    storage.finish_publishing("p2", sent=False)

    assert storage.crosspost_row("p2", PLATFORM) is None
    with storage.get_conn() as c:
        assert c.execute("SELECT status FROM posts WHERE post_id='p2'").fetchone()[0] \
            == "approved"


def test_disabled_integration_queues_nothing(db):
    """Выключатель — граница раскатки: старые посты не зеркалируются."""
    add_post("p3")
    storage.finish_publishing("p3", sent=True)

    assert storage.crosspost_row("p3", PLATFORM) is None
    with storage.get_conn() as c:
        assert c.execute("SELECT status FROM posts WHERE post_id='p3'").fetchone()[0] \
            == "published"


def test_old_published_posts_are_never_backfilled(db):
    """Включение Threads не выкладывает накопленное задним числом."""
    for i in range(5):
        add_post(f"old{i}", status="published")

    storage.init_db()
    assert storage.claim_crosspost(PLATFORM, storage.now_iso()) is None
    assert storage.crosspost_counts(
        PLATFORM, "2000-01-01", "2999-01-01")["queued"] == 0


def test_publish_post_does_not_call_threads(db):
    """В критическом пути Telegram обращений к Meta нет."""
    import inspect

    from src import approval_bot

    body = inspect.getsource(approval_bot._publish_post)
    for forbidden in ("ThreadsClient", "graph.threads.net", "create_text_container",
                      "publish_container"):
        assert forbidden not in body, f"{forbidden} не должно быть в _publish_post"


# ---------------------------------------------------------------------------
# Один пост Telegram → максимум один пост в Threads
# ---------------------------------------------------------------------------


def test_same_post_queued_twice_stays_one_job(db):
    add_post("p4")
    publish_and_queue("p4")
    storage.enqueue_crosspost("p4", PLATFORM)
    storage.enqueue_crosspost("p4", PLATFORM)

    with storage.get_conn() as c:
        assert c.execute(
            "SELECT COUNT(*) FROM crossposts WHERE post_id='p4'").fetchone()[0] == 1


def test_claim_is_exclusive(db):
    add_post("p5")
    publish_and_queue("p5")

    first = storage.claim_crosspost(PLATFORM, storage.now_iso())
    second = storage.claim_crosspost(PLATFORM, storage.now_iso())
    assert first is not None and second is None


def test_worker_run_twice_publishes_once(db, monkeypatch):
    add_post("p6")
    publish_and_queue("p6")

    cfg = make_cfg()
    client = FakeClient()
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(threads_api, "ThreadsClient", lambda *a, **k: client)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()
    crosspost_due.run()

    assert len(client.published) == 1
    assert storage.crosspost_row("p6", PLATFORM)["status"] == "done"


def test_crash_after_publish_does_not_duplicate(db, monkeypatch):
    """Обрыв ровно после публикации: повтор спрашивает Meta, а не гадает.

    Это тот случай, ради которого вообще заведён container_id. Пост уже в
    ленте, но мы об этом не узнали. Слепой повтор выложил бы второй.
    """
    add_post("p7")
    publish_and_queue("p7")
    storage.claim_crosspost(PLATFORM, storage.now_iso())
    storage.save_crosspost_container("p7", PLATFORM, "cont-abc")
    # процесс убит здесь: статус остался 'sending', исход неизвестен
    storage.recover_stuck_crossposts(
        PLATFORM, (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())

    cfg = make_cfg()
    client = FakeClient(container_status="PUBLISHED")
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()

    assert client.state_calls == ["cont-abc"]
    assert client.published == [], "второй публикации быть не должно"
    assert storage.crosspost_row("p7", PLATFORM)["status"] == "done"


def test_restart_between_queue_and_send_still_sends(db, monkeypatch):
    """Перезапуск между постановкой в очередь и отправкой ничего не теряет."""
    add_post("p8")
    publish_and_queue("p8")

    cfg = make_cfg()
    client = FakeClient()
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()
    assert storage.crosspost_row("p8", PLATFORM)["status"] == "done"
    assert len(client.published) == 1


def test_stuck_job_returns_to_queue(db):
    add_post("p9")
    publish_and_queue("p9")
    storage.claim_crosspost(PLATFORM, storage.now_iso())
    assert storage.crosspost_row("p9", PLATFORM)["status"] == "sending"

    freed = storage.recover_stuck_crossposts(
        PLATFORM, (datetime.now(timezone.utc) + timedelta(minutes=1)).isoformat())
    assert freed == 1
    assert storage.crosspost_row("p9", PLATFORM)["status"] == "pending"


# ---------------------------------------------------------------------------
# Отказы Meta
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("exc,expect_status", [
    (threads_api.ThreadsError("HTTP 500: oops", temporary=True), "failed"),
    (threads_api.ThreadsError("HTTP 429: slow down", temporary=True, retry_after=30),
     "failed"),
    (threads_api.ThreadsError("HTTP 400: bad text"), "dead"),
])
def test_meta_failures_do_not_touch_telegram(db, monkeypatch, exc, expect_status):
    add_post("pf")
    publish_and_queue("pf")

    cfg = make_cfg()
    client = FakeClient(create_exc=exc)
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    assert crosspost_due.run() == 0
    row = storage.crosspost_row("pf", PLATFORM)
    assert row["status"] == expect_status
    with storage.get_conn() as c:
        assert c.execute("SELECT status FROM posts WHERE post_id='pf'").fetchone()[0] \
            == "published", "статус поста в Telegram трогать нельзя"


def test_retry_is_bounded(db, monkeypatch):
    add_post("pb")
    publish_and_queue("pb")

    cfg = make_cfg(threads_max_attempts=3)
    client = FakeClient(create_exc=threads_api.ThreadsError("HTTP 503", temporary=True))
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    for _ in range(5):
        with storage.get_conn() as c:
            c.execute("UPDATE crossposts SET next_retry_at=NULL WHERE post_id='pb'")
        crosspost_due.run()

    row = storage.crosspost_row("pb", PLATFORM)
    assert row["status"] == "dead"
    assert row["attempts"] <= 3


def test_expired_token_marks_degraded_and_notifies_once(db, monkeypatch):
    add_post("pt")
    publish_and_queue("pt")

    cfg = make_cfg()
    auth_exc = threads_api.ThreadsError("HTTP 401: token expired", auth=True)
    client = FakeClient(create_exc=auth_exc)
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    sent = []
    from src import notify, roles
    monkeypatch.setattr(roles, "list_admins", lambda *a, **k: [{"user_id": 777}])
    monkeypatch.setattr(notify, "_send_message",
                        lambda cfg, chat, text, keyboard=None: sent.append(text))

    crosspost_due.run()
    assert storage.integration_get(PLATFORM)["degraded"] == 1
    assert len(sent) == 1
    assert "Threads требует повторной авторизации" in sent[0]

    # второй прогон не должен слать то же самое снова
    with storage.get_conn() as c:
        c.execute("UPDATE crossposts SET status='pending', next_retry_at=NULL")
    crosspost_due.run()
    assert len(sent) == 1, "уведомление не должно повторяться каждые пять минут"


def test_degraded_integration_stops_hammering_meta(db, monkeypatch):
    add_post("pd")
    publish_and_queue("pd")
    storage.integration_set(PLATFORM, degraded=1, last_error="протух")

    cfg = make_cfg()
    client = FakeClient()
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()
    assert client.created == [] and client.published == []
    assert storage.crosspost_row("pd", PLATFORM)["status"] == "pending"


def test_malformed_response_freezes_job_instead_of_retrying(db, monkeypatch):
    """Непонятный исход опаснее простоя: повтор мог бы задвоить пост."""
    add_post("pm")
    publish_and_queue("pm")

    class Broken(FakeClient):
        def create_text_container(self, text, link_url=""):
            raise RuntimeError("совершенно неожиданное")

    cfg = make_cfg()
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient",
                        lambda *a, **k: Broken())

    crosspost_due.run()
    assert storage.crosspost_row("pm", PLATFORM)["status"] == "unknown"


def test_app_disabled_does_nothing(db, monkeypatch):
    cfg = make_cfg(threads_enabled=False)
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    assert crosspost_due.run() == 0


# ---------------------------------------------------------------------------
# Секреты
# ---------------------------------------------------------------------------


def test_token_never_appears_in_stored_error(db, monkeypatch):
    add_post("ps")
    publish_and_queue("ps")

    cfg = make_cfg()
    token = cfg.threads_access_token
    leaky = threads_api.ThreadsError(
        threads_api._safe(
            f"HTTP 400 at https://graph.threads.net/v1.0/me?access_token={token}", token))
    client = FakeClient(create_exc=leaky)
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()
    row = storage.crosspost_row("ps", PLATFORM)
    assert token not in (row["last_error"] or "")
    assert "***" in row["last_error"]


def test_safe_scrubs_token_in_every_shape():
    token = "SECRET-VAL1"
    raw = (f"error at https://graph.threads.net/v1.0/me?access_token={token}&x=1 "
           f"body: {{'token': '{token}'}}")
    out = threads_api._safe(raw, token)
    assert token not in out, "известный секрет вырезается в любом виде"

    # Даже не зная секрета, параметр в URL вырезаем: именно так токен и
    # утекал в журнал в прошлый раз — через URL внутри исключения httpx.
    blind = threads_api._safe(f"GET https://graph.threads.net/me?access_token={token}&x=1")
    assert token not in blind


def test_integration_state_refuses_unknown_fields(db):
    with pytest.raises(ValueError):
        storage.integration_set(PLATFORM, access_token="секрет")


# ---------------------------------------------------------------------------
# Форматтер
# ---------------------------------------------------------------------------


def test_short_post_passes_through_unchanged():
    draft = ("Контрабандисты сбросили в океан полтонны кокаина.\n\n"
             "По версии следствия, одиннадцать участников группировки "
             "планировали выловить груз.\n\n"
             "По данным NCA.")
    res = threads_format.build(draft, "https://nca.gov.uk/x", 500)
    assert res.post is not None
    assert res.post.text == draft
    assert res.post.link_url == "https://nca.gov.uk/x"


def test_long_post_is_cut_by_paragraphs_and_keeps_attribution():
    draft = ("Первый абзац с крючком.\n\n"
             + "Второй абзац. " * 40 + "\n\n"
             + "По данным СК России.")
    res = threads_format.build(draft, "https://sledcom.ru/x", 500)
    assert res.post is not None
    assert len(res.post.text) <= 500
    assert res.post.text.startswith("Первый абзац с крючком.")
    assert res.post.text.endswith("По данным СК России."), "источник терять нельзя"


def test_never_cuts_mid_sentence():
    draft = "А" * 900 + "\n\nПо данным СК."
    res = threads_format.build(draft, "https://sledcom.ru/x", 500)
    assert res.post is None
    assert res.skip_reason == threads_format.SKIP_TOO_LONG


def test_legal_gate_blocks_truncated_text():
    """Обрезка не должна превращать оговорку в обвинение."""
    draft = ("Иван Петров убил соседа.\n\n"
             "По версии следствия, это произошло во время ссоры.\n\n"
             "По данным СК.")

    class Verdict:
        hard_fail = True
        issues = ["утверждение о виновности без ссылки на решение суда"]

    res = threads_format.build(draft, "https://x.ru/1", 500,
                               check_legal=lambda t, u: Verdict())
    assert res.post is None
    assert res.skip_reason == threads_format.SKIP_LEGAL


def test_skip_is_recorded_not_silent(db, monkeypatch):
    add_post("pk", text="Я" * 900 + "\n\nПо данным СК.")
    publish_and_queue("pk")

    cfg = make_cfg()
    client = FakeClient()
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)
    crosspost_due.run()
    row = storage.crosspost_row("pk", PLATFORM)
    assert row["status"] == "skipped"
    assert row["skip_reason"] == threads_format.SKIP_TOO_LONG
    assert client.created == [], "в Meta ничего уходить не должно"


def test_payload_is_frozen_after_first_attempt(db, monkeypatch):
    """Повтор отправляет ровно то, что готовилось изначально."""
    add_post("pz")
    publish_and_queue("pz")
    storage.claim_crosspost(PLATFORM, storage.now_iso())
    storage.save_crosspost_payload("pz", PLATFORM, "Замороженный текст.", "https://x.ru/1")

    with storage.get_conn() as c:
        c.execute("UPDATE posts SET draft_ru='Совсем другой текст.' WHERE post_id='pz'")
        c.execute("UPDATE crossposts SET status='pending' WHERE post_id='pz'")

    cfg = make_cfg()
    client = FakeClient()
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()
    assert client.created[0][0] == "Замороженный текст."


# ---------------------------------------------------------------------------
# Запись токена в .env
# ---------------------------------------------------------------------------


def test_env_update_is_atomic_and_preserves_rest(tmp_path):
    from src import env_file

    env = tmp_path / ".env"
    env.write_text("TELEGRAM_BOT_TOKEN=keep-me\nTHREADS_ACCESS_TOKEN=old\nOTHER=1\n",
                   encoding="utf-8")

    assert env_file.update_secret("THREADS_ACCESS_TOKEN", "new-token", env) is True
    body = env.read_text(encoding="utf-8")
    assert "THREADS_ACCESS_TOKEN=new-token" in body
    assert "TELEGRAM_BOT_TOKEN=keep-me" in body
    assert "OTHER=1" in body
    assert not list(tmp_path.glob(".env.*.tmp")), "временный файл не должен оставаться"


def test_env_update_refuses_broken_value(tmp_path):
    from src import env_file

    env = tmp_path / ".env"
    env.write_text("THREADS_ACCESS_TOKEN=old\n", encoding="utf-8")
    assert env_file.update_secret("THREADS_ACCESS_TOKEN", "bad\nvalue", env) is False
    assert env.read_text(encoding="utf-8") == "THREADS_ACCESS_TOKEN=old\n"


def test_env_update_missing_file_is_loud(tmp_path):
    from src import env_file

    assert env_file.update_secret("X", "y", tmp_path / "nope.env") is False


# ---------------------------------------------------------------------------
# Дыры, найденные независимым ревью
# ---------------------------------------------------------------------------


def test_live_job_is_not_stolen_as_stale(db):
    """Работа, взятая только что, не считается брошенной.

    Ревью нашло здесь путь к двойной публикации: старая по времени создания
    работа, взятая секунду назад, попадала под «протухшие» и уходила второму
    исполнителю. Брошенность считается по времени ЗАХВАТА.
    """
    add_post("live")
    publish_and_queue("live")
    old = (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(timespec="seconds")
    with storage.get_conn() as c:
        c.execute("UPDATE crossposts SET created_at=? WHERE post_id='live'", (old,))

    assert storage.claim_crosspost(PLATFORM, storage.now_iso()) is not None
    threshold = (datetime.now(timezone.utc)
                 - timedelta(minutes=15)).isoformat(timespec="seconds")
    assert storage.recover_stuck_crossposts(PLATFORM, threshold) == 0
    assert storage.crosspost_row("live", PLATFORM)["status"] == "sending"


def test_lost_queue_row_is_picked_up(db, monkeypatch):
    """Процесс умер между Telegram и очередью — воркер подбирает сам."""
    add_post("lost")
    storage.finish_publishing("lost", sent=True)   # очередь не создана
    assert storage.crosspost_row("lost", PLATFORM) is None

    cfg = make_cfg()
    client = FakeClient()
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()
    assert storage.crosspost_row("lost", PLATFORM)["status"] == "done"


def test_reconcile_never_reaches_back_before_enabling(db, monkeypatch):
    """Подбор не превращается в выкладку архива."""
    add_post("ancient", status="published")
    old = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat(timespec="seconds")
    with storage.get_conn() as c:
        c.execute("UPDATE posts SET created_at=? WHERE post_id='ancient'", (old,))
    storage.integration_set(PLATFORM, enabled_at=storage.now_iso())

    cfg = make_cfg()
    client = FakeClient()
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()
    assert storage.crosspost_row("ancient", PLATFORM) is None
    assert client.created == []


def test_unclear_container_state_freezes_instead_of_publishing(db, monkeypatch):
    """Непонятное состояние контейнера не приводит к повторной публикации."""
    add_post("uc")
    publish_and_queue("uc")
    storage.claim_crosspost(PLATFORM, storage.now_iso())
    storage.save_crosspost_container("uc", PLATFORM, "cont-x")
    with storage.get_conn() as c:
        c.execute("UPDATE crossposts SET status='pending' WHERE post_id='uc'")

    cfg = make_cfg()
    client = FakeClient(container_status="ЧТО-ТО НОВОЕ")
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()
    assert client.published == []
    assert storage.crosspost_row("uc", PLATFORM)["status"] == "unknown"


def test_container_still_processing_waits(db, monkeypatch):
    add_post("ip")
    publish_and_queue("ip")
    storage.claim_crosspost(PLATFORM, storage.now_iso())
    storage.save_crosspost_container("ip", PLATFORM, "cont-y")
    with storage.get_conn() as c:
        c.execute("UPDATE crossposts SET status='pending' WHERE post_id='ip'")

    cfg = make_cfg()
    client = FakeClient(container_status="IN_PROGRESS")
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()
    assert client.published == []
    row = storage.crosspost_row("ip", PLATFORM)
    assert row["status"] == "failed" and row["next_retry_at"]


def test_degraded_integration_self_heals(db, monkeypatch):
    """После переавторизации зеркало оживает само, без ручной команды."""
    add_post("heal")
    publish_and_queue("heal")
    storage.integration_set(PLATFORM, degraded=1, last_error="протух")

    cfg = make_cfg()
    client = FakeClient(profile_exc=False)
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)

    crosspost_due.run()
    assert storage.integration_get(PLATFORM)["degraded"] == 0
    assert storage.crosspost_row("heal", PLATFORM)["status"] == "done"


def test_auth_failure_does_not_retry_immediately(db, monkeypatch):
    """Отказ доступа откладывается, а не повторяется каждые пять минут."""
    add_post("au")
    publish_and_queue("au")

    cfg = make_cfg()
    client = FakeClient(
        create_exc=threads_api.ThreadsError("HTTP 401", auth=True))
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: client)
    from src import notify, roles
    monkeypatch.setattr(roles, "list_admins", lambda *a, **k: [])
    monkeypatch.setattr(notify, "_send_message", lambda *a, **k: None)

    crosspost_due.run()
    row = storage.crosspost_row("au", PLATFORM)
    assert row["status"] == "failed"
    assert row["next_retry_at"], "повтор должен быть отложен, а не немедленен"


def test_container_id_never_leaks_token_from_body(db, monkeypatch):
    """Эхо токена в теле ответа не попадает в базу."""
    cfg = make_cfg()
    token = cfg.threads_access_token

    class Echo(FakeClient):
        def create_text_container(self, text, link_url=""):
            raise threads_api.ThreadsError(
                threads_api._safe(f"Meta не вернула id: {{'access_token': '{token}'}}",
                                  token))

    add_post("echo")
    publish_and_queue("echo")
    monkeypatch.setattr(crosspost_due, "get_config", lambda: cfg)
    monkeypatch.setattr(crosspost_due.threads_api, "ThreadsClient", lambda *a, **k: Echo())

    crosspost_due.run()
    assert token not in (storage.crosspost_row("echo", PLATFORM)["last_error"] or "")
