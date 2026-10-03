"""Дешёвый отбор ДО обращения к модели: ищем СИЛУ ИСТОРИИ, а не тяжесть статьи.

Зачем вообще. Оценка релевантности — вызов модели на каждую новость из
ленты. Пока лент было три, это стоило копейки; со специализированными
источниками расходы росли бы линейно вслед за числом лент. Здесь ни сети,
ни модели — только регулярки по заголовку и описанию.

Что именно считаем. Не «насколько это формально криминальная новость» и не
«насколько солидный источник», а насколько это история, которую хочется
открыть: серийность, раскрытие спустя годы, судебная ошибка, неожиданный
способ раскрытия, побег, странный поворот, оплошность преступника.
Формальная криминальность баллов не даёт вообще — она только условие
допуска.

Два урока, оплаченных живыми прогонами 27.08.2026:

1. **Вес источника нельзя класть в общую сумму.** При +3 официальным лентам
   верх выдачи заняла рутина минюста США («Man Charged with Unlawfully
   Possessing a Firearm») — 19 слотов из 40. При +2 криминальному разделу
   издания то же самое сделали колонки и письма в редакцию: 22 слота из 30
   без единого сюжетного признака. Поэтому источник теперь различает только
   равных по сюжету (`tiebreak`), а порог проходит лишь материал, у которого
   есть хотя бы один сюжетный фактор.

2. **Рутина — штраф, а не отказ.** За стандартной юридической формулировкой
   может прятаться сильная история; два сюжетных фактора её вытянут.

Дальше по конвейеру всё равно работает модель — её дело судить о том, чего
регулярки не видят. Задача этого модуля: дёшево убрать заведомо не наше и
отсортировать остальное так, чтобы дорогая оценка досталась лучшим.
"""
from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Dict, List, Tuple

from src.normalize import NewsItem

# Балл жёсткого отказа: война, не-новость, не-криминал. Только объективные
# причины возвращают его, и возвращают НАПРЯМУЮ — суммой штрафов до −10
# дойти нельзя (рутина −5 плюс куцый заголовок −2 дают максимум −7, и это
# намеренный зазор). На зазоре держится допуск: PREFILTER_MIN_SCORE=−9
# означает «проходит всё, кроме жёстких отказов», а сюжетные факторы
# РАНЖИРУЮТ очередь на платную оценку, но не решают судьбу материала.
HARD_REJECT = -10

# ---------------------------------------------------------------------------
# Сюжетные факторы: за что материал получает баллы
# ---------------------------------------------------------------------------

FACTORS: List[Tuple[str, int, re.Pattern]] = [
    ("серийность", 5, re.compile(
        r"сери[йи]н|маньяк|несколько (?:жертв|убийств)|\d+ (?:жертв|убийств)|"
        r"serial (?:killer|killing|kill|rapist|arsonist|offender|attack)|"
        r"(?:killing|crime|shooting|stabbing) spree|multiple victims|"
        r"\b\d{2,} victims|killed (?:at least )?\d+|mass (?:shooting|murder|grave)", re.I)),

    ("висяк раскрыт", 5, re.compile(
        r"спустя \d+ (?:лет|год)|через \d+ лет|\d+ лет спустя|нераскрыт|висяк|"
        r"старое дело|архивн[оы]|возобновил[аи] дело|"
        r"cold case|unsolved|decades?[- ]old|\d+ years later|years after|"
        r"reopened|long[- ]dormant|solved after", re.I)),

    ("судебная ошибка", 5, re.compile(
        r"судебн[ао][йя] ошибк|оправдан|реабилитирован|невиновн|пересмотр (?:дела|приговора)|"
        r"wrongful(?:ly)? convict|exonerat|overturned|quashed|miscarriage of justice|"
        r"wrongly (?:jailed|imprisoned)|freed after \d+ years", re.I)),

    ("неожиданное раскрытие", 4, re.compile(
        r"\bднк\b|генеалог|генетическ|эксгум|отпечатк|случайн[ао] (?:улик|наш|попал)|"
        r"выдал[аои] себя|прокол|ошибк[ае] преступника|"
        r"\bdna\b|genetic genealogy|forensic breakthrough|exhum|fingerprint|"
        r"caught after|undone by|slip[- ]?up|mistake led|traced (?:through|via|by)|"
        r"chance (?:discovery|find)", re.I)),

    ("похищение, побег, охота", 4, re.compile(
        r"похищ|заложник|побег|сбежал|в розыске|скрывался|поимк|облав|"
        r"kidnap|abduct|hostage|manhunt|escape[ds]?|prison break|fugitive|"
        r"on the run|fled (?:to|abroad)|captured after", re.I)),

    ("необычный способ", 4, re.compile(
        r"отрави|расчлен|замуров|сжёг|закопал|кислот|канниб|мумифиц|"
        r"инсценир|подстро|двойн[аоы][яе] жизн|под чужим именем|"
        r"poison(?:ed|ing)|dismember|buried (?:the )?(?:body|victim|alive)|"
        r"(?:encased|buried) in concrete|body in a (?:freezer|suitcase|barrel)|"
        r"acid bath|cannibal|staged (?:the |a )?(?:scene|death|accident)|"
        r"faked (?:his|her|their) (?:own )?death|double life|"
        r"posed as|impersonat", re.I)),

    ("странный мотив, поворот", 4, re.compile(
        r"странн|неожиданн|загадочн|необычн|поворот|мистификац|"
        r"bizarre|strange|unusual|mysterious|twist|puzzling|"
        r"turned out|revealed to be|nobody suspected|hidden for", re.I)),

    ("оплошность, случайность", 3, re.compile(
        r"случайно|по ошибке|забыл|обронил|выронил|не учёл|прокололся|"
        r"almost|nearly|accidentally|by mistake|forgot|left behind|"
        r"dropped (?:his|her|their)|gave (?:himself|herself|themselves) away", re.I)),

    ("культ, картель, банда", 3, re.compile(
        r"секта|культ|картел|мафи|авторитет|главар|"
        r"cult|sect|cartel|mafia|kingpin|mastermind|crime (?:boss|lord)|"
        r"godfather|syndicate", re.I)),

    ("международный размах", 2, re.compile(
        r"крупнейш|беспрецедент|международн[аоы][яе] операц|\d+ стран|"
        r"largest|biggest|unprecedented|record[- ]breaking|"
        r"international operation|across (?:\w+ )?(?:countries|continents)|"
        r"dismantl(?:ed|ing) (?:a )?(?:major|international)", re.I)),

    ("человеческая драма", 2, re.compile(
        r"мать|отец|сын|дочь|вдов|семь[яию] (?:жертв|погибш)|выживш|"
        r"mother|father|daughter|\bson\b|widow|survivor|family of|"
        r"victim'?s? (?:family|mother|father)|life sentence|"
        r"whole life (?:order|term)", re.I)),
]

# Условие допуска: материал вообще про преступление. Баллов не даёт.
CRIME = re.compile(
    r"убийств|убил|похищ|мошенн|задержан|арестован|обвин|приговор|осужд|"
    r"подозрева|следстви|уголовн|розыск|труп|разбой|ограблен|краж|наркот|"
    r"банда|взятк|изнасил|педофил|поджог|вымогател|отмыван|контрабанд|"
    # Подлог, хищение, шантаж — тоже преступления, а в словаре их не было:
    # «Схема с поддельными завещаниями» отсеивалась как «не про преступление».
    r"подделк|поддельн|фальшив|подлог|афер|хищен|растрат|шантаж|"
    # Наркотики по-английски в словаре не было вовсе: «Cocaine worth £232m
    # seized» уходило в «не про преступление» (живой улов 30.08.2026).
    # heroin — с границей слова, иначе ловится heroine (героиня фильма).
    r"cocaine|heroin\b|fentanyl|methamphetamine|narcotic|opioid|"
    # Прошедшее время глаголов. Словарь знал причастия («задержан»,
    # «арестован»), но не «задержали»/«арестовали»; «стрельба» отсутствовала
    # вовсе. Живые пропуски 30.08: «В Одинцове мужчину задержали за стрельбу
    # из окна», «В Марселе при стрельбе погиб человек». Стемы покрывают
    # обе формы числа: «задержал» входит в «задержали».
    r"задержал|арестовал|осудил|похит|украл|ограбил|стрельб|застрел|"
    r"murder|homicide|manslaughter|killed|kidnap|fraud|scam|launder|traffick|"
    r"smuggl|cartel|gang|racketeer|extort|briber|arrest|indict|charged|"
    r"convict|sentenc|guilty|robbery|burglar|heist|theft|stolen|assault|"
    r"shooting|stabb|arson|fugitive|suspect|forensic|abuse|jail|prison",
    re.I)

# Война и политика — не наш жанр, даже когда там есть «похищение» и «удар».
# Без этого сводки с фронта лезут наверх через криминальную лексику в тексте.
# Только собственно война. Слова «президент», «министр», «выборы»,
# «санкции» отсюда убраны намеренно: взятка министра или дело о хищении на
# выборах — это ровно наш жанр, а фильтр их резал.
WAR = re.compile(
    r"хамас|цахал|сектор газа|\bвсу\b|минобороны|обстрел|ракетн[ыо]|"
    r"\bбпла\b|линия фронта|наступлени|перемири|мобилизац|военкомат|"
    r"hamas|\bidf\b|gaza strip|russian (?:forces|army|troops)|"
    r"missile (?:strike|attack)|drone strike|airstrike|air strike|"
    r"ceasefire|front line|shelling|nato (?:forces|troops)|"
    r"military (?:operation|strike|offensive)", re.I)

# Рутина: юридически важное, но нерассказываемое. ШТРАФ, не отказ.
ROUTINE = re.compile(
    r"felon in possession|unlawfully possessing a firearm|possession of a firearm|"
    r"armed career criminal|illegal (?:reentry|re-entry|alien)|"
    r"pleads? guilty to (?:possession|distribut|conspiracy)|"
    r"drug (?:trafficking|distribution) conspiracy|possession with intent|"
    r"child (?:pornography|sexual abuse material)|"
    r"tax (?:fraud|evasion)|medicare fraud|health ?care fraud|wire fraud scheme|"
    r"aggravated identity theft|money laundering conspiracy|"
    r"seized \d+ ?(?:kg|kilos|tonnes|tons)|indicted on|"
    r"charged with (?:drug|firearm)|"
    r"sentenced to \d+ (?:months|years) for (?:drug|firearm|fraud)|"
    r"изъяли \d+ (?:кг|тонн)",
    re.I)

# Не новость вовсе.
NOISE = re.compile(
    r"подкаст|вебинар|олимпиад|регистрац|конкурс|стипенди|ваканс|назначен|"
    r"поздравл|годовщин|конференци|форум|семинар|выставк|футбол|хоккей|матч|"
    r"чемпионат|погод|прогноз|курс валют|"
    r"podcast|webinar|newsletter|subscribe|appoint(?:ed|ment)|nominat|"
    r"anniversar|award|scholarship|internship|vacanc|conference|summit|"
    r"workshop|working arrangement|annual report|report (?:released|launch)|"
    r"campaign (?:launch|to protect|warning)|statement (?:on|by)|remarks (?:by|at)|"
    r"speech|testimony before|football|olympic|weather|forecast|review:|"
    r"strengthen cooperation|new report on",
    re.I)

# Узкий список формулировок, которые САМИ доказывают, что речь о
# преступлении. Только они пускают материал без единого слова из
# криминального словаря — иначе гейт обходят «Concrete plans approved»,
# «Shopping spree begins» и «Village mystery solved after 20 years»:
# все три ловились широкими регулярками сюжетных факторов.
CRIME_PROOF = re.compile(
    r"серийн[а-я]* (?:убийц|маньяк|насильник)|маньяк|"
    r"нераскрыт[а-я]* (?:убийств|дел|преступлен)|висяк|"
    r"судебн[ао][йя] ошибк|реабилитирован посмертно|"
    r"расчлен|замуров|отравил|"
    r"serial (?:killer|killings|rapist|murder)|"
    r"cold case (?:murder|killing|homicide|investigation)?|"
    r"unsolved (?:murder|killing|homicide|case|crime)|"
    r"wrongful(?:ly)? convict|exonerat|miscarriage of justice|"
    r"dismember|buried (?:the )?(?:body|victim)|poisoned (?:his|her|the)",
    re.I)

BODY_LIMIT = 600


@dataclass
class Verdict:
    item: NewsItem
    score: int
    reasons: List[str] = field(default_factory=list)


def score_item(item: NewsItem) -> Verdict:
    """Сила истории. Вес источника сюда НЕ входит — см. `tiebreak`."""
    title = item.title or ""
    body = (item.body or "")[:BODY_LIMIT]
    text = f"{title} {body}"
    reasons: List[str] = []

    # Война ищется и в тексте: «Kidnapped hostages freed after manhunt» с
    # телом про сектор Газа проходило как криминальная история.
    if WAR.search(text):
        return Verdict(item, HARD_REJECT, ["война, не криминал"])

    # Подкаст, отчёт, конференция, награждение — это не новость вообще, и
    # никакой сюжет внутри не делает её новостью. Штрафа здесь мало: с ним
    # «Podcast: Cold case murder solved after 30 years» набирал проходной
    # балл. Рутинная СВОДКА — другое дело, там остаётся штраф.
    if NOISE.search(title):
        return Verdict(item, HARD_REJECT, ["не новость, а анонс или отчёт"])

    total = 0
    for name, weight, rx in FACTORS:
        if rx.search(title):
            total += weight
            reasons.append(f"{name} +{weight}")
        elif rx.search(body):
            half = max(weight // 2, 1)
            total += half
            reasons.append(f"{name} (в тексте) +{half}")

    # Криминальный словарь — условие допуска, но не единственное. Заголовок
    # «Cold case from 1998 solved after DNA breakthrough» не содержит ни
    # одного слова из словаря, а это ровно наш жанр.
    #
    # Пускаем без словаря только по факторам, которые САМИ про преступление.
    # «Побег» и «странный поворот» для этого не годятся: на живом батче они
    # протащили «сотрудники ТЦК сбежали в Молдавию» и новость МИД Непала.
    if not CRIME.search(text) and not CRIME_PROOF.search(title):
        return Verdict(item, HARD_REJECT, ["не про преступление"])

    if ROUTINE.search(text):
        total -= 5
        reasons.append("рутинная сводка −5")

    if len(title) < 25:
        total -= 2
        reasons.append("куцый заголовок −2")

    return Verdict(item, total, reasons)


def tiebreak(item: NewsItem, weights: Dict[str, int]) -> int:
    """Вес источника — ТОЛЬКО различение равных по сюжету.

    Официальность источника это сигнал доверия, а не интересности истории.
    Дважды проверено на живых прогонах: как только вес попадает в основную
    сумму, он начинает протаскивать наверх материалы без единого сюжетного
    признака.
    """
    return weights.get(item.source, 0)


def select(
    items: List[NewsItem],
    weights: Dict[str, int],
    min_score: int,
    limit: int,
    quota_share: float = 0.4,
) -> Tuple[List[Verdict], List[Verdict]]:
    """Делит батч на «отдать модели» и «отсеяно», не тратя ни цента.

    Три ограничения подряд:
      1. порог — проходит только материал с сюжетом;
      2. квота на источник — ни одна лента не занимает больше `quota_share`
         от потолка, иначе один говорливый источник забивает весь список;
      3. потолок — жёсткая граница числа платных вызовов за прогон.

    Возвращает (отобранные, отсеянные) — сумма всегда равна входу.
    """
    verdicts = [score_item(it) for it in items]
    verdicts.sort(key=lambda v: (-v.score, -tiebreak(v.item, weights), v.item.title))

    passed = [v for v in verdicts if v.score >= min_score]
    dropped = [v for v in verdicts if v.score < min_score]

    # На малых потолках доля вырождается: int(4 × 0.4) = 1, то есть один
    # материал на источник вместо ожидаемых двух. Округляем вверх, а при
    # совсем маленьком списке квоту не применяем вовсе — делить нечего.
    quota = limit if limit <= 2 else max(math.ceil(limit * quota_share), 1)
    taken: List[Verdict] = []
    per_source: Dict[str, int] = {}
    for verdict in passed:
        if len(taken) >= limit:
            dropped.append(verdict)
            continue
        source = verdict.item.source
        if per_source.get(source, 0) >= quota:
            verdict.reasons.append(f"квота источника {quota}")
            dropped.append(verdict)
            continue
        per_source[source] = per_source.get(source, 0) + 1
        taken.append(verdict)

    return taken, dropped
