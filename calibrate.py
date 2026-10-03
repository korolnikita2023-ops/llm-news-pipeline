"""Калибровка порога MIN_RELEVANCE_SUM: собирает новости, скорит релевантность
и показывает РАСПРЕДЕЛЕНИЕ оценок — сколько новостей прошло бы при разных
порогах. Посты не пишет, в очередь ничего не кладёт, в канал не публикует.
"""
from config import get_config
from src import ingest_rss, normalize, dedupe_l1, relevance

cfg = get_config()
raw = ingest_rss.fetch_all(cfg.rss_feeds)
items = dedupe_l1.dedupe(normalize.normalize_batch(raw), cfg.dedupe_l1_threshold)
print(f"\n[calibrate] новостей после дедупа: {len(items)}\n")

results = []
for it in items:
    r = relevance.score_item(cfg, it)
    results.append((r.total, r.relevant, r.category, it.title))

results.sort(reverse=True)
print("\n=== ТОП-20 по оценке ===")
for total, rel, cat, title in results[:20]:
    print(f"{total:>3} | {'ok ' if rel else 'нет'} | {cat[:14]:14} | {title[:66]}")

print("\n=== СКОЛЬКО ПРОЙДЁТ ПРИ РАЗНЫХ ПОРОГАХ ===")
for bar in (24, 28, 30, 32, 34, 36, 38, 40):
    n = sum(1 for t, rel, _, _ in results if rel and t >= bar)
    mark = "  <-- сейчас" if bar == cfg.min_relevance_sum else ""
    print(f"порог {bar}: {n:>3} новостей в день{mark}")
