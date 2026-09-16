from __future__ import annotations

import json
import sqlite3


database = sqlite3.connect(
    "file:/var/lib/research-paper-monitor/research_papers.sqlite3?mode=ro", uri=True
)
database.row_factory = sqlite3.Row
rows = database.execute(
    """SELECT p.title,s.profile_label,s.score,s.reasons_json
       FROM paper_scores s JOIN papers p ON p.paper_id=s.paper_id
       ORDER BY s.score DESC,p.published_at DESC LIMIT 15"""
).fetchall()
for index, row in enumerate(rows, 1):
    reasons = " / ".join(json.loads(row["reasons_json"]))
    print(f"{index}. [{row['score']:.0f}] {row['profile_label']} | {row['title']}")
    print(f"   {reasons}")
