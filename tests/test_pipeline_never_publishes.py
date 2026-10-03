"""Регрессия на главный блокер запуска.

Было: при PUBLISH_MODE=live конвейер отправлял в канал КАЖДЫЙ сгенерированный
пост сразу при генерации — до того, как владелец его увидел, — а после
нажатия ✅ тот же пост уходил вторым сообщением.

Стало: единственный путь в канал — кнопка ✅ владельца. Этот тест обязан
падать, если кто-то вернёт публикацию в конвейер.
"""
from __future__ import annotations

from pathlib import Path


from src import main as main_module


def test_main_does_not_import_publish_helper():
    """Статическая проверка: в конвейере не должно быть вызова, который
    умеет отправлять в Telegram."""
    src = Path(main_module.__file__).read_text(encoding="utf-8")
    assert "publish_or_draft" not in src, (
        "конвейер снова умеет публиковать — это возвращает баг "
        "с непроверенными постами в эфире"
    )
    assert "send_live" not in src


def test_pipeline_run_sends_nothing_to_telegram(tmp_path, monkeypatch):
    """Функциональная проверка: полный прогон на sample-новостях в режиме
    live не делает ни одной отправки в Telegram."""
    import config
    from src import image_card, publisher, storage

    # ВАЖНО: модули делают `from config import DRAFTS_DIR, DB_PATH, ...`,
    # то есть копируют значения к себе в момент импорта. Подмены только в
    # `config` недостаточно — тест писал бы в боевую папку data/ и в боевую
    # базу. Патчим каждое место, где константа реально читается.
    drafts, images, db_file = tmp_path / "drafts", tmp_path / "drafts" / "images", tmp_path / "t.db"
    images.mkdir(parents=True)

    for module, name, value in (
        (config, "DATA_DIR", tmp_path), (config, "DRAFTS_DIR", drafts),
        (config, "IMAGES_DIR", images), (config, "DB_PATH", db_file),
        (config, "LOGS_DIR", tmp_path / "logs"),
        (storage, "DB_PATH", db_file),
        (publisher, "DRAFTS_DIR", drafts),
        (main_module, "DB_PATH", db_file), (main_module, "DRAFTS_DIR", drafts),
        (main_module, "LOGS_DIR", tmp_path / "logs"),
        (image_card, "IMAGES_DIR", images),
    ):
        monkeypatch.setattr(module, name, value, raising=False)

    sent = []

    def tripwire(*a, **kw):
        sent.append(a)
        raise AssertionError("конвейер попытался отправить пост в Telegram")

    monkeypatch.setattr(publisher, "send_live_text", tripwire)
    monkeypatch.setattr(publisher, "send_live", tripwire)

    # LLM выключен принудительно — тест не тратит деньги и не ходит в сеть.
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    monkeypatch.setenv("PUBLISH_MODE", "live")
    # Порог редакторской оценки задаём явно: тест про то, что конвейер не
    # публикует сам, а не про пропускную способность. Продуктовое значение
    # (MIN_EDITORIAL_AVG=8.6) отсекает шаблонные mock-тексты, и тест
    # ломался бы при каждой настройке потока.
    monkeypatch.setenv("MIN_EDITORIAL_AVG", "5.0")
    monkeypatch.setenv("MAX_TOPICS_PER_RUN", "10")
    monkeypatch.setenv("MAX_CANDIDATES_PER_DAY", "0")
    # Режим задаём ЯВНО. Тест проверяет, что конвейер, дойдя до готового
    # поста, всё равно не отправляет его сам. В режиме дайджеста конвейер
    # до поста не доходит вовсе — и тест падал бы на машине, где режим
    # включён в .env. Ровно это и случилось на боевом сервере 28.08:
    # деплой встал, потому что тест читал окружение вместо своего условия.
    monkeypatch.setenv("WORKFLOW_MODE", "legacy")

    main_module.run(["--dry-run"])

    assert sent == [], "в live-режиме конвейер обязан молчать"

    # Проверяем не только «не отправил», но и «сделал свою работу». Без этого
    # тест проходил бы даже если бы тело обработки поста выпотрошили целиком:
    # ничего не отправлено — значит зелёный, хотя конвейер мёртв.
    with storage.get_conn(tmp_path / "t.db") as conn:
        posts = conn.execute("SELECT COUNT(*) FROM posts").fetchone()[0]
        raws = conn.execute("SELECT COUNT(*) FROM raw_items").fetchone()[0]
    assert posts > 0, "конвейер не создал ни одного поста — он не работает"
    assert raws > 0, "конвейер не сохранил ни одной исходной новости"

    drafts = list((tmp_path / "drafts").glob("*.md"))
    assert drafts, "не записан ни один markdown-черновик"
    assert any("По данным" in d.read_text(encoding="utf-8") for d in drafts), (
        "в черновиках нет строки источника — гейт или сборка текста сломаны"
    )
