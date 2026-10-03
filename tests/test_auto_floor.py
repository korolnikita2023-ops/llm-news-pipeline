"""Политика auto_floor (с 30.08.2026): редакторская оценка
РАНЖИРУЕТ, а не служит абсолютным барьером.

Модель: >= MIN_EDITORIAL_AVG — отличный кандидат; >= AUTO_EDITORIAL_FLOOR —
приемлемый добор (публикуется лучшее доступное за день); ниже floor —
отказ, auto_editor берёт следующую историю рейтинга. На редакторскую
неудачу — максимум ОДНА дополнительная генерация, фиксируется лучший
вариант из оценённых (живой случай 30.08: серия 7.67 → 8.33 → 8.17 съела
три генерации и сохранила последний вариант вместо лучшего).

Юридический гейт не ослабляется ни в одном из этих тестов — это отдельный
контур со своим бюджетом починки формулировок.
"""
from __future__ import annotations

import json

from config import Config
from src import storage


def make_cfg(**over) -> Config:
    cfg = Config()
    cfg.llm_provider = "mock"
    cfg.gemini_api_key = ""
    cfg.min_editorial_avg = 8.6
    cfg.auto_editorial_floor = 8.0
    cfg.max_regenerate_attempts = 2
    for k, v in over.items():
        setattr(cfg, k, v)
    return cfg


class FakeScore:
    min_average = 8.6

    def __init__(self, average: float, legal_safety: int = 9, hard_fail: bool = False):
        self._avg = average
        self.legal_safety = legal_safety
        self.hard_fail = hard_fail
        self.issues: list = []
        self.suggested_fix = ""

    @property
    def average(self) -> float:
        return self._avg

    @property
    def decision(self) -> str:
        if self.hard_fail:
            return "reject"
        if self._avg >= self.min_average and self.legal_safety >= 6:
            return "approve"
        return "revise" if self._avg >= 5.0 else "reject"

    def to_dict(self) -> dict:
        return {"tone_match": 8, "news_value": 8, "engagement": 8,
                "originality": 8, "factual_safety": 8,
                "legal_safety": self.legal_safety, "average": self._avg,
                "hard_fail": self.hard_fail, "decision": self.decision,
                "issues": [], "suggested_fix": ""}


def _news_item():
    from src.normalize import NewsItem

    return NewsItem(
        url_hash="haf1", source="bbc.com",
        title="Fraudster jailed for eight years",
        body="The judge sentenced him on Friday.",
        url="https://bbc.co.uk/news/1", published_at="",
        fetched_at="2026-08-30T00:00:00+00:00", lang="en", category="",
        sample=False,
    )


def _fresh_draft():
    """Новый объект на каждую генерацию — как в бою: у каждой попытки свой
    черновик, и подмена одним общим объектом сделала бы best-of-two слепым."""
    from src.rewrite import PostDraft

    return PostDraft(
        id="paf1", source_item_hash="haf1", source_url="https://bbc.co.uk/news/1",
        source_name="BBC", category="громкое дело", format="A",
        hook="Суд приговорил мужчину к восьми годам.",
        body="Суд признал его виновным в мошенничестве.",
        detail_hook="Ущерб составил миллион фунтов.",
        source_line="По данным BBC.",
        full_text=("Суд приговорил мужчину к восьми годам.\n\n"
                   "Суд признал его виновным в мошенничестве.\n\n"
                   "Ущерб составил миллион фунтов.\n\nПо данным BBC."),
        headline="Приговор мошеннику",
    )


def _run(monkeypatch, scores: list, auto_floor: float | None,
         gate_fails_first: int = 0, gate_pattern: list | None = None, cfg=None):
    """Прогон _process_item: очередь оценок, счётчик генераций.

    `gate_fails_first` — сколько первых генераций валит юридический гейт
    (проверка бюджета «юридической починки»). `gate_pattern` — исход гейта
    на каждую генерацию по порядку (True = пройден); перекрывает
    `gate_fails_first` и позволяет чередование «прошёл → завалил».
    """
    from src import main as pipeline
    from src.legal_gate import GateResult
    from src.relevance import RelevanceResult

    cfg = cfg or make_cfg()
    queue = list(scores)
    generated = {"n": 0}

    def fake_generate(cfg_, item_, attempt=0, case_status=None):
        generated["n"] += 1
        return _fresh_draft()

    def fake_score(cfg_, d):
        score = queue.pop(0)
        d.scores = score.to_dict()   # как настоящий score_draft
        return score

    gate_calls = {"n": 0}

    def fake_check_post(text, url, strict_style=True, case_status=None):
        gate_calls["n"] += 1
        if gate_pattern is not None:
            ok = gate_pattern[gate_calls["n"] - 1]
        else:
            ok = gate_calls["n"] > gate_fails_first
        if not ok:
            return GateResult(passed=False, hard_fail=True, issues=["формулировка вины"])
        return GateResult(passed=True, hard_fail=False, issues=[])

    monkeypatch.setattr(pipeline, "generate_draft", fake_generate)
    monkeypatch.setattr(pipeline, "score_draft", fake_score)
    monkeypatch.setattr(pipeline.legal_gate, "check_post", fake_check_post)
    monkeypatch.setattr(pipeline.legal_gate, "check_headline",
                        lambda headline, case_status=None: GateResult(
                            passed=True, hard_fail=False, issues=[]))
    monkeypatch.setattr(pipeline, "render_card", lambda **k: None)
    monkeypatch.setattr(pipeline, "write_draft_markdown", lambda *a, **k: None)

    relevance = RelevanceResult(relevant=True, curiosity=8, emotion=7, unusual=7,
                                shareable=7, ru_fit=7, category="громкое дело")
    stats = {"approved": 0, "manual_only": 0, "hard_rejected": 0, "regenerated": 0}
    pipeline._process_item(cfg, _news_item(), relevance, stats,
                           borderline_ok=False, auto_floor=auto_floor)
    return generated["n"], stats


def _post():
    with storage.get_conn() as c:
        row = c.execute(
            "SELECT status, scores, reject_reason FROM posts WHERE post_id='paf1'"
        ).fetchone()
    if row is None:
        return None, {}, None
    return row["status"], json.loads(row["scores"] or "{}"), row["reject_reason"]


# ---------------------------------------------------------------------------
# Добор по floor
# ---------------------------------------------------------------------------


def test_best_of_two_above_floor_is_published(db, images_dir, monkeypatch):
    """Живой случай 30.08 наоборот: 7.67 → 8.33 теперь даёт пост 8.33,
    а не тишину в слоте."""
    n, stats = _run(monkeypatch, [FakeScore(7.67), FakeScore(8.33)], auto_floor=8.0)

    status, scores, _ = _post()
    assert status == "candidate", "лучший вариант выше floor обязан стать постом"
    assert scores.get("auto_floor") is True, "пост без пометки добора"
    assert scores.get("average") == 8.33, "сохранён не лучший вариант"
    assert n == 2, f"ожидали ровно две генерации, было {n}"
    assert stats["approved"] == 1


def test_excellent_first_attempt_needs_one_generation(db, images_dir, monkeypatch):
    n, _ = _run(monkeypatch, [FakeScore(9.0)], auto_floor=8.0)

    status, scores, _ = _post()
    assert status == "candidate"
    assert "auto_floor" not in scores, "оценка выше порога — это не добор"
    assert n == 1


def test_editorial_failure_costs_at_most_two_generations(db, images_dir, monkeypatch):
    """Три-четыре генерации на один посредственный сюжет — запрещённая
    трата: попытки нужны следующим историям рейтинга."""
    n, _ = _run(monkeypatch,
                [FakeScore(7.0), FakeScore(7.5), FakeScore(9.9)], auto_floor=8.0)

    status, scores, reason = _post()
    assert status == "rejected", "лучший 7.5 ниже floor 8.0 — отказ"
    assert reason == storage.REJECT_BY_EDITOR
    assert n == 2, f"третья генерация запрещена, было {n}"
    assert scores.get("average") == 7.5, "в отказе фиксируется лучший из двух"


def test_below_floor_is_still_a_rejection(db, images_dir, monkeypatch):
    _run(monkeypatch, [FakeScore(7.9), FakeScore(7.8)], auto_floor=8.0)
    status, _, _ = _post()
    assert status == "rejected"


def test_floor_requires_legal_safety(db, images_dir, monkeypatch):
    """Слабый именно по праву текст floor не спасает."""
    _run(monkeypatch,
         [FakeScore(8.4, legal_safety=4), FakeScore(8.3, legal_safety=4)],
         auto_floor=8.0)
    status, _, _ = _post()
    assert status == "rejected"


def test_hard_fail_variant_is_never_promoted(db, images_dir, monkeypatch):
    """Вариант с высокой средней, но hard_fail (слоп и т.п.) не должен
    вытеснить чистый вариант и утопить историю."""
    n, _ = _run(monkeypatch,
                [FakeScore(9.0, hard_fail=True), FakeScore(8.2)], auto_floor=8.0)

    status, scores, _ = _post()
    assert status == "candidate", "чистый 8.2 обязан выйти"
    assert scores.get("average") == 8.2
    assert scores.get("hard_fail") is False


def test_floor_at_threshold_disables_the_fallback(db, images_dir, monkeypatch):
    """AUTO_EDITORIAL_FLOOR >= MIN_EDITORIAL_AVG — прежняя строгая модель."""
    _run(monkeypatch, [FakeScore(8.4), FakeScore(8.5)], auto_floor=8.6)
    status, _, _ = _post()
    assert status == "rejected"


def test_no_floor_means_old_behaviour(db, images_dir, monkeypatch):
    """auto_floor=None (legacy и digest): полный бюджет regenerate,
    добора нет — ни одна строка старых режимов не меняется."""
    n, _ = _run(monkeypatch,
                [FakeScore(8.3), FakeScore(8.2), FakeScore(8.4)], auto_floor=None)

    status, scores, _ = _post()
    assert status == "rejected"
    assert "auto_floor" not in scores
    assert n == 3, "прежний бюджет regenerate обязан сохраниться"


# ---------------------------------------------------------------------------
# Юридическая починка — отдельный контур
# ---------------------------------------------------------------------------


def test_legal_repair_keeps_its_own_budget(db, images_dir, monkeypatch):
    """Отказ гейта чинится повторной формулировкой — этот бюджет политика
    auto_floor не трогает: две генерации валит гейт, третья проходит."""
    n, _ = _run(monkeypatch, [FakeScore(9.0)], auto_floor=8.0, gate_fails_first=2)

    status, _, _ = _post()
    assert status == "candidate", "починенная формулировка обязана дойти до поста"
    assert n == 3, "две юридические починки + успех — три генерации"


def test_good_variant_survives_later_gate_failures(db, images_dir, monkeypatch):
    """Гейт прошёл → оценка 8.3 → следующие генерации гейт завалил.
    Пригодный 8.3 обязан выйти по floor, а не утонуть под ложным
    REJECT_BY_LEGAL с чужим небезопасным текстом (нашёл Codex: промоция
    сравнением «строго больше» здесь не срабатывала — last_score уже
    указывал на тот же лучший вариант)."""
    _run(monkeypatch, [FakeScore(8.3)], auto_floor=8.0,
         gate_pattern=[True, False, False])

    status, scores, _ = _post()
    assert status == "candidate", "пригодный 8.3 утонул под отказами гейта"
    assert scores.get("auto_floor") is True
    assert scores.get("average") == 8.3


def test_later_gate_failures_do_not_fake_a_legal_rejection(db, images_dir, monkeypatch):
    """Тот же сценарий, но лучший вариант ниже floor: отказ обязан быть
    редакторским (7.5 зафиксирован), а не юридическим от отброшенной
    генерации."""
    _run(monkeypatch, [FakeScore(7.5)], auto_floor=8.0,
         gate_pattern=[True, False, False])

    status, scores, reason = _post()
    assert status == "rejected"
    assert reason == storage.REJECT_BY_EDITOR, "причина отказа — от чужого черновика"
    assert scores.get("average") == 7.5, "сохранён не лучший вариант"
    assert "legal_issues" not in scores, "претензии гейта от отброшенной генерации"


# ---------------------------------------------------------------------------
# Проводка и синхронность настроек
# ---------------------------------------------------------------------------


def test_auto_editor_passes_the_floor():
    """auto_editor обязан передавать floor из настроек — иначе политика
    существует только в тестах."""
    import inspect

    from src import auto_editor

    source = inspect.getsource(auto_editor._prepare)
    assert "auto_floor=cfg.auto_editorial_floor" in source
    assert "borderline_ok=False" in source, "пограничная полоса человека тут не действует"


def test_floor_default_matches_the_example_file():
    import pathlib
    import re as _re

    cfg = Config()
    example = pathlib.Path(__file__).resolve().parents[1] / ".env.example"
    match = _re.search(r"^AUTO_EDITORIAL_FLOOR=([\d.]+)",
                       example.read_text(encoding="utf-8"), _re.M)
    assert match, "в .env.example нет AUTO_EDITORIAL_FLOOR"
    assert float(match.group(1)) == cfg.auto_editorial_floor


def test_env_cannot_push_the_floor_below_the_hard_min(monkeypatch):
    """Опечатка в .env (AUTO_EDITORIAL_FLOOR=1.0) не имеет права молча
    уронить планку канала: значение клампится к AUTO_FLOOR_HARD_MIN."""
    import config as config_mod

    monkeypatch.setenv("AUTO_EDITORIAL_FLOOR", "1.0")
    assert Config().auto_editorial_floor == config_mod.AUTO_FLOOR_HARD_MIN


def test_env_can_raise_the_floor(monkeypatch):
    """Вверх — можно: 8.6 и выше выключает добор, это законная ручка."""
    monkeypatch.setenv("AUTO_EDITORIAL_FLOOR", "8.6")
    assert Config().auto_editorial_floor == 8.6


def test_floor_mark_reaches_the_owner_report():
    """Пометка добора обязана долетать до ежедневного отчёта: по ней
    владелец калибрует floor, не залезая в базу."""
    from src import auto_editor

    report = {"assigned": [{
        "slot": "day", "title": "☀️ День", "at_local": "30.08 в 14:00",
        "average": 8.33, "auto_floor": True,
        "story": {"title": "История", "score": 29},
    }], "rejected": [], "skipped": []}

    text = auto_editor.format_report(make_cfg(), report)
    assert "добор по floor" in text


def test_excellent_post_is_not_marked_as_floor():
    from src import auto_editor

    report = {"assigned": [{
        "slot": "day", "title": "☀️ День", "at_local": "30.08 в 14:00",
        "average": 9.0, "auto_floor": False,
        "story": {"title": "История", "score": 40},
    }], "rejected": [], "skipped": []}

    text = auto_editor.format_report(make_cfg(), report)
    assert "добор" not in text


def test_floor_flag_travels_to_the_assigned_slot(db, images_dir, monkeypatch):
    """Сквозная проводка: outcome подготовки → назначение слота → отчёт.
    Пропадёт звено — владелец перестанет видеть, что пост вышел добором."""
    from conftest import seed_post
    from test_auto_editor import add_story, at_moscow, make_cfg as auto_cfg
    from src import approval_bot, auto_editor

    add_story("s-floor", 29)
    seed_post("p-floor")

    def fake_generate(cfg_, story, borderline_ok=None, auto_floor=None):
        assert auto_floor == cfg_.auto_editorial_floor, "floor не дошёл до подготовки"
        return {"post_id": "p-floor", "outcome": "ok", "average": 8.33,
                "auto_floor": True, "paid_work": True,
                "threshold": 8.6, "weakest": [], "issues": []}

    monkeypatch.setattr(approval_bot, "_generate_from_digest", fake_generate)

    cfg = auto_cfg()
    report = auto_editor.fill_due_slots(cfg, at_moscow(8, 40))

    assert report["assigned"], report
    assert report["assigned"][0]["auto_floor"] is True
    assert "добор по floor" in auto_editor.format_report(cfg, report)


def test_floor_stays_inside_the_borderline_evidence(monkeypatch):
    """Floor не имеет права уехать ниже 8.0 без новых данных: текстов с
    оценкой ниже 8.0 в базе нет вовсе (промежуточные генерации не
    сохраняются), опускаться ниже не на чем. Осознанное решение владельца
    потребует поменять и этот тест — с новыми данными на руках."""
    monkeypatch.delenv("AUTO_EDITORIAL_FLOOR", raising=False)
    assert Config().auto_editorial_floor >= 8.0
