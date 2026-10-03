"""Общие фикстуры тестов.

Главное правило: ни один тест не трогает боевую базу data/news_bot.db
и не ходит в сеть. База — временная на каждый тест, Telegram и LLM замоканы.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from src import storage


@pytest.fixture(autouse=True)
def _never_touch_production_db(tmp_path_factory, monkeypatch) -> None:
    """Страховка: ни один тест не пишет в боевую data/news_bot.db.

    Раньше изоляцию давала только фикстура `db`, и её нужно было не забыть
    запросить. Пока права доступа читались из .env, тест без `db` просто
    ничего не трогал. Теперь права лежат в базе, и три таких теста ушли
    прямо в боевую: завели там фиктивного владельца 777 и отметку о
    переносе списка. Поэтому подмена пути включена ВСЕГДА; фикстура `db`
    перекрывает её своим файлом со схемой.

    Путь берём из общей папки прогона, а не из `tmp_path`: отдельный каталог
    на каждый из 300+ тестов заметно замедляет прогон на Windows.
    """
    monkeypatch.setattr(
        storage, "DB_PATH", tmp_path_factory.getbasetemp() / "guard.db"
    )

    # То же самое для каталога карточек: тест, забывший фикстуру images_dir,
    # рисовал PNG прямо в боевую папку. Одной страховки на базу мало —
    # утечка повторилась ровно тем же способом, только с картинками.
    import config
    from src import image_card

    guard_images = tmp_path_factory.getbasetemp() / "guard_images"
    guard_images.mkdir(exist_ok=True)
    monkeypatch.setattr(config, "IMAGES_DIR", guard_images)
    monkeypatch.setattr(image_card, "IMAGES_DIR", guard_images)


@pytest.fixture(autouse=True)
def _pin_workflow_mode(monkeypatch) -> None:
    """Режим конвейера для тестов задаётся тестом, а не машиной.

    На боевом сервере в `.env` стоит `WORKFLOW_MODE=digest`, на ноутбуке
    его нет. Тест, читающий режим из окружения, вёл себя по-разному в двух
    местах: локально зелёный, на сервере красный. Так 28.08 встал деплой —
    `test_pipeline_never_publishes` ждал созданных постов, а конвейер в
    режиме дайджеста до них не доходит.

    Значение по умолчанию — прежнее поведение. Тесту, которому нужен
    дайджест, ничто не мешает задать режим самому: через `monkeypatch.setenv`
    или прямо в объекте `Config`.
    """
    monkeypatch.setenv("WORKFLOW_MODE", "legacy")


@pytest.fixture(autouse=True)
def _pin_llm_provider(monkeypatch) -> None:
    """Tests never use a real LLM provider, whatever the local .env says.

    A developer may have LLM_PROVIDER=gemini and a real key in .env. Tests
    that need the non-mock path build their own Config with a fake key and
    monkeypatch the actual provider call, so no test can spend money or
    depend on the machine's configuration.
    """
    monkeypatch.setenv("LLM_PROVIDER", "mock")
    for name in ("GEMINI_API_KEY", "GEMINI_API_KEY_TEST", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def db(tmp_path, monkeypatch) -> Path:
    """Пустая временная БД со схемой. Подменяет путь во ВСЕХ модулях,
    которые импортировали DB_PATH к себе в неймспейс.
    """
    path = tmp_path / "test.db"
    monkeypatch.setattr(storage, "DB_PATH", path)
    storage.init_db(path)
    return path


@pytest.fixture
def images_dir(tmp_path, monkeypatch) -> Path:
    """Каталог карточек — во ВСЕХ модулях, а не только в config.

    approval_bot импортирует IMAGES_DIR внутри функций, и подмены в config
    ему хватало. А image_card импортирует его на уровне модуля, то есть
    держит собственную ссылку: тесты, рисующие карточки, писали в боевую
    папку. Так там и завелись p1.png, t1.png, t2.png, t3.png — пока
    безобидные, но при совпадении draft_id тест затёр бы реальную карточку.
    """
    import config
    from src import image_card

    path = tmp_path / "images"
    path.mkdir()
    monkeypatch.setattr(config, "IMAGES_DIR", path)
    monkeypatch.setattr(image_card, "IMAGES_DIR", path)
    return path


def seed_post(
    post_id: str = "p1",
    *,
    status: str = "candidate",
    draft: str = "Заголовок поста.\n\nТекст поста.\n\nПо данным BBC News.",
    url: str = "https://example.com/news/1",
    title: str = "Test crime story",
    created_at: str | None = None,
    scores: dict | None = None,
) -> str:
    """Кладёт в БД связку raw_item → event → post, как это делает конвейер.
    Без неё перегенерация не найдёт исходную новость.
    """
    event_id = f"evt_{post_id}"
    storage.insert_raw_item(
        {
            "url_hash": post_id,
            "source": "bbc.com",
            "title": title,
            "body": "Body of the story.",
            "url": url,
            "published_at": "2026-08-26T10:00:00+00:00",
            "fetched_at": "2026-08-26T10:05:00+00:00",
            "lang": "en",
            "category": "",
            "sample": False,
        }
    )
    storage.insert_event(event_id, post_id, n_sources=1)
    storage.insert_post(
        post_id=post_id,
        event_id=event_id,
        draft_ru=draft,
        fmt="A",
        status=status,
        scores=scores or {"average": 9.0, "decision": "approve"},
        manual_only=False,
    )
    if created_at:
        with storage.get_conn() as conn:
            conn.execute(
                "UPDATE posts SET created_at=? WHERE post_id=?", (created_at, post_id)
            )
    return post_id


def post_status(post_id: str) -> str | None:
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT status FROM posts WHERE post_id=?", (post_id,)
        ).fetchone()
    return row[0] if row else None


def post_text(post_id: str) -> str | None:
    with storage.get_conn() as conn:
        row = conn.execute(
            "SELECT draft_ru FROM posts WHERE post_id=?", (post_id,)
        ).fetchone()
    return row[0] if row else None


class FakeBot:
    """Ловит всё, что бот попытался бы отправить в Telegram."""

    def __init__(self) -> None:
        self.photos: list[dict] = []
        self.messages: list[dict] = []

    async def send_photo(self, **kwargs):
        self.photos.append(kwargs)
        return None

    async def send_message(self, **kwargs):
        self.messages.append(kwargs)
        return None

    @property
    def sent_count(self) -> int:
        return len(self.photos) + len(self.messages)

    def last_text(self) -> str:
        if self.photos:
            return self.photos[-1].get("caption", "")
        if self.messages:
            return self.messages[-1].get("text", "")
        return ""


class FakeMessage:
    """Сообщение-кандидат, к которому бот дописывает пометку решения."""

    def __init__(
        self,
        *,
        caption: str | None = None,
        text: str | None = None,
        user_id: int = 777,
        forward_from=None,
    ):
        self.caption = caption
        self.text = text
        self.answers: list[str] = []
        self.markups: list = []
        self.bot = FakeBot()
        # Обработчики читают message.from_user.id (например, экран доступа).
        self.from_user = type("U", (), {"id": user_id})()
        # Obrabotchiki shlyut post v chat togo, kto ego zaprosil.
        self.chat = type("C", (), {"id": user_id})()
        # Пересланное сообщение: так владелец добавляет редактора, не спрашивая ID.
        self.forward_from = forward_from

    async def edit_caption(self, caption: str, parse_mode=None):
        self.caption = caption

    async def edit_text(self, text: str, parse_mode=None):
        self.text = text

    async def answer(self, text: str, **kwargs):
        self.answers.append(text)
        self.markups.append(kwargs.get("reply_markup"))


class FakeCallback:
    """Нажатие на инлайн-кнопку."""

    def __init__(
        self, data: str, message: FakeMessage | None = None, *, user_id: int = 777
    ):
        self.data = data
        self.message = message or FakeMessage(caption="Кандидат")
        self.bot = self.message.bot
        self.answered: list[str] = []
        # Роль нажавшего проверяется внутри обработчиков управления
        # администраторами, поэтому у нажатия должен быть отправитель.
        self.from_user = type("U", (), {"id": user_id})()

    async def answer(self, text: str = "", **kwargs):
        self.answered.append(text)


def handlers_of(dp):
    """Достаёт зарегистрированные обработчики кнопок из диспетчера, чтобы
    дёргать их напрямую с фейковыми объектами — без сети и без токена.
    Ключ — префикс callback_data ('approve', 'reject', ...).
    """
    out = {}
    for h in dp.callback_query.handlers:
        name = h.callback.__name__.replace("cb_", "")
        out[name] = h.callback
    for h in dp.message.handlers:
        out["msg_" + h.callback.__name__] = h.callback
    return out
