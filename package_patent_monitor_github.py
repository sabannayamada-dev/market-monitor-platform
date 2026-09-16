from __future__ import annotations

import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "github_patent_monitor_ready"
FILES = (
    "patent_monitor_daily.py",
    "collect_patent_backfill_forever.py",
    "analyze_patent_monitor_run.py",
    "audit_patent_company_master.py",
    "audit_patent_monitor_backfill.py",
    "compare_patent_monitor_rematch.py",
    "run_patent_monitor_backfill_audit.bat",
    "sync_patent_state_supabase.py",
    "patent_monitor_config.json",
    "patent_company_master.csv",
    "requirements_patent_monitor.txt",
    "validate_patent_monitor_mock.py",
    "test_patent_monitor.py",
    "test_supabase_state.py",
    "GITHUB_ACTIONS_SUPABASE_SETUP.md",
    "patent_monitor/__init__.py",
    "patent_monitor/pipeline.py",
    "patent_monitor/market_feedback.py",
    "patent_monitor/notifications.py",
    "patent_monitor/secure_store.py",
    "patent_monitor/supabase_state.py",
    ".github/workflows/patent-monitor-daily.yml",
)


def main() -> int:
    if TARGET.exists():
        resolved = TARGET.resolve()
        if resolved.parent != ROOT.resolve() or resolved.name != "github_patent_monitor_ready":
            raise RuntimeError(f"安全でない出力先のため削除を拒否しました: {resolved}")
        shutil.rmtree(resolved)
    TARGET.mkdir(parents=True, exist_ok=True)
    for relative in FILES:
        source = ROOT / relative
        destination = TARGET / relative
        destination.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, destination)
    (TARGET / ".gitignore").write_text(
        "__pycache__/\n*.py[cod]\noutputs/\n.patent_monitor_gui_state.json\n*.sqlite3\n*.sqlite3-*\n.env\n",
        encoding="ascii",
    )
    (TARGET / "README.md").write_text(
        "# Patent Materiality Monitor\n\n"
        "GitHub Actions deployment package. See `GITHUB_ACTIONS_SUPABASE_SETUP.md`.\n",
        encoding="utf-8",
    )
    print(TARGET)
    print(f"files={len(FILES) + 2}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
