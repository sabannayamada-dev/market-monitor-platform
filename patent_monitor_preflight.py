from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any

from patent_monitor.pipeline import PipelineConfig, read_companies
from patent_monitor.secure_store import load_credentials


ROOT = Path(__file__).resolve().parent


def resolve_path(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def check(require_email: bool = False) -> dict[str, Any]:
    config_path = resolve_path(os.getenv("PATENT_CONFIG_PATH", ROOT / "patent_monitor_config.json"))
    company_path = resolve_path(os.getenv("PATENT_COMPANY_MASTER", ROOT / "patent_company_master.csv"))
    output_path = resolve_path(os.getenv("PATENT_OUTPUT_DIR", ROOT / "outputs" / "patent_monitor"))
    database_path = resolve_path(
        os.getenv("PATENT_DB_PATH", output_path / "patent_monitor.sqlite3")
    )
    errors: list[str] = []
    warnings: list[str] = []

    if not config_path.is_file():
        errors.append(f"Config file not found: {config_path}")
        config = None
    else:
        try:
            config = PipelineConfig.from_json(config_path)
        except Exception as exc:
            errors.append(f"Config could not be loaded: {type(exc).__name__}: {exc}")
            config = None

    if not company_path.is_file():
        errors.append(f"Company master not found: {company_path}")
        company_count = 0
    else:
        try:
            company_count = len([company for company in read_companies(company_path) if company.target])
            if not company_count:
                errors.append("Company master has no enabled target companies")
        except Exception as exc:
            company_count = 0
            errors.append(f"Company master could not be loaded: {type(exc).__name__}: {exc}")

    try:
        output_path.mkdir(parents=True, exist_ok=True)
        probe = output_path / ".preflight-write-test"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        errors.append(f"Output directory is not writable: {output_path}: {exc}")

    if database_path.exists():
        try:
            connection = sqlite3.connect(f"file:{database_path}?mode=rw", uri=True)
            connection.execute("PRAGMA quick_check").fetchone()
            connection.close()
        except sqlite3.Error as exc:
            errors.append(f"Database is not writable or is damaged: {database_path}: {exc}")
    elif not database_path.parent.exists():
        errors.append(f"Database parent directory does not exist: {database_path.parent}")

    credentials = load_credentials()
    for name in ("epo_ops_key", "epo_ops_secret"):
        if not credentials.get(name):
            errors.append(f"Required credential is missing: {name}")
    for name in ("openai_api_key", "gemini_api_key"):
        if not credentials.get(name):
            warnings.append(f"AI credential is missing; reviews will remain pending: {name}")

    smtp_ready = bool(
        os.getenv("SMTP_USER", "").strip()
        and os.getenv("EMAIL_RECIPIENT", os.getenv("SMTP_USER", "")).strip()
        and credentials.get("smtp_password")
    )
    if not smtp_ready:
        message = "SMTP is not configured; important patents will be retained in the outbox"
        (errors if require_email else warnings).append(message)

    if config:
        if config.daily_gpt_limit < 0 or config.daily_gemini_limit < 0:
            errors.append("Daily AI limits must be zero or greater")
        if config.ops_detail_max_records < 0:
            errors.append("ops_detail_max_records must be zero or greater")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "company_count": company_count,
        "smtp_ready": smtp_ready,
        "paths": {
            "config": str(config_path),
            "company_master": str(company_path),
            "output": str(output_path),
            "database": str(database_path),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate daily patent monitor settings without API calls")
    parser.add_argument("--require-email", action="store_true")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = check(args.require_email)
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("OK" if result["ok"] else "FAILED")
        for message in result["errors"]:
            print(f"ERROR: {message}")
        for message in result["warnings"]:
            print(f"WARNING: {message}")
        print(f"Target companies: {result['company_count']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    sys.exit(main())
