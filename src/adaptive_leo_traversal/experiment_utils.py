"""Shared helpers for experiment runners."""

from __future__ import annotations

import json
from dataclasses import asdict
from enum import Enum
from pathlib import Path

from adaptive_leo_traversal.models import TraversalStatus


def parse_traversal_statuses(values: object) -> tuple[TraversalStatus, ...]:
    """Parse configured terminal traversal status names."""

    if values is None:
        return ()
    if isinstance(values, str):
        raw_values = [values]
    else:
        raw_values = list(values)  # type: ignore[arg-type]

    statuses: list[TraversalStatus] = []
    for raw in raw_values:
        value = str(raw)
        try:
            statuses.append(TraversalStatus(value))
        except ValueError as exc:
            allowed = ", ".join(status.value for status in TraversalStatus)
            raise ValueError(f"unknown traversal status {value!r}; allowed: {allowed}") from exc
    return tuple(statuses)


def safe_run_name(value: str) -> str:
    """Return a filesystem-friendly run name."""

    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
    cleaned = cleaned.strip("._-")
    return cleaned or "run"


def to_jsonable(value: object) -> object:
    """Convert dataclasses and enums into JSON-serializable values."""

    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def write_json_file(path: Path, payload: object) -> None:
    """Write an indented JSON file."""

    path.write_text(json.dumps(to_jsonable(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(handle, payload: object) -> None:
    """Write one JSON object line."""

    handle.write(json.dumps(to_jsonable(payload), sort_keys=True) + "\n")
    handle.flush()
