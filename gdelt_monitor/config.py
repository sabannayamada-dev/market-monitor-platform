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
    company_master: Path
    market_cap_snapshot: Path
    toc_cache: Path

    @classmethod
    def from_env(cls) -> "RuntimePaths":
        database = Path(
            os.getenv(
                "GDELT_STATE_DB",
                ROOT / "outputs" / "gdelt_monitor" / "gdelt_monitor.sqlite3",
            )
        )
        return cls(
            config=Path(os.getenv("GDELT_CONFIG_PATH", ROOT / "gdelt_monitor_config.json")),
            database=database,
            company_master=Path(
                os.getenv("GDELT_COMPANY_MASTER", ROOT / "patent_company_master.csv")
            ),
            market_cap_snapshot=Path(
                os.getenv(
                    "GDELT_MARKET_CAP_SNAPSHOT",
                    ROOT / "data" / "current_market_cap_snapshot.csv",
                )
            ),
            toc_cache=Path(
                os.getenv("GDELT_TOC_CACHE_DIR", database.parent / "toc_cache")
            ),
        )


def load_config(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8-sig"))
    if int(payload.get("schema_version", 0)) != 1:
        raise ValueError("gdelt config schema_version must be 1")
    if not payload.get("broad_queries"):
        raise ValueError("gdelt config must contain broad_queries")
    return payload


def env_flag(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def operation_stage() -> int:
    """Return the explicitly selected rollout stage (1=collect, 4=email)."""
    raw = os.getenv("GDELT_OPERATION_STAGE", "1").strip()
    try:
        stage = int(raw)
    except ValueError as exc:
        raise ValueError("GDELT_OPERATION_STAGE must be an integer from 1 to 4") from exc
    if stage not in {1, 2, 3, 4}:
        raise ValueError("GDELT_OPERATION_STAGE must be an integer from 1 to 4")
    return stage
