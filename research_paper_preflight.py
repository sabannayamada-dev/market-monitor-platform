from __future__ import annotations

import os

from monitor_core.mail import SMTPSettings
from research_paper_monitor.config import RuntimePaths, load_config, operation_stage
from research_paper_monitor.database import PaperDatabase


def main() -> int:
    paths = RuntimePaths.from_env()
    config = load_config(paths.config)
    stage = operation_stage()
    database = PaperDatabase(paths.database)
    enabled = [name for name in ("openalex", "arxiv") if config.get("sources", {}).get(name, {}).get("enabled")]
    if stage >= 4 and not SMTPSettings.from_env().configured:
        raise RuntimeError("SMTP is required when PAPER_OPERATION_STAGE=4")
    print("OK")
    print("Sources: " + ", ".join(enabled))
    print(f"Profiles: {len(config['profiles'])}")
    print(f"Operation stage: {stage}")
    print(f"Database: {database.path}")
    print(f"OpenAlex key: {'SET' if os.getenv('OPENALEX_API_KEY', '').strip() else 'MISSING (keyless mode)'}")
    print(f"OpenAI AI review: {'on' if stage >= 3 and os.getenv('OPENAI_API_KEY', '').strip() else 'off'}")
    print(f"Email delivery: {'on' if stage >= 4 else 'off'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
