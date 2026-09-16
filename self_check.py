from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from datetime import datetime
from pathlib import Path

from app_meta import APP_BUILD_DATE, APP_NAME, APP_VERSION


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
REPORT_PATH = OUTPUTS / "self_check_report.json"


def check_file(path: Path) -> dict[str, object]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "size": path.stat().st_size if path.exists() else None,
    }


def module_available(name: str) -> bool:
    return importlib.util.find_spec(name) is not None


def load_json(path: Path) -> tuple[bool, str]:
    try:
        with path.open("r", encoding="utf-8-sig") as handle:
            json.load(handle)
        return True, ""
    except Exception as exc:
        return False, str(exc)


def run_command(command: list[str]) -> dict[str, object]:
    process = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
    )
    return {
        "command": command,
        "returncode": process.returncode,
        "stdout_tail": process.stdout[-2000:],
        "stderr_tail": process.stderr[-2000:],
    }


def main() -> int:
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    report: dict[str, object] = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "app_build_date": APP_BUILD_DATE,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "python": sys.version,
        "root": str(ROOT),
        "modules": {
            "pandas": module_available("pandas"),
            "openpyxl": module_available("openpyxl"),
            "requests": module_available("requests"),
            "tkinter": module_available("tkinter"),
        },
        "files": {},
        "json_files": {},
        "commands": {},
    }

    required_files = [
        ROOT / "app_meta.py",
        ROOT / "company_collection.py",
        ROOT / "company_scoring.py",
        ROOT / "company_scoring_tool.py",
        ROOT / "shokuba_enrichment.py",
        ROOT / "women_activity_enrichment.py",
        ROOT / "stock_bottom_financial_enrichment.py",
        ROOT / "pytrends_enrichment.py",
        ROOT / "launch_company_scoring_tool.bat",
        ROOT / "run_release_audit.bat",
        ROOT / "run_collection_sample.bat",
        ROOT / "release_audit.py",
        ROOT / "package_release.py",
        ROOT / "package_release.bat",
        ROOT / "requirements.txt",
        ROOT / "scoring_config.json",
        ROOT / "work" / "sample_job_input.csv",
        ROOT / "work" / "sample_corporate_master.csv",
        ROOT / "work" / "job_input_template.csv",
        ROOT / "work" / "corporate_master_template.csv",
        ROOT / "work" / "data_source_registry_template.json",
        ROOT / "outputs" / "input_file_format_guide.md",
        ROOT / "outputs" / "data_collection_completion_design.md",
        ROOT / "outputs" / "collection_job_output_spec.md",
        ROOT / "outputs" / "corporate_number_resolution_design.md",
        ROOT / "outputs" / "edinet_bulk_collection_design.md",
        ROOT / "outputs" / "prototype_test_plan.md",
        ROOT / "outputs" / "release_readme.txt",
    ]
    report["files"] = {path.name: check_file(path) for path in required_files}

    json_files = [
        ROOT / "scoring_config.json",
        ROOT / "work" / "scoring_config_standard.json",
        ROOT / "work" / "scoring_config_rd_focus.json",
        ROOT / "work" / "scoring_config_wage_focus.json",
        ROOT / "work" / "scoring_config_growth_test.json",
        ROOT / "work" / "data_source_registry_template.json",
    ]
    report["json_files"] = {
        str(path.relative_to(ROOT)): {"ok": ok, "error": error}
        for path in json_files
        for ok, error in [load_json(path)]
    }

    compile_result = run_command([sys.executable, "-m", "py_compile", "app_meta.py", "company_collection.py", "company_scoring.py", "company_scoring_tool.py", "shokuba_enrichment.py", "women_activity_enrichment.py", "stock_bottom_financial_enrichment.py", "pytrends_enrichment.py", "package_release.py", "release_audit.py"])
    sample_result = run_command(
        [
            sys.executable,
            "company_scoring.py",
            "--job-file",
            "work\\sample_job_input.csv",
            "--corporate-master",
            "work\\sample_corporate_master.csv",
        ]
    )
    report["commands"] = {
        "py_compile": compile_result,
        "sample_run": sample_result,
    }
    collection_result = run_command(
        [
            sys.executable,
            "company_collection.py",
            "--registry",
            "work\\data_source_registry_template.json",
            "--source-id",
            "job_file",
            "--output-dir",
            "outputs\\self_check_collection",
        ]
    )
    report["commands"]["collection_sample_run"] = collection_result

    output_files = [
        OUTPUTS / "ranking.csv",
        OUTPUTS / "all_records_clean.csv",
        OUTPUTS / "run_manifest.json",
        OUTPUTS / "errors.jsonl",
    ]
    report["output_files"] = {path.name: check_file(path) for path in output_files}

    ok = True
    ok = ok and all(item["exists"] for item in report["files"].values())  # type: ignore[union-attr]
    ok = ok and all(item["ok"] for item in report["json_files"].values())  # type: ignore[union-attr]
    ok = ok and compile_result["returncode"] == 0
    ok = ok and sample_result["returncode"] == 0
    ok = ok and collection_result["returncode"] == 0
    ok = ok and all(item["exists"] for item in report["output_files"].values())  # type: ignore[union-attr]
    report["ok"] = ok

    with REPORT_PATH.open("w", encoding="utf-8-sig") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)

    print(f"self_check ok={ok}")
    print(f"report: {REPORT_PATH}")
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
