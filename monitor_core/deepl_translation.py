from __future__ import annotations

import json
import logging
import os
import sqlite3
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Iterable
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger(__name__)
DEFAULT_KEY_FILE = "/var/lib/gdelt-news-monitor/deepl_api_key"
DEFAULT_CACHE_FILE = "/var/lib/market-monitor/deepl_translation.sqlite3"


def _already_japanese(text: str) -> bool:
    return any("\u3040" <= char <= "\u30ff" for char in text)


def _key() -> str:
    value = os.environ.get("DEEPL_API_KEY", "").strip()
    if value:
        return value
    path = Path(os.environ.get("DEEPL_API_KEY_FILE", DEFAULT_KEY_FILE))
    try:
        return path.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _endpoint(key: str) -> str:
    configured = os.environ.get("DEEPL_API_URL", "").strip()
    if configured:
        return configured
    host = "api-free.deepl.com" if key.endswith(":fx") else "api.deepl.com"
    return f"https://{host}/v2/translate"


def _connect(path: str) -> sqlite3.Connection:
    connection = sqlite3.connect(path, timeout=30)
    connection.execute("PRAGMA busy_timeout=30000")
    connection.execute(
        "CREATE TABLE IF NOT EXISTS translations ("
        "source_text TEXT PRIMARY KEY, translated_text TEXT NOT NULL, created_at TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE IF NOT EXISTS daily_usage (usage_date TEXT PRIMARY KEY, characters INTEGER NOT NULL)"
    )
    connection.commit()
    return connection


def translate_to_japanese(texts: Iterable[str]) -> dict[str, str]:
    """Translate unique non-Japanese strings, always falling back to the originals."""
    originals = list(dict.fromkeys(str(text or "").strip() for text in texts))
    result = {text: text for text in originals}
    pending = [text for text in originals if text and not _already_japanese(text)]
    key = _key()
    if not pending or not key:
        return result

    cache_path = os.environ.get("DEEPL_CACHE_FILE", DEFAULT_CACHE_FILE)
    daily_limit = max(0, int(os.environ.get("DEEPL_DAILY_CHARACTER_LIMIT", "16000")))
    today = datetime.now(ZoneInfo("Asia/Tokyo")).date().isoformat()
    connection: sqlite3.Connection | None = None
    try:
        connection = _connect(cache_path)
        placeholders = ",".join("?" for _ in pending)
        cached = connection.execute(
            f"SELECT source_text, translated_text FROM translations WHERE source_text IN ({placeholders})",
            pending,
        ).fetchall()
        for source, translated in cached:
            result[source] = translated
        missing = [text for text in pending if result[text] == text]
        if not missing:
            return result

        # Reserve the shared daily allowance while holding a write lock. This prevents
        # the GDELT and patent services from spending the same remaining characters.
        connection.execute("BEGIN IMMEDIATE")
        used_row = connection.execute(
            "SELECT characters FROM daily_usage WHERE usage_date=?", (today,)
        ).fetchone()
        remaining = daily_limit - int(used_row[0] if used_row else 0)
        selected: list[str] = []
        characters = 0
        for text in missing:
            if characters + len(text) > remaining or characters + len(text) > 12000:
                continue
            selected.append(text)
            characters += len(text)
        if not selected:
            connection.rollback()
            return result

        payload = json.dumps(
            {"text": selected, "target_lang": "JA", "preserve_formatting": True},
            ensure_ascii=False,
        ).encode("utf-8")
        request = urllib.request.Request(
            _endpoint(key),
            data=payload,
            headers={
                "Authorization": f"DeepL-Auth-Key {key}",
                "Content-Type": "application/json",
                "User-Agent": "market-monitor/1.0",
            },
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.loads(response.read().decode("utf-8"))
        translations = body.get("translations") or []
        if len(translations) != len(selected):
            raise RuntimeError("DeepL response count did not match request count")
        now = datetime.now().isoformat(timespec="seconds")
        for source, item in zip(selected, translations):
            translated = str(item.get("text") or "").strip()
            if translated:
                result[source] = translated
                connection.execute(
                    "INSERT OR REPLACE INTO translations VALUES(?,?,?)",
                    (source, translated, now),
                )
        connection.execute(
            "INSERT INTO daily_usage VALUES(?,?) ON CONFLICT(usage_date) DO UPDATE SET characters=characters+excluded.characters",
            (today, characters),
        )
        connection.commit()
    except (OSError, ValueError, RuntimeError, sqlite3.Error, urllib.error.URLError) as exc:
        if connection is not None:
            connection.rollback()
        LOGGER.warning("DeepL translation unavailable; using original text: %s", type(exc).__name__)
    finally:
        if connection is not None:
            connection.close()
    return result
