from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date, datetime, timedelta
from pathlib import Path
from types import ModuleType
from typing import Any
from urllib.parse import quote

from monitor_core.locking import acquire_lock_file
from stock_bottom_notifications import (
    SMTPConfig,
    baseline_exists,
    finish_run,
    initialize_baseline,
    mark_delivery_error,
    mark_sent,
    pending_signals,
    record_signal,
    send_digest,
    send_failure,
    start_run,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_QUARANTINED_SYMBOLS = {
    "4384.T", "4449.T", "4494.T", "6173.T", "7098.T", "9338.T",
}


def log(message: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


def load_stock_app(app_path: Path, stock_database: Path) -> ModuleType:
    os.environ["STOCK_CACHE_DB"] = str(stock_database)
    spec = importlib.util.spec_from_file_location("stock_bottom_app", app_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"app.pyを読み込めません: {app_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def smtp_config_from_env() -> SMTPConfig:
    return SMTPConfig.from_env()


def acquire_lock(path: Path) -> None:
    acquire_lock_file(path, "stock-bottom-daily", 6 * 3600)


def baseline_rows(app: ModuleType) -> list[dict[str, Any]]:
    with app.database_connection() as connection:
        rows = connection.execute(
            """SELECT s.symbol,s.name,a.stable_date,a.result_json
               FROM analyses a JOIN stocks s ON s.symbol=a.symbol
               WHERE a.algorithm_version=? AND a.detected=1""",
            (app.ALGORITHM_VERSION,),
        ).fetchall()
    result: list[dict[str, Any]] = []
    for row in rows:
        result.append(
            {
                "symbol": row["symbol"],
                "name": row["name"],
                "stable_date": row["stable_date"] or "unknown",
            }
        )
    return result


def quarantined_symbols() -> set[str]:
    configured = os.getenv(
        "STOCK_BOTTOM_QUARANTINED_SYMBOLS",
        ",".join(sorted(DEFAULT_QUARANTINED_SYMBOLS)),
    )
    return {item.strip().upper() for item in configured.split(",") if item.strip()}


def saved_symbols(app: ModuleType, limit: int = 0) -> list[str]:
    with app.database_connection() as connection:
        rows = connection.execute(
            "SELECT symbol FROM stocks ORDER BY symbol"
        ).fetchall()
    quarantine = quarantined_symbols()
    symbols = [str(row["symbol"]) for row in rows if str(row["symbol"]).upper() not in quarantine]
    return symbols[:limit] if limit > 0 else symbols


def signal_payload(app: ModuleType, history: Any, analysis: dict[str, Any]) -> dict[str, Any]:
    evaluation = app.primary_bottom_evaluation(analysis)
    current = history.points[-1] if history.points else None
    stable_date = (
        evaluation.get("date") if evaluation else analysis.get("stableDate")
    ) or "unknown"
    return {
        "symbol": history.symbol,
        "name": history.name,
        "currency": history.currency,
        "security_type": history.security_type,
        "stable_date": stable_date,
        "score_at_stable": analysis.get("scoreAtStable"),
        "reason": analysis.get("reason", ""),
        "bottom_verdict": evaluation.get("verdict") if evaluation else "",
        "stable_price": evaluation.get("price") if evaluation else None,
        "current_date": current.date_text if current else "",
        "current_price": current.close if current else None,
        "previous_peak_date": evaluation.get("previousPeakDate") if evaluation else "",
        "drawdown_from_peak_percent": (
            evaluation.get("drawdownFromPreviousPeakPercent") if evaluation else None
        ),
        "data_end_date": analysis.get("dataEndDate", ""),
        "source_url": f"https://finance.yahoo.com/quote/{quote(history.symbol)}",
    }


def stable_date_is_recent(stable_date: str, max_age_days: int) -> bool:
    try:
        value = date.fromisoformat(stable_date)
    except ValueError:
        return False
    return value >= date.today() - timedelta(days=max(0, max_age_days))


def analyze_symbol(
    app: ModuleType, symbol: str, deadline: float | None,
) -> dict[str, Any]:
    if deadline is not None and time.monotonic() >= deadline:
        return {"symbol": symbol, "skipped": True, "error": "runtime deadline"}
    try:
        history, analysis, changed = app.ensure_symbol_cache(
            symbol,
            update_missing=True,
            history_scope="long",
            prefer_saved_analysis=True,
            force_daily_refresh=True,
        )
        return {
            "symbol": symbol,
            "updated": bool(changed),
            "detected": bool(analysis.get("detected")),
            "payload": signal_payload(app, history, analysis)
            if analysis.get("detected") else None,
        }
    except Exception as exc:
        return {
            "symbol": symbol,
            "updated": False,
            "detected": False,
            "error": f"{type(exc).__name__}: {exc}",
        }


def run(args: argparse.Namespace) -> dict[str, Any]:
    app_path = Path(args.app_path).resolve()
    stock_database = Path(args.stock_db).resolve()
    state_database = Path(args.state_db).resolve()
    lock_path = state_database.parent / ".stock-bottom-daily.lock"
    acquire_lock(lock_path)
    app: ModuleType | None = None
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S")
    smtp = smtp_config_from_env()
    started = False
    try:
        app = load_stock_app(app_path, stock_database)
        app.initialize_database()
        if not baseline_exists(state_database, app.ALGORITHM_VERSION):
            count = initialize_baseline(
                state_database, app.ALGORITHM_VERSION, baseline_rows(app)
            )
            log(f"初回ベースライン登録: 過去の底判定{count}件を通知対象外にしました")
        if args.baseline_only:
            return {"run_id": run_id, "baseline_only": True, "success": True}

        quarantine = quarantined_symbols()
        symbols = saved_symbols(app, args.max_symbols)
        if quarantine:
            log("無効銘柄コードを隔離: " + ", ".join(sorted(quarantine)))
        start_run(state_database, run_id, app.ALGORITHM_VERSION, len(symbols))
        started = True
        max_runtime_minutes = max(0.0, float(args.max_runtime_minutes))
        deadline = (
            time.monotonic() + max_runtime_minutes * 60
            if max_runtime_minutes > 0 else None
        )
        results: list[dict[str, Any]] = []
        workers = max(1, min(8, int(args.workers)))
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = {
                executor.submit(analyze_symbol, app, symbol, deadline): symbol
                for symbol in symbols
            }
            for index, future in enumerate(as_completed(futures), 1):
                result = future.result()
                results.append(result)
                if index == 1 or index % 10 == 0 or index == len(symbols):
                    errors = sum(bool(item.get("error")) for item in results)
                    detected = sum(bool(item.get("detected")) for item in results)
                    log(
                        f"進捗 {index}/{len(symbols)} / 底判定={detected} / エラー={errors}"
                    )

        new_count = 0
        detected_count = 0
        for result in results:
            payload = result.get("payload")
            if not result.get("detected") or not isinstance(payload, dict):
                continue
            detected_count += 1
            status = (
                "pending"
                if stable_date_is_recent(payload["stable_date"], args.signal_max_age_days)
                else "historical"
            )
            if record_signal(
                state_database,
                app.ALGORITHM_VERSION,
                run_id,
                payload,
                initial_status=status,
            ):
                new_count += 1

        pending = pending_signals(state_database)
        sent_count = 0
        if pending and smtp.configured:
            try:
                send_digest(smtp, pending, run_id)
            except Exception as exc:
                mark_delivery_error(
                    state_database, pending, f"{type(exc).__name__}: {exc}"
                )
                raise
            else:
                mark_sent(state_database, pending)
                sent_count = len(pending)
                log(f"底検知メール送信完了: {sent_count}件")
        elif pending:
            log(f"SMTP未設定のため{len(pending)}件を送信待ちにしました")
        else:
            log("新規の底判定はありません")

        error_rows = [item for item in results if item.get("error")]
        detail = {
            "errors": error_rows[:100],
            "skipped_count": sum(bool(item.get("skipped")) for item in results),
            "quarantined_symbols": sorted(quarantine),
        }
        finish_run(
            state_database,
            run_id,
            completed_count=len(results),
            updated_count=sum(bool(item.get("updated")) for item in results),
            detected_count=detected_count,
            new_signal_count=new_count,
            sent_count=sent_count,
            error_count=len(error_rows),
            status="completed_with_errors" if error_rows else "completed",
            detail_json=json.dumps(detail, ensure_ascii=False),
        )
        return {
            "run_id": run_id,
            "target_count": len(symbols),
            "completed_count": len(results),
            "updated_count": sum(bool(item.get("updated")) for item in results),
            "detected_count": detected_count,
            "new_signal_count": new_count,
            "sent_count": sent_count,
            "error_count": len(error_rows),
            "quarantined_count": len(quarantine),
            "success": True,
        }
    except Exception as exc:
        error = f"{type(exc).__name__}: {exc}"
        if started:
            finish_run(
                state_database,
                run_id,
                status="failed",
                error_count=1,
                detail_json=json.dumps(
                    {"error": error, "traceback": traceback.format_exc()}, ensure_ascii=False
                ),
            )
        try:
            send_failure(smtp, error, run_id)
        except Exception as mail_exc:
            log(f"失敗通知メールも送信できませんでした: {mail_exc}")
        raise
    finally:
        lock_path.unlink(missing_ok=True)


def parser() -> argparse.ArgumentParser:
    value = argparse.ArgumentParser(description="日次株価更新・底判定メール")
    value.add_argument("--app-path", default=os.getenv("STOCK_BOTTOM_APP_PATH", str(ROOT / "app.py")))
    value.add_argument("--stock-db", default=os.getenv("STOCK_CACHE_DB", str(ROOT / "stock_cache.db")))
    value.add_argument(
        "--state-db",
        default=os.getenv(
            "STOCK_BOTTOM_STATE_DB", str(ROOT / "outputs" / "stock_bottom_daily" / "notifications.sqlite3")
        ),
    )
    value.add_argument("--workers", type=int, default=int(os.getenv("STOCK_BOTTOM_WORKERS", "3")))
    value.add_argument(
        "--max-runtime-minutes",
        type=float,
        default=float(os.getenv("STOCK_BOTTOM_MAX_RUNTIME_MINUTES", "180")),
    )
    value.add_argument(
        "--signal-max-age-days",
        type=int,
        default=int(os.getenv("STOCK_BOTTOM_SIGNAL_MAX_AGE_DAYS", "10")),
    )
    value.add_argument("--max-symbols", type=int, default=0)
    value.add_argument("--baseline-only", action="store_true")
    return value


def main() -> int:
    args = parser().parse_args()
    try:
        result = run(args)
    except Exception as exc:
        log(f"日次底検知失敗: {type(exc).__name__}: {exc}")
        return 1
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
