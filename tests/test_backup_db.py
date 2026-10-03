"""Резервные снимки базы.

Проверяется главное: снимок согласован (а не скопированный на ходу файл с
недописанным WAL), негодный снимок ловится сразу, ротация не съедает лишнего,
и из снимка действительно можно восстановиться.
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from src import backup_db


def make_db(path, posts: int = 3) -> None:
    conn = sqlite3.connect(path)
    with conn:
        conn.execute("CREATE TABLE posts (post_id TEXT PRIMARY KEY, draft_ru TEXT)")
        for i in range(posts):
            conn.execute("INSERT INTO posts VALUES (?,?)", (f"p{i}", f"Текст {i}"))
    conn.close()


def test_snapshot_is_verified_and_counted(tmp_path, capsys):
    src = tmp_path / "prod.db"
    make_db(src, posts=7)

    dst = backup_db.run(src, tmp_path / "backups")

    assert dst.exists()
    assert backup_db._verify(dst) == 7
    assert "проверка целостности пройдена" in capsys.readouterr().out


def test_snapshot_catches_open_write_transaction(tmp_path):
    """Снимок делается на живой базе, пока в неё пишут.

    Ради этого и нужен backup API вместо cp: копия обязана быть
    согласованной, а не поймать базу на середине записи.
    """
    src = tmp_path / "prod.db"
    make_db(src, posts=2)

    writer = sqlite3.connect(src)
    writer.execute("BEGIN")
    writer.execute("INSERT INTO posts VALUES ('незавершённый','ещё не подтверждён')")
    try:
        dst = backup_db.run(src, tmp_path / "backups")
        # Незакоммиченная запись в снимок попасть не должна.
        conn = sqlite3.connect(dst)
        assert conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0] == 2
        conn.close()
    finally:
        writer.rollback()
        writer.close()


def test_snapshot_leaves_no_wal_sidecars(tmp_path):
    """Рядом со снимком не остаётся -wal и -shm.

    Они наследуются от боевой базы, бесполезны для копии и не подбирались
    ротацией — то есть копились бы в каталоге бесконечно.
    """
    src = tmp_path / "prod.db"
    make_db(src)
    conn = sqlite3.connect(src)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.close()

    root = tmp_path / "backups"
    backup_db.run(src, root)

    strays = list(root.rglob("*-wal")) + list(root.rglob("*-shm"))
    assert not strays, f"остались служебные файлы: {[p.name for p in strays]}"


def test_missing_database_is_loud(tmp_path):
    with pytest.raises(backup_db.BackupError, match="база не найдена"):
        backup_db.run(tmp_path / "нет.db", tmp_path / "backups")


def test_empty_snapshot_is_rejected(tmp_path):
    """База без таблицы постов проходит integrity_check, но снимком не является."""
    src = tmp_path / "пустая.db"
    sqlite3.connect(src).close()

    with pytest.raises(backup_db.BackupError, match="нет таблицы постов"):
        backup_db.run(src, tmp_path / "backups")


def test_daily_rotation_keeps_fourteen(tmp_path):
    src = tmp_path / "prod.db"
    make_db(src)
    root = tmp_path / "backups"
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for day in range(20):
        backup_db.run(src, root, now=start + timedelta(days=day))

    daily = sorted((root / "daily").glob("news_bot_*.db"))
    assert len(daily) == backup_db.KEEP_DAILY
    # Остаться должны САМЫЕ СВЕЖИЕ, а не первые попавшиеся.
    assert daily[-1].name.startswith("news_bot_20260120")


def test_weekly_slice_is_one_per_week(tmp_path):
    src = tmp_path / "prod.db"
    make_db(src)
    root = tmp_path / "backups"
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for day in range(21):
        backup_db.run(src, root, now=start + timedelta(days=day))

    weekly = sorted((root / "weekly").glob("news_bot_w*.db"))
    assert 1 <= len(weekly) <= backup_db.KEEP_WEEKLY
    assert len(weekly) == len({p.name for p in weekly}), "недели не должны дублироваться"


def test_weekly_rotation_keeps_four(tmp_path):
    src = tmp_path / "prod.db"
    make_db(src)
    root = tmp_path / "backups"
    start = datetime(2026, 1, 1, tzinfo=timezone.utc)

    for week in range(8):
        backup_db.run(src, root, now=start + timedelta(weeks=week))

    weekly = sorted((root / "weekly").glob("news_bot_w*.db"))
    assert len(weekly) == backup_db.KEEP_WEEKLY


def test_restore_from_snapshot_gives_back_the_data(tmp_path):
    """Восстановление: снимок открывается и содержит ровно то, что было."""
    src = tmp_path / "prod.db"
    make_db(src, posts=5)
    dst = backup_db.run(src, tmp_path / "backups")

    # «Потеряли» боевую базу и встали на снимок.
    src.unlink()
    restored = tmp_path / "restored.db"
    restored.write_bytes(dst.read_bytes())

    conn = sqlite3.connect(restored)
    rows = conn.execute("SELECT post_id FROM posts ORDER BY post_id").fetchall()
    conn.close()
    assert [r[0] for r in rows] == ["p0", "p1", "p2", "p3", "p4"]


def test_env_is_never_copied(tmp_path):
    """Секреты в ежедневную ротацию не входят."""
    src = tmp_path / "prod.db"
    make_db(src)
    (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=секрет\n", encoding="utf-8")

    root = tmp_path / "backups"
    backup_db.run(src, root)

    assert not list(root.rglob("*.env*")), "файлы .env не должны попадать в снимки"
    assert not list(root.rglob("*.bak"))


def test_failure_returns_nonzero_without_raising(tmp_path, monkeypatch, capsys):
    """Провал снимка виден systemd, но не роняет процесс исключением."""
    monkeypatch.setattr(backup_db, "DB_PATH", tmp_path / "нет.db")
    monkeypatch.setattr(backup_db, "BACKUP_ROOT", tmp_path / "backups")

    assert backup_db.main() == 1
    assert "СНИМОК НЕ СДЕЛАН" in capsys.readouterr().err
