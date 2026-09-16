from __future__ import annotations

import csv
import json
import os
from pathlib import Path

from patent_monitor.pipeline import PatentPipeline, PipelineConfig, mock_patents, read_companies
from patent_monitor.secure_store import load_credentials


ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "outputs" / "patent_monitor_historical_validation"
DB = OUTPUT / "historical_validation.sqlite3"
PATENT_NUMBER = "JP7301490B2"


def main() -> int:
    try:
        credentials = load_credentials()
    except OSError:
        credentials = {}
    openai_api_key = (credentials.get("openai_api_key", "") or os.getenv("OPENAI_API_KEY", "")).strip()
    gemini_api_key = (credentials.get("gemini_api_key", "") or os.getenv("GEMINI_API_KEY", "")).strip()

    record = next(item for item in mock_patents() if item.publication_number == PATENT_NUMBER)
    config = PipelineConfig.from_json(ROOT / "patent_monitor_config.json")
    # Routing only: keep every component score unchanged and send this known case to Gemini once.
    config.gemini_threshold = 0.0
    config.gemini_escalation_threshold = 0.0
    config.company_percentile_threshold = 100.0
    config.random_reject_audit_rate = 0.0
    result = PatentPipeline(
        config,
        read_companies(ROOT / "patent_company_master.csv"),
        DB,
        OUTPUT,
        openai_api_key=openai_api_key,
        gemini_api_key=gemini_api_key,
    ).run([record], "historical_blind_gemini_validation")

    with Path(result["evaluation_csv"]).open("r", encoding="utf-8-sig", newline="") as handle:
        row = next(csv.DictReader(handle))
    report = {
        "blind_test": True,
        "known_stock_reaction_sent_to_gemini": False,
        "case": "日東精工 特許第7301490号",
        "known_result_for_after_the_fact_comparison": "2023-07-03開示、翌営業日+11.6%",
        "local_scores": {
            key: row[key]
            for key in ("technology_score", "metadata_score", "novelty_score", "materiality_score", "final_score")
        },
        "gpt_first_stage": {
            "executed": bool(row["gpt_model"] and row["gpt_decision"]),
            "model": row["gpt_model"],
            "decision": row["gpt_decision"],
            "importance_score": row["gpt_importance"],
            "short_term_market_impact_score": row["gpt_short_term_market_impact"],
            "long_term_business_value_score": row["gpt_long_term_business_value"],
            "materiality_score": row["gpt_materiality"],
        },
        "gemini": {
            "executed": bool(row["gemini_model"] and row["gemini_decision"]),
            "model": row["gemini_model"],
            "decision": row["gemini_decision"],
            "importance_score": row["gemini_importance"],
            "short_term_market_impact_score": row["gemini_short_term_market_impact"],
            "long_term_business_value_score": row["gemini_long_term_business_value"],
            "materiality_score": row["gemini_materiality"],
            "market_impact_reason": row["gemini_market_impact_reason"],
            "expected_time_horizon": row["gemini_expected_time_horizon"],
            "summary": row["gemini_summary"],
        },
        "output": result,
    }
    report_path = Path(result["evaluation_csv"]).parent / "historical_blind_test_report.json"
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({**report, "report_json": str(report_path.resolve())}, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
