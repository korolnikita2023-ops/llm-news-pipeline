"""Судебный статус дела, прочитанный из САМОГО ИСТОЧНИКА.

Зачем это нужно. Юридический гейт требует оговорки «по версии следствия»
рядом с любым утверждением о виновности — и правильно делает, пока
виновность не установлена судом. Но когда источник прямо говорит
`sentenced`, `convicted`, `pleaded guilty`, виновность УЖЕ установлена, и
требовать оговорку не только незачем, но и неверно: «по версии следствия
приговорённый похитил деньги» — это плохая журналистика, а не осторожная.

Два реальных случая, из-за которых это написано:

* DOJ, «Berkeley County Woman **Sentenced** for Defrauding Small Business»,
  в теле — «will spend nearly four years in federal prison». Приговор есть.
  Переписчик написал «Федеральный суд округа Беркли признал женщину
  виновной» — юридически безупречно, а гейт трижды забраковал.
* BBC, «**Thieves** steal Bronze Age artefacts from Spanish museum» — ни
  одного задержанного, ни одного обвиняемого. Переписчик написал
  «Неизвестные похитили артефакты», и это тоже забраковали: клеветать
  здесь не на кого, лица нет вообще.

Определение — правилами по ключевым словам, без обращения к модели: статус
читается из заголовка и первых строк, где новостные агентства его и
называют. Ошибка в сторону строгости безопасна: непонятный статус даёт
`UNKNOWN`, а на нём гейт работает ровно так же строго, как раньше.
"""
from __future__ import annotations

import re

# Порядок важен: он же порядок приоритета при проверке.
ACQUITTED = "acquitted"            # оправдан — говорить о вине НЕЛЬЗЯ вообще
SENTENCED = "sentenced"            # приговорён
CONVICTED = "convicted"            # суд признал виновным
PLEADED_GUILTY = "pleaded_guilty"  # признал вину
CHARGED = "charged"                # предъявлено обвинение
SUSPECTED = "suspected"            # подозревается, задержан
NO_SUSPECT = "no_suspect"          # лица нет: «неизвестные», «воры»
UNKNOWN = "unknown"

# Статусы, при которых виновность установлена судом или признана самим
# человеком. Только они снимают требование оговорки.
ESTABLISHED = frozenset({SENTENCED, CONVICTED, PLEADED_GUILTY})

TITLES = {
    ACQUITTED: "оправдан судом",
    SENTENCED: "приговорён",
    CONVICTED: "суд признал виновным",
    PLEADED_GUILTY: "признал вину",
    CHARGED: "предъявлено обвинение",
    SUSPECTED: "подозревается",
    NO_SUSPECT: "лицо не установлено",
    UNKNOWN: "статус дела не назван",
}

# Как об этом статусе ПРАВИЛЬНО писать по-русски. Идёт в промпт переписчика,
# чтобы он с первой попытки писал юридически корректно, а не подбирал
# формулировку через отказы гейта — каждый отказ стоит денег.
RU_GUIDANCE = {
    ACQUITTED: (
        "Суд ОПРАВДАЛ этого человека. Называть его виновным нельзя ни в "
        "какой форме. Пиши: «обвинялся», «суд оправдал», «оправдательный "
        "приговор». Об оправдании обязательно скажи в тексте — иначе пост "
        "выглядит обвинением человека, которого суд признал невиновным."
    ),
    SENTENCED: (
        "Суд вынес ПРИГОВОР. Писать о вине можно прямо: «приговорён к…», "
        "«суд признал виновным», «осуждён за…». Оговорка «по версии "
        "следствия» здесь НЕ нужна и будет ошибкой."
    ),
    CONVICTED: (
        "Суд ПРИЗНАЛ ВИНОВНЫМ. Писать о вине можно прямо: «суд признал "
        "виновным», «осуждён». Оговорка «по версии следствия» не нужна."
    ),
    PLEADED_GUILTY: (
        "Человек ПРИЗНАЛ ВИНУ. Пиши «признал вину», «согласился с "
        "обвинением». Приговор мог быть ещё не вынесен — не пиши "
        "«приговорён», если об этом не сказано в источнике."
    ),
    CHARGED: (
        "Предъявлено ОБВИНЕНИЕ, суда ещё не было. Только «обвиняемый», "
        "«по версии следствия», «по версии обвинения». Виновным не называй."
    ),
    SUSPECTED: (
        "Человек ПОДОЗРЕВАЕТСЯ, обвинение может быть не предъявлено. "
        "Только «подозреваемый», «задержан», «по версии следствия»."
    ),
    NO_SUSPECT: (
        "Никто не задержан и не назван. Пиши безлично: «неизвестные», "
        "«злоумышленники», «нападавшие». Имён и ярлыков вроде «преступник» "
        "не используй — приписывать поступок конкретному лицу здесь не к кому."
    ),
    UNKNOWN: (
        "Судебный статус в источнике не назван. Считай виновность "
        "НЕустановленной: «по версии следствия», «подозреваемый»."
    ),
}


def _any(text: str, patterns) -> bool:
    return any(re.search(p, text) for p in patterns)


# Английские и русские маркеры. Ищутся по нормализованному нижнему регистру.
_ACQUITTED = (
    r"\bacquitt", r"\bfound not guilty\b", r"\bcleared of\b",
    r"\bnot guilty verdict\b", r"\bcharges dropped\b", r"\bexonerat",
    # «оправдал», «оправдана», «оправдательный» — одним корнем: суд
    # оправдывает в любой из этих форм, и пропустить хоть одну нельзя.
    r"оправда(?:л|н|ть|ни|в)", r"снял[иа]? обвинени", r"дело прекращен",
)
# Только конструкции о наказании, назначенном КОНКРЕТНОМУ человеку.
# Голый корень «sentenc» давал `sentenced` на заголовке «Sentencing reform
# debated after Alex Ivanov is charged with fraud» — приговора нет, а гейт
# после такого статуса разрешил бы утверждать вину обвиняемого.
_SENTENCED = (
    r"\bsentenced\b", r"\bwas sentenced\b", r"\bsentenced to\b",
    r"\bjailed for\b", r"\bjailed\b", r"\bimprisoned\b",
    r"\bgets? \d+ years?\b",
    r"\bwill spend .{0,30}\b(?:years?|months?) in (?:federal )?prison\b",
    r"\bhanded (?:a )?(?:\d+[- ]year )?(?:jail|prison) (?:term|sentence)\b",
    r"\bgiven a life sentence\b", r"\breceived a .{0,20}sentence\b",
    r"приговор[её]н", r"приговорил", r"вынес приговор", r"осужд[её]н", r"осуждена",
)
_CONVICTED = (
    r"\bconvict", r"\bfound guilty\b", r"\bguilty verdict\b",
    r"\bjury (?:found|convicted)\b",
    r"признан виновн", r"признана виновн", r"признали виновн", r"суд признал",
)
_PLEADED = (
    r"\bplead(?:ed|s|ing)? guilty\b", r"\badmitted (?:to )?(?:the )?(?:charges?|offence)",
    r"\bguilty plea\b", r"признал[аи]? вину", r"признал[аи]? свою вину",
)
_CHARGED = (
    r"\bcharged with\b", r"\bindicted\b", r"\bindictment\b", r"\bfaces charges\b",
    r"\bprosecutors? (?:have )?charged\b", r"\bappeared in court charged\b",
    r"обвиняем", r"предъявлен[ыо] обвинени", r"обвиняется", r"уголовное дело против",
)
_SUSPECTED = (
    r"\bsuspect", r"\barrested\b", r"\bdetained\b", r"\bin custody\b",
    r"\bunder investigation\b", r"\bpolice are questioning\b",
    r"подозрева", r"задержан", r"разыскива",
)
# Безличный исполнитель: действие описано, лица нет.
_ANONYMOUS = (
    r"\bthieves\b", r"\bthief\b", r"\brobbers?\b", r"\bburglars?\b",
    r"\braiders?\b", r"\battackers?\b", r"\bunknown (?:men|persons?|assailants?)\b",
    r"\bunidentified\b", r"\bno arrests?\b", r"\bmanhunt\b", r"\bat large\b",
    r"неизвестн", r"злоумышленник", r"нападавш", r"личность не установлена",
)


# Признаки СТАРОЙ судимости. «Ранее судимый Петров обвиняется в убийстве»
# содержит слово «convicted», но относится оно к прошлому делу, а не к
# нынешнему обвинению. Без этой проверки статус получался `convicted`, и
# гейт разрешал утверждать вину по НОВОМУ, ещё не рассмотренному делу.
_PRIOR_CONVICTION = (
    r"\bpreviously convicted\b", r"\bprior convictions?\b",
    r"\bpast convictions?\b", r"\bconvicted (?:sex offender|killer|criminal)\b",
    r"\breleased from (?:jail|prison)\b", r"\bon parole\b", r"\bex-convict\b",
    r"ранее судим", r"ранее осужд", r"освободился из колонии",
)

_ORDER = (
    (ACQUITTED, _ACQUITTED),
    (SENTENCED, _SENTENCED),
    (CONVICTED, _CONVICTED),
    (PLEADED_GUILTY, _PLEADED),
    (CHARGED, _CHARGED),
    (SUSPECTED, _SUSPECTED),
)


def _strongest(text: str) -> str | None:
    for status, patterns in _ORDER:
        if _any(text, patterns):
            return status
    return None


def _weakest(text: str) -> str | None:
    for status, patterns in reversed(_ORDER):
        if _any(text, patterns) and status != ACQUITTED:
            return status
    return None


def detect(title: str, body: str = "") -> str:
    """Судебный статус дела по тексту источника.

    Оправдание проверяется первым: если в материале есть и обвинение, и
    оправдание, юридически значимо именно оправдание.

    Заголовок весомее тела: новостной заголовок называет СЕГОДНЯШНЕЕ
    событие, а в теле рядом лежат обстоятельства прошлых дел. Если статус
    читается уже из заголовка, тело не смотрим.

    Упоминание прошлой судимости роняет статус до самого слабого из
    найденных: «ранее судимый Петров обвиняется в убийстве» — это
    обвинение, а не приговор, и вину утверждать нельзя.
    """
    head = (title or "").lower()
    full = f"{title or ''} {body or ''}".lower()

    for text in (head, full):
        if not text.strip():
            continue
        status = _strongest(text)
        if status is None:
            continue
        if status in ESTABLISHED and _any(full, _PRIOR_CONVICTION):
            # Ошибка в сторону строгости: непонятно, к какому делу
            # относится приговор, — считаем вину неустановленной.
            return _weakest(text) or SUSPECTED
        return status

    # Ни одного признака судебного производства. Если при этом исполнитель
    # назван безлично — лица в материале нет, и клеветать не на кого.
    if _any(full, _ANONYMOUS):
        return NO_SUSPECT
    return UNKNOWN


def guidance(status: str) -> str:
    return RU_GUIDANCE.get(status, RU_GUIDANCE[UNKNOWN])


def title(status: str) -> str:
    return TITLES.get(status, TITLES[UNKNOWN])
