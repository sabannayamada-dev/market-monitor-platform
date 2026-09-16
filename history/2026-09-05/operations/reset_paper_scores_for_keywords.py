from __future__ import annotations

import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


APP = Path("/opt/patent-news-monitor/app")
DATABASE = Path("/var/lib/research-paper-monitor/research_papers.sqlite3")
BACKUPS = Path("/opt/patent-news-monitor/backups")


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    destination = BACKUPS / f"{stamp}_paper_keyword_change"
    destination.mkdir(parents=True, mode=0o750)
    shutil.copy2(APP / "research_paper_config.json", destination / "research_paper_config.json")
    source = sqlite3.connect(DATABASE)
    backup = sqlite3.connect(destination / "research_papers.sqlite3")
    try:
        source.backup(backup)
    finally:
        backup.close()
        source.close()
    with sqlite3.connect(DATABASE) as connection:
        removed = connection.execute("SELECT COUNT(*) FROM paper_scores").fetchone()[0]
        connection.execute("DELETE FROM paper_scores")
    print(f"BACKUP={destination}")
    print(f"SCORES_CLEARED={removed}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
