"""Быстрая сверка расходов LLM по cost_log (запуск: python check_cost.py)."""
import sqlite3

c = sqlite3.connect("data/news_bot.db")
schema = c.execute("select sql from sqlite_master where name='cost_log'").fetchone()
print(schema[0] if schema else "нет таблицы cost_log")
cols = [r[1] for r in c.execute("pragma table_info(cost_log)")]
cost_col = next((x for x in cols if "cost" in x or "usd" in x), None)
if cost_col:
    total, n = c.execute(f"select round(sum({cost_col}),4), count(*) from cost_log").fetchone()
    print(f"итого: ${total} за {n} вызовов")
