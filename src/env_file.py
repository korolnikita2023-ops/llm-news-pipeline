"""Безопасная перезапись одного секрета в `.env`.

Нужно ровно для одного случая: Threads продлевает токен раз в 45 дней, и
новое значение обязано пережить перезапуск. Правило проекта — секреты живут
только в `.env`, поэтому писать приходится туда.

Как это делается, чтобы не потерять файл:
  * читаем, меняем одну строку, пишем во временный файл РЯДОМ (тот же
    каталог — иначе os.replace не атомарен между разделами);
  * права 600 ставим ДО записи содержимого, а не после: иначе секрет
    какое-то время лежит с правами по умолчанию;
  * fsync перед заменой — без него после внезапного выключения файл может
    оказаться пустым;
  * os.replace — атомарная подмена, читатель видит либо старый файл целиком,
    либо новый;
  * блокировка на весь цикл: два писателя иначе затрут правки друг друга.

Долгоживущий бот перечитает `.env` только при перезапуске. Это не проблема:
токен Threads нужен лишь разовому воркеру, который читает файл при каждом
запуске.
"""
from __future__ import annotations

import os
import re
import sys
import tempfile
import time
from pathlib import Path
from typing import Optional

BASE_DIR = Path(__file__).resolve().parent.parent
ENV_PATH = BASE_DIR / ".env"
LOCK_SUFFIX = ".lock"
LOCK_TIMEOUT_S = 10


class _Lock:
    """Простая файловая блокировка. Работает и на Windows, и на Linux."""

    def __init__(self, target: Path):
        self.path = target.with_suffix(target.suffix + LOCK_SUFFIX)
        self.fd: Optional[int] = None

    def __enter__(self):
        deadline = time.monotonic() + LOCK_TIMEOUT_S
        while True:
            try:
                self.fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                return self
            except FileExistsError:
                if time.monotonic() > deadline:
                    # Замок НЕ отбираем ни при каких условиях. Соблазн снять
                    # «протухший» по времени файла велик, но writer может
                    # просто висеть на медленном диске: отобрав у него замок,
                    # мы получим двух писателей и потерянную правку .env, а
                    # испорченный .env кладёт бота при следующем перезапуске.
                    # Громко не продлить токен безопаснее, чем тихо испортить
                    # файл: до конца жизни токена ещё две недели и сотни
                    # попыток.
                    raise TimeoutError(
                        f"не дождался блокировки {self.path.name} — возможно, "
                        f"остался файл от убитого процесса, снимите его вручную")
                time.sleep(0.1)

    def __exit__(self, *exc):
        # Снимаем ТОЛЬКО свой замок: unlink без проверки владения удалил бы
        # чужой и впустил второго писателя.
        if self.fd is not None:
            os.close(self.fd)
            self.fd = None
            self.path.unlink(missing_ok=True)
        return False


def update_secret(key: str, value: str, path: Path | None = None) -> bool:
    """Заменяет значение `key` в `.env`. True — записано.

    Значение не логируется ни при каких обстоятельствах: это секрет.
    """
    target = Path(path) if path else ENV_PATH
    if not key or "\n" in value or "\r" in value:
        print(f"[env] отказ: недопустимое значение для {key}", file=sys.stderr)
        return False
    if not target.exists():
        print(f"[env] отказ: {target} не найден", file=sys.stderr)
        return False

    try:
        with _Lock(target):
            original = target.read_text(encoding="utf-8")
            lines = original.splitlines()
            pattern = re.compile(rf"^{re.escape(key)}=")
            replaced = False
            for i, line in enumerate(lines):
                if pattern.match(line):
                    lines[i] = f"{key}={value}"
                    replaced = True
                    break
            if not replaced:
                lines.append(f"{key}={value}")

            body = "\n".join(lines) + "\n"
            # Проверяем ДО подмены: испорченный .env положит бота при
            # следующем перезапуске, а это уже касается Telegram.
            if f"{key}={value}" not in body or len(body) < len(original) // 2:
                print(f"[env] отказ: проверка перед записью не прошла для {key}",
                      file=sys.stderr)
                return False

            fd, tmp_name = tempfile.mkstemp(dir=str(target.parent), prefix=".env.",
                                            suffix=".tmp")
            tmp = Path(tmp_name)
            try:
                # mkstemp уже создал файл с правами 600 — секрет ни секунды
                # не лежит доступным другим пользователям.
                with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(body)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, target)
                # Без синхронизации КАТАЛОГА переименование может не пережить
                # внезапное отключение питания: файл окажется старым или
                # исчезнет вовсе. На Windows каталог так не открыть — там
                # os.replace и без того выполняется файловой системой.
                try:
                    dir_fd = os.open(str(target.parent), os.O_RDONLY)
                    try:
                        os.fsync(dir_fd)
                    finally:
                        os.close(dir_fd)
                except (OSError, AttributeError):
                    pass
            except BaseException:
                tmp.unlink(missing_ok=True)
                raise
            try:
                os.chmod(target, 0o600)
            except OSError:
                pass
        return True
    except Exception as exc:
        print(f"[env] не удалось обновить {key}: {type(exc).__name__}", file=sys.stderr)
        return False
