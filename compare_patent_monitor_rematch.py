from __future__ import annotations

import argparse
import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

from patent_monitor.pipeline import CompanyMatcher, PipelineConfig, read_companies, split_values


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = ROOT / "outputs" / "patent_monitor"


def latest_run_dir(output_root: Path) -> Path:
    candidates = [
        path for path in output_root.iterdir()
        if path.is_dir() and (path / "patent_evaluations.csv").exists()
    ]
    if not candidates:
        raise FileNotFoundError(f"No patent monitor run directories found under {output_root}")
    return max(candidates, key=lambda path: path.stat().st_mtime)


def read_csv(path: Path) -> list[dict[str, str]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def safe_float(value: Any) -> float:
    try:
        return float(str(value or "").replace(",", ""))
    except (TypeError, ValueError):
        return 0.0


def compare(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir) if args.run_dir else latest_run_dir(Path(args.output_root))
    rows = read_csv(run_dir / "patent_evaluations.csv")
    config = PipelineConfig.from_json(args.config)
    matcher = CompanyMatcher(
        read_companies(args.company_master),
        threshold=config.fuzzy_match_threshold,
        margin=config.fuzzy_match_margin,
    )
    rematched: list[dict[str, Any]] = []
    for row in rows:
        if row.get("company_id"):
            continue
        match = matcher.match(split_values(row.get("applicants", "")))
        if not match.company_id:
            continue
        rematched.append({
            "publication_number": row.get("publication_number", ""),
            "previous_company_id": row.get("company_id", ""),
            "new_company_id": match.company_id,
            "new_company_name": match.company_name,
            "match_method": match.method,
            "match_confidence": round(match.confidence, 6),
            "technology_score": safe_float(row.get("technology_score")),
            "final_score": safe_float(row.get("final_score")),
            "route": row.get("route", ""),
            "applicants": row.get("applicants", ""),
            "title": row.get("title", ""),
        })
    rematched.sort(key=lambda item: (item["technology_score"], item["final_score"]), reverse=True)
    output_dir = Path(args.output_dir) if args.output_dir else run_dir
    rematch_path = output_dir / "rematch_impact.csv"
    write_csv(rematch_path, rematched, [
        "publication_number", "previous_company_id", "new_company_id", "new_company_name",
        "match_method", "match_confidence", "technology_score", "final_score",
        "route", "applicants", "title",
    ])
    summary = {
        "run_dir": str(run_dir.resolve()),
        "row_count": len(rows),
        "previous_matched_count": sum(bool(row.get("company_id")) for row in rows),
        "previous_unmatched_count": sum(not bool(row.get("company_id")) for row in rows),
        "newly_matchable_count": len(rematched),
        "newly_matchable_high_technology_count": sum(
            item["technology_score"] >= args.high_technology_threshold for item in rematched
        ),
        "newly_matchable_by_company": dict(Counter(item["new_company_id"] for item in rematched)),
        "output_files": {"rematch_impact": str(rematch_path.resolve())},
    }
    summary_path = output_dir / "rematch_impact_summary.json"
    summary["summary_json"] = str(summary_path.resolve())
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Replay company matching on an existing patent monitor run.")
    parser.add_argument("--run-dir", default="")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--company-master", default=str(ROOT / "patent_company_master.csv"))
    parser.add_argument("--config", default=str(ROOT / "patent_monitor_config.json"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--high-technology-threshold", type=float, default=24.0)
    print(json.dumps(compare(parser.parse_args()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
