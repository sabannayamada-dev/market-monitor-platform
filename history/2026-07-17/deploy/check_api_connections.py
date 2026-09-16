from __future__ import annotations

import base64
import smtplib
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path


ENV_PATH = Path("/etc/patent-news-monitor.env")


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    for raw_line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("\"").strip("'")
    return values


def http_check(name: str, request: urllib.request.Request) -> None:
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            response.read(512)
            elapsed = time.monotonic() - started
            print(f"{name}: OK (HTTP {response.status}, {elapsed:.2f}s)")
    except urllib.error.HTTPError as exc:
        elapsed = time.monotonic() - started
        print(f"{name}: AUTH/HTTP ERROR (HTTP {exc.code}, {elapsed:.2f}s)")
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"{name}: CONNECTION ERROR ({type(exc).__name__}, {elapsed:.2f}s)")


def check_epo(env: dict[str, str]) -> None:
    key = env.get("EPO_OPS_KEY", "")
    secret = env.get("EPO_OPS_SECRET", "")
    if not key or not secret:
        print("EPO OPS: NOT CONFIGURED")
        return
    token = base64.b64encode(f"{key}:{secret}".encode()).decode()
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    request = urllib.request.Request(
        "https://ops.epo.org/3.2/auth/accesstoken",
        data=body,
        headers={
            "Authorization": f"Basic {token}",
            "Content-Type": "application/x-www-form-urlencoded",
        },
        method="POST",
    )
    http_check("EPO OPS", request)


def check_openai(env: dict[str, str]) -> None:
    key = env.get("OPENAI_API_KEY", "")
    if not key:
        print("OpenAI: NOT CONFIGURED")
        return
    request = urllib.request.Request(
        "https://api.openai.com/v1/models",
        headers={"Authorization": f"Bearer {key}"},
    )
    http_check("OpenAI", request)


def check_gemini(env: dict[str, str]) -> None:
    key = env.get("GEMINI_API_KEY", "")
    if not key:
        print("Gemini: NOT CONFIGURED")
        return
    request = urllib.request.Request(
        "https://generativelanguage.googleapis.com/v1beta/models",
        headers={"x-goog-api-key": key},
    )
    http_check("Gemini", request)


def check_smtp(env: dict[str, str]) -> None:
    host = env.get("SMTP_HOST", "")
    user = env.get("SMTP_USER", "")
    password = env.get("SMTP_PASSWORD", "")
    if not host or not user or not password:
        print("SMTP: NOT CONFIGURED")
        return
    port = int(env.get("SMTP_PORT", "587"))
    started = time.monotonic()
    try:
        with smtplib.SMTP(host, port, timeout=20) as client:
            client.ehlo()
            client.starttls(context=ssl.create_default_context())
            client.ehlo()
            client.login(user, password)
        elapsed = time.monotonic() - started
        print(f"SMTP: OK (login only, no email sent, {elapsed:.2f}s)")
    except smtplib.SMTPAuthenticationError:
        elapsed = time.monotonic() - started
        print(f"SMTP: AUTH ERROR ({elapsed:.2f}s)")
    except Exception as exc:
        elapsed = time.monotonic() - started
        print(f"SMTP: CONNECTION ERROR ({type(exc).__name__}, {elapsed:.2f}s)")


def main() -> None:
    env = read_env()
    check_epo(env)
    check_openai(env)
    check_gemini(env)
    check_smtp(env)


if __name__ == "__main__":
    main()
