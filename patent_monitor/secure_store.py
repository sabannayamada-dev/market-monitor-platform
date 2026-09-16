from __future__ import annotations

import ctypes
import json
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any


APP_FOLDER = "PatentMaterialityMonitor"
SECRET_FILE = "credentials.dpapi"
CRYPTPROTECT_UI_FORBIDDEN = 0x01


class DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_ubyte))]


def default_secret_path() -> Path:
    base = Path(os.getenv("LOCALAPPDATA") or Path.home() / "AppData" / "Local")
    return base / APP_FOLDER / SECRET_FILE


def _blob(data: bytes) -> tuple[DATA_BLOB, Any]:
    buffer = ctypes.create_string_buffer(data)
    blob = DATA_BLOB(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ubyte)))
    return blob, buffer


def _require_windows() -> None:
    if os.name != "nt":
        raise OSError("資格情報の暗号化保存はWindowsでのみ利用できます")


def protect(data: bytes) -> bytes:
    _require_windows()
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(b"PatentMaterialityMonitor-v1")
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptProtectData(
        ctypes.byref(input_blob),
        "Patent Materiality Monitor",
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    _ = input_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def unprotect(data: bytes) -> bytes:
    _require_windows()
    input_blob, input_buffer = _blob(data)
    entropy_blob, entropy_buffer = _blob(b"PatentMaterialityMonitor-v1")
    output_blob = DATA_BLOB()
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    ok = crypt32.CryptUnprotectData(
        ctypes.byref(input_blob),
        None,
        ctypes.byref(entropy_blob),
        None,
        None,
        CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(output_blob),
    )
    _ = input_buffer, entropy_buffer
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(output_blob.pbData, output_blob.cbData)
    finally:
        kernel32.LocalFree(output_blob.pbData)


def save_credentials(credentials: dict[str, str], path: str | Path | None = None) -> Path:
    target = Path(path) if path else default_secret_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    sanitized = {key: str(value) for key, value in credentials.items() if str(value)}
    encrypted = protect(json.dumps(sanitized, ensure_ascii=False).encode("utf-8"))
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_bytes(encrypted)
    os.replace(temporary, target)
    return target


def load_credentials(path: str | Path | None = None) -> dict[str, str]:
    target = Path(path) if path else default_secret_path()
    credentials: dict[str, str] = {}
    if os.name == "nt" and target.exists():
        try:
            value = json.loads(unprotect(target.read_bytes()).decode("utf-8"))
            credentials.update({str(key): str(item) for key, item in value.items()})
        except (OSError, ValueError, UnicodeDecodeError):
            # A DPAPI file belongs to the Windows account that created it. Ignore
            # an inaccessible/stale file so environment credentials still work.
            pass
    environment_names = {
        "openai_api_key": "OPENAI_API_KEY",
        "gemini_api_key": "GEMINI_API_KEY",
        "epo_ops_key": "EPO_OPS_KEY",
        "epo_ops_secret": "EPO_OPS_SECRET",
        "smtp_password": "SMTP_PASSWORD",
    }
    for key, environment_name in environment_names.items():
        value = os.getenv(environment_name, "").strip()
        if value:
            credentials[key] = value
    return credentials


def delete_credentials(path: str | Path | None = None) -> bool:
    target = Path(path) if path else default_secret_path()
    if not target.exists():
        return False
    target.unlink()
    return True
