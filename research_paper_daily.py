from __future__ import annotations

import argparse
import json

from research_paper_monitor.service import run_daily_service


def main() -> int:
    parser = argparse.ArgumentParser(description="Research paper discovery and digest monitor")
    parser.add_argument("--force-email", action="store_true")
    parser.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    result = run_daily_service(force_email=args.force_email, baseline=args.baseline)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["status"] in {"completed", "completed_with_errors", "baseline_completed"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
