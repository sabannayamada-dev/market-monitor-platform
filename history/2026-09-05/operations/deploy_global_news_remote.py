from __future__ import annotations

import os
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path


STAGING = Path("/tmp/gdelt-global-news-20260907")
APP = Path("/opt/patent-news-monitor/app")
DATABASE = Path("/var/lib/gdelt-news-monitor/gdelt_monitor.sqlite3")
BACKUPS = Path("/opt/patent-news-monitor/backups")


def install(relative: str) -> None:
    source = STAGING / relative
    destination = APP / relative
    if destination.exists():
        owner = destination.stat()
        mode = owner.st_mode & 0o777
        uid, gid = owner.st_uid, owner.st_gid
    else:
        owner = destination.parent.stat()
        mode = 0o640
        uid, gid = owner.st_uid, owner.st_gid
    temporary = destination.with_name(destination.name + ".global-news-new")
    shutil.copy2(source, temporary)
    os.chmod(temporary, mode)
    os.chown(temporary, uid, gid)
    os.replace(temporary, destination)


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = BACKUPS / f"{stamp}_gdelt_global_news"
    backup.mkdir(parents=True, mode=0o750)
    for relative in (
        "gdelt_monitor_config.json",
        "gdelt_monitor/database.py",
        "gdelt_monitor/emergency.py",
        "gdelt_monitor/notifications.py",
        "gdelt_monitor/service.py",
    ):
        source = APP / relative
        target = backup / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    source_db = sqlite3.connect(DATABASE)
    backup_db = sqlite3.connect(backup / "gdelt_monitor.sqlite3")
    try:
        source_db.backup(backup_db)
    finally:
        backup_db.close()
        source_db.close()
    for relative in (
        "gdelt_monitor_config.json",
        "test_gdelt_monitor.py",
        "gdelt_monitor/global_news.py",
        "gdelt_monitor/database.py",
        "gdelt_monitor/emergency.py",
        "gdelt_monitor/notifications.py",
        "gdelt_monitor/service.py",
    ):
        install(relative)
    print(f"GLOBAL_NEWS_DEPLOYMENT_OK backup={backup}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
