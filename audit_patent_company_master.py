from __future__ import annotations

import argparse
import csv
import json
from collections import defaultdict
from pathlib import Path
from typing import Any

from patent_monitor.pipeline import PipelineConfig, normalize_name, read_companies


ROOT = Path(__file__).resolve().parent


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def audit(args: argparse.Namespace) -> dict[str, Any]:
    company_master = Path(args.company_master)
    output_dir = Path(args.output_dir) if args.output_dir else company_master.parent / "outputs" / "patent_monitor_master_audit"
    output_dir.mkdir(parents=True, exist_ok=True)
    config = PipelineConfig.from_json(args.config)
    search_name_limit = args.search_name_limit if args.search_name_limit is not None else config.ops_company_search_name_limit
    companies = read_companies(company_master)

    overflow_rows: list[dict[str, Any]] = []
    short_name_rows: list[dict[str, Any]] = []
    normalized_index: dict[str, list[dict[str, str]]] = defaultdict(list)

    for company in companies:
        if not company.target:
            continue
        search_names = list(dict.fromkeys(
            name.replace('"', " ").strip()
            for name in [company.company_name, *company.aliases, *company.subsidiaries]
            if name.strip()
        ))
        if len(search_names) > search_name_limit:
            overflow_rows.append({
                "company_id": company.company_id,
                "company_name": company.company_name,
                "ticker": company.ticker,
                "search_name_count": len(search_names),
                "search_name_limit": search_name_limit,
                "searched_names": " | ".join(search_names[:search_name_limit]),
                "omitted_names": " | ".join(search_names[search_name_limit:]),
            })
        for relation, names in (
            ("company_name", [company.company_name]),
            ("alias", company.aliases),
            ("subsidiary", company.subsidiaries),
        ):
            for name in names:
                normalized = normalize_name(name)
                if not normalized:
                    continue
                normalized_index[normalized].append({
                    "company_id": company.company_id,
                    "company_name": company.company_name,
                    "ticker": company.ticker,
                    "relation": relation,
                    "name": name,
                    "normalized": normalized,
                })
                if len(normalized) <= args.short_name_length:
                    short_name_rows.append({
                        "company_id": company.company_id,
                        "company_name": company.company_name,
                        "ticker": company.ticker,
                        "relation": relation,
                        "name": name,
                        "normalized": normalized,
                        "normalized_length": len(normalized),
                    })

    duplicate_rows: list[dict[str, Any]] = []
    for normalized, entries in normalized_index.items():
        company_ids = sorted({entry["company_id"] for entry in entries})
        if len(company_ids) <= 1:
            continue
        duplicate_rows.append({
            "normalized": normalized,
            "company_count": len(company_ids),
            "company_ids": " | ".join(company_ids),
            "names": " | ".join(
                f"{entry['company_id']}:{entry['relation']}:{entry['name']}" for entry in entries
            ),
        })

    overflow_rows.sort(key=lambda row: (int(row["search_name_count"]), row["company_id"]), reverse=True)
    duplicate_rows.sort(key=lambda row: (int(row["company_count"]), row["normalized"]), reverse=True)
    short_name_rows.sort(key=lambda row: (int(row["normalized_length"]), row["company_id"]))

    overflow_path = output_dir / "company_search_name_overflow.csv"
    duplicate_path = output_dir / "duplicate_normalized_company_names.csv"
    short_path = output_dir / "short_normalized_company_names.csv"
    summary_path = output_dir / "company_master_audit_summary.json"

    write_csv(overflow_path, overflow_rows, [
        "company_id", "company_name", "ticker", "search_name_count", "search_name_limit",
        "searched_names", "omitted_names",
    ])
    write_csv(duplicate_path, duplicate_rows, [
        "normalized", "company_count", "company_ids", "names",
    ])
    write_csv(short_path, short_name_rows, [
        "company_id", "company_name", "ticker", "relation", "name", "normalized", "normalized_length",
    ])
    summary = {
        "company_master": str(company_master.resolve()),
        "company_count": len(companies),
        "target_company_count": sum(company.target for company in companies),
        "search_name_limit": search_name_limit,
        "overflow_company_count": len(overflow_rows),
        "duplicate_normalized_name_count": len(duplicate_rows),
        "short_normalized_name_count": len(short_name_rows),
        "output_files": {
            "company_search_name_overflow": str(overflow_path.resolve()),
            "duplicate_normalized_company_names": str(duplicate_path.resolve()),
            "short_normalized_company_names": str(short_path.resolve()),
            "summary": str(summary_path.resolve()),
        },
    }
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit patent company master for matching/search coverage risks.")
    parser.add_argument("--company-master", default=str(ROOT / "patent_company_master.csv"))
    parser.add_argument("--config", default=str(ROOT / "patent_monitor_config.json"))
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--search-name-limit", type=int, default=None)
    parser.add_argument("--short-name-length", type=int, default=3)
    print(json.dumps(audit(parser.parse_args()), ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
