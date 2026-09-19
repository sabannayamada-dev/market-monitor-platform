from __future__ import annotations

import json
import os
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


ENV_PATH = Path("/etc/patent-news-monitor.env")


def validate_key(value: str) -> str:
    key = str(value or "").strip()
    if len(key) < 8 or any(character.isspace() for character in key):
        raise ValueError("TypeSafe API key is empty or has an invalid format")
    return key


def test_key(key: str) -> dict[str, object]:
    payload = {
        "state": "API connectivity test",
        "model": "jev-latest",
        "questions": {
            "is_connectivity_test": {
                "type": "noul",
                "instructions": "Is this state explicitly an API connectivity test?",
                "criteria": {
                    "true": "It explicitly says it is an API connectivity test.",
                    "false": "It describes something else.",
                },
            }
        },
    }
    request = urllib.request.Request(
        "https://api.typesafe.ai/v1/systemone",
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8-sig"))
    answers = result.get("answers") if isinstance(result, dict) else None
    if not isinstance(answers, dict) or "is_connectivity_test" not in answers:
        raise RuntimeError("TypeSafe returned an unexpected response")
    return result


def update_environment(key: str) -> None:
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines() if ENV_PATH.exists() else []
    updated = [
        line for line in lines
        if not ("=" in line and line.split("=", 1)[0].strip() == "TYPESAFE_API_KEY")
    ]
    if updated and updated[-1].strip():
        updated.append("")
    updated.extend(["# TypeSafe AI / Jev emergency review", f"TYPESAFE_API_KEY={key}"])

    descriptor, temporary_name = tempfile.mkstemp(
        prefix="patent-news-monitor.env.", dir=str(ENV_PATH.parent), text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(updated).rstrip() + "\n")
        os.chmod(temporary_name, 0o600)
        os.replace(temporary_name, ENV_PATH)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)


def main() -> int:
    payload = json.load(sys.stdin)
    key = validate_key(payload.get("typesafe_api_key", ""))
    result = test_key(key)
    update_environment(key)
    usage = result.get("usage", {})
    print("TYPESAFE_API_TEST=OK")
    print("TYPESAFE_API_KEY=SET")
    if isinstance(usage, dict):
        print(f"INPUT_TOKENS={int(usage.get('input_tokens', 0))}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"TYPESAFE_SETUP_FAILED: {type(exc).__name__}: {exc}", file=sys.stderr)
        raise SystemExit(1)
