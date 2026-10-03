"""Модули конвейера, которые до ревью не были покрыты ничем:
normalize, dedupe_l1, image_card, ingest_rss, rewrite (пост-обработка),
legal_gate.check_pre и загрузка slop-листа.

Два из них уже ломались в проде раньше (трекинг-параметры в ссылках и
гомоглифы «сall-центр» с латинской c), поэтому тесты здесь — не для галочки.
"""
from __future__ import annotations

import pytest

from src import dedupe_l1, legal_gate, normalize, rewrite


# ---------------------------------------------------------------------------
# normalize.clean_url — чистка трекинга
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "dirty,clean",
    [
        ("https://bbc.com/news?utm_source=rss&utm_medium=feed", "https://bbc.com/news"),
        ("https://tass.ru/a/1?fbclid=XYZ", "https://tass.ru/a/1"),
        ("https://x.com/a?gclid=1&id=7", "https://x.com/a?id=7"),
        ("https://x.com/a?id=7", "https://x.com/a?id=7"),
    ],
)
def test_clean_url_strips_tracking(dirty, clean):
    assert normalize.clean_url(dirty) == clean


def test_clean_url_keeps_meaningful_params():
    """Чистка не должна выкидывать параметры, несущие смысл."""
    url = "https://example.com/article?id=42&page=2"
    assert "id=42" in normalize.clean_url(url)


@pytest.mark.parametrize("bad", ["", None, "не ссылка", "javascript:alert(1)"])
def test_clean_url_survives_garbage(bad):
    """Мусор на входе не должен ронять нормализацию."""
    normalize.clean_url(bad)


# ---------------------------------------------------------------------------
# normalize.normalize_raw / batch
# ---------------------------------------------------------------------------


def _raw(**kw):
    base = {
        "title": "Заголовок",
        "body": "Текст",
        "url": "https://bbc.com/1",
        "published_at": "2026-08-27",
        "source": "bbc",
    }
    base.update(kw)
    return base


def test_normalize_drops_item_without_title():
    """Новость без заголовка бесполезна — она не должна попасть в конвейер."""
    assert normalize.normalize_raw(_raw(title="")) is None


def test_normalize_builds_item():
    item = normalize.normalize_raw(_raw())
    assert item is not None
    assert item.title == "Заголовок"
    assert item.url_hash


def test_normalize_batch_skips_broken_items():
    items = normalize.normalize_batch([_raw(), _raw(title=""), _raw(url="https://b/2")])
    assert len(items) == 2, "битые записи выбрасываются, остальные проходят"


def test_same_url_gives_same_hash():
    """Хэш ссылки — основа дедупликации, он обязан быть стабильным."""
    a = normalize.normalize_raw(_raw(url="https://bbc.com/x?utm_source=rss"))
    b = normalize.normalize_raw(_raw(url="https://bbc.com/x"))
    assert a.url_hash == b.url_hash, "трекинг не должен создавать новый хэш"


# ---------------------------------------------------------------------------
# dedupe_l1
# ---------------------------------------------------------------------------


def test_dedupe_removes_exact_duplicates():
    items = normalize.normalize_batch([_raw(), _raw()])
    assert len(dedupe_l1.dedupe(items, 0.75)) == 1


def test_dedupe_keeps_different_stories():
    items = normalize.normalize_batch(
        [
            _raw(title="Ограбление банка в Лионе", url="https://a/1"),
            _raw(title="Суд по делу о поджоге в Осло", url="https://a/2"),
        ]
    )
    assert len(dedupe_l1.dedupe(items, 0.75)) == 2


def test_dedupe_on_empty_list():
    assert dedupe_l1.dedupe([], 0.75) == []


# ---------------------------------------------------------------------------
# rewrite._fix_mixed_alphabet — гомоглифы
# ---------------------------------------------------------------------------


def test_mixed_alphabet_is_fixed():
    """Реальный баг из прода: модель написала «сall-центр» с КИРИЛЛИЧЕСКОЙ 'с'
    в начале латинского слова. Такое слово не находится поиском и выглядит
    опечаткой. Проверяем точный результат, а не «что-то поменялось»."""
    assert (
        rewrite._fix_mixed_alphabet("Мошенники создали сall-центр")
        == "Мошенники создали call-центр"
    )


def test_mixed_alphabet_fixes_toward_cyrillic():
    """Обратный случай: латинская 'o' внутри русского слова."""
    assert rewrite._fix_mixed_alphabet("Тoлько") == "Только"


def test_pure_latin_words_untouched():
    """Английские слова и названия трогать нельзя."""
    text = "Компания BBC News сообщила"
    assert rewrite._fix_mixed_alphabet(text) == text


def test_pure_cyrillic_untouched():
    text = "Обычный русский текст без латиницы"
    assert rewrite._fix_mixed_alphabet(text) == text


@pytest.mark.parametrize("bad", ["", "   "])
def test_mixed_alphabet_survives_empty(bad):
    rewrite._fix_mixed_alphabet(bad)


# ---------------------------------------------------------------------------
# legal_gate.check_pre — до сих пор не был покрыт ничем
# ---------------------------------------------------------------------------


def _item(title="Обычная новость", body="Текст новости", url="https://bbc.com/1"):
    return normalize.normalize_raw(_raw(title=title, body=body, url=url))


def test_check_pre_passes_normal_news():
    r = legal_gate.check_pre(_item())
    assert r.hard_fail is False


def test_check_pre_hard_fails_without_url():
    """Новость без ссылки не должна доходить до генерации текста —
    это трата денег на модель и заведомо непубликуемый пост."""
    item = _item()
    item.url = ""
    assert legal_gate.check_pre(item).hard_fail is True


def test_check_pre_marks_army_rf_manual_only():
    """Ст. 207.3: армейская тема уходит только в ручную модерацию."""
    r = legal_gate.check_pre(
        _item(title="Военнослужащих ВС РФ обвиняют в пытках", body="Подробности")
    )
    assert r.manual_only is True


def test_check_pre_and_post_agree_on_army():
    """Проверка ст. 207.3 должна работать на ОБОИХ концах конвейера.
    Раньше check_post её вообще не делал — текст, переписанный моделью
    или набранный владельцем руками, проходил без сигнала."""
    text = "Военнослужащие ВС РФ обвиняются в пытках задержанных."
    assert legal_gate.check_post(text, "https://bbc.com/1").manual_only is True


# ---------------------------------------------------------------------------
# slop-лист
# ---------------------------------------------------------------------------


def test_slop_list_loads():
    """Список запрещённых штампов должен читаться. Если файла нет —
    обязан быть непустой запасной список, иначе фильтр молча отключается."""
    assert len(legal_gate.load_slop_list()) > 0


def test_slop_list_fallback_when_file_missing(monkeypatch):
    import config

    monkeypatch.setattr(config, "SLOP_LIST_PATH", config.PROJECT_ROOT / "нет-такого.txt")
    legal_gate.load_slop_list.cache_clear()
    try:
        assert len(legal_gate.load_slop_list()) > 0, "запасной список пуст — фильтр мёртв"
    finally:
        legal_gate.load_slop_list.cache_clear()


# ---------------------------------------------------------------------------
# image_card
# ---------------------------------------------------------------------------


def test_card_renders(tmp_path, monkeypatch):
    import config
    from src.image_card import render_card

    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path)
    path = render_card(
        title="Обычный заголовок поста",
        category="громкое дело",
        source_name="BBC News",
        draft_id="t1",
    )
    assert path.exists() and path.stat().st_size > 0


def test_card_handles_very_long_title(tmp_path, monkeypatch):
    """Длинный заголовок не должен ронять генерацию карточки."""
    import config
    from src.image_card import render_card

    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path)
    path = render_card(
        title="Очень длинный заголовок " * 20,
        category="курьёз",
        source_name="BBC",
        draft_id="t2",
    )
    assert path.exists()


def test_card_handles_emoji_and_empty(tmp_path, monkeypatch):
    import config
    from src.image_card import render_card

    monkeypatch.setattr(config, "IMAGES_DIR", tmp_path)
    assert render_card(title="🔪 Эмодзи", category="", source_name="", draft_id="t3").exists()
