"""Судебный статус дела и юридический гейт.

Написано по двум РЕАЛЬНЫМ отказам 28.08.2026, когда корректные материалы
трижды не проходили гейт и владелец терял выбор впустую:

* DOJ, «Berkeley County Woman **Sentenced** for Defrauding Small Business
  Out of Nearly $300,000» — приговор вынесен, переписчик написал
  «Федеральный суд округа Беркли признал женщину виновной», гейт отказал;
* BBC, «**Thieves** steal Bronze Age artefacts from Spanish museum» —
  задержанных нет, переписчик написал «Неизвестные похитили артефакты»,
  гейт отказал.

Половина файла проверяет, что защита НЕ ослабла: там, где виновность не
установлена, каждое утверждение о вине блокируется ровно как раньше.
"""
from __future__ import annotations

import pytest

from src import case_status, legal_gate


# ---------------------------------------------------------------------------
# Определение статуса по тексту источника
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("title,body,expected", [
    # --- реальные заголовки, из-за которых всё это написано
    ("Berkeley County Woman Sentenced for Defrauding Small Business Out of Nearly $300,000",
     "A Martinsburg, West Virginia, woman will spend nearly four years in federal "
     "prison for defrauding a small business out of nearly $300,000.",
     case_status.SENTENCED),
    ("Thieves steal Bronze Age artefacts from Spanish museum in four-minute heist",
     "The Treasure of Villena is a unique trove of almost ten kilos of gold.",
     case_status.NO_SUSPECT),

    # --- шесть статусов, которые владелец потребовал различать
    ("Man arrested on suspicion of arson", "Police detained a 34-year-old.",
     case_status.SUSPECTED),
    ("Man charged with murder of neighbour", "He was indicted on Tuesday.",
     case_status.CHARGED),
    ("Businessman pleaded guilty to wire fraud", "He admitted the charges.",
     case_status.PLEADED_GUILTY),
    ("Jury finds former mayor guilty of bribery", "He was convicted on all counts.",
     case_status.CONVICTED),
    ("Fraudster jailed for eight years", "The judge sentenced him on Friday.",
     case_status.SENTENCED),
    ("Man acquitted of all charges after retrial", "The jury found him not guilty.",
     case_status.ACQUITTED),

    # --- русские источники
    ("Суд приговорил экс-чиновника к семи годам", "", case_status.SENTENCED),
    ("Задержан подозреваемый в серии краж", "", case_status.SUSPECTED),
    ("Обвиняемому предъявлено обвинение в мошенничестве", "", case_status.CHARGED),
    ("Суд оправдал фигуранта дела", "", case_status.ACQUITTED),
    ("Неизвестные вынесли из квартиры сейф", "", case_status.NO_SUSPECT),

    # --- ничего не понятно: строгий режим по умолчанию
    ("Museum reopens after renovation", "", case_status.UNKNOWN),
])
def test_status_detected_from_source(title, body, expected):
    assert case_status.detect(title, body) == expected


def test_acquittal_beats_accusation():
    """В материале есть и обвинение, и оправдание — значимо оправдание."""
    assert case_status.detect(
        "Man charged with fraud is acquitted after three-year trial",
        "He had been charged in 2023 but the jury found him not guilty.",
    ) == case_status.ACQUITTED


def test_every_status_has_russian_guidance():
    """Подсказка идёт в промпт переписчика — пустой она быть не может."""
    for status in (case_status.ACQUITTED, case_status.SENTENCED,
                   case_status.CONVICTED, case_status.PLEADED_GUILTY,
                   case_status.CHARGED, case_status.SUSPECTED,
                   case_status.NO_SUSPECT, case_status.UNKNOWN):
        assert len(case_status.guidance(status)) > 40
        assert case_status.title(status)


# ---------------------------------------------------------------------------
# Дело с приговором: гейт больше не требует «по версии следствия»
# ---------------------------------------------------------------------------

URL = "https://www.justice.gov/usao-ndwv/pr/berkeley-county-woman-sentenced"


def test_real_doj_sentence_now_passes():
    """Ровно тот текст, который гейт забраковал на третьей попытке."""
    text = (
        "Федеральный суд округа Беркли признал женщину из Мартинсберга виновной "
        "в обмане предприятия малого бизнеса.\n\n"
        "Ей назначено почти четыре года лишения свободы.\n\nПо данным DOJ."
    )
    gate = legal_gate.check_post(text, URL, case_status=case_status.SENTENCED)
    assert not gate.hard_fail, gate.issues


def test_court_verdict_with_words_in_between_passes():
    """«Суд ... признал ... виновной» — оговорка с разрывом между словами.

    Проверка подстрокой её не видела: обрубок «суд признал виновной»
    проходил, а правильная русская фраза с подлежащим — нет.
    """
    text = ("Городской суд Барселоны признал подсудимого виновным в хищении.\n\n"
            "Наказание — четыре года.\n\nПо данным BBC.")
    gate = legal_gate.check_post(text, "https://bbc.co.uk/news/1",
                                 case_status=case_status.CONVICTED)
    assert not gate.hard_fail, gate.issues


def test_sentenced_status_allows_plain_guilt_wording():
    """«Похитила со счетов» по делу с приговором — законная формулировка."""
    text = ("По материалам дела, осуждённая похитила со счетов бизнеса около "
            "300 тысяч долларов.\n\nПо данным DOJ.")
    gate = legal_gate.check_post(text, URL, case_status=case_status.SENTENCED)
    assert not gate.hard_fail, gate.issues


def test_pleaded_guilty_allows_admission_wording():
    text = ("Мужчина признал вину в переводе денег по поддельным реквизитам.\n\n"
            "По данным DOJ.")
    gate = legal_gate.check_post(text, URL, case_status=case_status.PLEADED_GUILTY)
    assert not gate.hard_fail, gate.issues


# ---------------------------------------------------------------------------
# Дело без задержанных: обвинять некого
# ---------------------------------------------------------------------------


def test_real_bbc_theft_now_passes():
    """Ровно тот текст, который гейт забраковал на первой попытке."""
    text = ("Неизвестные проникли в испанский музей и похитили артефакты "
            "бронзового века до приезда патруля.\n\nПо данным BBC.")
    gate = legal_gate.check_post(text, "https://bbc.co.uk/news/1",
                                 case_status=case_status.NO_SUSPECT)
    assert not gate.hard_fail, gate.issues


def test_event_headline_without_a_person_passes():
    """«Ограбление музея за четыре минуты» — название события, не обвинение."""
    gate = legal_gate.check_headline("Ограбление музея за четыре минуты",
                                     case_status.NO_SUSPECT)
    assert not gate.hard_fail, gate.issues


def test_named_person_is_still_protected_when_no_suspect():
    """Даже при «задержанных нет» имя в предложении возвращает строгость."""
    text = ("Неизвестные похитили коллекцию у Иванова, который сам и организовал "
            "кражу.\n\nПо данным BBC.")
    gate = legal_gate.check_post(text, "https://bbc.co.uk/news/1",
                                 case_status=case_status.NO_SUSPECT)
    assert gate.hard_fail


def test_label_is_still_blocked_when_no_suspect():
    """Ярлык «преступник» без приговора блокируется при любом статусе."""
    text = ("Преступник вынес из музея золото бронзового века.\n\nПо данным BBC.")
    gate = legal_gate.check_post(text, "https://bbc.co.uk/news/1",
                                 case_status=case_status.NO_SUSPECT)
    assert gate.hard_fail


# ---------------------------------------------------------------------------
# Защита НЕ ослабла: вина не установлена — правила прежние
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("status", [
    case_status.CHARGED, case_status.SUSPECTED, case_status.UNKNOWN, None,
])
def test_guilt_assertion_still_blocked_without_a_verdict(status):
    """Обвиняемый и подозреваемый — не осуждённые. Ровно как раньше."""
    text = ("Сергей Петров зарезал соседа во время ссоры.\n\nПо данным ТАСС.")
    gate = legal_gate.check_post(text, "https://tass.ru/1", case_status=status)
    assert gate.hard_fail, status


@pytest.mark.parametrize("status", [
    case_status.CHARGED, case_status.SUSPECTED, case_status.UNKNOWN, None,
])
def test_qualifier_still_required_and_still_works(status):
    text = ("По версии следствия, Сергей Петров зарезал соседа во время ссоры.\n\n"
            "По данным ТАСС.")
    gate = legal_gate.check_post(text, "https://tass.ru/1", case_status=status)
    assert not gate.hard_fail, gate.issues


def test_qualifier_from_another_sentence_still_does_not_help():
    """Оговорка должна стоять рядом с утверждением, а не где-то ниже."""
    text = ("Убийца задержан. Обвиняемый доставлен в суд.\n\nПо данным ТАСС.")
    gate = legal_gate.check_post(text, "https://tass.ru/1",
                                 case_status=case_status.CHARGED)
    assert gate.hard_fail


def test_status_cannot_be_faked_by_the_rewriter():
    """Статус приходит из ИСТОЧНИКА, а не из текста, который написала модель.

    Иначе модель, дописав «суд признал виновным», сама себе выдавала бы
    разрешение утверждать вину.
    """
    assert case_status.detect(
        "Man arrested over neighbour's death", "Police detained a suspect."
    ) == case_status.SUSPECTED


# ---------------------------------------------------------------------------
# Оправдание: защита УСИЛЕНА
# ---------------------------------------------------------------------------


def test_acquitted_person_cannot_be_called_guilty_even_with_qualifier():
    """«По версии следствия» не спасает: версия следствия отвергнута судом."""
    text = ("По версии следствия, мужчина убил соседа.\n\nПо данным BBC.")
    gate = legal_gate.check_post(text, "https://bbc.co.uk/news/1",
                                 case_status=case_status.ACQUITTED)
    assert gate.hard_fail
    assert any("ОПРАВДАЛ" in i for i in gate.issues)


def test_post_about_acquitted_must_mention_the_acquittal():
    """Материал, оставляющий одни обвинения, публиковать нельзя."""
    text = ("Мужчину задержали после гибели соседа, дело дошло до суда.\n\n"
            "По данным BBC.")
    gate = legal_gate.check_post(text, "https://bbc.co.uk/news/1",
                                 case_status=case_status.ACQUITTED)
    assert gate.hard_fail
    assert any("ОПРАВДАНИИ" in i for i in gate.issues)


def test_correct_acquittal_wording_passes():
    text = ("Мужчина обвинялся в убийстве соседа, но суд его оправдал: "
            "присяжные не нашли доказательств.\n\nПо данным BBC.")
    gate = legal_gate.check_post(text, "https://bbc.co.uk/news/1",
                                 case_status=case_status.ACQUITTED)
    assert not gate.hard_fail, gate.issues


def test_gate_and_detector_agree_on_status_names():
    """Гейт держит имена статусов своими строками — намеренно.

    Так ошибка в детекторе не может уронить гейт целиком. Но разъехаться
    эти два списка не должны: молча разъехавшись, они отключили бы либо
    послабление для дел с приговором, либо усиление для оправданных.
    """
    from src.legal_gate import (
        _ACQUITTED_STATUS, _ESTABLISHED_STATUSES, _NO_SUSPECT_STATUS,
    )

    assert _ESTABLISHED_STATUSES == case_status.ESTABLISHED
    assert _ACQUITTED_STATUS == case_status.ACQUITTED
    assert _NO_SUSPECT_STATUS == case_status.NO_SUSPECT


# ---------------------------------------------------------------------------
# Находки независимого ревью 28.08.2026. Каждая — путь к публикации клеветы.
# ---------------------------------------------------------------------------


def test_model_cannot_grant_itself_a_verdict():
    """Источник говорит «обвиняется» — модель не может дописать приговор.

    Переписчик, добавив «суд признал его виновным» к тексту про обвиняемого,
    выдавал себе разрешение утверждать вину. Статус приходит из источника,
    и слова модели его не перебивают.
    """
    text = ("Иванов украл деньги со счетов компании; суд признал его виновным.\n\n"
            "По данным ТАСС.")
    gate = legal_gate.check_post(text, "https://tass.ru/1",
                                 case_status=case_status.CHARGED)
    assert gate.hard_fail


def test_investigators_verdict_is_not_a_court_verdict():
    """Следствие никого виновным признать не может — это делает суд."""
    text = ("Следствие после проверки признало Иванова виновным: он украл "
            "деньги.\n\nПо данным ТАСС.")
    gate = legal_gate.check_post(text, "https://tass.ru/1",
                                 case_status=case_status.CHARGED)
    assert gate.hard_fail


def test_verdict_qualifier_does_not_leak_across_a_semicolon():
    """Обвинение третьего лица не прячется за приговором по другому делу.

    Гейт не умеет связывать приговор с конкретным человеком, поэтому при
    подтверждённом приговоре разрешение действует на весь пост. Но такое
    предложение обязано быть ВИДНО: владелец получает его предупреждением
    перед публикацией, а не узнаёт о нём из иска.

    До деления по «;» второе утверждение не попадало даже в предупреждения:
    оговорка из первой половины фразы гасила всю фразу целиком.
    """
    text = ("Суд по делу о мошенничестве признал Петрова виновным; Иванов "
            "убил Сидорова.\n\nПо данным ТАСС.")
    gate = legal_gate.check_post(text, "https://tass.ru/1",
                                 case_status=case_status.CONVICTED)
    assert any("Иванов убил Сидорова" in i for i in gate.issues), gate.issues


def test_third_party_accusation_is_blocked_when_no_verdict_in_source():
    """Без приговора в источнике то же предложение блокируется жёстко."""
    text = ("Суд по делу о мошенничестве признал Петрова виновным; Иванов "
            "убил Сидорова.\n\nПо данным ТАСС.")
    gate = legal_gate.check_post(text, "https://tass.ru/1",
                                 case_status=case_status.CHARGED)
    assert gate.hard_fail


def test_sentencing_reform_headline_is_not_a_verdict():
    """«Sentencing reform» — не приговор конкретному человеку."""
    assert case_status.detect(
        "Sentencing reform debated after Alex Ivanov is charged with fraud", ""
    ) == case_status.CHARGED


def test_prior_conviction_does_not_legalise_the_new_case():
    """Старая судимость не разрешает утверждать вину по новому обвинению."""
    status = case_status.detect(
        "Previously convicted Petrov charged with murder",
        "He was released from prison in 2024 and has now been charged.",
    )
    assert status not in case_status.ESTABLISHED

    text = "Петров убил человека.\n\nПо данным ТАСС."
    gate = legal_gate.check_post(text, "https://tass.ru/1", case_status=status)
    assert gate.hard_fail


def test_self_declared_innocence_is_not_an_acquittal():
    """«Считает себя невиновным» — не оправдательный приговор."""
    text = ("Иванов считает себя невиновным, но украл деньги.\n\n"
            "По данным ТАСС.")
    gate = legal_gate.check_post(text, "https://tass.ru/1",
                                 case_status=case_status.ACQUITTED)
    assert gate.hard_fail


def test_no_suspect_needs_an_explicit_anonymous_actor():
    """Отсутствия имени во фразе мало — нужен явный безличный исполнитель.

    «Иванов приехал в музей. Он похитил артефакты» — во второй фразе имени
    нет, но речь всё равно про Иванова.
    """
    text = ("Иванов приехал в музей. Он похитил артефакты бронзового века.\n\n"
            "По данным BBC.")
    gate = legal_gate.check_post(text, "https://bbc.co.uk/news/1",
                                 case_status=case_status.NO_SUSPECT)
    assert gate.hard_fail


def test_no_suspect_relaxation_does_not_apply_to_post_text_without_actor():
    """Без слова «неизвестные» послабление не работает даже в статусе no_suspect."""
    text = "Похитили десять килограммов золота из музея.\n\nПо данным BBC."
    gate = legal_gate.check_post(text, "https://bbc.co.uk/news/1",
                                 case_status=case_status.NO_SUSPECT)
    assert gate.hard_fail


def test_named_headline_is_still_blocked_when_no_suspect():
    """Послабление заголовка не действует, если в нём есть имя."""
    gate = legal_gate.check_headline("Ограбление музея устроил Иванов",
                                     case_status.NO_SUSPECT)
    assert gate.hard_fail


def test_label_headline_is_still_blocked_when_no_suspect():
    gate = legal_gate.check_headline("Грабитель вынес золото из музея",
                                     case_status.NO_SUSPECT)
    assert gate.hard_fail


def test_status_is_computed_from_source_not_from_the_draft():
    """Проводка: конвейер берёт статус из сырой новости, а не из текста поста."""
    import inspect

    from src import main as pipeline

    src = inspect.getsource(pipeline._process_item)
    assert "case_status_mod.detect(item.title, item.body)" in src
