from __future__ import annotations

import json
from datetime import datetime, timezone

from research_paper_monitor.ai_review import review_candidates
from research_paper_monitor.config import RuntimePaths, load_config
from research_paper_monitor.database import PaperDatabase


def main() -> int:
    paths = RuntimePaths.from_env()
    config = load_config(paths.config)
    database = PaperDatabase(paths.database)
    threshold = float(config.get("scoring", {}).get("digest_threshold", 50))
    with database.connection() as connection:
        rows = connection.execute(
            """SELECT paper_id FROM paper_scores
               WHERE score>=? ORDER BY score DESC,scored_at DESC LIMIT 1""",
            (threshold,),
        ).fetchall()
    paper_ids = [str(row["paper_id"]) for row in rows]
    if not paper_ids:
        print("AI_DIAGNOSTIC=SKIPPED_NO_CANDIDATE")
        return 0
    candidates = database.candidate_rows(paper_ids, threshold, 1)
    stats, errors = review_candidates(database, candidates, config, datetime.now(timezone.utc))
    print("AI_DIAGNOSTIC=" + ("OK" if stats["ai_completed"] == 1 else "FAILED"))
    print(json.dumps({"stats": stats, "errors": errors}, ensure_ascii=False))
    return 0 if stats["ai_completed"] == 1 else 1


if __name__ == "__main__":
    raise SystemExit(main())
