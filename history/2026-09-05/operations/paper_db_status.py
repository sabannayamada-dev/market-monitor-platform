from __future__ import annotations

import json
import sqlite3


DATABASE = "/var/lib/research-paper-monitor/research_papers.sqlite3"


def main() -> int:
    connection = sqlite3.connect(f"file:{DATABASE}?mode=ro", uri=True)
    total = connection.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    scored = connection.execute("SELECT COUNT(*) FROM paper_scores").fetchone()[0]
    baseline_row = connection.execute(
        "SELECT state_value FROM service_state WHERE state_key='baseline_completed'"
    ).fetchone()
    cooldown_row = connection.execute(
        "SELECT state_value FROM service_state WHERE state_key='source_rate_limit:arxiv'"
    ).fetchone()
    print(f"PAPERS_TOTAL={total}")
    print(f"SCORED_TOTAL={scored}")
    for row in connection.execute("SELECT source,COUNT(*) FROM source_records GROUP BY source ORDER BY source"):
        print(f"SOURCE={row[0]} RECORDS={row[1]}")
    latest_run = connection.execute("SELECT run_id FROM collection_runs ORDER BY started_at DESC LIMIT 1").fetchone()
    if latest_run:
        for row in connection.execute(
            "SELECT source,status,fetched,error_message FROM source_runs WHERE run_id=? ORDER BY source",
            (latest_run[0],),
        ):
            print(f"LAST_SOURCE={row[0]} STATUS={row[1]} FETCHED={row[2]} ERROR={row[3]}")
    for row in connection.execute(
        """SELECT profile_label,COUNT(*) AS total,
                  SUM(CASE WHEN score>=50 THEN 1 ELSE 0 END) AS publishable,
                  SUM(CASE WHEN score>=70 THEN 1 ELSE 0 END) AS priority,
                  MAX(score) AS maximum
           FROM paper_scores GROUP BY profile_label ORDER BY profile_label"""
    ):
        print(
            "PROFILE=" + str(row[0])
            + f" TOTAL={row[1]} PUBLISHABLE={row[2]} PRIORITY={row[3]} MAX={row[4]:.0f}"
        )
    print("BASELINE_COMPLETED=" + ("YES" if baseline_row and baseline_row[0] else "NO"))
    if cooldown_row:
        state = json.loads(cooldown_row[0])
        print(f"ARXIV_CONSECUTIVE_429={int(state.get('consecutive_429', 0))}")
        print(f"ARXIV_COOLDOWN_UNTIL={state.get('cooldown_until', '')}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
