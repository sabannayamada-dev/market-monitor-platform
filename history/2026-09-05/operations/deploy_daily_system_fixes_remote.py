from __future__ import annotations

import json
import py_compile
import re
import shutil
import subprocess
from datetime import datetime
from pathlib import Path


APP = Path("/opt/patent-news-monitor/app")
STAGE = Path("/tmp/daily-system-fixes-20260915")
ENV = Path("/etc/patent-news-monitor.env")
FILES = (
    "patent_monitor/pipeline.py",
    "patent_monitor_config.json",
    "research_paper_monitor/collectors.py",
    "research_paper_config.json",
    "gdelt_monitor/global_news.py",
    "gdelt_monitor_config.json",
    "test_research_paper_monitor.py",
    "test_gdelt_monitor.py",
)
CORE_FILES = FILES[:6]


def main() -> int:
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup = APP.parent / "backups" / f"{stamp}_daily_system_fixes"
    for relative in FILES:
        if not (STAGE / relative).is_file():
            raise RuntimeError(f"missing staged file: {relative}")
    for relative in CORE_FILES:
        if not (APP / relative).is_file():
            raise RuntimeError(f"missing installed file: {relative}")
    backup.mkdir(parents=True, exist_ok=False)
    for relative in FILES:
        target = APP / relative
        if not target.is_file():
            continue
        destination = backup / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(target, destination)
    shutil.copy2(ENV, backup / "patent-news-monitor.env")

    try:
        for relative in FILES:
            target = APP / relative
            shutil.copy2(STAGE / relative, target)

        env_text = ENV.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"(?m)^PATENT_COMPANY_SEARCH_TIME_GUARD_MINUTES=.*$",
            "PATENT_COMPANY_SEARCH_TIME_GUARD_MINUTES=90",
            env_text,
        )
        if count != 1:
            raise RuntimeError("patent company-search guard setting was not found exactly once")
        ENV.write_text(updated, encoding="utf-8")

        for relative in (
            "patent_monitor/pipeline.py",
            "research_paper_monitor/collectors.py",
            "gdelt_monitor/global_news.py",
        ):
            py_compile.compile(str(APP / relative), doraise=True)
        for relative in (
            "patent_monitor_config.json",
            "research_paper_config.json",
            "gdelt_monitor_config.json",
        ):
            json.loads((APP / relative).read_text(encoding="utf-8"))
        patent_config = json.loads((APP / "patent_monitor_config.json").read_text(encoding="utf-8"))
        if float(patent_config["company_search_time_guard_minutes"]) != 90.0:
            raise RuntimeError("patent company-search guard was not set to 90 minutes")
        if (
            float(patent_config["company_search_time_guard_minutes"])
            + float(patent_config["postprocess_time_guard_minutes"])
            > 180.0
        ):
            raise RuntimeError("patent total processing budget exceeds 180 minutes")

        completed = subprocess.run(
            [str(APP.parent / "venv/bin/python"), "-m", "unittest",
             "test_research_paper_monitor.py", "test_gdelt_monitor.py"],
            cwd=APP,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
        print(completed.stdout)
        if completed.returncode:
            raise RuntimeError(f"remote tests failed: {completed.returncode}")
    except Exception:
        for relative in FILES:
            saved = backup / relative
            target = APP / relative
            if saved.is_file():
                shutil.copy2(saved, target)
            elif target.exists():
                target.unlink()
        shutil.copy2(backup / "patent-news-monitor.env", ENV)
        print("DEPLOYMENT_ROLLED_BACK=YES")
        raise

    print(f"BACKUP={backup}")
    print("PATENT_COMPANY_SEARCH_TIME_GUARD_MINUTES=90")
    print("DEPLOYMENT=OK")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
