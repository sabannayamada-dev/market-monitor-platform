from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from patent_monitor.secure_store import load_credentials
from patent_monitor.supabase_state import SupabaseStateConfig, SupabaseStateStore
from patent_monitor.market_feedback import window_is_mature


class SupabaseStateTests(unittest.TestCase):
    def test_market_feedback_waits_until_window_can_mature(self):
        today = date(2026, 7, 15)
        self.assertFalse(window_is_mature("2026-07-10", 5, today))
        self.assertTrue(window_is_mature("2026-07-01", 5, today))
        self.assertFalse(window_is_mature("2026-01-01", 250, today))

    def test_environment_credentials_work_without_dpapi(self):
        values = {
            "OPENAI_API_KEY": "openai-test",
            "GEMINI_API_KEY": "gemini-test",
            "EPO_OPS_KEY": "epo-test",
            "EPO_OPS_SECRET": "epo-secret-test",
            "SMTP_PASSWORD": "smtp-test",
        }
        with patch.dict("os.environ", values, clear=True):
            credentials = load_credentials(Path("missing.dpapi"))
        self.assertEqual(credentials["openai_api_key"], "openai-test")
        self.assertEqual(credentials["epo_ops_secret"], "epo-secret-test")
        self.assertEqual(credentials["smtp_password"], "smtp-test")

    def test_sqlite_backup_and_restore_round_trip(self):
        config = SupabaseStateConfig("https://example.supabase.co", "secret-test")
        store = SupabaseStateStore(config)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "source.sqlite3"
            connection = sqlite3.connect(source)
            connection.execute("CREATE TABLE sample(value TEXT)")
            connection.execute("INSERT INTO sample VALUES('preserved')")
            connection.commit()
            connection.close()

            uploaded: bytes = b""

            def capture(request, attempts=4):
                nonlocal uploaded
                uploaded = request.data
                return b"{}"

            with patch.object(store, "_request", side_effect=capture):
                result = store.backup(source)
            self.assertTrue(result["uploaded"])
            self.assertTrue(uploaded.startswith(b"\x1f\x8b"))

            restored = root / "restored.sqlite3"
            with patch.object(store, "_request", return_value=uploaded):
                result = store.restore(restored)
            self.assertTrue(result["restored"])
            connection = sqlite3.connect(restored)
            try:
                value = connection.execute("SELECT value FROM sample").fetchone()[0]
            finally:
                connection.close()
            self.assertEqual(value, "preserved")


if __name__ == "__main__":
    unittest.main()
