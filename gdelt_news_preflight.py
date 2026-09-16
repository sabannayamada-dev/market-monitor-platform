from __future__ import annotations

import os
from datetime import datetime, timezone

from gdelt_monitor.adaptive import load_state
from gdelt_monitor.analysis import load_company_aliases
from gdelt_monitor.collector import build_tasks
from gdelt_monitor.config import RuntimePaths, load_config, operation_stage
from gdelt_monitor.database import NewsDatabase


def main() -> int:
    paths = RuntimePaths.from_env()
    config = load_config(paths.config)
    stage = operation_stage()
    tasks = build_tasks(config)
    aliases = load_company_aliases(paths.company_master)
    database = NewsDatabase(paths.database)
    adaptive = load_state(database, config.get("adaptive_control", {}), datetime.now(timezone.utc))
    if not tasks:
        raise RuntimeError("No GDELT queries configured")
    if not aliases:
        raise RuntimeError("No company aliases loaded")
    if stage >= 3:
        _require_secret("OPENAI_API_KEY")
        _require_secret("GEMINI_API_KEY")
    if stage >= 4:
        for name in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "EMAIL_RECIPIENT"):
            _require_secret(name)
    print("OK")
    print(f"Broad queries: {len(tasks)}")
    print(f"Company aliases: {len(aliases)}")
    print(f"Operation stage: {stage}")
    print(f"AI review: {'on' if stage >= 3 else 'off'}")
    print(f"Email delivery: {'on' if stage >= 4 else 'off'}")
    print(f"Adaptive interval x: {adaptive.interval_minutes:g} minutes")
    print(f"Adaptive cooldown y: {adaptive.cooldown_hours:g} hours")
    print(f"Query spacing z: {adaptive.query_spacing_seconds:g} seconds")
    print(f"Next collection: {adaptive.next_collection_at}")
    return 0


def _require_secret(name: str) -> None:
    value = os.getenv(name, "").strip()
    if not value or value.lower() in {"replace_me", "replace_with_app_password"}:
        raise RuntimeError(f"{name} is required for the selected GDELT operation stage")


if __name__ == "__main__":
    raise SystemExit(main())
