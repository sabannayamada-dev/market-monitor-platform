from __future__ import annotations

import os
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path


STAGING = Path("/tmp/research-paper-deploy-20260905")
APP = Path("/opt/patent-news-monitor/app")
ENV = Path("/etc/patent-news-monitor.env")
BACKUP = Path("/opt/patent-news-monitor/backups") / (
    datetime.now().strftime("%Y%m%d_%H%M%S") + "_research_paper_initial"
)


def copy_file(source: Path, destination: Path, mode: int, uid: int, gid: int) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)
    os.chmod(destination, mode)
    os.chown(destination, uid, gid)


def update_environment() -> None:
    existing_stat = ENV.stat()
    lines = ENV.read_text(encoding="utf-8").splitlines()
    names = {"PAPER_CONFIG_PATH", "PAPER_STATE_DB", "PAPER_OPERATION_STAGE"}
    kept = [
        line for line in lines
        if not ("=" in line and line.split("=", 1)[0].strip() in names)
    ]
    if kept and kept[-1].strip():
        kept.append("")
    kept.extend(
        [
            "# Research paper monitor runtime",
            "PAPER_CONFIG_PATH=/opt/patent-news-monitor/app/research_paper_config.json",
            "PAPER_STATE_DB=/var/lib/research-paper-monitor/research_papers.sqlite3",
            "PAPER_OPERATION_STAGE=1",
        ]
    )
    descriptor, temporary_name = tempfile.mkstemp(prefix="patent-news-monitor.env.", dir=str(ENV.parent), text=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(kept).rstrip() + "\n")
        os.chmod(temporary_name, existing_stat.st_mode & 0o777)
        os.chown(temporary_name, existing_stat.st_uid, existing_stat.st_gid)
        os.replace(temporary_name, ENV)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    app_stat = APP.stat()
    app_uid, app_gid = app_stat.st_uid, app_stat.st_gid
    BACKUP.mkdir(parents=True, exist_ok=False)
    shutil.copy2(APP / "monitor_services.json", BACKUP / "monitor_services.json")

    package_target = APP / "research_paper_monitor"
    package_target.mkdir(mode=0o750, parents=False, exist_ok=True)
    os.chown(package_target, app_uid, app_gid)
    for source in (STAGING / "research_paper_monitor").glob("*.py"):
        copy_file(source, package_target / source.name, 0o640, app_uid, app_gid)

    for name in (
        "research_paper_daily.py",
        "research_paper_preflight.py",
        "research_paper_config.json",
        "test_research_paper_monitor.py",
        "RESEARCH_PAPER_MONITOR_README.md",
        "monitor_services.json",
    ):
        copy_file(STAGING / name, APP / name, 0o640, app_uid, app_gid)

    update_environment()
    copy_file(
        STAGING / "research-paper-daily.service",
        Path("/etc/systemd/system/research-paper-daily.service"),
        0o644,
        0,
        0,
    )
    copy_file(
        STAGING / "research-paper-daily.timer",
        Path("/etc/systemd/system/research-paper-daily.timer"),
        0o644,
        0,
        0,
    )
    subprocess.run(["systemctl", "daemon-reload"], check=True)
    print(f"DEPLOYMENT_OK backup={BACKUP}")
    print("PAPER_OPERATION_STAGE=1")
    print("TIMER_ENABLED=no")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
