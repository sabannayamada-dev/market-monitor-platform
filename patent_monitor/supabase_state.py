from __future__ import annotations

import gzip
import hashlib
import json
import os
import sqlite3
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SupabaseStateConfig:
    url: str
    api_key: str
    bucket: str = "patent-monitor-state"
    object_path: str = "state/patent_monitor.sqlite3.gz"
    timeout_seconds: int = 60

    @classmethod
    def from_env(cls) -> "SupabaseStateConfig | None":
        url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        api_key = (
            os.getenv("SUPABASE_SECRET_KEY", "").strip()
            or os.getenv("SUPABASE_SERVICE_ROLE_KEY", "").strip()
        )
        if not url and not api_key:
            return None
        if not url or not api_key:
            raise RuntimeError("SUPABASE_URLとSUPABASE_SECRET_KEYの両方を設定してください")
        if not url.startswith("https://"):
            raise RuntimeError("SUPABASE_URLにはhttps://で始まるProject URLを指定してください")
        return cls(
            url=url,
            api_key=api_key,
            bucket=os.getenv("SUPABASE_STORAGE_BUCKET", "patent-monitor-state").strip()
            or "patent-monitor-state",
            object_path=os.getenv(
                "SUPABASE_DB_OBJECT", "state/patent_monitor.sqlite3.gz"
            ).strip()
            or "state/patent_monitor.sqlite3.gz",
            timeout_seconds=max(10, int(os.getenv("SUPABASE_TIMEOUT_SECONDS", "60"))),
        )


class SupabaseStateStore:
    """Persist the local SQLite state as a compressed private Storage object."""

    def __init__(self, config: SupabaseStateConfig):
        self.config = config

    def _url(self, authenticated: bool) -> str:
        bucket = urllib.parse.quote(self.config.bucket, safe="")
        object_path = urllib.parse.quote(self.config.object_path.lstrip("/"), safe="/")
        prefix = "authenticated/" if authenticated else ""
        return f"{self.config.url}/storage/v1/object/{prefix}{bucket}/{object_path}"

    def _headers(self, **extra: str) -> dict[str, str]:
        return {
            "apikey": self.config.api_key,
            "Authorization": f"Bearer {self.config.api_key}",
            "User-Agent": "patent-materiality-monitor/0.6",
            **extra,
        }

    def _request(self, request: urllib.request.Request, attempts: int = 4) -> bytes:
        for attempt in range(attempts):
            try:
                with urllib.request.urlopen(
                    request, timeout=self.config.timeout_seconds
                ) as response:
                    return response.read()
            except urllib.error.HTTPError as exc:
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt + 1 >= attempts:
                    try:
                        body = exc.read().decode("utf-8", errors="replace")[:1000]
                    except Exception:
                        body = ""
                    raise RuntimeError(
                        f"Supabase Storage HTTP {exc.code} {exc.reason}"
                        + (f" / {body}" if body else "")
                    ) from exc
            except (TimeoutError, urllib.error.URLError) as exc:
                if attempt + 1 >= attempts:
                    raise RuntimeError(f"Supabase Storage接続失敗: {exc}") from exc
            time.sleep(2 ** attempt)
        raise AssertionError("unreachable")

    @staticmethod
    def _quick_check(path: Path) -> None:
        connection = sqlite3.connect(path)
        try:
            row = connection.execute("PRAGMA quick_check").fetchone()
        finally:
            connection.close()
        if not row or row[0] != "ok":
            raise RuntimeError(f"SQLite整合性検査に失敗しました: {row[0] if row else '結果なし'}")

    def restore(self, database_path: str | Path) -> dict[str, Any]:
        target = Path(database_path)
        request = urllib.request.Request(
            self._url(authenticated=True), headers=self._headers(), method="GET"
        )
        try:
            compressed = self._request(request)
        except RuntimeError as exc:
            if "HTTP 404" in str(exc):
                return {"restored": False, "reason": "remote_state_not_found"}
            raise
        if not compressed:
            raise RuntimeError("Supabase上のSQLiteバックアップが空です")
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="patent_state_restore_") as directory:
            temporary = Path(directory) / "restored.sqlite3"
            try:
                temporary.write_bytes(gzip.decompress(compressed))
            except (OSError, EOFError) as exc:
                raise RuntimeError("Supabase上のSQLiteバックアップを展開できません") from exc
            self._quick_check(temporary)
            os.replace(temporary, target)
        return {
            "restored": True,
            "compressed_bytes": len(compressed),
            "database_bytes": target.stat().st_size,
            "sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }

    def backup(self, database_path: str | Path) -> dict[str, Any]:
        source_path = Path(database_path)
        if not source_path.exists():
            return {"uploaded": False, "reason": "local_database_not_found"}
        with tempfile.TemporaryDirectory(prefix="patent_state_backup_") as directory:
            snapshot = Path(directory) / "snapshot.sqlite3"
            source = sqlite3.connect(source_path, timeout=30)
            destination = sqlite3.connect(snapshot)
            try:
                source.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                source.backup(destination)
            finally:
                destination.close()
                source.close()
            self._quick_check(snapshot)
            raw = snapshot.read_bytes()
            compressed = gzip.compress(raw, compresslevel=6, mtime=0)
        request = urllib.request.Request(
            self._url(authenticated=False),
            data=compressed,
            headers=self._headers(
                **{
                    "Content-Type": "application/gzip",
                    "Cache-Control": "no-cache",
                    "x-upsert": "true",
                }
            ),
            method="POST",
        )
        response = self._request(request)
        return {
            "uploaded": True,
            "database_bytes": len(raw),
            "compressed_bytes": len(compressed),
            "sha256": hashlib.sha256(raw).hexdigest(),
            "response": json.loads(response.decode("utf-8")) if response else {},
        }
