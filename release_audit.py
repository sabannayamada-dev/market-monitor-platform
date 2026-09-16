from __future__ import annotations

import json
import re
import subprocess
import sys
import zipfile
from datetime import datetime
from pathlib import Path

from app_meta import APP_NAME, APP_VERSION


ROOT = Path(__file__).resolve().parent
OUTPUTS = ROOT / "outputs"
AUDIT_PATH = OUTPUTS / "release_audit_report.json"


def run_command(command: list[str], timeout: int = 180) -> dict[str, object]:
    process = subprocess.run(
        command,
        cwd=ROOT,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
    )
    return {
        "command": command,
        "returncode": process.returncode,
        "stdout_tail": process.stdout[-3000:],
        "stderr_tail": process.stderr[-3000:],
    }


def read_json(path: Path) -> dict[str, object]:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def scan_stale_versions() -> list[dict[str, str]]:
    pattern = re.compile(r"v0\.9\.\d+|app_version[\"']?\s*[:=]\s*[\"']0\.9\.\d+")
    targets = [
        ROOT / "README.md",
        ROOT / "outputs" / "prototype_test_plan.md",
        ROOT / "outputs" / "release_readme.txt",
        ROOT / "outputs" / "input_file_format_guide.md",
    ]
    stale: list[dict[str, str]] = []
    current_tokens = {f"v{APP_VERSION}", f"app_version={APP_VERSION}", f'"app_version": "{APP_VERSION}"'}
    for path in targets:
        if not path.exists():
            continue
        for line_no, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
            for match in pattern.findall(line):
                if match not in current_tokens and APP_VERSION not in match:
                    stale.append({"file": str(path.relative_to(ROOT)), "line": str(line_no), "match": match})
    return stale


def inspect_latest_package() -> dict[str, object]:
    manifest = read_json(OUTPUTS / "dist" / "latest_release_manifest.json")
    package_path = Path(str(manifest.get("package_path", ""))) if manifest.get("package_path") else Path()
    info: dict[str, object] = {
        "manifest_found": bool(manifest),
        "package_path": str(package_path) if package_path else "",
        "package_exists": package_path.exists() if package_path else False,
        "package_version": manifest.get("app_version"),
        "missing_files": manifest.get("missing_files", []),
        "zip_entries": 0,
        "has_release_manifest": False,
    }
    if package_path and package_path.exists():
        with zipfile.ZipFile(package_path) as archive:
            names = archive.namelist()
        info["zip_entries"] = len(names)
        info["has_release_manifest"] = "release_manifest.json" in names
    return info


def main() -> int:
    OUTPUTS.mkdir(parents=True, exist_ok=True)
    compile_result = run_command([sys.executable, "-m", "py_compile", "app_meta.py", "company_collection.py", "company_scoring.py", "company_scoring_tool.py", "pytrends_enrichment.py", "self_check.py", "package_release.py", "release_audit.py"])
    self_check_result = run_command([sys.executable, "self_check.py"])
    package_result = run_command([sys.executable, "package_release.py"])
    package_info = inspect_latest_package()
    stale_versions = scan_stale_versions()
    self_check_report = read_json(OUTPUTS / "self_check_report.json")

    checks = {
        "compile_ok": compile_result["returncode"] == 0,
        "self_check_ok": self_check_result["returncode"] == 0 and self_check_report.get("ok") is True,
        "package_ok": package_result["returncode"] == 0,
        "package_version_ok": package_info.get("package_version") == APP_VERSION,
        "package_missing_files_ok": package_info.get("missing_files") == [],
        "package_manifest_ok": package_info.get("has_release_manifest") is True,
        "stale_versions_ok": not stale_versions,
    }
    report = {
        "app_name": APP_NAME,
        "app_version": APP_VERSION,
        "checked_at": datetime.now().isoformat(timespec="seconds"),
        "checks": checks,
        "ok": all(checks.values()),
        "compile_result": compile_result,
        "self_check_result": self_check_result,
        "package_result": package_result,
        "package_info": package_info,
        "stale_versions": stale_versions,
    }
    with AUDIT_PATH.open("w", encoding="utf-8-sig") as handle:
        json.dump(report, handle, ensure_ascii=False, indent=2)
    print(f"release_audit ok={report['ok']}")
    print(f"report: {AUDIT_PATH}")
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
