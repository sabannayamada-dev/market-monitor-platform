from __future__ import annotations

import json
import os
import socket
import time
from datetime import datetime
from pathlib import Path


class LockAlreadyHeld(RuntimeError):
    pass


def _lock_description(path: Path) -> str:
    try:
        raw = path.read_text(encoding="utf-8").strip()
        if not raw:
            return "details unavailable"
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            return f"pid={raw}"
        return ", ".join(
            f"{key}={payload[key]}"
            for key in ("service", "pid", "host", "acquired_at")
            if payload.get(key) not in {None, ""}
        )
    except OSError:
        return "details unavailable"


def acquire_lock_file(
    path: str | Path,
    service: str,
    stale_after_seconds: int = 6 * 3600,
) -> None:
    """Acquire a portable file lock and retain useful owner metadata."""
    lock_path = Path(path)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    if lock_path.exists():
        try:
            age = time.time() - lock_path.stat().st_mtime
        except OSError:
            age = 0
        if age < max(1, stale_after_seconds):
            raise LockAlreadyHeld(
                f"{service} is already running ({_lock_description(lock_path)})"
            )
        lock_path.unlink(missing_ok=True)
    payload = json.dumps(
        {
            "service": service,
            "pid": os.getpid(),
            "host": socket.gethostname(),
            "acquired_at": datetime.now().isoformat(timespec="seconds"),
        },
        ensure_ascii=False,
    ).encode("utf-8")
    try:
        descriptor = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError as exc:
        raise LockAlreadyHeld(
            f"{service} is already running ({_lock_description(lock_path)})"
        ) from exc
    try:
        os.write(descriptor, payload)
    finally:
        os.close(descriptor)
