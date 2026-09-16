from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from monitor_core.journal import RunJournal
from monitor_core.locking import LockAlreadyHeld, acquire_lock_file
from monitor_core.registry import load_registry


ROOT = Path(__file__).resolve().parent


class MonitorPlatformTests(unittest.TestCase):
    def test_registry_loads_service_contract(self) -> None:
        registry = load_registry(ROOT / "monitor_services.json")
        self.assertEqual(
            {"stock-bottom", "patent-materiality", "gdelt-news", "research-paper"}, set(registry.services)
        )
        self.assertEqual(
            ("stock_bottom_daily.py",), registry.get("stock-bottom").command
        )

    def test_lock_rejects_live_file_and_reclaims_stale_file(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            lock = Path(temporary) / "service.lock"
            acquire_lock_file(lock, "first", stale_after_seconds=60)
            payload = json.loads(lock.read_text(encoding="utf-8"))
            self.assertEqual("first", payload["service"])
            with self.assertRaises(LockAlreadyHeld):
                acquire_lock_file(lock, "second", stale_after_seconds=60)
            old = time.time() - 120
            os.utime(lock, (old, old))
            acquire_lock_file(lock, "second", stale_after_seconds=60)
            payload = json.loads(lock.read_text(encoding="utf-8"))
            self.assertEqual("second", payload["service"])

    def test_journal_records_lifecycle(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = RunJournal(Path(temporary) / "platform.sqlite3")
            journal.start("demo", "run-1")
            journal.phase("demo", "run-1", "worker")
            journal.event("demo", "run-1", "sample", "working")
            journal.finish("demo", "run-1", "completed", 0, {"count": 2})
            latest = journal.latest("demo")
            self.assertIsNotNone(latest)
            assert latest is not None
            self.assertEqual("completed", latest["status"])
            self.assertEqual(0, latest["exit_code"])
            self.assertEqual(2, json.loads(latest["detail_json"])["count"])

    def test_journal_recovers_running_rows_after_interruption(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            journal = RunJournal(Path(temporary) / "platform.sqlite3")
            journal.start("demo", "abandoned")
            self.assertEqual(1, journal.recover_interrupted("demo"))
            latest = journal.latest("demo")
            assert latest is not None
            self.assertEqual("interrupted", latest["status"])
            self.assertEqual(143, latest["exit_code"])
            self.assertIsNotNone(latest["completed_at"])
            self.assertEqual(0, journal.recover_interrupted("demo"))

    def _run_demo(self, preflight_exit: int) -> tuple[subprocess.CompletedProcess[str], RunJournal, Path]:
        temporary = Path(tempfile.mkdtemp())
        marker = temporary / "worker-ran.txt"
        (temporary / "preflight.py").write_text(
            f"print('PREFLIGHT')\nraise SystemExit({preflight_exit})\n", encoding="utf-8"
        )
        (temporary / "worker.py").write_text(
            f"from pathlib import Path\nPath({str(marker)!r}).write_text('yes')\nprint('WORKER')\n",
            encoding="utf-8",
        )
        registry = {
            "schema_version": 1,
            "services": {
                "demo": {
                    "label": "demo",
                    "schedule": "manual",
                    "preflight": ["preflight.py"],
                    "command": ["worker.py"],
                    "timeout_seconds": 30,
                    "state_hint": "",
                }
            },
        }
        registry_path = temporary / "registry.json"
        registry_path.write_text(json.dumps(registry), encoding="utf-8")
        state_path = temporary / "platform.sqlite3"
        environment = os.environ.copy()
        environment.update(
            {
                "MONITOR_REGISTRY_PATH": str(registry_path),
                "MONITOR_PLATFORM_STATE_DB": str(state_path),
                "MONITOR_PLATFORM_LOCK_DIR": str(temporary / "locks"),
            }
        )
        completed = subprocess.run(
            [sys.executable, str(ROOT / "monitorctl.py"), "run", "demo"],
            cwd=ROOT,
            env=environment,
            text=True,
            encoding="utf-8",
            capture_output=True,
            timeout=60,
        )
        return completed, RunJournal(state_path), marker

    def test_runner_executes_preflight_worker_and_journals(self) -> None:
        completed, journal, marker = self._run_demo(0)
        self.assertEqual(0, completed.returncode, completed.stdout + completed.stderr)
        self.assertTrue(marker.exists())
        latest = journal.latest("demo")
        assert latest is not None
        self.assertEqual("completed", latest["status"])

    def test_runner_stops_when_preflight_fails(self) -> None:
        completed, journal, marker = self._run_demo(7)
        self.assertEqual(7, completed.returncode)
        self.assertFalse(marker.exists())
        latest = journal.latest("demo")
        assert latest is not None
        self.assertEqual("preflight_failed", latest["status"])


if __name__ == "__main__":
    unittest.main()
