from __future__ import annotations

import argparse
import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from monitor_core.journal import RunJournal
from monitor_core.locking import acquire_lock_file
from monitor_core.registry import ServiceDefinition, ServiceRegistry, load_registry, resolve_command


ROOT = Path(__file__).resolve().parent


def registry_path() -> Path:
    return Path(os.getenv("MONITOR_REGISTRY_PATH", ROOT / "monitor_services.json")).resolve()


def state_database() -> Path:
    return Path(
        os.getenv(
            "MONITOR_PLATFORM_STATE_DB",
            ROOT / "outputs" / "monitor_platform" / "platform.sqlite3",
        )
    ).resolve()


def lock_directory() -> Path:
    return Path(
        os.getenv(
            "MONITOR_PLATFORM_LOCK_DIR",
            state_database().parent / "locks",
        )
    ).resolve()


def executable_command(registry: ServiceRegistry, command: tuple[str, ...]) -> list[str]:
    resolved = resolve_command(registry, command)
    if resolved[0].lower().endswith(".py"):
        return [sys.executable, "-u", *resolved]
    return resolved


def validate_registry(registry: ServiceRegistry) -> list[str]:
    errors: list[str] = []
    for service in registry.services.values():
        for label, command in (("preflight", service.preflight), ("command", service.command)):
            resolved = resolve_command(registry, command)
            if not Path(resolved[0]).is_file():
                errors.append(f"{service.service_id}: {label} not found: {resolved[0]}")
    return errors


def stream_command(command: list[str], timeout_seconds: int) -> tuple[int, list[str], float]:
    started = time.monotonic()
    tail: list[str] = []
    process = subprocess.Popen(
        command,
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        bufsize=1,
    )
    assert process.stdout is not None
    output_queue: queue.Queue[str | None] = queue.Queue()

    def read_output() -> None:
        try:
            for line in process.stdout:
                output_queue.put(line)
        finally:
            output_queue.put(None)

    reader = threading.Thread(target=read_output, name="monitor-output-reader", daemon=True)
    reader.start()
    try:
        output_finished = False
        while True:
            if time.monotonic() - started > timeout_seconds:
                process.kill()
                process.wait(timeout=10)
                print(f"TIMEOUT after {timeout_seconds}s", flush=True)
                return 124, tail[-100:], time.monotonic() - started
            try:
                line = output_queue.get(timeout=0.1)
            except queue.Empty:
                line = ""
            if line is None:
                output_finished = True
            elif line:
                print(line, end="", flush=True)
                tail.append(line.rstrip("\r\n"))
                tail = tail[-100:]
            if output_finished and process.poll() is not None:
                break
        return int(process.returncode or 0), tail[-100:], time.monotonic() - started
    finally:
        reader.join(timeout=1)
        process.stdout.close()


def run_service(registry: ServiceRegistry, service: ServiceDefinition, skip_preflight: bool) -> int:
    run_id = datetime.now().strftime("%Y%m%d_%H%M%S_") + uuid.uuid4().hex[:6]
    journal = RunJournal(state_database())
    lock_path = lock_directory() / f"{service.service_id}.lock"
    acquire_lock_file(lock_path, f"monitorctl:{service.service_id}", service.timeout_seconds + 900)
    recovered = journal.recover_interrupted(service.service_id)
    journal.start(service.service_id, run_id)
    detail: dict[str, Any] = {"schedule": service.schedule, "recovered_interrupted_runs": recovered}
    try:
        if not skip_preflight:
            journal.phase(service.service_id, run_id, "preflight")
            preflight = executable_command(registry, service.preflight)
            journal.event(service.service_id, run_id, "command_started", "preflight", detail={"command": preflight})
            exit_code, tail, duration = stream_command(preflight, min(600, service.timeout_seconds))
            detail["preflight"] = {
                "exit_code": exit_code,
                "duration_seconds": round(duration, 3),
                "tail": tail if exit_code else tail[-10:],
            }
            if exit_code:
                journal.finish(service.service_id, run_id, "preflight_failed", exit_code, detail)
                return exit_code
        journal.phase(service.service_id, run_id, "worker")
        command = executable_command(registry, service.command)
        journal.event(service.service_id, run_id, "command_started", "worker", detail={"command": command})
        exit_code, tail, duration = stream_command(command, service.timeout_seconds)
        detail["worker"] = {
            "exit_code": exit_code,
            "duration_seconds": round(duration, 3),
            "tail": tail if exit_code else tail[-20:],
        }
        status = "completed" if exit_code == 0 else "failed"
        journal.finish(service.service_id, run_id, status, exit_code, detail)
        return exit_code
    except Exception as exc:
        detail["exception"] = f"{type(exc).__name__}: {exc}"
        journal.event(service.service_id, run_id, "runner_exception", detail["exception"], level="error")
        journal.finish(service.service_id, run_id, "runner_failed", 1, detail)
        raise
    finally:
        lock_path.unlink(missing_ok=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="市場監視サービス共通管理CLI")
    sub = parser.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    sub.add_parser("validate")
    run_parser = sub.add_parser("run")
    run_parser.add_argument("service_id")
    run_parser.add_argument("--skip-preflight", action="store_true")
    preflight_parser = sub.add_parser("preflight")
    preflight_parser.add_argument("service_id")
    status_parser = sub.add_parser("status")
    status_parser.add_argument("service_id", nargs="?")
    history_parser = sub.add_parser("history")
    history_parser.add_argument("service_id")
    history_parser.add_argument("--limit", type=int, default=10)
    args = parser.parse_args()

    registry = load_registry(registry_path())
    if args.action == "list":
        for service in registry.services.values():
            print(f"{service.service_id}\t{service.schedule}\t{service.label}")
        return 0
    if args.action == "validate":
        errors = validate_registry(registry)
        print(json.dumps({"ok": not errors, "errors": errors}, ensure_ascii=False, indent=2))
        return 1 if errors else 0
    if args.action == "run":
        return run_service(registry, registry.get(args.service_id), args.skip_preflight)
    if args.action == "preflight":
        service = registry.get(args.service_id)
        exit_code, _, _ = stream_command(executable_command(registry, service.preflight), 600)
        return exit_code

    journal = RunJournal(state_database())
    if args.action == "status":
        ids = [args.service_id] if args.service_id else sorted(registry.services)
        payload = {service_id: journal.latest(service_id) for service_id in ids}
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0
    if args.action == "history":
        print(json.dumps(journal.history(args.service_id, args.limit), ensure_ascii=False, indent=2))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
