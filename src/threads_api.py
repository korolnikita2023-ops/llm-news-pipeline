"""Клиент Threads API. Только то, что нужно зеркалированию.

Публикация в Threads двухшаговая, как у Instagram: сначала создаётся
контейнер с текстом, потом контейнер публикуется. Между шагами пост ещё не
виден никому.

Проверено по документации Meta 29.08.2026:
  https://developers.facebook.com/docs/threads/posts
  https://developers.facebook.com/docs/threads/troubleshooting
  https://developers.facebook.com/docs/threads/get-started/long-lived-tokens

  POST /{user-id}/threads          media_type=TEXT, text, link_attachment
  POST /{user-id}/threads_publish  creation_id
  GET  /{container-id}             fields=status,error_message
  GET  /refresh_access_token       grant_type=th_refresh_token

ТОКЕН ЕЗДИТ В URL. Это значит, что любое исключение httpx, любой её repr и
любой лог запроса содержат секрет целиком. На этом проекте уже утекал токен
Telegram-бота в системный журнал ровно так. Поэтому наружу из этого модуля
не выходит ни одного текста, не прошедшего через `_safe()`, а сам токен
никогда не кладётся в объект исключения.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

try:
    import httpx
except ImportError:  # pragma: no cover - httpx есть в проде
    httpx = None  # type: ignore

BASE = "https://graph.threads.net/v1.0"
AUTH_BASE = "https://graph.threads.net"

# Лимит поста в Threads: 500 знаков (документация Meta, 29.08.2026).
TEXT_LIMIT = 500

# Коды Meta, означающие «токен больше не годится». Повторять с ними
# бессмысленно: пауза ничего не лечит, нужна новая авторизация.
AUTH_ERROR_CODES = {102, 190, 463, 467}
AUTH_ERROR_TYPES = {"OAuthException"}


class ThreadsError(Exception):
    """Отказ Threads API. Текст уже очищен от секретов.

    `temporary` отделяет то, что лечится паузой (429, 5xx, обрыв связи), от
    того, что паузой не лечится. `auth` выделяет отдельно протухший или
    отозванный доступ: он требует человека, а не повторов.
    """

    def __init__(self, message: str, *, temporary: bool = False, auth: bool = False,
                 retry_after: Optional[int] = None):
        super().__init__(message)
        self.temporary = temporary
        self.auth = auth
        self.retry_after = retry_after


@dataclass
class ContainerState:
    status: str
    error_message: str = ""


def _safe(text: str, *secrets: str) -> str:
    """Вычищает секреты из текста, включая URL-кодированный вид.

    Токен приезжает в сообщение об ошибке двумя путями: как параметр URL в
    исключении httpx и как эхо в теле ответа Meta. Режем оба, и режем
    длинные «токеноподобные» хвосты на случай, если Meta вернёт не тот
    токен, что мы послали.
    """
    out = text or ""
    for secret in secrets:
        secret = (secret or "").strip()
        if len(secret) < 8:
            continue
        out = out.replace(secret, "***")
        # httpx показывает URL уже закодированным
        from urllib.parse import quote

        out = out.replace(quote(secret, safe=""), "***")
    # Последний рубеж: любой access_token=… в тексте, откуда бы он ни взялся.
    out = re.sub(r"(access_token=)[^&\s\"']+", r"\1***", out)
    out = re.sub(r"(client_secret=)[^&\s\"']+", r"\1***", out)
    return out[:1000]


def _classify(resp: "httpx.Response", token: str) -> ThreadsError:
    """Превращает ответ Meta в понятный отказ, не пронося наружу секрет."""
    retry_after = None
    raw = resp.headers.get("retry-after")
    if raw:
        try:
            retry_after = int(float(raw))
        except ValueError:
            retry_after = None

    body: Dict[str, Any] = {}
    try:
        body = resp.json()
    except (ValueError, json.JSONDecodeError):
        pass
    err = body.get("error") if isinstance(body, dict) else None
    err = err if isinstance(err, dict) else {}
    code = err.get("code")
    etype = str(err.get("type") or "")
    msg = str(err.get("message") or resp.text or "")[:400]

    detail = _safe(f"HTTP {resp.status_code}: {msg}", token)

    if code in AUTH_ERROR_CODES or etype in AUTH_ERROR_TYPES:
        return ThreadsError(detail, auth=True)
    if resp.status_code == 429 or code in (4, 17, 32, 613):
        return ThreadsError(detail, temporary=True, retry_after=retry_after)
    if resp.status_code >= 500:
        return ThreadsError(detail, temporary=True, retry_after=retry_after)
    return ThreadsError(detail)


class ThreadsClient:
    """Минимальный клиент. Один экземпляр — один прогон воркера."""

    def __init__(self, user_id: str, token: str, timeout: float = 30.0):
        if httpx is None:
            raise ThreadsError("httpx не установлен — публикация в Threads невозможна")
        if not user_id or not token:
            raise ThreadsError("не заданы THREADS_USER_ID / THREADS_ACCESS_TOKEN")
        self.user_id = user_id
        self._token = token
        self._timeout = timeout

    # -- транспорт ---------------------------------------------------------

    def _request(self, method: str, url: str, params: Dict[str, Any]) -> Dict[str, Any]:
        params = dict(params)
        params["access_token"] = self._token
        try:
            resp = httpx.request(method, url, params=params, timeout=self._timeout)
        except Exception as exc:
            # В тексте исключения httpx лежит полный URL с токеном. Наружу
            # уходит только имя класса и очищенный текст.
            raise ThreadsError(
                _safe(f"{type(exc).__name__}: {exc}", self._token), temporary=True
            ) from None
        if resp.status_code >= 400:
            raise _classify(resp, self._token)
        try:
            data = resp.json()
        except (ValueError, json.JSONDecodeError):
            raise ThreadsError(
                _safe(f"ответ Meta не разобрался: {resp.text[:200]}", self._token)
            ) from None
        if not isinstance(data, dict):
            raise ThreadsError("ответ Meta не является объектом")
        return data

    def sanitize(self, text: str) -> str:
        """Очистка чужого текста тем же секретом. Для вызывающего кода."""
        return _safe(text, self._token)

    # -- операции ----------------------------------------------------------

    def profile(self) -> Dict[str, Any]:
        """Проверка доступа без публикации чего бы то ни было."""
        return self._request("GET", f"{BASE}/me",
                             {"fields": "id,username,threads_profile_picture_url"})

    def create_text_container(self, text: str, link_url: str = "") -> str:
        params: Dict[str, Any] = {"media_type": "TEXT", "text": text}
        if link_url:
            # Ссылка на источник отдельным вложением, а не строкой в тексте:
            # так она не съедает лимит в 500 знаков и показывается карточкой.
            params["link_attachment"] = link_url
        data = self._request("POST", f"{BASE}/{self.user_id}/threads", params)
        container_id = str(data.get("id") or "")
        if not container_id:
            # Тело ответа может содержать эхо токена. Наружу — только очищенное.
            raise ThreadsError(
                _safe(f"Meta не вернула id контейнера: {str(data)[:200]}", self._token))
        return container_id

    def container_state(self, container_id: str) -> ContainerState:
        """Состояние контейнера.

        Ради этого вызова всё и затевалось: он единственный отвечает на
        вопрос «пост уже опубликован или ещё нет» после обрыва. Без него
        повтор публикации — это ставка на недокументированное поведение
        Meta, то есть риск второго поста в ленте.
        """
        data = self._request("GET", f"{BASE}/{container_id}",
                             {"fields": "status,error_message"})
        return ContainerState(
            status=str(data.get("status") or "").upper(),
            error_message=_safe(str(data.get("error_message") or ""), self._token))

    def publish_container(self, container_id: str) -> str:
        data = self._request("POST", f"{BASE}/{self.user_id}/threads_publish",
                             {"creation_id": container_id})
        post_id = str(data.get("id") or "")
        if not post_id:
            raise ThreadsError(
                _safe(f"Meta не вернула id поста: {str(data)[:200]}", self._token))
        return post_id

    def delete_post(self, post_id: str) -> bool:
        """Удаление своего поста. Нужно только для проверочной публикации."""
        data = self._request("DELETE", f"{BASE}/{post_id}", {})
        return bool(data.get("success", True))

    def refresh_token(self) -> Tuple[str, datetime]:
        """Продлевает долгоживущий токен ещё на 60 дней.

        Продлевать можно только живой токен старше суток. Просроченный не
        восстанавливается ничем — только новая авторизация руками, поэтому
        обновляемся сильно заранее.
        """
        data = self._request("GET", f"{AUTH_BASE}/refresh_access_token",
                             {"grant_type": "th_refresh_token"})
        token = str(data.get("access_token") or "")
        if not token:
            raise ThreadsError("Meta не вернула обновлённый токен")
        try:
            expires_in = int(data.get("expires_in") or 0)
        except (TypeError, ValueError):
            expires_in = 0
        if expires_in <= 0:
            expires_in = 60 * 24 * 3600
        return token, datetime.now(timezone.utc) + timedelta(seconds=expires_in)
