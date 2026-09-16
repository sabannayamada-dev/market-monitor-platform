from __future__ import annotations

from datetime import datetime, timezone

from research_paper_monitor.config import RuntimePaths, load_config
from research_paper_monitor.database import PaperDatabase
from research_paper_monitor.scoring import score_paper


def main() -> int:
    paths = RuntimePaths.from_env()
    database = PaperDatabase(paths.database)
    profiles = list(load_config(paths.config)["profiles"])
    paper_ids = database.unscored_paper_ids()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for paper_id in paper_ids:
        database.save_score(score_paper(database.paper(paper_id), profiles), now)
    print(f"SCORES_RECALCULATED={len(paper_ids)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
