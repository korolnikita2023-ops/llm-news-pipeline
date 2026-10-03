"""Тесты юридического гейта — самой ответственной части проекта.

Гейт защищает от исков и от ст. 207.3: он должен срабатывать ДО того, как
текст увидит канал, и не должен зависеть от того, писал текст ИИ или человек.
"""
from __future__ import annotations

import pytest

from src import legal_gate

SRC = "https://bbc.com/news/1"


# ---------------------------------------------------------------------------
# Источник обязателен
# ---------------------------------------------------------------------------


def test_post_without_source_url_is_hard_fail():
    """Нет ссылки на источник — публиковать нельзя ни при каких условиях."""
    r = legal_gate.check_post("Обычный нейтральный текст новости.", "")
    assert r.hard_fail is True
    assert not r.passed


@pytest.mark.parametrize("url", ["", "   ", None])
def test_empty_source_variants(url):
    assert legal_gate.check_source_url(url) is False


def test_valid_source_passes():
    assert legal_gate.check_source_url(SRC) is True


# ---------------------------------------------------------------------------
# Презумпция невиновности
# ---------------------------------------------------------------------------


def test_guilt_word_without_qualifier_blocked():
    """«Убийца» без оговорки о статусе дела — блок."""
    r = legal_gate.check_post("Убийца задержан в аэропорту.", SRC)
    assert r.hard_fail is True


def test_guilt_word_with_qualifier_allowed():
    """С оговоркой «по версии следствия» тот же сюжет проходит."""
    r = legal_gate.check_post(
        "По версии следствия, задержанный причастен к убийству.", SRC
    )
    assert r.hard_fail is False


def test_neutral_text_passes():
    r = legal_gate.check_post(
        "Суд отправил бывшего чиновника под стражу. Расследование продолжается.",
        SRC,
    )
    assert r.passed is True


# ---------------------------------------------------------------------------
# Несовершеннолетние
# ---------------------------------------------------------------------------


def test_minor_with_full_name_is_flagged():
    """Несовершеннолетний рядом с ФИО — обязательно поднять флаг."""
    issues = legal_gate.check_minors(
        "15-летняя Мария Иванова пропала по дороге из школы."
    )
    assert issues, "упоминание ребёнка с именем должно давать issue"


def test_minor_without_name_is_ok():
    issues = legal_gate.check_minors("15-летняя школьница пропала по дороге домой.")
    assert issues == []


# ---------------------------------------------------------------------------
# ст. 207.3 — армия/госорганы РФ
# ---------------------------------------------------------------------------


def test_army_rf_negative_context_detected():
    assert legal_gate.check_army_rf("Военные ВС РФ обвиняются в преступлении.") is True


def test_army_rf_neutral_not_flagged():
    assert legal_gate.check_army_rf("В Зимбабве задержали чиновника.") is False


# ---------------------------------------------------------------------------
# Гейт применяется дважды и одинаково — независимо от автора текста
# ---------------------------------------------------------------------------


def test_gate_is_deterministic():
    """Один и тот же текст даёт один и тот же вердикт: гейт — это код,
    а не мнение модели."""
    text = "Убийца пойман."
    a = legal_gate.check_post(text, SRC)
    b = legal_gate.check_post(text, SRC)
    assert a.hard_fail == b.hard_fail


def test_gate_handles_empty_text():
    """Пустой текст не должен ронять гейт."""
    r = legal_gate.check_post("", SRC)
    assert isinstance(r.hard_fail, bool)
