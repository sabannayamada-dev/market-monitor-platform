from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent


def resolve(value: str | Path) -> Path:
    path = Path(value)
    return path if path.is_absolute() else ROOT / path


def check() -> dict[str, Any]:
    app_path = resolve(os.getenv("STOCK_BOTTOM_APP_PATH", ROOT / "app.py"))
    stock_db = resolve(os.getenv("STOCK_CACHE_DB", ROOT / "stock_cache.db"))
    state_db = resolve(
        os.getenv(
            "STOCK_BOTTOM_STATE_DB",
            ROOT / "outputs" / "stock_bottom_daily" / "notifications.sqlite3",
        )
    )
    errors: list[str] = []
    warnings: list[str] = []
    stock_count = 0
    detected_count = 0
    algorithm_version = ""

    if not app_path.is_file():
        errors.append(f"app.py not found: {app_path}")
    else:
        os.environ["STOCK_CACHE_DB"] = str(stock_db)
        try:
            spec = importlib.util.spec_from_file_location("stock_bottom_preflight_app", app_path)
            if spec is None or spec.loader is None:
                raise RuntimeError("module spec could not be created")
            module = importlib.util.module_from_spec(spec)
            sys.modules[spec.name] = module
            spec.loader.exec_module(module)
            algorithm_version = str(module.ALGORITHM_VERSION)
            if not hasattr(module, "ensure_symbol_cache"):
                errors.append("app.py does not expose ensure_symbol_cache")
        except Exception as exc:
            errors.append(f"app.py import failed: {type(exc).__name__}: {exc}")

    if not stock_db.is_file():
        errors.append(f"stock database not found: {stock_db}")
    else:
        try:
            connection = sqlite3.connect(f"file:{stock_db}?mode=rw", uri=True)
            stock_count = int(connection.execute("SELECT COUNT(*) FROM stocks").fetchone()[0])
            if algorithm_version:
                detected_count = int(
                    connection.execute(
                        "SELECT COUNT(*) FROM analyses WHERE algorithm_version=? AND detected=1",
                        (algorithm_version,),
                    ).fetchone()[0]
                )
            connection.close()
            if not stock_count:
                errors.append("stock database has no symbols")
        except sqlite3.Error as exc:
            errors.append(f"stock database is not writable or valid: {exc}")

    try:
        state_db.parent.mkdir(parents=True, exist_ok=True)
        probe = state_db.parent / ".preflight-write-test"
        probe.touch()
        probe.unlink()
    except OSError as exc:
        errors.append(f"state directory is not writable: {state_db.parent}: {exc}")

    smtp_ready = bool(
        os.getenv("SMTP_USER", "").strip()
        and os.getenv("SMTP_PASSWORD", "").strip()
        and os.getenv("EMAIL_RECIPIENT", os.getenv("SMTP_USER", "")).strip()
    )
    if not smtp_ready:
        warnings.append("SMTP is not configured; new signals will remain pending")

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
        "stock_count": stock_count,
        "detected_count": detected_count,
        "algorithm_version": algorithm_version,
        "smtp_ready": smtp_ready,
        "paths": {
            "app": str(app_path),
            "stock_db": str(stock_db),
            "state_db": str(state_db),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate the daily stock bottom monitor")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()
    result = check()
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("OK" if result["ok"] else "FAILED")
        for message in result["errors"]:
            print(f"ERROR: {message}")
        for message in result["warnings"]:
            print(f"WARNING: {message}")
        print(f"Symbols: {result['stock_count']}")
        print(f"Existing detections: {result['detected_count']}")
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
