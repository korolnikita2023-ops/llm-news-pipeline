"""Telegram News Bot. Главный оркестратор.

Поток:
    ingest (GDELT+RSS, или sample в dry-run) → normalize → dedupe L1 (MinHash)
    → relevance (keyword mock / LLM) → legal gate PRE → rewrite RU
    → legal gate POST → editorial scorer (regenerate до MAX_REGENERATE_ATTEMPTS)
    → image card → storage (SQLite) → publisher (draft markdown / live)

Запуск:
    python -m src.main --dry-run          # sample-данные, без сети, без Telegram
    python -m src.main                    # реальный ingest (GDELT/RSS) — требует
                                           # сети; публикация всё равно зависит
                                           # от PUBLISH_MODE в .env, не от --dry-run

NO SILENT FAILURES: каждая стадия печатает явный статус. Непредвиденное
исключение внутри стадии не глотается — процесс падает с ненулевым кодом,
чтобы сбой не остался незамеченным (в проде это должно быть подключено к
алертам).
"""
from __future__ import annotations

import argparse
import json
import sys
import uuid
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from typing import List

from config import DB_PATH, DRAFTS_DIR, LOGS_DIR, SAMPLE_NEWS_JSON, get_config, ensure_dirs
from src import legal_gate, storage
from src.dedupe_l1 import dedupe, drop_cross_source_repeats
from src.image_card import render_card
from src.llm_client import LLMBudgetExceeded
from src.normalize import NewsItem, normalize_batch
from src.publisher import write_draft_markdown
from src.relevance import score_and_filter
from src.rewrite import generate_draft
from src.scorer import score_draft

# Windows-консоль по умолчанию открывает stdout/stderr в cp1252/cp866, что
# ломается на кириллице и юникодных стрелках/эмодзи в логах. Переключаем на
# UTF-8 максимально рано — иначе первый же print с "→"/"·" валит процесс
# UnicodeEncodeError, а это и есть тихий сбой ровно там, где логика верна.
for _stream in (sys.stdout, sys.stderr):
    if hasattr(_stream, "reconfigure"):
        try:
            _stream.reconfigure(encoding="utf-8")
        except Exception:
            pass


def parse_args(argv: List[str] | None = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Telegram News Bot")
    p.add_argument(
        "--dry-run", action="store_true",
        help="Использовать data/sample_news.json вместо реального ingest; "
             "не отправлять в Telegram независимо от PUBLISH_MODE.",
    )
    p.add_argument(
        "--discovery-only", action="store_true",
        help="Забрать источники, отсеять дёшево и показать, ЧТО ушло бы в "
             "платную оценку и почём. Ни одного обращения к модели, ни одной "
             "записи в базу — способ посмотреть улов новых лент перед "
             "включением их в обычный прогон.",
    )
    return p.parse_args(argv)


def _too_old(item: NewsItem, max_age_hours: int) -> bool:
    """Новость старше потолка по дате публикации из ленты.

    Дата отсутствует — считаем новость свежей: ленты без дат (Europol)
    нельзя выбрасывать целиком, а их записи попадают на оценку в первый же
    прогон после появления.

    Форматы: RSS отдаёт RFC 822 («Thu, 20 Aug 2026 12:38:34 +0000») — его
    пробуем ПЕРВЫМ, это формат всех боевых лент; ISO 8601 — запасной
    (sample-данные, БД). Первая версия парсила только ISO и молча считала
    все боевые записи свежими — фильтр был no-op, нашло независимое ревью.
    Нечитаемая дата логируется громко (NO SILENT FAILURES), новость при
    этом проходит: резать материал за кривое поле ленты нельзя.
    """
    raw = (item.published_at or "").strip()
    if not raw:
        return False
    published = None
    try:
        published = parsedate_to_datetime(raw)
    except (TypeError, ValueError):
        try:
            published = datetime.fromisoformat(raw.replace("Z", "+00:00"))
        except ValueError:
            print(f"[main] дата публикации не разобралась: {raw!r} "
                  f"({item.source}) — считаю новость свежей")
            return False
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - published > timedelta(hours=max_age_hours)


def _load_sample_news() -> List[dict]:
    if not SAMPLE_NEWS_JSON.exists():
        raise SystemExit(f"[main] Нет файла с sample-данными: {SAMPLE_NEWS_JSON}")
    raw = json.loads(SAMPLE_NEWS_JSON.read_text(encoding="utf-8"))
    if not raw:
        raise SystemExit(f"[main] {SAMPLE_NEWS_JSON} пуст.")
    return raw


def _ingest_live(cfg) -> tuple[List[dict], List[dict]]:
    """Забирает все включённые источники. Возвращает (записи, отчёт по лентам).

    Отчёт нужен, чтобы замолчавшая лента была видна: раньше сбой одной из
    трёх терялся среди остальных, и понять это можно было только по
    странно маленькому числу сырых записей.
    """
    from src import ingest_gdelt, ingest_rss, sources

    out: List[dict] = []
    report: List[dict] = []

    if cfg.gdelt_enabled:
        out.extend(ingest_gdelt.fetch_all())
    else:
        print("[main] GDELT_ENABLED=false — пропускаю GDELT (см. .env.example).")

    enabled = sources.enabled_sources(cfg)
    if enabled:
        items, report = ingest_rss.fetch_sources(
            enabled, timeout=cfg.feed_timeout_sec, retries=cfg.feed_retries
        )
        out.extend(items)
    elif cfg.rss_feeds:
        # Реестр пуст (всё выключено флагами) — работаем на старом плоском
        # списке лент. Так конвейер не встаёт из-за одной строки в .env.
        out.extend(ingest_rss.fetch_all(cfg.rss_feeds))

    if not out:
        raise SystemExit(
            "[main] Живой ingest не вернул НИ ОДНОЙ записи (GDELT выключен и/или "
            "все RSS упали). Это не 'нет новостей' — это отказ источников. "
            "Проверь сеть/конфиг или запусти с --dry-run на sample-данных."
        )
    return out, report


# Худшая цена одной оценки релевантности: два обращения к провайдеру (retry
# на невалидный JSON) по щедрой оценке размера запроса. Считается той же
# функцией, что и фактический учёт, — второго независимого счёта расходов в
# проекте быть не должно.
_WORST_CASE_INPUT_TOKENS = 1500
_WORST_CASE_OUTPUT_TOKENS = 300


def _worst_case_item_cost(cfg) -> float:
    from config import LLM_TIER_CHEAP
    from src.llm_client import _cost_for

    if cfg.is_mock():
        return 0.0
    per_call = _cost_for(
        cfg.model_for_tier(LLM_TIER_CHEAP),
        _WORST_CASE_INPUT_TOKENS,
        _WORST_CASE_OUTPUT_TOKENS,
    )
    return per_call * 2


def _worst_case_post_cost(cfg) -> float:
    """Худшая цена доведения ОДНОЙ темы до кандидата.

    Переписывание может повториться (MAX_REGENERATE_ATTEMPTS), и каждая
    попытка тянет за собой редакторскую оценку. Считается той же функцией
    стоимости, что и фактический учёт: второго независимого счёта расходов
    в проекте быть не должно.
    """
    from config import LLM_TIER_CHEAP, LLM_TIER_MID
    from src.llm_client import _cost_for

    if cfg.is_mock():
        return 0.0
    attempts = cfg.max_regenerate_attempts + 1
    rewrite = _cost_for(cfg.model_for_tier(LLM_TIER_MID), 4000, 1200)
    scorer = _cost_for(cfg.model_for_tier(LLM_TIER_CHEAP), 2000, 400)
    return attempts * (rewrite + scorer)


def _print_discovery_report(cfg, kept, dropped, source_report, worst_case) -> None:
    """Что нашли бы источники и почём — без единого обращения к модели.

    Нужен, чтобы включать новые ленты по фактическому улову, а не по
    обещаниям: видно и что прошло дешёвый отбор, и что он выбросил.
    """
    print("\n=== DISCOVERY-ONLY ===")
    print("Ни одного обращения к модели, ни одного кандидата, ни одной записи "
          "в базу: схема не трогается, журнал виденного не пополняется и не "
          "чистится.")
    print("\nПо источникам (сырьё из лент):")
    for row in source_report:
        print(f"  {row['name']:<16} {row['kind']:<8} {row['count']:>3} зап · {row['status']}")

    by_source: dict = {}
    for v in kept:
        by_source[v.item.source] = by_source.get(v.item.source, 0) + 1
    print("\nПрошло дешёвый отбор — по источникам:")
    for name, count in sorted(by_source.items(), key=lambda kv: -kv[1]):
        print(f"  {name:<16} {count}")

    print(f"\nУшло бы в платную оценку: {len(kept)}")
    print(f"Худшая цена одной оценки: ${worst_case:.5f} · "
          f"потолок прогона ${cfg.max_run_cost_usd:.2f} · "
          f"потолок вызовов {cfg.max_llm_items_per_run}")
    print(f"Оценка стоимости прогона: до ${len(kept) * worst_case:.4f} "
          f"— это худший случай: два обращения к провайдеру на каждый "
          f"материал при щедрой оценке размера запроса. Фактически "
          f"повторное обращение бывает редко.")

    print("\nЛучшие находки:")
    for v in kept[:20]:
        print(f"  [{v.score:+d}] {v.item.source:<15} {v.item.title[:88]}")

    print("\nОтсеяно дешёвым отбором (первые 10):")
    for v in dropped[:10]:
        print(f"  [{v.score:+d}] {', '.join(v.reasons) or 'нет сигналов':<28} {v.item.title[:70]}")


def _save_digest(cfg, run_id, scored, verdicts, raw, source_report,
                 items_after_seen, llm_items, prefilter_dropped, run_cost_start,
                 dry_run: bool = False) -> int:
    """Сохраняет короткий список дня и завершает прогон.

    Дорогая стадия (переписывание, гейт, редактор, карточка) здесь НЕ
    запускается: за неё платим только для того, что владелец выберет сам.
    Исходники всех историй кладём в raw_items — иначе после выбора нечего
    будет переписывать: раньше туда попадали только обработанные.

    В `--dry-run` список только печатается. Записанный, он вытеснил бы
    настоящий дайджест выдуманными sample-новостями, и владелец, нажав на
    такую строку, оплатил бы генерацию поста по несуществующему делу.
    """
    from src import digest

    rows = digest.build(scored, verdicts, cfg.digest_size)
    if dry_run:
        print("[main] dry-run: дайджест НЕ сохранён, показан только на экране")
    else:
        # Перевод — единственный платный шаг дайджеста: ОДИН дешёвый вызов
        # на весь список. Делается только перед сохранением: в пробном
        # прогоне список никуда не идёт, платить за него незачем.
        rows = digest.translate(cfg, rows)
        for item, _ in scored:
            storage.insert_raw_item(item.to_dict())
        added = storage.save_digest(run_id, rows)
        storage.archive_stale_digest(cfg.digest_ttl_hours)

    spent = storage.get_today_cost() - run_cost_start
    print("\n=== Run summary (дайджест) ===")
    print(f"  run_id: {run_id}")
    print("  Источники: " + (" · ".join(
        f"{r['name']}={r['count']}" for r in source_report) or "sample-данные"))
    print(f"  Сырьё: {len(raw)} → после дедупа и журнала: {items_after_seen}")
    print(f"  Дешёвый отбор пропустил: {llm_items} (отсеял {prefilter_dropped})")
    print(f"  Оплаченных оценок: {llm_items} · потрачено за прогон: ${spent:.4f}")
    print(f"  В дайджест сохранено: {len(rows)}")
    print("  Дорогая генерация НЕ запускалась — ждём выбора владельца в боте.")
    for i, row in enumerate(rows, 1):
        print(f"    {i}. [{row['score']}] {row['source']:<15} {row['title'][:70]}")

    with open(LOGS_DIR / "run_log.md", "a", encoding="utf-8") as f:
        f.write(f"- run_id={run_id} режим=digest raw={len(raw)} "
                f"оценено={llm_items} в_дайджест={len(rows)}"
                f"{' (dry-run, не сохранён)' if dry_run else ''}\n")

    # Рассылка редакции. В пробном прогоне не шлём: список не сохранён, и
    # кнопки в таком сообщении вели бы в пустоту.
    if not dry_run:
        _notify_digest(cfg, run_id, len(raw), llm_items, added)
    return 0


def _notify_digest(cfg, run_id: str, raw_count: int, scored_count: int,
                   added_count: int = 0) -> None:
    """Отправляет свежий список редакции. Ошибку не поднимает наверх.

    Сбор новостей — главная работа прогона, и она уже сделана. Провалить
    её из-за того, что в эту минуту был недоступен Telegram, было бы
    неверно: systemd счёл бы прогон неудачным на ровном месте.
    """
    from src import notify

    try:
        # Рассылается АКТУАЛЬНЫЙ пул, а не улов одного прогона: владельцу
        # нужен полный список того, из чего сейчас можно выбирать, включая
        # ещё живые истории прошлых сборов.
        rows = storage.digest_pool(cfg.telegram_channel_id, cfg.digest_size)
        notify.send_digest(cfg, run_id, rows, {
            "collected_at": _collected_at(cfg),
            "raw": raw_count,
            "scored": scored_count,
            "added": added_count,
        })
    except Exception as exc:
        print(f"[main] дайджест собран, но разослать не удалось "
              f"({type(exc).__name__}: {exc}) — это не отменяет сбор")


def _collected_at(cfg) -> str:
    """Время сбора в зоне владельца — сервер живёт в другой."""
    from datetime import datetime, timezone

    from src import news_schedule, schedule as slots

    now = datetime.now(timezone.utc).astimezone(slots._tz(cfg))
    return (f"{now.day:02d}.{now.month:02d} в {now.hour:02d}:{now.minute:02d} "
            f"{news_schedule.zone_label(cfg)}")


def _process_item(cfg, item: NewsItem, relevance, stats: dict,
                  borderline_ok: bool = False,
                  supersede_auto_reject: bool = False,
                  auto_floor: float | None = None) -> None:
    """Обрабатывает один прошедший relevance кандидат: legal gate PRE →
    rewrite (+legal gate POST + editorial scorer, с циклом regenerate) →
    image card → storage → publisher. Мутирует `stats` на месте.

    `borderline_ok` — режим, где каждый пост всё равно проходит через руки
    владельца (WORKFLOW_MODE=digest). В нём материал, прошедший юридический
    гейт и набравший от EDITORIAL_BORDERLINE_MIN до MIN_EDITORIAL_AVG, не
    выбрасывается: он попадает в очередь с пометкой, и решение принимает
    человек. Отдельного пути публикации при этом не появляется — пост идёт
    той же дорогой, что и любой другой кандидат, просто в его оценках
    стоит флаг `borderline`. Юридический гейт не ослабляется НИКОГДА: без
    него в пограничную полосу не попадают вовсе.

    `auto_floor` — политика автоматической редакции (WORKFLOW_MODE=auto,
    решено 30.08.2026): редакторская оценка РАНЖИРУЕТ, а не служит
    абсолютным барьером. Оценка ниже MIN_EDITORIAL_AVG даёт РОВНО ОДНУ
    дополнительную генерацию (а не полный бюджет regenerate — сегодня один
    сюжет съел три генерации: 7.67 → 8.33 → 8.17, причём сохранился
    последний, а не лучший). Из оценённых вариантов фиксируется лучший;
    если он прошёл юридический гейт, не hard_fail, legal_safety >= 6 и
    набрал не меньше `auto_floor` — пост принимается с пометкой
    `auto_floor` в оценках. Бюджет regenerate на ЮРИДИЧЕСКИЕ отказы гейта
    не трогается: там повторная формулировка реально чинит текст.
    None — политика выключена, поведение прежнее (legacy и digest).

    Исход СВОЕГО прогона кладётся в `stats["outcome"]`. Это не украшение:
    раньше бот определял, чем кончилась подготовка, запросом «самый свежий
    пост по этому сюжету» — и читал ЧУЖУЮ, оставшуюся от прошлого раза
    запись. По ней он сообщал владельцу небылицу: конвейер честно получил
    отказ редактора, а человек видел «что-то сорвалось по дороге».

    Может поднять `LLMBudgetExceeded` (дневной лимит исчерпан посреди
    прогона — ловится в `run()`) или `LLMResponseError`/любое другое
    исключение LLM-стадий — ничего не глотается тут (NO SILENT FAILURES),
    падает наверх до `run()` и дальше до необработанного исключения в main.
    """
    print(f"\n[main] === Обрабатываю: {item.title[:80]!r} (total={relevance.total}) ===")

    # persist сырую новость независимо от исхода гейта — для аудита
    storage.insert_raw_item(item.to_dict())

    # Судебный статус читается из источника ОДИН раз и дальше идёт и в
    # промпт переписчика, и в гейт. Без него гейт трижды браковал корректные
    # русские формулировки по делам с приговором, а каждая попытка платная.
    from src import case_status as case_status_mod

    case_status = case_status_mod.detect(item.title, item.body)
    print(f"[main] статус дела по источнику: {case_status_mod.title(case_status)}")

    # 5. legal gate PRE
    pre_gate = legal_gate.check_pre(item)
    if pre_gate.issues:
        print(f"[main] legal_gate PRE issues: {pre_gate.issues}")
    if pre_gate.hard_fail:
        print(f"[main] REJECTED (pre-gate hard fail): {item.title[:80]!r}")
        stats["hard_rejected"] += 1
        return

    event_id = f"evt_{item.url_hash}"

    # Один исходный материал — один пост. Без этой проверки повторный
    # прогон делал по этой же статье второго кандидата, и владелец видел
    # в очереди один сюжет дважды (реальный случай: попугаи и Локерби).
    # Канал важен: пост, сделанный для песочницы, не должен мешать
    # подготовить ту же историю заново для боевого канала.
    existing = storage.active_post_for_event(event_id, cfg.telegram_channel_id)
    if existing:
        # Работы не было: к модели не обращались, денег не потратили.
        # Вызывающий код обязан отличать это от неудачи, иначе владелец
        # получает «что-то сорвалось» и теряет платную попытку ни за что.
        print(f"[main] Пропуск: по этому сюжету пост уже есть "
              f"({existing['status']}) — {item.title[:60]!r}")
        stats["outcome"] = {
            "kind": "skipped",
            "post_id": existing["post_id"],
            "post_status": existing["status"],
            "paid_work": False,
        }
        return

    storage.insert_event(event_id, item.url_hash, n_sources=1)

    # 6. rewrite (+7. legal gate POST, 8. scorer — с циклом regenerate)
    draft = None
    approved = False
    # Последняя попытка, дошедшая до редакторской оценки: только её и можно
    # предложить владельцу как пограничную. Отказ юридического гейта сюда
    # не попадает по построению — до скорера дело не доходит.
    last_score = None
    last_gate_passed = False
    gate_issues: list = []
    # Для auto_floor: лучший из ПРИГОДНЫХ оценённых вариантов (гейт пройден
    # по построению — скорер зовётся только после гейта; не hard_fail;
    # legal_safety >= 6). Вариант с высокой средней, но hard_fail сюда не
    # попадает — иначе он вытеснил бы чистый вариант и утопил историю.
    best_draft = None
    best_score = None
    editorial_runs = 0
    for attempt in range(cfg.max_regenerate_attempts + 1):
        draft = generate_draft(cfg, item, attempt=attempt, case_status=case_status)
        # Реальная стоимость каждого LLM-вызова логируется внутри
        # src/llm_client.py (generate_json -> storage.log_cost) на КАЖДЫЙ
        # фактический вызов провайдера — отдельный лог здесь больше не
        # нужен. В mock-режиме вызовов провайдера нет —
        # соответственно и cost_log для mock-прогона не растёт.

        post_gate = legal_gate.check_post(draft.full_text, draft.source_url,
                                          case_status=case_status)
        # Заголовок карточки — тоже публикация: он печатается на картинке и
        # уходит в канал. check_post его не видит (он работает по тексту
        # поста), поэтому проверяем отдельно, иначе «Убийца пойман» на
        # картинке прошло бы мимо всех правил.
        headline_gate = legal_gate.check_headline(draft.headline, case_status)
        post_gate = post_gate.merge(headline_gate)
        if post_gate.issues:
            print(f"[main] legal_gate POST issues (attempt {attempt}): {post_gate.issues}")
        gate_issues = list(post_gate.issues)
        last_gate_passed = not post_gate.hard_fail
        if post_gate.hard_fail:
            print(f"[main] REJECTED (post-gate hard fail, attempt {attempt})")
            if attempt < cfg.max_regenerate_attempts:
                stats["regenerated"] += 1
                continue
            break

        score = score_draft(cfg, draft)
        last_score = score
        editorial_runs += 1
        if (not score.hard_fail and score.legal_safety >= 6
                and (best_score is None or score.average > best_score.average)):
            best_draft, best_score = draft, score
        print(f"[main] editorial decision={score.decision} avg={score.average} (attempt {attempt})")
        if score.decision == "approve":
            approved = True
            break
        # Политика auto_floor: на редакторскую неудачу — максимум одна
        # дополнительная генерация. Жечь весь бюджет regenerate на один
        # посредственный сюжет нельзя: попытки нужны следующим историям
        # рейтинга. Юридические отказы гейта сюда не считаются — они
        # прерывают итерацию выше и чинятся своим бюджетом.
        if auto_floor is not None and editorial_runs >= 2:
            break
        if score.decision == "reject":
            if attempt < cfg.max_regenerate_attempts:
                stats["regenerated"] += 1
                continue
            break
        # decision == "revise" -> тоже пробуем regenerate в пределах лимита
        if attempt < cfg.max_regenerate_attempts:
            stats["regenerated"] += 1
            continue
        break

    if draft is None:
        return

    # Автоматическая редакция фиксирует ЛУЧШИЙ пригодный вариант, а не
    # последний: серия 7.67 → 8.33 → 8.17 раньше сохраняла 8.17, выбросив
    # 8.33. Промоция БЕЗУСЛОВНАЯ, не «если лучше last_score»: после удачной
    # оценки гейт может завалить следующие генерации, и тогда last_score
    # уже указывает на тот же лучший вариант, а draft — на небезопасный
    # последний. Сравнение по «строго больше» оставляло в этом случае
    # чужой текст и ложный REJECT_BY_LEGAL (нашёл Codex). Пригодность
    # проверена при отборе в best_draft, его гейт пройден по построению —
    # значит и претензии гейта от отброшенных генераций больше не о нём.
    if auto_floor is not None and best_score is not None and not approved:
        draft, last_score = best_draft, best_score
        last_gate_passed = True
        gate_issues = []

    # Сам floor: оценка ниже MIN_EDITORIAL_AVG, но не ниже auto_floor —
    # публикуем. Это ranked-модель «TOP-3 лучших доступных за день», а не
    # «молчим, пока не случится 8.6». Floor, поднятый до порога и выше,
    # выключает добор — остаётся старое строгое поведение.
    if (not approved and auto_floor is not None and last_score is not None
            and auto_floor < cfg.min_editorial_avg
            and last_gate_passed
            and cfg.min_editorial_avg > last_score.average >= auto_floor
            and last_score.legal_safety >= 6
            and not last_score.hard_fail):
        approved = True
        draft.scores = {**(draft.scores or {}), "auto_floor": True}
        print(
            f"[main] AUTO-floor: лучший вариант {last_score.average} при "
            f"пороге {cfg.min_editorial_avg} и floor {auto_floor} — "
            f"публикуем как лучшее доступное"
        )

    # Пограничная полоса. Условия жёсткие и все обязательны: юридический
    # гейт пройден, режим ручной, оценка ВНУТРИ полосы, и по юридической
    # безопасности претензий нет (иначе «пограничным» стал бы текст,
    # который скорер забраковал именно за право).
    #
    # Верхняя граница задана явно, хотя оценка выше порога сюда и так не
    # дошла бы — её приняли бы раньше. Полагаться на это нельзя: измени
    # кто-нибудь условия approve, и «пограничной» стала бы любая оценка,
    # включая забракованную скорером по другой причине.
    if (not approved and borderline_ok and last_gate_passed and last_score is not None
            and cfg.editorial_borderline_min <= last_score.average < cfg.min_editorial_avg
            and last_score.legal_safety >= 6
            and not last_score.hard_fail):
        approved = True
        draft.scores = {**(draft.scores or {}), "borderline": True}
        print(
            f"[main] ПОГРАНИЧНАЯ оценка {last_score.average} при пороге "
            f"{cfg.min_editorial_avg} — пост идёт владельцу с пометкой, "
            f"решение за человеком"
        )

    draft.manual_only = pre_gate.manual_only
    status = "candidate" if approved else "rejected"
    if pre_gate.manual_only and approved:
        status = "candidate"  # остаётся в очереди, но approval_bot покажет ⚠️ manual-only

    if not approved:
        # Кто именно забраковал — записываем сразу. Без этого бот мог
        # сказать владельцу только «не прошло гейт ИЛИ оценку», и человеку
        # приходилось лезть в логи сервера.
        reason = (storage.REJECT_BY_EDITOR if last_gate_passed
                  else storage.REJECT_BY_LEGAL)
        who = ("редакторская оценка" if reason == storage.REJECT_BY_EDITOR
               else "юридический гейт")
        print(f"[main] Пост забракован: {who}. Reject.")
        if reason == storage.REJECT_BY_LEGAL:
            # Претензии гейта складываем рядом с оценками: скорер до этого
            # текста не дошёл, и другого места для них нет. Без этого бот
            # мог сказать владельцу только «гейт не пропустил», а чем именно
            # не понравилась формулировка — снова искать в логах сервера.
            draft.scores = {**(draft.scores or {}), "legal_issues": gate_issues[:3]}
        stats["hard_rejected"] += 1
        storage.insert_post(
            post_id=draft.id, event_id=event_id, draft_ru=draft.full_text,
            fmt=draft.format, status="rejected", scores=draft.scores,
            manual_only=draft.manual_only, reject_reason=reason,
            headline=draft.headline, channel_id=cfg.telegram_channel_id,
            supersede_auto_reject=supersede_auto_reject,
        )
        stats["outcome"] = {
            "kind": reason,                 # legal_gate или editorial
            "post_id": None,
            "scores": draft.scores or {},
            "gate_issues": gate_issues[:3],
            "paid_work": True,
        }
        return

    # 9. image card
    try:
        image_path = render_card(
            # На карточку идёт КОРОТКИЙ заголовок, а не хук: хук — это первое
            # предложение поста (до 120 знаков), на картинке оно нечитаемо.
            title=draft.headline or draft.hook,
            category=draft.category,
            source_name=draft.source_name, draft_id=draft.id,
        )
    except Exception as exc:
        print(f"[main] WARNING: не удалось отрендерить карточку для {draft.id}: {exc}")
        image_path = None

    # 10. storage
    persisted = storage.insert_post(
        post_id=draft.id, event_id=event_id, draft_ru=draft.full_text,
        fmt=draft.format, status=status, scores=draft.scores,
        manual_only=draft.manual_only, headline=draft.headline,
        channel_id=cfg.telegram_channel_id,
        supersede_auto_reject=supersede_auto_reject,
    )
    if not persisted:
        # Запись не легла: пока мы работали, по этому сюжету появилось
        # решение, которое переписывать нельзя. Успехом это считать
        # нельзя — иначе владельцу показали бы пост, которого нет.
        print(f"[main] Пост {draft.id} НЕ записан: по сюжету уже есть "
              f"решение, которое не перезаписывается")
        stats["outcome"] = {
            "kind": "not_saved",
            "post_id": None,
            "scores": draft.scores or {},
            "paid_work": True,
        }
        return

    stats["outcome"] = {
        "kind": "ok",
        "post_id": draft.id,
        "scores": draft.scores or {},
        "borderline": bool((draft.scores or {}).get("borderline")),
        "paid_work": True,
    }

    # 11. Только markdown-черновик на диск. В канал конвейер НЕ публикует
    # НИКОГДА, даже при PUBLISH_MODE=live: единственный путь в эфир —
    # кнопка ✅ владельца в approval_bot. Иначе непроверенные посты уходили
    # бы в канал при генерации, а после нажатия ✅ дублировались вторым
    # сообщением. Автопубликация безопасных категорий — отдельная задача
    # и должна включаться явным флагом, а не молча.
    md_path = write_draft_markdown(draft, image_path=image_path)
    print(f"[main] draft markdown: {md_path}")

    if pre_gate.manual_only:
        stats["manual_only"] += 1
    stats["approved"] += 1


def run(argv: List[str] | None = None) -> int:
    args = parse_args(argv)
    cfg = get_config()
    if args.dry_run:
        cfg.publish_mode = "dry-run"  # --dry-run всегда переопределяет .env

    # Автоматический режим собирает пул ровно так же, как ручной: конвейер
    # останавливается после оценки и никого не публикует. Отличается лишь
    # то, кто потом выбирает из пула — человек кнопкой или auto_editor.
    # Объявлено здесь, а не ниже: признак нужен уже на ранних выходах.
    digest_mode = cfg.workflow_mode in ("digest", "auto")

    ensure_dirs()
    # Режим разведки обещает не трогать данные — значит, и схему тоже.
    # init_db создаёт таблицы, включает WAL и может выполнить миграцию: на
    # боевой базе это запись, пусть и безобидная. Читаем то, что есть.
    if not args.discovery_only:
        storage.init_db()

    run_id = uuid.uuid4().hex[:12]
    print(
        f"[main] run_id={run_id} · llm_provider={cfg.llm_provider} · "
        f"has_llm={cfg.has_llm()} · publish_mode={cfg.publish_mode} · "
        f"dry_run={args.dry_run}"
    )

    # Дневной лимит проверяется до всего остального — но только там, где
    # вообще возможны платные вызовы. В разведке их нет по построению, а
    # база может отсутствовать: читать расходы неоткуда.
    if not args.discovery_only:
        # Месячный потолок — главный: бюджет проекта задан в месяц.
        # Дневной ниже — это предохранитель на случай, если что-то пойдёт
        # вразнос за один день, а не норма расхода.
        month_cost = storage.get_month_cost()
        if month_cost >= cfg.monthly_cost_limit_usd:
            raise SystemExit(
                f"[main] СТОП: месячный предел расходов исчерпан "
                f"(${month_cost:.2f} >= ${cfg.monthly_cost_limit_usd:.2f}). "
                f"Это абсолютный потолок, а не цель — работа остановлена до "
                f"следующего месяца. Настройка MONTHLY_COST_LIMIT_USD."
            )
        if month_cost >= cfg.monthly_cost_target_usd:
            print(
                f"[main] ⚠️  Месячная цель превышена: ${month_cost:.2f} из "
                f"${cfg.monthly_cost_target_usd:.2f}. До остановки осталось "
                f"${cfg.monthly_cost_limit_usd - month_cost:.2f}."
            )

        today_cost = storage.get_today_cost()
        if today_cost >= cfg.daily_cost_limit_usd:
            raise SystemExit(
                f"[main] СТОП: дневной лимит LLM-расходов исчерпан "
                f"(${today_cost:.2f} >= ${cfg.daily_cost_limit_usd:.2f}). "
                f"Это громкая остановка, а не тихий пропуск дня — см. .env DAILY_COST_LIMIT_USD."
            )

    # 1. ingest
    source_report: List[dict] = []
    if args.dry_run:
        raw = _load_sample_news()
    else:
        raw, source_report = _ingest_live(cfg)
    for row in source_report:
        mark = "  " if row["count"] else "⚠️"
        print(f"[ingest] {mark} {row['name']:<16} {row['role']:<12} "
              f"{row['count']:>3} зап · {row['seconds']}с · {row['status']}")
    print(f"[main] raw ingested: {len(raw)}")

    # 2. normalize
    items: List[NewsItem] = normalize_batch(raw, default_source="unknown")
    print(f"[main] normalized: {len(items)}")

    # 3. dedupe L1
    items = dedupe(items, threshold=cfg.dedupe_l1_threshold)

    # 3a. Отсев уже обработанных новостей — ДО платной оценки релевантности.
    # RSS отдаёт одни и те же записи часами, дедуп работает только внутри
    # батча, поэтому каждый прогон заново платил за оценку тех же новостей
    # (в боевой базе накопилось 676 вызовов relevance на 18 уникальных) и мог
    # снова создать кандидата по уже разобранному сюжету.
    if not args.dry_run:
        # sqlite3.connect создаёт файл базы самим фактом подключения, даже на
        # чтение. В разведке это была бы та самая запись, которой мы обещали
        # не делать, поэтому журнал читаем только если база уже существует.
        # На боевой машине она есть всегда, и картина отбора точная.
        if args.discovery_only and not DB_PATH.exists():
            seen = set()
        else:
            seen = storage.known_url_hashes()
        before = len(items)
        # Отфильтрованный список раньше вычислялся, печатался в лог и
        # ВЫБРАСЫВАЛСЯ: дальше по конвейеру шёл исходный `items`, и каждый
        # прогон заново платил за оценку уже виденных новостей. Журнал
        # помечал, но не фильтровал — то есть не делал того, ради чего
        # заведён.
        items = [it for it in items if it.url_hash not in seen]
        print(f"[main] новых новостей: {len(items)} (уже обработанных пропущено: {before - len(items)})")
        # Чистка журнала — тоже запись в базу. Режим разведки обещает не
        # трогать данные вообще, поэтому там её не делаем.
        if not args.discovery_only:
            storage.purge_old_seen(days=30)
        if not items:
            print("[main] Все новости этого прогона уже обрабатывались раньше — выходим.")
            return 0

        # 3a'. Слишком старое — объективный отказ. Свежесть AUTO считается
        # от попадания истории в пул, а не от даты новости, поэтому без
        # этого фильтра статья недельной давности, впервые прошедшая отбор
        # сегодня, ушла бы в канал как свежая. Запись без даты проходит:
        # у Europol дат нет вовсе, а его записи оцениваются в первый же
        # прогон после появления в ленте.
        if cfg.story_max_age_hours > 0:
            before = len(items)
            items = [it for it in items
                     if not _too_old(it, cfg.story_max_age_hours)]
            if before - len(items):
                print(f"[main] отсеяно по возрасту (> {cfg.story_max_age_hours}ч "
                      f"от даты публикации): {before - len(items)}")

    # 3b. Суточный потолок кандидатов: очередь не должна разрастаться, а
    # расходы — быть предсказуемыми. Проверяем ДО обращения к модели.
    #
    # В режиме дайджеста потолок НЕ применяется, и вот почему. Он считает
    # кандидатов, созданных за сутки, — а дайджест-прогон не создаёт их
    # вовсе: он лишь собирает короткий список, дорогая стадия запускается
    # потом и только по кнопке владельца. Проверка стоит до ветвления и
    # рубила прогон целиком: 28.08 вечерний сбор завершился, ничего не
    # сделав, потому что днём владелец подготовил четыре поста при
    # потолке в три. Свежего списка в этот день он бы не увидел.
    #
    # Поток в этом режиме ограничен своим: нормой готовых постов, числом
    # платных попыток и денежными лимитами прогона, суток и месяца.
    # В прежнем режиме поведение не меняется ни на йоту.
    if (cfg.max_candidates_per_day > 0 and not args.discovery_only
            and cfg.workflow_mode != "digest"):
        today_count = storage.count_candidates_created_today()
        if today_count >= cfg.max_candidates_per_day:
            print(
                f"[main] Суточный лимит кандидатов достигнут "
                f"({today_count}/{cfg.max_candidates_per_day}) — прогон завершён "
                f"без обращения к модели. Настройка MAX_CANDIDATES_PER_DAY."
            )
            return 0

    # 3c. Очередь ожидающих решения. Если владелец не успевает разбирать —
    # плодить новые посты бессмысленно и дорого. Очередь важнее суточного
    # потолка: тот считает созданное за сегодня, а этот — накопившееся.
    if cfg.max_pending_queue > 0 and not args.discovery_only:
        pending = storage.count_pending_posts()
        if pending >= cfg.max_pending_queue:
            print(
                f"[main] В очереди уже {pending} постов ждут решения "
                f"(потолок {cfg.max_pending_queue}) — прогон завершён без "
                f"обращения к модели. Разберите очередь или поднимите "
                f"MAX_PENDING_QUEUE."
            )
            return 0

    # 3d. Дешёвый отбор ПЕРЕД платной стадией.
    #
    # До него оценка модели вызывалась на КАЖДУЮ запись ленты, и расходы
    # росли линейно вслед за числом источников. Здесь ни сети, ни модели —
    # только регулярки по заголовку и описанию: явный мусор отсеивается,
    # остальное сортируется, и дальше проходит не больше потолка.
    #
    # Потолок двойной: по числу элементов и по деньгам. Денежный считается
    # ДО стадии, по худшей цене вызова, — проверять постфактум поздно.
    from src import prefilter, sources as sources_registry

    # В разведке базы может не быть вовсе — читать расходы неоткуда и незачем.
    # Делать get_today_cost() терпимым к отсутствию таблицы нельзя: на этой
    # функции держится дневной лимит, и «таблицы нет» превратилось бы в
    # «потрачено ноль, трать дальше».
    run_cost_start = 0.0 if args.discovery_only else storage.get_today_cost()
    worst_case = _worst_case_item_cost(cfg)
    affordable = cfg.max_llm_items_per_run
    if worst_case > 0 and cfg.max_run_cost_usd > 0:
        affordable = min(affordable, int(cfg.max_run_cost_usd / worst_case))

    kept, dropped = prefilter.select(
        items,
        weights=sources_registry.weights(),
        min_score=cfg.prefilter_min_score,
        limit=max(affordable, 0),
        quota_share=cfg.prefilter_source_quota,
    )
    print(
        f"[prefilter] на входе {len(items)} → отобрано {len(kept)} "
        f"(потолок вызовов {cfg.max_llm_items_per_run}, по бюджету прогона "
        f"{affordable}, отсеяно {len(dropped)})"
    )
    for verdict in dropped[:5]:
        print(f"[prefilter] отсеяно ({verdict.score:+d}, {', '.join(verdict.reasons) or 'нет сигналов'}): "
              f"{verdict.item.title[:70]}")
    prefilter_verdicts = {v.item.url_hash: v for v in kept}
    items = [v.item for v in kept]
    items_after_seen = len(items) + len(dropped)
    llm_items = len(items)
    prefilter_dropped = len(dropped)

    if args.discovery_only:
        _print_discovery_report(cfg, kept, dropped, source_report, worst_case)
        return 0

    if not items:
        print("[main] После дешёвого отбора не осталось ничего похожего на "
              "криминальную историю. Это валидный исход, не сбой.")
        if digest_mode and not args.dry_run:
            _notify_digest(cfg, run_id, len(raw), 0)
        return 0

    # 4. relevance
    #
    # Журнал виденного пополняется ПОШТУЧНО, по факту каждой оплаченной
    # оценки. Раньше вся пачка помечалась заранее, ещё до проверки суточного
    # лимита: прогон, упёршийся в лимит, выбрасывал свежие новости молча и
    # навсегда — в боевом прогоне так ушло 137 штук. Теперь непроверенное
    # остаётся непомеченным и вернётся следующим прогоном.
    # Упавшая оценка — отдельный случай. Стоимость вызова пишется сразу
    # после ответа провайдера, а разбор JSON и повторная проверка бюджета
    # идут ПОСЛЕ: бывает, что деньги за новость уже списаны, а результата
    # нет. Такую новость надо пометить, иначе следующий прогон заплатит за
    # неё второй раз. Если же оценка упала до обращения к провайдеру
    # (сработал дневной лимит) — не помечаем, новость вернётся.
    mark_scored = mark_failed = None
    if not args.dry_run:
        spent = {"at_last_item": storage.get_today_cost()}

        def mark_scored(item) -> None:  # noqa: F811 — одно имя на обе ветки
            storage.mark_seen([item.url_hash])
            spent["at_last_item"] = storage.get_today_cost()

        def mark_failed(item) -> None:  # noqa: F811
            if storage.get_today_cost() > spent["at_last_item"]:
                print(
                    f"[main] оценка упала, но вызов уже оплачен — помечаю "
                    f"виденной, чтобы не платить дважды: {item.title[:60]!r}"
                )
                storage.mark_seen([item.url_hash])

    cross = {"dropped": 0}

    def dedupe_hook(pairs):
        kept_pairs = drop_cross_source_repeats(pairs)
        cross["dropped"] += len(pairs) - len(kept_pairs)
        return kept_pairs

    if cfg.workflow_mode not in ("legacy", "digest", "auto"):
        raise SystemExit(
            f"[main] WORKFLOW_MODE={cfg.workflow_mode!r} не реализован. "
            f"Допустимо: legacy (конвейер сам доводит темы до кандидата), "
            f"digest (короткий список на выбор владельцу) или "
            f"auto (система выбирает сама)."
        )

    try:
        scored = score_and_filter(
            cfg, items, on_scored=mark_scored, on_failed=mark_failed,
            dedupe_sorted=dedupe_hook,
            # В дайджесте берём глубже: это тот же уже оплаченный список,
            # обрезка до двух — правило дорогой стадии, а не оценки.
            top_n=cfg.digest_size if digest_mode else None,
        )
    except LLMBudgetExceeded as exc:
        raise SystemExit(f"[main] СТОП: {exc}") from exc
    print(f"[main] relevant after scoring: {len(scored)}")

    # Дедуп повторов одного дела применён внутри score_and_filter, до
    # обрезки до MAX_TOPICS_PER_RUN — см. комментарий там.
    cross_dropped = cross["dropped"]

    if not scored:
        storage.log_cost(stage="run", provider=cfg.llm_provider, model="-", usd_cost=0.0)
        print("[main] Нет релевантных новостей в этом прогоне. Это валидный исход, не сбой.")
        # Владельцу всё равно нужно знать, что сбор прошёл: молчание он
        # прочтёт как поломку и полезет проверять сервер.
        if digest_mode and not args.dry_run:
            _notify_digest(cfg, run_id, len(raw), llm_items)
        return 0

    if digest_mode:
        return _save_digest(cfg, run_id, scored, prefilter_verdicts, raw,
                            source_report, items_after_seen, llm_items,
                            prefilter_dropped, run_cost_start,
                            dry_run=args.dry_run)

    stats = {"approved": 0, "manual_only": 0, "hard_rejected": 0, "regenerated": 0}
    failed: List[str] = []
    day_budget = (
        cfg.max_candidates_per_day - storage.count_candidates_created_today()
        if cfg.max_candidates_per_day > 0
        else None
    )

    for item, relevance in scored:
        # Потолок суток проверяется и внутри цикла: за один прогон нельзя
        # выскочить за дневную норму, даже если тем в очереди больше.
        if day_budget is not None and stats["approved"] >= day_budget:
            print(
                f"[main] Достигнут суточный лимит кандидатов "
                f"({cfg.max_candidates_per_day}) — остальные темы пропущены."
            )
            break
        # Потолок стоимости прогона. Резервируем ЗАРАНЕЕ: сравнивать
        # потраченное с лимитом уже после вызова поздно — переписывание с
        # регенерациями стоит на порядок дороже оценки, и один элемент
        # успел бы увести прогон за лимит. Останавливаемся, если следующий
        # элемент в худшем случае за него выйдет.
        spent_this_run = storage.get_today_cost() - run_cost_start
        reserve = _worst_case_post_cost(cfg)
        if cfg.max_run_cost_usd > 0 and spent_this_run + reserve > cfg.max_run_cost_usd:
            print(
                f"[main] Потолок стоимости прогона: потрачено ${spent_this_run:.4f}, "
                f"следующая тема в худшем случае стоит ${reserve:.4f}, лимит "
                f"${cfg.max_run_cost_usd:.2f} — остальные темы пропущены. "
                f"Настройка MAX_RUN_COST_USD."
            )
            break

        # Потолок очереди проверяется и здесь, а не только перед стадией:
        # за один прогон создаётся до двух постов, и очередь 9 из 10 иначе
        # превращалась бы в 11.
        if cfg.max_pending_queue > 0:
            pending_now = storage.count_pending_posts()
            if pending_now >= cfg.max_pending_queue:
                print(
                    f"[main] Очередь заполнилась в ходе прогона "
                    f"({pending_now}/{cfg.max_pending_queue}) — остальные темы "
                    f"пропущены."
                )
                break
        try:
            _process_item(cfg, item, relevance, stats)
        except LLMBudgetExceeded as exc:
            # Бюджет — единственная причина остановить весь прогон: продолжать
            # означает тратить деньги сверх лимита.
            raise SystemExit(
                f"[main] СТОП: дневной лимит LLM-расходов исчерпан посреди прогона "
                f"(последняя новость: {item.title[:60]!r}). {exc}"
            ) from exc
        except Exception as exc:
            # Одна кривая новость не должна обрушивать весь прогон. Раньше
            # ловился только LLMBudgetExceeded, и любой другой сбой (модель
            # вернула невалидный JSON дважды, 4xx от провайдера, неверный ID
            # модели) прерывал обработку ОСТАЛЬНЫХ кандидатов — при первом же
            # боевом запуске с опечаткой в имени модели конвейер умирал на
            # первой новости. Теперь: пропускаем эту, идём дальше, а в конце
            # громко сообщаем обо всех сбоях (NO SILENT FAILURES).
            print(
                f"[main] ОШИБКА на новости {item.title[:70]!r}: "
                f"{type(exc).__name__}: {exc}"
            )
            failed.append(f"{item.title[:60]!r}: {type(exc).__name__}: {exc}")

    print("\n=== Run summary ===")
    print(f"  run_id: {run_id}")
    if failed:
        print(f"  ⚠️  СБОЕВ ПРИ ОБРАБОТКЕ: {len(failed)} — эти новости пропущены:")
        for line in failed:
            print(f"      - {line}")
    sources_line = " · ".join(f"{r['name']}={r['count']}" for r in source_report)
    print(f"  Источники: {sources_line or 'sample-данные'}")
    print(f"  Сырьё: {len(raw)} → после дедупа и журнала: {items_after_seen}")
    print(f"  Дешёвый отбор пропустил: {llm_items} (отсеял {prefilter_dropped})")
    print(f"  Оплаченных оценок: {llm_items} · потрачено за прогон: "
          f"${storage.get_today_cost() - run_cost_start:.4f} "
          f"(потолок ${cfg.max_run_cost_usd:.2f})")
    print(f"  За сутки: ${storage.get_today_cost():.4f} из ${cfg.daily_cost_limit_usd:.2f} · "
          f"за месяц: ${storage.get_month_cost():.4f} из ${cfg.monthly_cost_target_usd:.2f} "
          f"(предел ${cfg.monthly_cost_limit_usd:.2f})")
    if cross_dropped:
        print(f"  Склеено сюжетов из разных источников: {cross_dropped}")
    print(f"  Кандидатов на входе relevance: {len(scored)}")
    print(f"  Approved (в очереди на approve-бота): {stats['approved']}")
    print(f"  ...из них manual-only (ст. 207.3 и т.п.): {stats['manual_only']}")
    print(f"  Rejected (гейт/scorer): {stats['hard_rejected']}")
    print(f"  Regenerate-попыток потрачено: {stats['regenerated']}")
    print(f"  Черновики: {DRAFTS_DIR}")
    print(f"  БД: {DB_PATH}")
    print(f"  Mode: {'LLM=' + cfg.llm_provider if cfg.has_llm() else 'mock'} · publish_mode={cfg.publish_mode}")

    with open(LOGS_DIR / "run_log.md", "a", encoding="utf-8") as f:
        f.write(
            f"- run_id={run_id} raw={len(raw)} relevant={len(scored)} "
            f"approved={stats['approved']} rejected={stats['hard_rejected']} "
            f"manual_only={stats['manual_only']} dry_run={args.dry_run}\n"
        )

    return 0


if __name__ == "__main__":
    sys.exit(run())
