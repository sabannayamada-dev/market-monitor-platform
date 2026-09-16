from __future__ import annotations

import os
import subprocess
from pathlib import Path


def main() -> int:
    environment = os.environ.copy()
    for raw_line in Path("/etc/patent-news-monitor.env").read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#") and "=" in line:
            name, value = line.split("=", 1)
            environment[name.strip()] = value.strip()
    completed = subprocess.run(
        [
            "/opt/patent-news-monitor/venv/bin/python",
            "/opt/patent-news-monitor/app/research_paper_daily.py",
            "--baseline",
        ],
        cwd="/opt/patent-news-monitor/app",
        env=environment,
        user="app",
        group="app",
    )
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
