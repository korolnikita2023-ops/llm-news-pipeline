"""Ежедневный резервный снимок боевой базы. Конечный прогон.

Зачем отдельный модуль, а не `cp`. База живёт в режиме WAL, и её файл почти
всегда неполон: часть свежих записей лежит в `-wal` и ещё не перенесена.
Скопированный на ходу файл выглядит целым и открывается — но теряет
последние публикации, и обнаруживается это только при восстановлении, когда
уже поздно. Поэтому снимок делается штатным SQLite backup API: он
согласован по определению и работает, пока бот пишет.

Что здесь охраняется. В базе лежит история публикаций, а на ней держится
защита от повторов: потеряв её, канал начнёт предлагать уже отработанные
истории, а зеркало в Threads — задваивать посты. Файлы восстановимы,
репутация ленты — нет.

Секреты сюда не попадают: `.env` в ежедневную ротацию не входит намеренно.
Он меняется раз в месяцы, снимается вручную перед правкой, и плодить его
копии по каталогу — значит размножать секрет без нужды.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from config import DB_PATH

BACKUP_ROOT = Path("/var/backups/news_bot")
DAILY_DIRNAME = "daily"
WEEKLY_DIRNAME = "weekly"

# Две недели ежедневных снимков и месяц недельных. Порча базы замечается не
# в тот же день: если история публикаций тихо поехала, это видно по
# повторяющимся темам через неделю-полторы.
KEEP_DAILY = 14
KEEP_WEEKLY = 4

PREFIX = "news_bot_"
SUFFIX = ".db"


class BackupError(Exception):
    """Снимок не сделан или не прошёл проверку. Тишины тут быть не должно."""


def _snapshot(src: Path, dst: Path) -> None:
    """Согласованная копия базы штатным SQLite backup API."""
    if not src.exists():
        raise BackupError(f"база не найдена: {src}")
    source = sqlite3.connect(f"file:{src}?mode=ro", uri=True)
    try:
        target = sqlite3.connect(dst)
        try:
            with target:
                source.backup(target)
            # Снимок наследует от боевой базы режим WAL, и рядом с ним
            # остаются -wal и -shm. Для копии, которую только читают, они
            # бесполезны, а ротация их не подбирает — файлы копились бы
            # вечно. Перевод в обычный журнал сливает всё в один файл и
            # убирает спутников штатно, а не удалением руками.
            target.execute("PRAGMA journal_mode=DELETE")
        finally:
            target.close()
    finally:
        source.close()


def _verify(path: Path) -> int:
    """Проверяет снимок и возвращает число постов в нём.

    Одной `integrity_check` мало: она подтверждает целостность страниц, но
    промолчит о пустой базе с правильной структурой. Поэтому дополнительно
    читаем таблицу постов — снимок без единого поста означает, что копировали
    не то.
    """
    conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
    try:
        verdict = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if verdict != "ok":
            raise BackupError(f"проверка целостности не пройдена: {verdict}")
        try:
            posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        except sqlite3.Error as exc:
            raise BackupError(f"в снимке нет таблицы постов: {exc}") from None
    finally:
        conn.close()
    return posts


def _prune(directory: Path, keep: int) -> List[Path]:
    """Оставляет `keep` самых свежих снимков. Возвращает удалённые."""
    snaps = sorted(directory.glob(f"{PREFIX}*{SUFFIX}"))
    doomed = snaps[:-keep] if keep > 0 and len(snaps) > keep else []
    removed = []
    for path in doomed:
        try:
            path.unlink()
            # Спутники WAL, если вдруг остались от прежних версий.
            for extra in (path.with_name(path.name + "-wal"),
                          path.with_name(path.name + "-shm")):
                extra.unlink(missing_ok=True)
            removed.append(path)
        except OSError as exc:
            print(f"[backup] не удалось убрать старый снимок {path.name}: {exc}",
                  file=sys.stderr)
    return removed


def run(db_path: Optional[Path] = None, root: Optional[Path] = None,
        now: Optional[datetime] = None) -> Path:
    """Делает, проверяет и подчищает. Возвращает путь к снимку."""
    src = Path(db_path) if db_path else Path(DB_PATH)
    base = Path(root) if root else BACKUP_ROOT
    moment = now or datetime.now(timezone.utc)

    daily = base / DAILY_DIRNAME
    weekly = base / WEEKLY_DIRNAME
    for d in (daily, weekly):
        d.mkdir(parents=True, exist_ok=True)
        try:
            d.chmod(0o700)   # снимки читает только пользователь службы
        except OSError:
            pass

    name = f"{PREFIX}{moment:%Y%m%d_%H%M%S}{SUFFIX}"
    dst = daily / name
    _snapshot(src, dst)
    try:
        dst.chmod(0o600)
    except OSError:
        pass

    posts = _verify(dst)
    size_mb = dst.stat().st_size / 1024 / 1024
    print(f"[backup] снимок {dst.name}: {posts} постов, {size_mb:.2f} МБ, "
          f"проверка целостности пройдена")

    # Недельный срез: один на календарную неделю. Двухнедельной глубины
    # ежедневных мало, если порчу заметили поздно.
    year, week, _ = moment.isocalendar()
    tag = f"{PREFIX}w{year}-{week:02d}{SUFFIX}"
    weekly_path = weekly / tag
    if not weekly_path.exists():
        shutil.copy2(dst, weekly_path)
        try:
            weekly_path.chmod(0o600)
        except OSError:
            pass
        print(f"[backup] недельный срез: {weekly_path.name}")

    for gone in _prune(daily, KEEP_DAILY):
        print(f"[backup] убран старый снимок: {gone.name}")
    # Недельные лежат под своим именем, поэтому чистим их отдельным правилом.
    weeklies = sorted(weekly.glob(f"{PREFIX}w*{SUFFIX}"))
    for gone in weeklies[:-KEEP_WEEKLY] if len(weeklies) > KEEP_WEEKLY else []:
        try:
            gone.unlink()
            print(f"[backup] убран старый недельный срез: {gone.name}")
        except OSError as exc:
            print(f"[backup] не удалось убрать {gone.name}: {exc}", file=sys.stderr)

    return dst


def main() -> int:
    try:
        run()
    except (BackupError, sqlite3.Error, OSError) as exc:
        # Громко и с ненулевым кодом: systemd пометит прогон провалившимся,
        # и это видно в `systemctl list-timers` и в журнале. Тихо пропущенный
        # бэкап хуже отсутствующего — на него рассчитывают.
        print(f"[backup] СНИМОК НЕ СДЕЛАН: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
