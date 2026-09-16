from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SERVICE_ID = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class ServiceDefinition:
    service_id: str
    label: str
    schedule: str
    command: tuple[str, ...]
    preflight: tuple[str, ...]
    timeout_seconds: int
    state_hint: str


@dataclass(frozen=True)
class ServiceRegistry:
    path: Path
    root: Path
    services: dict[str, ServiceDefinition]

    def get(self, service_id: str) -> ServiceDefinition:
        try:
            return self.services[service_id]
        except KeyError as exc:
            choices = ", ".join(sorted(self.services))
            raise KeyError(f"unknown service {service_id!r}; available: {choices}") from exc


def _command(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value or not all(isinstance(x, str) and x for x in value):
        raise ValueError(f"{name} must be a non-empty string array")
    return tuple(value)


def load_registry(path: str | Path) -> ServiceRegistry:
    registry_path = Path(path).resolve()
    payload = json.loads(registry_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != 1:
        raise ValueError("unsupported monitor registry schema_version")
    raw_services = payload.get("services")
    if not isinstance(raw_services, dict) or not raw_services:
        raise ValueError("monitor registry has no services")
    services: dict[str, ServiceDefinition] = {}
    for service_id, raw in raw_services.items():
        if not SERVICE_ID.fullmatch(str(service_id)):
            raise ValueError(f"invalid service id: {service_id!r}")
        if not isinstance(raw, dict):
            raise ValueError(f"service {service_id!r} must be an object")
        services[service_id] = ServiceDefinition(
            service_id=service_id,
            label=str(raw.get("label") or service_id),
            schedule=str(raw.get("schedule") or "manual"),
            command=_command(raw.get("command"), f"services.{service_id}.command"),
            preflight=_command(raw.get("preflight"), f"services.{service_id}.preflight"),
            timeout_seconds=max(1, int(raw.get("timeout_seconds", 3600))),
            state_hint=str(raw.get("state_hint") or ""),
        )
    return ServiceRegistry(registry_path, registry_path.parent, services)


def resolve_command(registry: ServiceRegistry, command: tuple[str, ...]) -> list[str]:
    first = Path(command[0])
    if not first.is_absolute():
        first = registry.root / first
    return [str(first.resolve()), *command[1:]]
