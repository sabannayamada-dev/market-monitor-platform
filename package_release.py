from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

from app_meta import APP_BUILD_DATE, APP_NAME, APP_VERSION


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
DIST = OUTPUTS / "dist"


INCLUDE_FILES = [
    "app_meta.py",
    "company_collection.py",
    "company_scoring.py",
    "company_scoring_tool.py",
    "shokuba_enrichment.py",
    "women_activity_enrichment.py",
    "stock_bottom_financial_enrichment.py",
    "pytrends_enrichment.py",
    "launch_company_scoring_tool.bat",
    "run_self_check.bat",
    "run_release_audit.bat",
    "run_collection_sample.bat",
    "self_check.py",
    "release_audit.py",
    "requirements.txt",
    "README.md",
    "scoring_config.json",
    "work/sample_job_input.csv",
    "work/sample_job_input.xlsx",
    "work/sample_corporate_master.csv",
    "work/job_input_template.csv",
    "work/corporate_master_template.csv",
    "work/scoring_config_standard.json",
    "work/scoring_config_rd_focus.json",
    "work/scoring_config_wage_focus.json",
    "work/scoring_config_growth_test.json",
    "work/data_source_registry_template.json",
    "outputs/input_file_format_guide.md",
    "outputs/data_collection_completion_design.md",
    "outputs/collection_job_output_spec.md",
    "outputs/corporate_number_resolution_design.md",
    "outputs/edinet_bulk_collection_design.md",
    "outputs/prototype_test_plan.md",
    "outputs/prototype_release_notes.md",
    "outputs/how_to_run_anaconda.md",
    "outputs/release_readme.txt",
]


def main() -> int:
    DIST.mkdir(parents=True, exist_ok=True)
    package_name = f"company_scoring_tool_v{APP_VERSION}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.zip"
    package_path = DIST / package_name
    manifest = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "app_build_date": APP_BUILD_DATE,
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "included_files": [],
        "missing_files": [],
    }

    with zipfile.ZipFile(package_path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in INCLUDE_FILES:
            path = ROOT / relative
            if path.exists() and path.is_file():
                archive.write(path, arcname=relative)
                manifest["included_files"].append(relative)
            else:
                manifest["missing_files"].append(relative)
        archive.writestr("release_manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))

    manifest_path = DIST / "latest_release_manifest.json"
    with manifest_path.open("w", encoding="utf-8-sig") as handle:
        json.dump({**manifest, "package_path": str(package_path)}, handle, ensure_ascii=False, indent=2)

    print(f"package: {package_path}")
    print(f"manifest: {manifest_path}")
    if manifest["missing_files"]:
        print("missing_files:")
        for item in manifest["missing_files"]:
            print(f"- {item}")
    return 0 if not manifest["missing_files"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
