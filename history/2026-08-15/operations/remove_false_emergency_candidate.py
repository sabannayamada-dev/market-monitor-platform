import sqlite3


title = "Iran Says Oman Talks, Reopening Strait Of Hormuz Are Separate Issues; US Must Meet Conditions"
with sqlite3.connect("/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3") as connection:
    cursor = connection.execute(
        "DELETE FROM emergency_candidates WHERE event_key=? AND event_state=? AND title=?",
        ("chokepoint:strait_of_hormuz", "reopened", title),
    )
print(f"deleted={cursor.rowcount}")
