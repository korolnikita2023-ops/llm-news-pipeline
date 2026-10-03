"""Тесты src/llm_client.py.

Покрывает: извлечение JSON из ответа модели (прямой/фенс/встроенный в
текст), расчёт стоимости по прайс-константам, дневной бюджет (LLMBudgetExceeded
ДО сетевого вызова), retry с backoff на transient-ошибки, 1 retry на
невалидный JSON, и переключение mock/live в relevance.py/rewrite.py/scorer.py.

НИКАКИХ реальных вызовов API: провайдер-транспорт (`llm_client._call_llm`)
всегда подменяется через monkeypatch — тесты не должны требовать сеть или
ключи. Запуск: `python -m pytest tests/test_llm_client.py -v` из этой
директории (тот же трюк с sys.path[0], что и `python -m src.main`).
"""
from __future__ import annotations

import sys
from pathlib import Path

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

import pytest

import config
from config import Config
from src import llm_client, storage
from src.llm_client import (
    LLMBudgetExceeded,
    LLMResponseError,
    LLMTransientError,
    ProviderResult,
)


# ---------------------------------------------------------------------------
# _extract_json
# ---------------------------------------------------------------------------


def test_extract_json_plain():
    assert llm_client._extract_json('{"a": 1, "b": "x"}') == {"a": 1, "b": "x"}


def test_extract_json_fenced_with_lang_tag():
    text = '```json\n{"relevant": true, "total": 42}\n```'
    assert llm_client._extract_json(text) == {"relevant": True, "total": 42}


def test_extract_json_fenced_without_lang_tag():
    text = "Вот ответ:\n```\n{\"hook\": \"...\"}\n```\nСпасибо."
    assert llm_client._extract_json(text) == {"hook": "..."}


def test_extract_json_embedded_in_prose():
    text = 'Конечно, вот JSON: {"category": "громкое дело", "n": 3} — надеюсь, помогло.'
    assert llm_client._extract_json(text) == {"category": "громкое дело", "n": 3}


def test_extract_json_balanced_handles_nested_and_string_braces():
    # Проверяет, что поиск сбалансированного {...} не путается на '{'/'}'
    # ВНУТРИ строковых значений и корректно считает вложенные объекты.
    text = (
        'Ответ модели: {"full_text": "детали {в фигурных} скобках", '
        '"nested": {"a": 1}} — конец.'
    )
    data = llm_client._extract_json(text)
    assert data["full_text"] == "детали {в фигурных} скобках"
    assert data["nested"] == {"a": 1}


def test_extract_json_invalid_raises_value_error():
    with pytest.raises(ValueError):
        llm_client._extract_json("это вообще не JSON, простите")


def test_extract_json_empty_raises_value_error():
    with pytest.raises(ValueError):
        llm_client._extract_json("")
    with pytest.raises(ValueError):
        llm_client._extract_json(None)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# _cost_for — расчёт реальной стоимости по прайс-константам config.py
# ---------------------------------------------------------------------------


def test_cost_for_known_model_computes_expected_price():
    model = "gpt-5.4-nano"
    prices = config.LLM_PRICES_PER_1M[model]
    cost = llm_client._cost_for(model, input_tokens=1_000_000, output_tokens=1_000_000)
    assert cost == pytest.approx(prices["input"] + prices["output"])


def test_cost_for_zero_tokens_is_zero():
    assert llm_client._cost_for("gpt-5.4-nano", 0, 0) == 0.0


def test_cost_for_unknown_model_charges_worst_known_price(capsys):
    """Неизвестная модель НЕ должна считаться бесплатной.

    Тест раньше требовал ровно этого (`cost == 0.0`) и тем самым закреплял
    дыру: нули в cost_log означали, что get_today_cost() навсегда 0, и
    дневной лимит расходов не срабатывал никогда. Достаточно было указать
    в .env ID модели, которого нет в таблице цен, — и ограничение бюджета
    исчезало молча. Правильное поведение — считать по верхней известной
    цене: лимит сработает раньше времени (заметно и безопасно), а не позже.
    """
    cost = llm_client._cost_for("some-model-not-in-price-list", 1000, 1000)
    assert cost > 0.0, "неизвестная модель не может стоить ноль — это отключает лимит"
    captured = capsys.readouterr()
    assert "WARNING" in captured.out
    assert "some-model-not-in-price-list" in captured.out


# ---------------------------------------------------------------------------
# generate_json — защита от вызова в mock-режиме
# ---------------------------------------------------------------------------


def test_generate_json_refuses_mock_config():
    cfg = Config(llm_provider="mock")
    with pytest.raises(LLMResponseError):
        llm_client.generate_json(
            cfg, tier=config.LLM_TIER_CHEAP, stage="test",
            system_prompt="s", user_prompt="u",
        )


# ---------------------------------------------------------------------------
# generate_json — успешный путь + реальная стоимость в cost_log (P0-2)
# ---------------------------------------------------------------------------


def test_generate_json_success_logs_real_cost(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    storage.init_db(db_path)

    cfg = Config(llm_provider="openai", openai_api_key="fake-key", daily_cost_limit_usd=3.0)
    calls = []

    def fake_call_llm(cfg_, provider, model, system_prompt, user_prompt):
        calls.append((provider, model))
        return ProviderResult(text='{"relevant": true, "total": 42}', input_tokens=1000, output_tokens=500)

    monkeypatch.setattr(llm_client, "_call_llm", fake_call_llm)

    data = llm_client.generate_json(
        cfg, tier=config.LLM_TIER_CHEAP, stage="relevance",
        system_prompt="system", user_prompt="user", db_path=db_path,
    )

    assert data == {"relevant": True, "total": 42}
    assert calls == [("openai", cfg.model_for_tier(config.LLM_TIER_CHEAP))]

    expected_cost = llm_client._cost_for(cfg.model_for_tier(config.LLM_TIER_CHEAP), 1000, 500)
    assert expected_cost > 0  # sanity: это настоящая модель из прайс-листа, не 0.0
    assert storage.get_today_cost(db_path) == pytest.approx(expected_cost)


# ---------------------------------------------------------------------------
# generate_json — дневной лимит блокирует ДО сетевого вызова (P0-2)
# ---------------------------------------------------------------------------


def test_generate_json_budget_exceeded_blocks_before_call(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    storage.init_db(db_path)
    storage.log_cost(stage="rewrite", provider="openai", model="gpt-5.4-mini", usd_cost=5.0, db_path=db_path)

    cfg = Config(llm_provider="openai", openai_api_key="fake-key", daily_cost_limit_usd=3.0)

    def fail_if_called(*args, **kwargs):
        raise AssertionError("_call_llm НЕ должен вызываться при исчерпанном дневном бюджете")

    monkeypatch.setattr(llm_client, "_call_llm", fail_if_called)

    with pytest.raises(LLMBudgetExceeded):
        llm_client.generate_json(
            cfg, tier=config.LLM_TIER_MID, stage="rewrite",
            system_prompt="s", user_prompt="u", db_path=db_path,
        )


# ---------------------------------------------------------------------------
# generate_json — retry с backoff на transient-ошибки (сеть/5xx/429)
# ---------------------------------------------------------------------------


def test_generate_json_retries_transient_then_succeeds(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    storage.init_db(db_path)
    monkeypatch.setattr(llm_client, "_sleep", lambda seconds: None)  # не ждать реальные секунды

    cfg = Config(llm_provider="gemini", gemini_api_key="fake-key", daily_cost_limit_usd=3.0)
    attempts = {"n": 0}

    def flaky_call_llm(cfg_, provider, model, system_prompt, user_prompt):
        attempts["n"] += 1
        if attempts["n"] < 3:
            raise LLMTransientError("временная 503")
        return ProviderResult(text='{"ok": true}', input_tokens=10, output_tokens=5)

    monkeypatch.setattr(llm_client, "_call_llm", flaky_call_llm)

    data = llm_client.generate_json(
        cfg, tier=config.LLM_TIER_CHEAP, stage="scorer",
        system_prompt="s", user_prompt="u", db_path=db_path,
    )
    assert data == {"ok": True}
    assert attempts["n"] == 3  # 2 неудачные попытки + 1 успешная = MAX_ATTEMPTS


def test_generate_json_exhausts_retries_raises(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    storage.init_db(db_path)
    monkeypatch.setattr(llm_client, "_sleep", lambda seconds: None)

    cfg = Config(llm_provider="gemini", gemini_api_key="fake-key", daily_cost_limit_usd=3.0)

    def always_fails(cfg_, provider, model, system_prompt, user_prompt):
        raise LLMTransientError("сеть недоступна")

    monkeypatch.setattr(llm_client, "_call_llm", always_fails)

    with pytest.raises(LLMResponseError):
        llm_client.generate_json(
            cfg, tier=config.LLM_TIER_CHEAP, stage="scorer",
            system_prompt="s", user_prompt="u", db_path=db_path,
        )


# ---------------------------------------------------------------------------
# generate_json — 1 retry на невалидный JSON (со строгим промптом)
# ---------------------------------------------------------------------------


def test_generate_json_invalid_json_retries_once_then_succeeds(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    storage.init_db(db_path)

    cfg = Config(llm_provider="openai", openai_api_key="fake-key", daily_cost_limit_usd=3.0)
    responses = ["это не JSON вообще", '{"fixed": true}']

    def fake_call_llm(cfg_, provider, model, system_prompt, user_prompt):
        return ProviderResult(text=responses.pop(0), input_tokens=10, output_tokens=10)

    monkeypatch.setattr(llm_client, "_call_llm", fake_call_llm)

    data = llm_client.generate_json(
        cfg, tier=config.LLM_TIER_MID, stage="rewrite",
        system_prompt="s", user_prompt="u", db_path=db_path,
    )
    assert data == {"fixed": True}
    assert responses == []  # оба заготовленных ответа израсходованы

    with storage.get_conn(db_path) as conn:
        n_rows = conn.execute("SELECT COUNT(*) AS n FROM cost_log").fetchone()["n"]
    assert n_rows == 2  # обе попытки (невалидная + валидная) стоили денег и залогированы


def test_generate_json_invalid_json_twice_raises(tmp_path, monkeypatch):
    db_path = tmp_path / "test.db"
    storage.init_db(db_path)

    cfg = Config(llm_provider="openai", openai_api_key="fake-key", daily_cost_limit_usd=3.0)

    def always_garbage(cfg_, provider, model, system_prompt, user_prompt):
        return ProviderResult(text="совсем не JSON", input_tokens=10, output_tokens=10)

    monkeypatch.setattr(llm_client, "_call_llm", always_garbage)

    with pytest.raises(LLMResponseError):
        llm_client.generate_json(
            cfg, tier=config.LLM_TIER_MID, stage="rewrite",
            system_prompt="s", user_prompt="u", db_path=db_path,
        )


# ---------------------------------------------------------------------------
# Переключение mock/live — Config.is_mock()/has_llm()/model_for_tier()
# ---------------------------------------------------------------------------


def test_config_is_mock_without_key():
    cfg = Config(llm_provider="gemini", gemini_api_key="")
    assert cfg.is_mock() is True
    assert cfg.has_llm() is False


def test_config_is_live_with_key():
    cfg = Config(llm_provider="gemini", gemini_api_key="test-key")
    assert cfg.is_mock() is False
    assert cfg.has_llm() is True


def test_config_model_for_tier_openai():
    cfg = Config(llm_provider="openai", openai_api_key="x")
    assert cfg.model_for_tier(config.LLM_TIER_CHEAP) == cfg.openai_model_cheap
    assert cfg.model_for_tier(config.LLM_TIER_MID) == cfg.openai_model_mid


# ---------------------------------------------------------------------------
# Переключение mock/live на уровне вызывающих модулей — без ключа код должен
# идти в mock-ветку и НЕ трогать llm_client.generate_json вообще; с ключом —
# идти в live-ветку и звать llm_client.generate_json (сам вызов замокан).
# ---------------------------------------------------------------------------


def test_relevance_dispatches_to_mock_without_key(monkeypatch):
    from src import relevance
    from src.normalize import NewsItem

    called = {"llm": False}

    def fail_if_called(*args, **kwargs):
        called["llm"] = True
        raise AssertionError("generate_json не должен вызываться в mock-режиме")

    monkeypatch.setattr(llm_client, "generate_json", fail_if_called)

    cfg = Config(llm_provider="mock")
    item = NewsItem(
        url_hash="h1", source="Test", title="Задержан подозреваемый в убийстве",
        body="Следствие устанавливает обстоятельства дела.", url="https://example.com/1",
        published_at="2026-08-01", fetched_at="2026-08-01", lang="ru",
    )
    result = relevance.score_item(cfg, item)
    assert called["llm"] is False
    assert result is not None


def test_rewrite_dispatches_to_llm_when_key_present(monkeypatch):
    from src import rewrite
    from src.normalize import NewsItem

    fake_response = {
        "hook": "хук", "body": "тело", "detail_hook": "деталь",
        "source_line": "По данным Test.",
        "full_text": "хук\n\nтело\n\nдеталь\n\nПо данным Test.",
        "format": "A", "rejected_no_source": False,
    }

    def fake_generate_json(cfg_, tier, stage, system_prompt, user_prompt, db_path=None):
        assert stage == "rewrite"
        return dict(fake_response)

    monkeypatch.setattr(llm_client, "generate_json", fake_generate_json)

    cfg = Config(llm_provider="openai", openai_api_key="fake-key")
    item = NewsItem(
        url_hash="h2", source="Test", title="Суд рассмотрит дело о мошенничестве",
        body="Подробности дела.", url="https://example.com/2",
        published_at="2026-08-01", fetched_at="2026-08-01", lang="ru",
    )
    draft = rewrite.generate_draft(cfg, item, attempt=0)
    assert draft.hook == "хук"
    assert draft.source_url == "https://example.com/2"
    assert draft.full_text == fake_response["full_text"]


def test_scorer_dispatches_to_llm_when_key_present(monkeypatch):
    from src import scorer
    from src.rewrite import PostDraft

    fake_response = {
        "tone_match": 8, "news_value": 8, "engagement": 7, "originality": 6,
        "factual_safety": 8, "legal_safety": 8, "hard_fail": False,
        "issues": [], "suggested_fix": "",
    }

    def fake_generate_json(cfg_, tier, stage, system_prompt, user_prompt, db_path=None):
        assert stage == "scorer"
        return dict(fake_response)

    monkeypatch.setattr(llm_client, "generate_json", fake_generate_json)

    cfg = Config(llm_provider="openai", openai_api_key="fake-key")
    # Порог задаём явно: тест проверяет ДИСПЕТЧЕРИЗАЦИЮ в LLM, а не значение
    # MIN_EDITORIAL_AVG. Без этого он ломался каждый раз при изменении
    # продуктовой настройки пропускной способности.
    cfg.min_editorial_avg = 7.0
    draft = PostDraft(
        id="abc123", source_item_hash="h3", source_url="https://example.com/3",
        source_name="Test", category="громкое дело", format="A",
        hook="хук", body="тело", detail_hook="деталь", source_line="По данным Test.",
        full_text="хук\n\nтело\n\nдеталь\n\nПо данным Test.",
    )
    result = scorer.score_draft(cfg, draft)
    assert result.decision == "approve"
    assert result.hard_fail is False
