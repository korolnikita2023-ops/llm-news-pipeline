"""Зеркалирование опубликованных постов в Threads. Конечный прогон.

Telegram — первичный канал, Threads — независимый downstream. Сюда работа
попадает уже после того, как пост ушёл в канал: этот процесс не умеет
публиковать в Telegram, не умеет менять статусы постов и не влияет на
конвейер. Упасть он может как угодно — на Telegram это не отразится.

Порядок на одну работу:
  1. Захватить (атомарно, один исполнитель).
  2. Собрать текст, если он ещё не собран, и СРАЗУ записать его. Повтор
     обязан отправить ровно то, что готовилось.
  3. Создать контейнер и СРАЗУ записать его id.
  4. Опубликовать контейнер, записать id поста.

Ключевая тонкость — шаг 4 после обрыва. Если процесс умер сразу после
POST /threads_publish, пост мог уже появиться в ленте, а мы об этом не
знаем. Слепой повтор здесь способен выложить второй пост. Поэтому при
возобновлении работы с уже созданным контейнером мы сперва спрашиваем у
Meta его статус: PUBLISHED означает «готово, ничего не делаем».
"""
from __future__ import annotations

import random
import sys
from datetime import datetime, timedelta, timezone
from typing import Optional

from config import get_config
from src import storage, threads_api, threads_format

PLATFORM = "threads"

# Работа, застрявшая в 'sending' дольше этого, считается брошенной убитым
# процессом. Порог заведомо больше и таймаута HTTP, и TimeoutStartSec службы.
STUCK_AFTER_MINUTES = 15

# Сколько работ берём за прогон. Больше трёх постов в сутки система не
# делает, так что запас на разбор накопившейся очереди достаточный.
BATCH = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _backoff(attempts: int, retry_after: Optional[int]) -> str:
    """Когда повторить. Уважает Retry-After, иначе растущая пауза с разбросом.

    Разброс нужен, чтобы несколько работ, упавших разом на одном отказе
    Meta, не пошли на повтор одной секундой и не получили 429 повторно.
    """
    if retry_after and retry_after > 0:
        delay = min(retry_after, 3600)
    else:
        delay = min(60 * (2 ** max(attempts - 1, 0)), 3600)
        delay = int(delay * (0.8 + random.random() * 0.4))
    return (_now() + timedelta(seconds=delay)).isoformat(timespec="seconds")


def _notify_degraded(cfg, message: str) -> None:
    """Одно понятное сообщение владельцу. Не чаще раза в сутки.

    Требование владельца: узнать о том, что Threads отвалился, но не
    получать это каждые пять минут.
    """
    state = storage.integration_get(PLATFORM)
    last = state.get("last_notified_at")
    if last:
        try:
            if _now() - datetime.fromisoformat(last) < timedelta(hours=24):
                return
        except ValueError:
            pass

    from src import notify, roles

    sent = 0
    for admin in roles.list_admins():
        chat_id = admin.get("user_id")
        if not chat_id:
            continue
        try:
            notify._send_message(cfg, chat_id, message)
            sent += 1
        except Exception as exc:
            print(f"[crosspost] уведомление не доставлено: {exc}", file=sys.stderr)
    if sent:
        storage.integration_set(PLATFORM, last_notified_at=storage.now_iso())


def _mark_degraded(cfg, reason: str) -> None:
    storage.integration_set(PLATFORM, degraded=1, last_error=reason[:500])
    _notify_degraded(
        cfg,
        "⚠️ Threads требует повторной авторизации.\n\n"
        "Публикация в Telegram работает как обычно — затронуто только "
        "зеркало в Threads.\n\n"
        f"Причина: {reason[:200]}",
    )


def _refresh_token_if_needed(cfg) -> None:
    """Продлевает токен заранее. Просроченный не восстанавливается ничем.

    Токен живёт 60 дней и продлевается только пока жив. Проспать этот срок
    значит потерять доступ насовсем и идти к Meta руками, поэтому
    обновляемся за две недели до конца.
    """
    state = storage.integration_get(PLATFORM)
    expires_at = state.get("token_expires_at")
    if not expires_at:
        return
    try:
        left = datetime.fromisoformat(expires_at) - _now()
    except ValueError:
        return
    if left > timedelta(days=max(cfg.threads_refresh_before_days, 1)):
        return

    if left <= timedelta(0):
        _mark_degraded(cfg, "срок действия токена истёк, нужна новая авторизация")
        return

    try:
        client = threads_api.ThreadsClient(cfg.threads_user_id, cfg.threads_access_token)
        token, new_expiry = client.refresh_token()
    except threads_api.ThreadsError as exc:
        print(f"[crosspost] токен не продлился: {exc}", file=sys.stderr)
        if exc.auth:
            _mark_degraded(cfg, f"продление токена отклонено: {exc}")
        return

    from src import env_file

    if env_file.update_secret("THREADS_ACCESS_TOKEN", token):
        storage.integration_set(PLATFORM, token_expires_at=new_expiry.isoformat(),
                                token_refreshed_at=storage.now_iso(), degraded=0,
                                last_error=None)
        cfg.threads_access_token = token
        print(f"[crosspost] токен Threads продлён до {new_expiry:%d.%m.%Y}")
    else:
        print("[crosspost] новый токен не удалось записать в .env", file=sys.stderr)


def _healthy(cfg, client) -> bool:
    """Можно ли сейчас обращаться к Meta.

    Сломанная интеграция не должна ни долбить API каждые пять минут, ни
    застревать навсегда после того, как владелец переавторизовался. Поэтому
    раз в час — один безобидный запрос профиля, ничего не публикующий.
    """
    state = storage.integration_get(PLATFORM)
    if not state.get("degraded"):
        return True

    last = state.get("last_probe_at")
    if last:
        try:
            if _now() - datetime.fromisoformat(last) < timedelta(hours=1):
                print("[crosspost] нужна новая авторизация Threads; очередь ждёт, "
                      "Telegram не затронут")
                return False
        except ValueError:
            pass

    storage.integration_set(PLATFORM, last_probe_at=storage.now_iso())
    try:
        client.profile()
    except threads_api.ThreadsError as exc:
        print(f"[crosspost] доступ к Threads всё ещё не восстановлен: {exc}")
        return False

    storage.integration_set(PLATFORM, degraded=0, last_error=None)
    print("[crosspost] доступ к Threads восстановлен, продолжаем")
    return True


def _payload(cfg, job: dict) -> Optional[threads_format.ThreadsPost]:
    """Текст поста. Собранный ранее — как есть, иначе собираем и сохраняем."""
    if job.get("payload_text"):
        return threads_format.ThreadsPost(text=job["payload_text"],
                                          link_url=job.get("link_url") or "")

    from src import legal_gate
    from src.approval_bot import _case_status_of, _source_url_of

    with storage.get_conn() as conn:
        row = conn.execute("SELECT draft_ru FROM posts WHERE post_id=?",
                           (job["post_id"],)).fetchone()
    if row is None or not (row[0] or "").strip():
        storage.skip_crosspost(job["post_id"], PLATFORM, threads_format.SKIP_EMPTY,
                               "исходный пост не найден или пуст")
        return None

    source_url = _source_url_of(job["post_id"])
    case_status = _case_status_of(job["post_id"])

    def check(text: str, url: str):
        return legal_gate.check_post(text, url, strict_style=False, case_status=case_status)

    result = threads_format.build(row[0], source_url, cfg.threads_text_limit, check)
    if result.post is None:
        storage.skip_crosspost(job["post_id"], PLATFORM, result.skip_reason, result.detail)
        print(f"[crosspost] {job['post_id']} не публикуется в Threads: "
              f"{result.skip_reason} — {result.detail}")
        return None

    storage.save_crosspost_payload(job["post_id"], PLATFORM,
                                   result.post.text, result.post.link_url)
    return result.post


def _send_one(cfg, client, job: dict) -> None:
    post_id = job["post_id"]
    container_id = job.get("container_id") or ""

    # Возобновление после обрыва: контейнер уже создан, а чем кончилась
    # публикация — неизвестно. Спрашиваем Meta, вместо того чтобы гадать.
    if container_id:
        try:
            state = client.container_state(container_id)
        except threads_api.ThreadsError as exc:
            _handle_error(cfg, job, exc)
            return

        if state.status == "PUBLISHED":
            storage.finish_crosspost(post_id, PLATFORM, container_id)
            print(f"[crosspost] {post_id}: контейнер уже опубликован, повтор не нужен")
            return
        if state.status in ("EXPIRED", "ERROR"):
            # Контейнер мёртв — начинаем с чистого листа. Второго поста это
            # не создаст: мёртвый контейнер опубликован не был.
            storage.save_crosspost_container(post_id, PLATFORM, "")
            container_id = ""
        elif state.status == "IN_PROGRESS":
            # Meta ещё обрабатывает. Публиковать рано, ждём следующего тика.
            storage.fail_crosspost(post_id, PLATFORM, "контейнер ещё обрабатывается",
                                   _backoff(1, 60))
            return
        elif state.status != "FINISHED":
            # Состояние не разобралось. Публиковать вслепую нельзя: пост мог
            # уже уйти в ленту, и повтор выложил бы второй. Замораживаем.
            storage.unknown_crosspost(
                post_id, PLATFORM,
                f"непонятное состояние контейнера: {state.status or 'пусто'}")
            print(f"[crosspost] {post_id}: состояние контейнера не разобралось "
                  f"({state.status or 'пусто'}) — нужен разбор вручную", file=sys.stderr)
            return
        elif job.get("publish_attempted_at"):
            # FINISHED после нашей попытки публикации означает, что попытка
            # не прошла: у Meta FINISHED и PUBLISHED — разные состояния.
            # Повтор безопасен.
            print(f"[crosspost] {post_id}: прошлая публикация не состоялась, повтор")

    post = _payload(cfg, job)
    if post is None:
        return

    if not container_id:
        try:
            container_id = client.create_text_container(post.text, post.link_url)
        except threads_api.ThreadsError as exc:
            _handle_error(cfg, job, exc)
            return
        storage.save_crosspost_container(post_id, PLATFORM, container_id)

    # Отмечаем ДО вызова: после обрыва надо отличить «не пробовали» от
    # «пробовали, исход неизвестен».
    storage.mark_publish_attempted(post_id, PLATFORM)
    try:
        remote_id = client.publish_container(container_id)
    except threads_api.ThreadsError as exc:
        _handle_error(cfg, job, exc)
        return

    storage.finish_crosspost(post_id, PLATFORM, remote_id)
    storage.integration_set(PLATFORM, degraded=0, last_error=None)
    print(f"[crosspost] {post_id} → Threads {remote_id}")


def _handle_error(cfg, job: dict, exc: threads_api.ThreadsError) -> None:
    post_id = job["post_id"]
    attempts = int(job.get("attempts") or 1)

    if exc.auth:
        # Паузой это не лечится, нужна новая авторизация. Работу не хороним:
        # после переавторизации она уедет сама. Но и повторять её сразу
        # нельзя — иначе снятый признак degraded открыл бы бесконечный
        # цикл обращений к Meta.
        storage.fail_crosspost(post_id, PLATFORM, str(exc),
                               _backoff(4, None), terminal=False)
        _mark_degraded(cfg, str(exc))
        print(f"[crosspost] {post_id}: доступ отозван или истёк — {exc}", file=sys.stderr)
        return

    if not exc.temporary:
        storage.fail_crosspost(post_id, PLATFORM, str(exc), None, terminal=True)
        print(f"[crosspost] {post_id}: неисправимый отказ — {exc}", file=sys.stderr)
        return

    if attempts >= max(cfg.threads_max_attempts, 1):
        storage.fail_crosspost(post_id, PLATFORM, str(exc), None, terminal=True)
        print(f"[crosspost] {post_id}: попытки исчерпаны ({attempts}) — {exc}",
              file=sys.stderr)
        return

    when = _backoff(attempts, exc.retry_after)
    storage.fail_crosspost(post_id, PLATFORM, str(exc), when)
    print(f"[crosspost] {post_id}: попытка {attempts} не удалась ({exc}), "
          f"повтор после {when}")


def run() -> int:
    cfg = get_config()
    if not cfg.threads_enabled:
        return 0
    storage.init_db()

    if not cfg.threads_user_id or not cfg.threads_access_token:
        print("[crosspost] СТОП: THREADS_ENABLED=true, но THREADS_USER_ID/"
              "THREADS_ACCESS_TOKEN не заданы", file=sys.stderr)
        return 1

    # Момент включения ставится один раз и служит границей раскатки.
    if not storage.integration_get(PLATFORM).get("enabled_at"):
        storage.integration_set(PLATFORM, enabled_at=storage.now_iso())
        print("[crosspost] зеркало в Threads включено; посты, опубликованные "
              "раньше этого момента, не зеркалируются")

    stuck = storage.recover_stuck_crossposts(
        PLATFORM,
        (_now() - timedelta(minutes=STUCK_AFTER_MINUTES)).isoformat(timespec="seconds"),
    )
    if stuck:
        print(f"[crosspost] возвращено в очередь после обрыва: {stuck}")

    try:
        client = threads_api.ThreadsClient(cfg.threads_user_id, cfg.threads_access_token)
    except threads_api.ThreadsError as exc:
        print(f"[crosspost] СТОП: {exc}", file=sys.stderr)
        return 1

    # Сломанная интеграция не долбит Meta каждые пять минут, но и не
    # застревает навсегда: раз в час пробуем безобидный запрос профиля. Если
    # владелец переавторизовался, признак снимется сам, без ручной команды.
    if not _healthy(cfg, client):
        return 0

    _refresh_token_if_needed(cfg)

    # Пост мог уйти в Telegram, а процесс умереть до постановки в очередь.
    # Подбираем такие — но только опубликованные ПОСЛЕ включения зеркала.
    picked = storage.reconcile_crossposts(
        PLATFORM, storage.integration_get(PLATFORM).get("enabled_at") or storage.now_iso(),
        cfg.telegram_channel_id)
    if picked:
        print(f"[crosspost] подобрано постов без строки очереди: {picked}")

    handled = 0
    for _ in range(BATCH):
        job = storage.claim_crosspost(PLATFORM, storage.now_iso())
        if job is None:
            break
        handled += 1
        try:
            _send_one(cfg, client, job)
        except Exception as exc:
            # Непредвиденное. Работа не должна остаться в 'sending' навсегда,
            # но и слепо повторять её нельзя: исход отправки неизвестен.
            from src.threads_api import _safe

            detail = _safe(f"{type(exc).__name__}: {exc}", cfg.threads_access_token)
            storage.unknown_crosspost(job["post_id"], PLATFORM, detail)
            print(f"[crosspost] {job['post_id']}: исход неизвестен — {detail}",
                  file=sys.stderr)

    if not handled:
        print(f"[crosspost] {storage.now_iso()}: к отправке ничего не готово")
    return 0


if __name__ == "__main__":
    raise SystemExit(run())
