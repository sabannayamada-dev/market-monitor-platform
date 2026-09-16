from __future__ import annotations

import argparse
import json

from gdelt_monitor.service import run_daily_service


def main() -> int:
    parser = argparse.ArgumentParser(description="GDELT news materiality monitor")
    parser.add_argument("--max-queries", type=int)
    parser.add_argument("--force-email", action="store_true")
    args = parser.parse_args()
    result = run_daily_service(args.max_queries, args.force_email)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    healthy_statuses = {
        "completed", "completed_with_errors", "cooldown_skipped", "schedule_skipped", "email_only",
    }
    return 0 if result["status"] in healthy_statuses else 1


if __name__ == "__main__":
    raise SystemExit(main())
