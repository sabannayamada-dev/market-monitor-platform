from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class RuntimePaths:
    config: Path
    database: Path

    @classmethod
    def from_env(cls) -> "RuntimePaths":
        return cls(
            config=Path(os.getenv("PAPER_CONFIG_PATH", ROOT / "research_paper_config.json")),
            database=Path(
                os.getenv(
                    "PAPER_STATE_DB",
                    ROOT / "outputs" / "research_paper_monitor" / "research_papers.sqlite3",
                )
            ),
        )


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if payload.get("schema_version") != 1:
        raise ValueError("research paper config schema_version must be 1")
    profiles = payload.get("profiles")
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("research paper config must contain profiles")
    if not any(payload.get("sources", {}).get(name, {}).get("enabled") for name in ("openalex", "arxiv", "jstage")):
        raise ValueError("at least one paper source must be enabled")
    return payload


def operation_stage() -> int:
    raw = os.getenv("PAPER_OPERATION_STAGE", "1").strip()
    try:
        stage = int(raw)
    except ValueError as exc:
        raise ValueError("PAPER_OPERATION_STAGE must be an integer from 1 to 4") from exc
    if stage not in {1, 2, 3, 4}:
        raise ValueError("PAPER_OPERATION_STAGE must be an integer from 1 to 4")
    return stage
