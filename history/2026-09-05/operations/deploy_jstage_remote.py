from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


STAGING = Path("/tmp/research-paper-jstage-20260905")
APP = Path("/opt/patent-news-monitor/app")
DATABASE = Path("/var/lib/research-paper-monitor/research_papers.sqlite3")
BACKUPS = Path("/opt/patent-news-monitor/backups")


def install(source: Path, destination: Path) -> None:
    owner = destination.stat()
    temporary = destination.with_name(destination.name + ".jstage-new")
    shutil.copy2(source, temporary)
    os.chmod(temporary, owner.st_mode & 0o777)
    os.chown(temporary, owner.st_uid, owner.st_gid)
    os.replace(temporary, destination)


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUPS / f"{stamp}_paper_jstage"
    backup.mkdir(parents=True, mode=0o750)
    for relative in (
        "research_paper_config.json",
        "research_paper_monitor/collectors.py",
        "research_paper_monitor/config.py",
        "research_paper_monitor/notifications.py",
        "research_paper_monitor/scoring.py",
        "research_paper_monitor/service.py",
    ):
        source = APP / relative
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    source_database = sqlite3.connect(DATABASE)
    backup_database = sqlite3.connect(backup / "research_papers.sqlite3")
    try:
        source_database.backup(backup_database)
    finally:
        backup_database.close()
        source_database.close()
    for relative in (
        "research_paper_config.json",
        "test_research_paper_monitor.py",
        "research_paper_monitor/collectors.py",
        "research_paper_monitor/config.py",
        "research_paper_monitor/notifications.py",
        "research_paper_monitor/scoring.py",
        "research_paper_monitor/service.py",
    ):
        install(STAGING / relative, APP / relative)
    print(f"JSTAGE_DEPLOYMENT_OK backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
