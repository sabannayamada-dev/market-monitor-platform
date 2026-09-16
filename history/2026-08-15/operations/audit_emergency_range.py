import sqlite3

c = sqlite3.connect("file:/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3?mode=ro", uri=True)
print(c.execute(
    "SELECT MIN(market_impact),MAX(market_impact),MIN(urgency),MAX(urgency),"
    "SUM(input_tokens),SUM(output_tokens),ROUND(SUM(estimated_cost_usd),6) "
    "FROM emergency_ai_reviews WHERE reviewed_at>='2026-08-18T15:00:00+00:00'"
).fetchone())
print(c.execute(
    "SELECT model,COUNT(*) FROM emergency_ai_reviews "
    "WHERE reviewed_at>='2026-08-18T15:00:00+00:00' GROUP BY model"
).fetchall())
