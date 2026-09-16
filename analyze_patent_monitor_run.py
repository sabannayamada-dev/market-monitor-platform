from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any

from patent_monitor.pipeline import CompanyMatcher, PipelineConfig, normalize_name, read_companies, split_values


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = ROOT / "outputs" / "patent_monitor"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        text = str(value or "").replace(",", "").strip()
        return float(text) if text else default
    except (TypeError, ValueError):
        return default


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


def jp_like(text: str) -> bool:
    return "[JP]" in text or bool(re.search(r"[ぁ-んァ-ン]", text))

def classify_unmatched_applicant(applicant: str, near_miss: dict[str, Any] | None = None) -> str:
    upper = applicant.upper()
    if near_miss and near_miss.get("similarity", 0) >= 0.90 and safe_float(near_miss.get("similarity_gap"), 0.0) >= 0.20:
        return "existing_company_alias_leak"
    if re.search(r"\b(?:UNIV|UNIVERSITY|COLLEGE|INSTITUTE|INST|NATL|NATIONAL)\b", upper):
        return "university_or_research_institute"
    if re.search(r"\b(?:GMBH|S\.?A\.?|NV|B\.?V\.?|LLC|PLC|AG|SAS)\b", upper):
        return "out_of_scope_foreign_company"
    if jp_like(applicant):
        return "subsidiary_candidate"
    return "unlisted_or_out_of_scope_company"


def company_alias_index(companies: list[Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for company in companies:
        if not company.target:
            continue
        for name in company.all_names:
            normalized = normalize_name(name)
            if not normalized:
                continue
            relation = "subsidiary" if name in company.subsidiaries else "alias"
            if name == company.company_name:
                relation = "company_name"
            rows.append({
                "company_id": company.company_id,
                "company_name": company.company_name,
                "alias": name,
                "normalized_alias": normalized,
                "relation": relation,
            })
    return rows


def best_near_miss(applicant: str, aliases: list[dict[str, Any]]) -> dict[str, Any] | None:
    normalized = normalize_name(applicant)
    if not normalized:
        return None
    candidates: list[dict[str, Any]] = []
    for alias in aliases:
        ratio = SequenceMatcher(None, normalized, alias["normalized_alias"]).ratio()
        shorter = min(len(normalized), len(alias["normalized_alias"]))
        if shorter >= 8 and (alias["normalized_alias"] in normalized or normalized in alias["normalized_alias"]):
            ratio = max(ratio, 0.94)
        candidates.append({**alias, "similarity": ratio})
    if not candidates:
        return None
    candidates.sort(key=lambda item: item["similarity"], reverse=True)
    best = candidates[0]
    second = next((item for item in candidates[1:] if item["company_id"] != best["company_id"]), None)
    best["second_company_id"] = second["company_id"] if second else ""
    best["second_company_name"] = second["company_name"] if second else ""
    best["second_similarity"] = round(second["similarity"], 6) if second else ""
    best["similarity_gap"] = round(best["similarity"] - second["similarity"], 6) if second else ""
    return best


def summarize_run(run_dir: Path) -> dict[str, Any]:
    rows = read_csv(run_dir / "patent_evaluations.csv")
    summary_path = run_dir / "run_summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8")) if summary_path.exists() else {}
    return {
        "run_id": run_dir.name,
        "app_version": summary.get("app_version", ""),
        "rows": len(rows),
        "counts": dict(Counter(row.get("route", "") for row in rows)),
        "matched_count": sum(bool(row.get("company_id")) for row in rows),
        "unmatched_count": sum(not bool(row.get("company_id")) for row in rows),
        "cpc_present_count": sum(bool(row.get("cpc_codes")) for row in rows),
        "ipc_present_count": sum(bool(row.get("ipc_codes")) for row in rows),
        "classification_present_count": sum(bool(row.get("cpc_codes") or row.get("ipc_codes")) for row in rows),
        "detail_enriched_count": sum(str(row.get("epo_detail_enriched", "")).strip() == "1" for row in rows),
        "detail_warning_count": sum(bool(row.get("epo_detail_warnings")) for row in rows),
        "detail_error_count": sum(bool(row.get("epo_detail_errors")) for row in rows),
        "gpt_decision_count": sum(bool(row.get("gpt_decision")) for row in rows),
        "gemini_decision_count": sum(bool(row.get("gemini_decision")) for row in rows),
        "score_mean": (
            sum(safe_float(row.get("final_score")) for row in rows) / len(rows)
            if rows else 0.0
        ),
        "data_quality_from_summary": summary.get("data_quality", {}),
    }


def analyze(args: argparse.Namespace) -> dict[str, Any]:
    run_dir = Path(args.run_dir) if args.run_dir else latest_run_dir(Path(args.output_root))
    previous_run_dir = Path(args.previous_run_dir) if args.previous_run_dir else None
    company_master = Path(args.company_master)
    config = PipelineConfig.from_json(args.config)
    companies = read_companies(company_master)
    matcher = CompanyMatcher(companies, threshold=config.fuzzy_match_threshold, margin=config.fuzzy_match_margin)
    aliases = company_alias_index(companies)
    rows = read_csv(run_dir / "patent_evaluations.csv")

    high_unmatched: list[dict[str, Any]] = []
    leak_candidates: list[dict[str, Any]] = []
    near_miss_candidates: list[dict[str, Any]] = []
    alias_patch_buckets: dict[tuple[str, str], dict[str, Any]] = {}
    unmatched_class_counts: Counter[str] = Counter()
    applicant_rollup: dict[str, dict[str, Any]] = {}

    for row in rows:
        applicants = row.get("applicants", "")
        technology_score = safe_float(row.get("technology_score"))
        final_score = safe_float(row.get("final_score"))
        is_unmatched = not bool(row.get("company_id"))
        if is_unmatched and technology_score >= args.high_technology_threshold:
            high_unmatched.append({
                "publication_number": row.get("publication_number", ""),
                "technology_score": technology_score,
                "final_score": final_score,
                "publication_date": row.get("publication_date", ""),
                "applicants": applicants,
                "title": row.get("title", ""),
                "cpc_codes": row.get("cpc_codes", ""),
                "ipc_codes": row.get("ipc_codes", ""),
                "source_url": row.get("source_url", ""),
                "jp_like": int(jp_like(applicants)),
            })
        if is_unmatched:
            match = matcher.match(split_values(applicants))
            if match.company_id:
                leak_candidates.append({
                    "publication_number": row.get("publication_number", ""),
                    "matched_company_id": match.company_id,
                    "matched_company_name": match.company_name,
                    "match_method": match.method,
                    "match_confidence": round(match.confidence, 6),
                    "technology_score": technology_score,
                    "final_score": final_score,
                    "applicants": applicants,
                    "title": row.get("title", ""),
                })
            for applicant in split_values(applicants):
                applicant_is_jp_like = jp_like(applicant)
                near_miss = None
                if technology_score >= args.high_technology_threshold or applicant_is_jp_like:
                    near_miss = best_near_miss(applicant, aliases)
                    if near_miss and near_miss["similarity"] >= args.near_miss_threshold:
                        near_miss_candidates.append({
                            "publication_number": row.get("publication_number", ""),
                            "applicant": applicant,
                            "suggested_company_id": near_miss["company_id"],
                            "suggested_company_name": near_miss["company_name"],
                            "matched_existing_name": near_miss["alias"],
                            "matched_existing_relation": near_miss["relation"],
                            "similarity": round(near_miss["similarity"], 6),
                            "second_company_id": near_miss["second_company_id"],
                            "second_company_name": near_miss["second_company_name"],
                            "second_similarity": near_miss["second_similarity"],
                            "similarity_gap": near_miss["similarity_gap"],
                            "technology_score": technology_score,
                            "final_score": final_score,
                            "jp_like": int(applicant_is_jp_like),
                            "title": row.get("title", ""),
                        })
                        gap = safe_float(near_miss.get("similarity_gap"), 0.0)
                        if near_miss["similarity"] >= getattr(args, "alias_patch_threshold", 0.90) and gap >= getattr(args, "alias_patch_gap", 0.20):
                            patch_key = (near_miss["company_id"], applicant)
                            bucket = alias_patch_buckets.setdefault(patch_key, {
                                "suggested_company_id": near_miss["company_id"],
                                "suggested_company_name": near_miss["company_name"],
                                "suggested_alias": applicant,
                                "matched_existing_name": near_miss["alias"],
                                "matched_existing_relation": near_miss["relation"],
                                "similarity": round(near_miss["similarity"], 6),
                                "similarity_gap": gap,
                                "count": 0,
                                "max_technology_score": 0.0,
                                "max_final_score": 0.0,
                                "sample_publications": [],
                                "sample_titles": [],
                                "recommended_column": "subsidiaries" if near_miss["relation"] == "subsidiary" else "aliases",
                                "review_status": "needs_human_confirmation",
                            })
                            bucket["count"] += 1
                            bucket["max_technology_score"] = max(bucket["max_technology_score"], technology_score)
                            bucket["max_final_score"] = max(bucket["max_final_score"], final_score)
                            if row.get("publication_number", "") not in bucket["sample_publications"] and len(bucket["sample_publications"]) < 8:
                                bucket["sample_publications"].append(row.get("publication_number", ""))
                            if row.get("title", "") not in bucket["sample_titles"] and len(bucket["sample_titles"]) < 3:
                                bucket["sample_titles"].append(row.get("title", ""))
                unmatched_class = classify_unmatched_applicant(applicant, near_miss)
                unmatched_class_counts[unmatched_class] += 1
                bucket = applicant_rollup.setdefault(applicant, {
                    "applicant": applicant,
                    "count": 0,
                    "max_technology_score": 0.0,
                    "max_final_score": 0.0,
                    "jp_like": int(applicant_is_jp_like),
                    "unmatched_class": unmatched_class,
                    "sample_publications": [],
                    "sample_titles": [],
                })
                bucket["count"] += 1
                bucket["max_technology_score"] = max(bucket["max_technology_score"], technology_score)
                bucket["max_final_score"] = max(bucket["max_final_score"], final_score)
                if len(bucket["sample_publications"]) < 5:
                    bucket["sample_publications"].append(row.get("publication_number", ""))
                if len(bucket["sample_titles"]) < 3:
                    bucket["sample_titles"].append(row.get("title", ""))

    applicant_candidates = [
        {
            **bucket,
            "sample_publications": " | ".join(bucket["sample_publications"]),
            "sample_titles": " | ".join(bucket["sample_titles"]),
        }
        for bucket in applicant_rollup.values()
        if bucket["jp_like"] or bucket["max_technology_score"] >= args.high_technology_threshold
    ]
    applicant_candidates.sort(
        key=lambda item: (item["jp_like"], item["max_technology_score"], item["count"]),
        reverse=True,
    )
    high_unmatched.sort(key=lambda item: (item["technology_score"], item["final_score"]), reverse=True)
    leak_candidates.sort(key=lambda item: (item["technology_score"], item["final_score"]), reverse=True)
    near_miss_candidates.sort(
        key=lambda item: (item["jp_like"], item["similarity"], item["technology_score"]),
        reverse=True,
    )
    alias_patch_candidates = [
        {
            **bucket,
            "sample_publications": " | ".join(bucket["sample_publications"]),
            "sample_titles": " | ".join(bucket["sample_titles"]),
        }
        for bucket in alias_patch_buckets.values()
    ]
    alias_patch_candidates.sort(
        key=lambda item: (item["max_technology_score"], item["similarity"], item["count"]),
        reverse=True,
    )

    diagnostics = {
        "run": summarize_run(run_dir),
        "previous_run": summarize_run(previous_run_dir) if previous_run_dir else None,
        "thresholds": {
            "high_technology_threshold": args.high_technology_threshold,
            "fuzzy_match_threshold": config.fuzzy_match_threshold,
            "fuzzy_match_margin": config.fuzzy_match_margin,
        },
        "findings": {
            "high_technology_unmatched_count": len(high_unmatched),
            "jp_like_unmatched_applicant_count": sum(item["jp_like"] for item in applicant_candidates),
            "currently_matchable_leak_count": len(leak_candidates),
            "near_miss_company_candidate_count": len(near_miss_candidates),
            "suggested_alias_patch_count": len(alias_patch_candidates),
            "unmatched_class_counts": dict(unmatched_class_counts),
            "top_unmatched_applicants": applicant_candidates[:20],
            "top_currently_matchable_leaks": leak_candidates[:20],
            "top_near_miss_company_candidates": near_miss_candidates[:20],
            "top_suggested_alias_patches": alias_patch_candidates[:20],
        },
        "output_files": {},
    }

    output_dir = Path(args.output_dir) if args.output_dir else run_dir
    diagnostics_path = output_dir / "run_diagnostics.json"
    high_unmatched_path = output_dir / "high_technology_unmatched.csv"
    leak_candidates_path = output_dir / "match_leak_candidates.csv"
    near_miss_candidates_path = output_dir / "near_miss_company_candidates.csv"
    applicant_candidates_path = output_dir / "unmatched_applicant_candidates.csv"
    alias_patch_candidates_path = output_dir / "suggested_alias_patches.csv"

    diagnostics["output_files"] = {
        "run_diagnostics": str(diagnostics_path.resolve()),
        "high_technology_unmatched": str(high_unmatched_path.resolve()),
        "match_leak_candidates": str(leak_candidates_path.resolve()),
        "near_miss_company_candidates": str(near_miss_candidates_path.resolve()),
        "unmatched_applicant_candidates": str(applicant_candidates_path.resolve()),
        "suggested_alias_patches": str(alias_patch_candidates_path.resolve()),
    }
    diagnostics_path.write_text(json.dumps(diagnostics, ensure_ascii=False, indent=2), encoding="utf-8")
    write_csv(high_unmatched_path, high_unmatched, [
        "publication_number", "technology_score", "final_score", "publication_date",
        "applicants", "title", "cpc_codes", "ipc_codes", "source_url", "jp_like",
    ])
    write_csv(leak_candidates_path, leak_candidates, [
        "publication_number", "matched_company_id", "matched_company_name", "match_method",
        "match_confidence", "technology_score", "final_score", "applicants", "title",
    ])
    write_csv(near_miss_candidates_path, near_miss_candidates, [
        "publication_number", "applicant", "suggested_company_id", "suggested_company_name",
        "matched_existing_name", "matched_existing_relation", "similarity",
        "second_company_id", "second_company_name", "second_similarity", "similarity_gap",
        "technology_score", "final_score", "jp_like", "title",
    ])
    write_csv(applicant_candidates_path, applicant_candidates, [
        "applicant", "count", "max_technology_score", "max_final_score", "jp_like",
        "unmatched_class", "sample_publications", "sample_titles",
    ])
    write_csv(alias_patch_candidates_path, alias_patch_candidates, [
        "suggested_company_id", "suggested_company_name", "suggested_alias",
        "recommended_column", "review_status", "matched_existing_name", "matched_existing_relation",
        "similarity", "similarity_gap", "count", "max_technology_score", "max_final_score",
        "sample_publications", "sample_titles",
    ])
    return diagnostics


def main() -> int:
    parser = argparse.ArgumentParser(description="Analyze Patent Materiality Monitor run outputs.")
    parser.add_argument("--run-dir", default="", help="Run directory. Defaults to latest run under output root.")
    parser.add_argument("--previous-run-dir", default="", help="Optional previous run directory for comparison.")
    parser.add_argument("--output-root", default=str(DEFAULT_OUTPUT_ROOT), help="Patent monitor output root.")
    parser.add_argument("--company-master", default=str(ROOT / "patent_company_master.csv"))
    parser.add_argument("--config", default=str(ROOT / "patent_monitor_config.json"))
    parser.add_argument("--output-dir", default="", help="Directory for diagnostic outputs. Defaults to run directory.")
    parser.add_argument("--high-technology-threshold", type=float, default=24.0)
    parser.add_argument("--near-miss-threshold", type=float, default=0.82)
    parser.add_argument("--alias-patch-threshold", type=float, default=0.90)
    parser.add_argument("--alias-patch-gap", type=float, default=0.20)
    diagnostics = analyze(parser.parse_args())
    print(json.dumps({
        "run_id": diagnostics["run"]["run_id"],
        "matched_count": diagnostics["run"]["matched_count"],
        "high_technology_unmatched_count": diagnostics["findings"]["high_technology_unmatched_count"],
        "currently_matchable_leak_count": diagnostics["findings"]["currently_matchable_leak_count"],
        "near_miss_company_candidate_count": diagnostics["findings"]["near_miss_company_candidate_count"],
        "suggested_alias_patch_count": diagnostics["findings"]["suggested_alias_patch_count"],
        "output_files": diagnostics["output_files"],
    }, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
