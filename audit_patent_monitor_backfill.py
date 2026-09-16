from __future__ import annotations

import argparse
import csv
import json
from collections import Counter, defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

from analyze_patent_monitor_run import analyze as analyze_run
from patent_monitor.pipeline import EPOOPSProvider, PatentPipeline, PipelineConfig, read_companies
from patent_monitor.secure_store import load_credentials


ROOT = Path(__file__).resolve().parent
DEFAULT_AUDIT_ROOT = ROOT / "outputs" / "patent_monitor_backfill_audit"


def write_csv(path: Path, rows: list[dict[str, Any]], columns: list[str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8-sig", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)


def read_csv(path: Path) -> list[dict[str, str]]:
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def windows_for_period(end_date: date, days: int, window_days: int) -> list[tuple[date, date]]:
    if days <= 0:
        raise ValueError("--days must be positive")
    if window_days <= 0:
        raise ValueError("--window-days must be positive")
    start_date = end_date - timedelta(days=days - 1)
    windows: list[tuple[date, date]] = []
    cursor = start_date
    while cursor <= end_date:
        window_end = min(end_date, cursor + timedelta(days=window_days - 1))
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def aggregate_outputs(audit_dir: Path, diagnostics: list[dict[str, Any]]) -> dict[str, Any]:
    leak_rows: dict[str, dict[str, Any]] = {}
    near_miss_rows: dict[str, dict[str, Any]] = {}
    alias_patch_rows: dict[str, dict[str, Any]] = {}
    high_rows: dict[str, dict[str, Any]] = {}
    applicant_buckets: dict[str, dict[str, Any]] = defaultdict(lambda: {
        "applicant": "",
        "count": 0,
        "max_technology_score": 0.0,
        "max_final_score": 0.0,
        "jp_like": 0,
        "sample_publications": [],
        "sample_titles": [],
    })

    for diagnostic in diagnostics:
        output_files = diagnostic.get("output_files", {})
        for row in read_csv(Path(output_files.get("match_leak_candidates", ""))):
            leak_rows.setdefault(row.get("publication_number", ""), row)
        for row in read_csv(Path(output_files.get("near_miss_company_candidates", ""))):
            key = row.get("publication_number", "") + "|" + row.get("applicant", "") + "|" + row.get("suggested_company_id", "")
            near_miss_rows.setdefault(key, row)
        for row in read_csv(Path(output_files.get("suggested_alias_patches", ""))):
            key = row.get("suggested_company_id", "") + "|" + row.get("suggested_alias", "")
            alias_patch_rows.setdefault(key, row)
        for row in read_csv(Path(output_files.get("high_technology_unmatched", ""))):
            high_rows.setdefault(row.get("publication_number", ""), row)
        for row in read_csv(Path(output_files.get("unmatched_applicant_candidates", ""))):
            applicant = row.get("applicant", "")
            if not applicant:
                continue
            bucket = applicant_buckets[applicant]
            bucket["applicant"] = applicant
            bucket["count"] += int(float(row.get("count") or 0))
            bucket["max_technology_score"] = max(
                bucket["max_technology_score"], float(row.get("max_technology_score") or 0)
            )
            bucket["max_final_score"] = max(
                bucket["max_final_score"], float(row.get("max_final_score") or 0)
            )
            bucket["jp_like"] = max(bucket["jp_like"], int(float(row.get("jp_like") or 0)))
            for key in ("sample_publications", "sample_titles"):
                for value in str(row.get(key) or "").split(" | "):
                    if value and value not in bucket[key] and len(bucket[key]) < 8:
                        bucket[key].append(value)

    leak_list = sorted(
        leak_rows.values(),
        key=lambda row: (float(row.get("technology_score") or 0), float(row.get("final_score") or 0)),
        reverse=True,
    )
    high_list = sorted(
        high_rows.values(),
        key=lambda row: (float(row.get("technology_score") or 0), float(row.get("final_score") or 0)),
        reverse=True,
    )
    near_miss_list = sorted(
        near_miss_rows.values(),
        key=lambda row: (
            int(float(row.get("jp_like") or 0)),
            float(row.get("similarity") or 0),
            float(row.get("technology_score") or 0),
        ),
        reverse=True,
    )
    alias_patch_list = sorted(
        alias_patch_rows.values(),
        key=lambda row: (
            float(row.get("max_technology_score") or 0),
            float(row.get("similarity") or 0),
            int(float(row.get("count") or 0)),
        ),
        reverse=True,
    )
    applicant_list = [
        {
            **bucket,
            "sample_publications": " | ".join(bucket["sample_publications"]),
            "sample_titles": " | ".join(bucket["sample_titles"]),
        }
        for bucket in applicant_buckets.values()
    ]
    applicant_list.sort(
        key=lambda row: (int(row["jp_like"]), float(row["max_technology_score"]), int(row["count"])),
        reverse=True,
    )

    leak_path = audit_dir / "aggregate_match_leak_candidates.csv"
    near_miss_path = audit_dir / "aggregate_near_miss_company_candidates.csv"
    alias_patch_path = audit_dir / "aggregate_suggested_alias_patches.csv"
    high_path = audit_dir / "aggregate_high_technology_unmatched.csv"
    applicant_path = audit_dir / "aggregate_unmatched_applicant_candidates.csv"
    write_csv(leak_path, leak_list, [
        "publication_number", "matched_company_id", "matched_company_name", "match_method",
        "match_confidence", "technology_score", "final_score", "applicants", "title",
    ])
    write_csv(high_path, high_list, [
        "publication_number", "technology_score", "final_score", "publication_date",
        "applicants", "title", "cpc_codes", "ipc_codes", "source_url", "jp_like",
    ])
    write_csv(near_miss_path, near_miss_list, [
        "publication_number", "applicant", "suggested_company_id", "suggested_company_name",
        "matched_existing_name", "matched_existing_relation", "similarity",
        "second_company_id", "second_company_name", "second_similarity", "similarity_gap",
        "technology_score", "final_score", "jp_like", "title",
    ])
    write_csv(alias_patch_path, alias_patch_list, [
        "suggested_company_id", "suggested_company_name", "suggested_alias",
        "recommended_column", "review_status", "matched_existing_name", "matched_existing_relation",
        "similarity", "similarity_gap", "count", "max_technology_score", "max_final_score",
        "sample_publications", "sample_titles",
    ])
    write_csv(applicant_path, applicant_list, [
        "applicant", "count", "max_technology_score", "max_final_score", "jp_like",
        "sample_publications", "sample_titles",
    ])
    return {
        "aggregate_match_leak_candidates": str(leak_path.resolve()),
        "aggregate_near_miss_company_candidates": str(near_miss_path.resolve()),
        "aggregate_suggested_alias_patches": str(alias_patch_path.resolve()),
        "aggregate_high_technology_unmatched": str(high_path.resolve()),
        "aggregate_unmatched_applicant_candidates": str(applicant_path.resolve()),
        "aggregate_match_leak_count": len(leak_list),
        "aggregate_near_miss_company_candidate_count": len(near_miss_list),
        "aggregate_suggested_alias_patch_count": len(alias_patch_list),
        "aggregate_high_technology_unmatched_count": len(high_list),
        "aggregate_unmatched_applicant_count": len(applicant_list),
    }


def run_backfill(args: argparse.Namespace) -> dict[str, Any]:
    timezone = ZoneInfo(args.timezone)
    end_date = (
        datetime.strptime(args.end_date, "%Y-%m-%d").date()
        if args.end_date else datetime.now(timezone).date()
    )
    windows = windows_for_period(end_date, args.days, args.window_days)
    audit_dir = Path(args.output_dir) if args.output_dir else (
        DEFAULT_AUDIT_ROOT / datetime.now(timezone).strftime("%Y%m%d_%H%M%S")
    )
    audit_dir.mkdir(parents=True, exist_ok=True)
    if args.dry_run:
        return {
            "dry_run": True,
            "audit_dir": str(audit_dir.resolve()),
            "windows": [{"start": str(start), "end": str(end)} for start, end in windows],
        }

    secrets = load_credentials()
    missing = [key for key in ("epo_ops_key", "epo_ops_secret") if not secrets.get(key)]
    if missing:
        raise RuntimeError("Missing EPO credentials: " + ", ".join(missing))
    if args.with_ai:
        missing_ai = [key for key in ("openai_api_key", "gemini_api_key") if not secrets.get(key)]
        if missing_ai:
            raise RuntimeError("--with-ai requires credentials: " + ", ".join(missing_ai))

    config = PipelineConfig.from_json(args.config)
    config.daily_digest_enabled = False
    config.urgent_notifications_enabled = False
    if not args.with_ai:
        config.random_reject_audit_rate = 0.0
    if args.detail_max_records is not None:
        config.ops_detail_max_records = args.detail_max_records
    if args.company_search_name_limit is not None:
        config.ops_company_search_name_limit = args.company_search_name_limit

    companies = read_companies(args.company_master)
    provider = EPOOPSProvider(
        secrets["epo_ops_key"],
        secrets["epo_ops_secret"],
        config.ops_requests_per_minute,
        config.request_timeout_seconds,
    )
    if config.ops_detail_cache_enabled:
        provider.set_detail_cache(config.ops_detail_cache_path or (audit_dir / "epo_detail_cache.sqlite3"))
    database = audit_dir / "patent_monitor_backfill_audit.sqlite3"
    diagnostics: list[dict[str, Any]] = []
    window_results: list[dict[str, Any]] = []
    capped_rows: list[dict[str, Any]] = []
    retry_rows: list[dict[str, Any]] = []

    for index, (start, end) in enumerate(windows, 1):
        print(f"[{index}/{len(windows)}] EPO backfill {start} to {end}", flush=True)
        records = provider.search_companies(
            companies,
            start.isoformat(),
            end.isoformat(),
            max_per_company=args.max_per_company,
            max_applicant_names=config.ops_company_search_name_limit,
            progress=lambda message: print(message, flush=True),
            checkpoint_path=audit_dir / f"company_search_checkpoint_{start}_{end}.json",
        )
        search_stats = provider.last_company_search_stats
        for row in search_stats.get("capped_companies", []):
            capped_rows.append({
                "window_start": str(start),
                "window_end": str(end),
                "company_id": row.get("company_id", ""),
                "company_name": row.get("company_name", ""),
                "record_count": row.get("record_count", 0),
                "max_per_company": args.max_per_company,
                "searched_names": " | ".join(row.get("searched_names", [])),
                "retry_reason": "hit_max_per_company",
            })
            retry_rows.append(capped_rows[-1])
        detail_summary = None
        if config.ops_enrich_details:
            detail_summary = provider.enrich_records(
                records,
                companies,
                max_records=config.ops_detail_max_records,
                match_threshold=config.fuzzy_match_threshold,
                match_margin=config.fuzzy_match_margin,
                progress=lambda message: print(message, flush=True),
            )
        pipeline = PatentPipeline(
            config,
            companies,
            database,
            audit_dir,
            openai_api_key=secrets.get("openai_api_key", "") if args.with_ai else "",
            gemini_api_key=secrets.get("gemini_api_key", "") if args.with_ai else "",
            progress=lambda message, ratio: print(f"{ratio:.1%} {message}", flush=True),
        )
        try:
            result = pipeline.run(records, f"epo_backfill_{start}_{end}")
        finally:
            pipeline.database.close()
        run_dir = Path(result["summary_json"]).parent
        analyze_args = argparse.Namespace(
            run_dir=str(run_dir),
            previous_run_dir="",
            output_root=str(audit_dir),
            company_master=args.company_master,
            config=args.config,
            output_dir=str(run_dir),
            high_technology_threshold=args.high_technology_threshold,
            near_miss_threshold=args.near_miss_threshold,
            alias_patch_threshold=args.alias_patch_threshold,
            alias_patch_gap=args.alias_patch_gap,
        )
        diagnostic = analyze_run(analyze_args)
        diagnostics.append(diagnostic)
        window_results.append({
            "start": str(start),
            "end": str(end),
            "input_records": len(records),
            "run_id": result["run_id"],
            "matched_count": diagnostic["run"]["matched_count"],
            "high_technology_unmatched_count": diagnostic["findings"]["high_technology_unmatched_count"],
            "currently_matchable_leak_count": diagnostic["findings"]["currently_matchable_leak_count"],
            "detail_enrichment": detail_summary,
            "company_search": {
                "failure_count": search_stats.get("failure_count", 0),
                "capped_company_count": len(search_stats.get("capped_companies", [])),
                "skipped_company_count": len(search_stats.get("skipped_companies", [])),
                "stopped_by_deadline": search_stats.get("stopped_by_deadline", False),
            },
            "summary_json": result["summary_json"],
            "diagnostics_json": diagnostic["output_files"]["run_diagnostics"],
        })

    aggregate = aggregate_outputs(audit_dir, diagnostics)
    capped_path = audit_dir / "capped_companies_retry_queue.csv"
    write_csv(capped_path, retry_rows, [
        "window_start", "window_end", "company_id", "company_name", "record_count",
        "max_per_company", "searched_names", "retry_reason",
    ])
    aggregate["capped_companies_retry_queue"] = str(capped_path.resolve())
    aggregate["capped_company_count"] = len(retry_rows)
    audit_summary = {
        "created_at": datetime.now(timezone).isoformat(timespec="seconds"),
        "audit_dir": str(audit_dir.resolve()),
        "days": args.days,
        "window_days": args.window_days,
        "max_per_company": args.max_per_company,
        "with_ai": args.with_ai,
        "windows": window_results,
        "totals": {
            "input_records": sum(item["input_records"] for item in window_results),
            "matched_count": sum(item["matched_count"] for item in window_results),
            "currently_matchable_leak_count": sum(item["currently_matchable_leak_count"] for item in window_results),
            "high_technology_unmatched_count": sum(item["high_technology_unmatched_count"] for item in window_results),
            "capped_company_count": len(retry_rows),
        },
        "aggregate_outputs": aggregate,
    }
    summary_path = audit_dir / "backfill_audit_summary.json"
    audit_summary["summary_json"] = str(summary_path.resolve())
    summary_path.write_text(json.dumps(audit_summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return audit_summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Run bounded EPO backfill audits for patent monitor quality checks.")
    parser.add_argument("--days", type=int, default=30)
    parser.add_argument("--window-days", type=int, default=7)
    parser.add_argument("--end-date", default="", help="YYYY-MM-DD. Defaults to today in timezone.")
    parser.add_argument("--timezone", default="Asia/Tokyo")
    parser.add_argument("--max-per-company", type=int, default=50, help="0 means unlimited per company per window.")
    parser.add_argument("--detail-max-records", type=int, default=None)
    parser.add_argument("--company-search-name-limit", type=int, default=None)
    parser.add_argument("--high-technology-threshold", type=float, default=24.0)
    parser.add_argument("--near-miss-threshold", type=float, default=0.82)
    parser.add_argument("--alias-patch-threshold", type=float, default=0.90)
    parser.add_argument("--alias-patch-gap", type=float, default=0.20)
    parser.add_argument("--with-ai", action="store_true", help="Enable GPT/Gemini review. Disabled by default for audit safety.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--output-dir", default="")
    parser.add_argument("--company-master", default=str(ROOT / "patent_company_master.csv"))
    parser.add_argument("--config", default=str(ROOT / "patent_monitor_config.json"))
    result = run_backfill(parser.parse_args())
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
