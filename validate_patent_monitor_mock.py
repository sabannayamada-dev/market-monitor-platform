from __future__ import annotations

import csv
import json
from pathlib import Path
from tempfile import TemporaryDirectory

from patent_monitor.pipeline import PatentPipeline, PipelineConfig, mock_patents, read_companies


ROOT = Path(__file__).resolve().parent


def read_rows(path: str) -> list[dict[str, str]]:
    with Path(path).open("r", encoding="utf-8-sig", newline="") as handle:
        return list(csv.DictReader(handle))


def main() -> int:
    with TemporaryDirectory(prefix="patent_monitor_mock_") as directory:
        work = Path(directory)
        config = PipelineConfig.from_json(ROOT / "patent_monitor_config.json")
        config.random_reject_audit_rate = 0.0
        companies = read_companies(ROOT / "patent_company_master.csv")
        progress_messages: list[str] = []
        result = PatentPipeline(
            config,
            companies,
            work / "history.sqlite3",
            work / "outputs",
            gemini_api_key="",
            progress=lambda message, _ratio: progress_messages.append(message),
        ).run(mock_patents(), "mock_validation")

        rows = read_rows(result["evaluation_csv"])
        family_rows = [row for row in rows if row["family_key"] == "MOCKFAMSEMICONDUCTOR"]
        matched_ids = {row["company_id"] for row in rows if row["company_id"]}
        routes = {row["route"] for row in rows}

        checks = {
            "入力12件": result["input_count"] == 12,
            "ファミリー統合後11件": result["family_count"] == 11,
            "重複ファミリーを2件へ統合": len(family_rows) == 1 and family_rows[0]["duplicate_count"] == "2",
            "日本語社名の完全一致": "JP8035" in matched_ids,
            "英語別名の一致": {"JP3402", "JP7203", "JP6758", "JP6857"}.issubset(matched_ids),
            "未上場出願人を未一致へ隔離": result["unmatched_count"] >= 1 and "unmatched" in routes,
            "重要候補をGPT送信待ちへ保存": "gpt_pending" in routes,
            "APIを呼ばずエラー0件": result["gpt_count"] == 0 and result["gemini_count"] == 0 and result["error_count"] == 0,
            "進捗通知あり": bool(progress_messages) and progress_messages[-1] == "完了",
            "5出力ファイルあり": all(Path(result[key]).exists() for key in (
                "evaluation_csv", "gpt_csv", "gemini_csv", "match_review_csv", "summary_json"
            )),
        }
        report = {
            "passed": all(checks.values()),
            "checks": checks,
            "pipeline_result": result,
            "routes": {route: sum(row["route"] == route for row in rows) for route in sorted(routes)},
            "matched_company_count": len(matched_ids),
        }
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
