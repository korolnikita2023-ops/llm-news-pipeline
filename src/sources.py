"""Реестр источников новостей: что тянем, зачем и как это выключить.

Раньше источники были плоским списком URL в конфиге. Пока лент было три,
этого хватало; с появлением специализированных криминальных источников
списка мало — нужно знать про каждый: официальный он или журналистский,
ищем мы им сюжеты или подтверждаем найденное, можно ли его выключить одной
строкой в `.env`, и какой у него вес в дешёвом префильтре.

Два концептуальных слоя:

    discovery    — здесь мы НАХОДИМ сюжет. Специализированные ленты
                   правоохранителей и криминальные разделы изданий:
                   концентрация полезного высокая, объём небольшой.
    verification — здесь мы ПОДТВЕРЖДАЕМ и дополняем. Большие агентства
                   (BBC, ТАСС, Интерфакс): общий поток, зато проверяемость
                   и знакомое читателю имя в строке источника.

Сейчас обе группы одинаково идут в ingest — verification-ленты остаются
рабочим источником кандидатов и страховкой на случай, если discovery-слой
выключат. Отдельный шаг «найти вторую ссылку на тот же сюжет» здесь НЕ
делается, но поле `role` и происхождение каждой записи сохраняются, чтобы
его можно было добавить позже, ничего не переписывая.

Выключение без правки кода (`.env`):

    DISCOVERY_ENABLED=false          — выключить весь discovery-слой
    SOURCES_DISABLED=Europol,NCA     — выключить отдельные источники по имени

Каждый источник проверен запросом с сервера 27.08.2026. Недоступное
с этого IP в реестр внесено со статусом `default_on=False` и комментарием
почему. Доступность лент зависит от IP и меняется — проверяйте сами.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List

ROLE_DISCOVERY = "discovery"
ROLE_VERIFICATION = "verification"

KIND_OFFICIAL = "official"
KIND_MEDIA = "media"


@dataclass(frozen=True)
class Source:
    name: str            # короткое имя, им же выключается в SOURCES_DISABLED
    url: str
    role: str            # discovery | verification
    kind: str            # official | media
    lang: str            # ru | en
    # Бонус в дешёвом отборе. Это НЕ «авторитетность источника», а оценка
    # вероятности, что материал этой ленты стоит времени модели:
    #   официальные (1) — там криминал ВЕСЬ, поэтому криминальное слово в
    #       заголовке ничего не сообщает; большой бонус выносил рутинную
    #       федеральную сводку выше сюжетной журналистики (проверено на
    #       живом прогоне 27.08: 19 из 40 слотов заняла рутина DOJ);
    #   криминальный раздел издания (2) — редактор уже отобрал материал по
    #       интересности, это дорогого стоит;
    #   общая лента (0) — криминальное слово там редкость и потому сигнал
    #       сам по себе.
    weight: int = 0
    default_on: bool = True
    note: str = ""

    @property
    def is_discovery(self) -> bool:
        return self.role == ROLE_DISCOVERY


# ---------------------------------------------------------------------------
# Реестр
# ---------------------------------------------------------------------------

REGISTRY: List[Source] = [
    # --- discovery, официальные -------------------------------------------
    Source(
        name="DOJ",
        url="https://www.justice.gov/news/rss?type=press_release",
        role=ROLE_DISCOVERY, kind=KIND_OFFICIAL, lang="en", weight=1,
        note="Пресс-релизы минюста США и федеральных прокуратур. Проверено: "
             "25 записей за запрос, 84% криминальных, все свежие (медиана 0 дней), "
             "у каждой ссылка, дата и текст. Фильтр по component сайтом игнорируется — "
             "лента общая по всем подразделениям.",
    ),
    Source(
        name="Europol",
        url="https://www.europol.europa.eu/rss.xml",
        role=ROLE_DISCOVERY, kind=KIND_OFFICIAL, lang="en", weight=1,
        note="Международные операции ЕС: раскрытые сети, аресты, изъятия. "
             "10 записей, БЕЗ ДАТ в ленте — свежесть определяем журналом "
             "виденного, дата поста берётся из времени забора.",
    ),
    Source(
        name="NCA",
        url="https://www.nationalcrimeagency.gov.uk/news?format=feed&type=rss",
        role=ROLE_DISCOVERY, kind=KIND_OFFICIAL, lang="en", weight=1,
        note="Национальное агентство по борьбе с преступностью Великобритании: "
             "оргпреступность, отмывание, трафик. 20 записей, 60% криминальных, "
             "лента медленная (медиана 33 дня) — берём качеством, не объёмом.",
    ),

    # --- discovery, криминальные разделы изданий ---------------------------
    Source(
        name="Guardian-crime",
        url="https://www.theguardian.com/uk/ukcrime/rss",
        role=ROLE_DISCOVERY, kind=KIND_MEDIA, lang="en", weight=2,
        note="Криминальный раздел Guardian: сюжетные материалы, а не сводки. "
             "20 записей, медиана 6 дней, полные описания.",
    ),
    Source(
        name="Guardian-law",
        url="https://www.theguardian.com/law/rss",
        role=ROLE_DISCOVERY, kind=KIND_MEDIA, lang="en", weight=2,
        default_on=False,
        note="Суды и правосудие: линия «как работает и ошибается следствие». "
             "20 записей, медиана 3 дня. ВЫКЛЮЧЕН на первом развёртывании: "
             "криминального содержания только 30% — сначала смотрим "
             "фактический улов четырёх сильных источников. Включить: SOURCES_ENABLED=Guardian-law.",
    ),

    # --- verification / общий поток (существующие) -------------------------
    Source(
        name="BBC",
        url="http://feeds.bbci.co.uk/news/world/rss.xml",
        role=ROLE_VERIFICATION, kind=KIND_MEDIA, lang="en", weight=0,
        note="Мировая лента BBC. Общий поток, криминального в нём мало, "
             "зато это узнаваемое имя в строке источника.",
    ),
    Source(
        name="TASS",
        url="https://tass.ru/rss/v2.xml",
        role=ROLE_VERIFICATION, kind=KIND_MEDIA, lang="ru", weight=0,
        note="ТАСС, общая лента. Основной поставщик российских сюжетов.",
    ),
    Source(
        name="Interfax",
        url="https://www.interfax.ru/rss.asp",
        role=ROLE_VERIFICATION, kind=KIND_MEDIA, lang="ru", weight=0,
        note="Интерфакс, общая лента.",
    ),

    # --- исследовательский список: проверено, но НЕ включено ---------------
    Source(
        name="RAPSI",
        url="http://rapsinews.ru/export/rss2/archive/index.xml",
        role=ROLE_DISCOVERY, kind=KIND_MEDIA, lang="ru", weight=1,
        default_on=False,
        note="РАПСИ: суды и приговоры на русском, 80 записей, все свежие. "
             "Но криминального только 35% — остальное правовые новости и "
             "пресс-релизы. Включать по желанию владельца: "
             "SOURCES_DISABLED убрать/добавить имя RAPSI.",
    ),
]


# Недоступное с сервера, на котором проверялось, — в реестр НЕ вносим, чтобы не делать вид,
# что источник есть. Здесь список с причинами: пригодится, если появится
# прокси или сменится хостинг.
UNREACHABLE = {
    "FBI National Press Releases": "HTTP 403 с IP дата-центра, HTTP 200 с домашнего IP — "
                                   "блокировка дата-центров на стороне CDN. Большинство тех же дел "
                                   "приходит через DOJ.",
    "FBI Top Stories / Wanted": "HTTP 403, та же блокировка.",
    "FBI field offices": "Отдельных лент по офисам на fbi.gov сейчас нет (все проверенные URL 403/404).",
    "INTERPOL": "HTTP 503 и с сервера, и с домашнего IP — публичной рабочей RSS-ленты нет.",
    "US Marshals / DEA / ATF": "HTTP 403 с обоих IP.",
    "DOJ Criminal Division отдельно": "Параметр component на justice.gov/news/rss игнорируется — "
                                      "отдаётся общая лента пресс-релизов (она и подключена).",
    "Eurojust": "Лента отвечает 200, но записей 0.",
    "СК РФ, МВД РФ, ФСИН": "403 / таймаут — российские госсайты не отдают ленту зарубежному IP.",
    "Коммерсантъ (крим. раздел)": "HTTP 403.",
    "РИА, Лента, Фонтанка (крим. разделы)": "404 / 0 записей / HTTP 428.",
    "Google News RSS по запросам": "Технически работает, но это агрегатор — "
                                   "иерархия источников проекта запрещает уровень D.",
}


def _disabled_names(cfg) -> set:
    raw = str(getattr(cfg, "sources_disabled", "") or "")
    return {n.strip().lower() for n in raw.split(",") if n.strip()}


def enabled_sources(cfg) -> List[Source]:
    """Источники, из которых реально тянем в этом прогоне.

    Порядок проверок: выключен ли слой discovery целиком → выключен ли
    источник поимённо → включён ли он по умолчанию.
    """
    disabled = _disabled_names(cfg)
    discovery_on = bool(getattr(cfg, "discovery_enabled", True))

    out: List[Source] = []
    for src in REGISTRY:
        if not src.default_on and src.name.lower() not in _forced_on(cfg):
            continue
        if src.name.lower() in disabled:
            continue
        if src.is_discovery and not discovery_on:
            continue
        out.append(src)
    return out


def _forced_on(cfg) -> set:
    """Источники из исследовательского списка, включённые вручную:
    SOURCES_ENABLED=RAPSI в .env."""
    raw = str(getattr(cfg, "sources_enabled", "") or "")
    return {n.strip().lower() for n in raw.split(",") if n.strip()}


def by_name(name: str) -> Source | None:
    for src in REGISTRY:
        if src.name.lower() == (name or "").lower():
            return src
    return None


def weights() -> Dict[str, int]:
    return {src.name: src.weight for src in REGISTRY}
