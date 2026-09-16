from __future__ import annotations

import importlib.util
import os
import sys
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from stock_bottom_daily import quarantined_symbols, stable_date_is_recent
from stock_bottom_notifications import (
    initialize_baseline,
    mark_sent,
    pending_signals,
    record_signal,
)


ROOT = Path(__file__).resolve().parent


def load_app_module():
    spec = importlib.util.spec_from_file_location("stock_bottom_app_test", ROOT / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class StockBottomDailyTests(unittest.TestCase):
    def test_known_invalid_symbols_are_quarantined_and_can_be_overridden(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertTrue({"4384.T", "6173.T", "9338.T"} <= quarantined_symbols())
        with patch.dict(os.environ, {"STOCK_BOTTOM_QUARANTINED_SYMBOLS": "1111.T, 2222.t"}):
            self.assertEqual({"1111.T", "2222.T"}, quarantined_symbols())

    def test_batch_timing_step_does_not_suppress_exceptions_without_collector(self):
        app = load_app_module()
        token = app.CURRENT_BATCH_TIMINGS.set(None)
        try:
            with self.assertRaisesRegex(RuntimeError, "通信失敗"):
                with app.batch_timing_step("test"):
                    raise RuntimeError("通信失敗")
        finally:
            app.CURRENT_BATCH_TIMINGS.reset(token)

    def test_configured_database_path_does_not_probe_legacy_home(self):
        app = load_app_module()
        configured = Path("C:/service-data/stock_cache.db")
        with (
            patch.dict(os.environ, {"STOCK_CACHE_DB": str(configured)}),
            patch.object(Path, "exists", side_effect=AssertionError("legacy path was probed")),
        ):
            self.assertEqual(app.resolve_database_path(), configured)

    def test_baseline_is_not_emailed_and_new_signal_is_retryable(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "notifications.sqlite3"
            initialize_baseline(
                database,
                "v1",
                [{"symbol": "1111.T", "stable_date": "2026-01-01"}],
            )
            self.assertEqual(pending_signals(database), [])
            inserted = record_signal(
                database,
                "v1",
                "run-1",
                {
                    "symbol": "2222.T",
                    "name": "Example",
                    "stable_date": "2026-08-14",
                },
            )
            self.assertTrue(inserted)
            pending = pending_signals(database)
            self.assertEqual([row["symbol"] for row in pending], ["2222.T"])
            self.assertFalse(
                record_signal(
                    database,
                    "v1",
                    "run-2",
                    {"symbol": "2222.T", "stable_date": "2026-08-14"},
                )
            )
            self.assertEqual(len(pending_signals(database)), 1)
            mark_sent(database, pending)
            self.assertEqual(pending_signals(database), [])

    def test_historical_signal_is_not_queued(self):
        with TemporaryDirectory() as directory:
            database = Path(directory) / "notifications.sqlite3"
            self.assertFalse(
                record_signal(
                    database,
                    "v1",
                    "run-1",
                    {"symbol": "3333.T", "stable_date": "2020-01-01"},
                    initial_status="historical",
                )
            )
            self.assertEqual(pending_signals(database), [])

    def test_recent_date_filter(self):
        from datetime import date, timedelta

        self.assertTrue(stable_date_is_recent(date.today().isoformat(), 10))
        self.assertFalse(
            stable_date_is_recent((date.today() - timedelta(days=11)).isoformat(), 10)
        )
        self.assertFalse(stable_date_is_recent("unknown", 10))

    def test_force_daily_refresh_checks_incremental_data_for_detected_symbol(self):
        app = load_app_module()
        now = int(time.time())
        point = app.PricePoint(
            timestamp=now,
            date_text="2026-08-14",
            close=100.0,
            high=101.0,
            low=99.0,
            volume=1000,
        )
        cached = app.StockResult(
            symbol="1111.T",
            name="Example",
            currency="JPY",
            exchange="JPX",
            points=[point],
            first_trade_timestamp=now - 400 * 86400,
            security_type="stock",
        )
        saved = {"detected": True, "dataEndDate": point.date_text}
        with (
            patch.object(app, "initialize_database"),
            patch.object(app, "stock_row", return_value={"long_history_complete": 1}),
            patch.object(app, "cached_stock_result", return_value=cached),
            patch.object(app, "load_saved_analysis", return_value=saved),
            patch.object(app, "volume_data_is_sufficient", return_value=True),
            patch.object(app, "fetch_incremental_daily_history", return_value=(None, False)) as fetch,
        ):
            _history, analysis, changed = app.ensure_symbol_cache(
                "1111.T",
                update_missing=True,
                history_scope="long",
                prefer_saved_analysis=True,
                force_daily_refresh=True,
            )
        self.assertFalse(changed)
        self.assertIs(analysis, saved)
        fetch.assert_called_once()


if __name__ == "__main__":
    unittest.main()
