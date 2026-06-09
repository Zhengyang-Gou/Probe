"""Serializable probe packet payloads for emulation agents."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any


PROBE_PACKET_VERSION = 1


@dataclass(slots=True)
class ProbePacketPayload:
    """A compact JSON payload carried by UDP probe packets."""

    run_id: str
    sequence: int
    root: int
    current_node: int
    next_telemetry_node: int | None
    hop_count: int = 0
    hop_limit: int | None = None
    visited: list[int] = field(default_factory=list)
    path: list[int] = field(default_factory=list)
    telemetry_records: list[dict[str, Any]] = field(default_factory=list)
    version: int = PROBE_PACKET_VERSION

    def to_bytes(self) -> bytes:
        """Encode the payload as deterministic UTF-8 JSON bytes."""

        return json.dumps(
            self.to_dict(),
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")

    def to_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable dictionary."""

        return {
            "version": self.version,
            "run_id": self.run_id,
            "sequence": self.sequence,
            "root": self.root,
            "current_node": self.current_node,
            "next_telemetry_node": self.next_telemetry_node,
            "hop_count": self.hop_count,
            "hop_limit": self.hop_limit,
            "visited": list(self.visited),
            "path": list(self.path),
            "telemetry_records": list(self.telemetry_records),
        }

    @classmethod
    def from_bytes(cls, data: bytes) -> "ProbePacketPayload":
        """Decode a payload from UTF-8 JSON bytes."""

        try:
            decoded = json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("probe packet is not valid JSON") from exc
        if not isinstance(decoded, dict):
            raise ValueError("probe packet JSON must be an object")
        return cls.from_dict(decoded)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProbePacketPayload":
        """Build a payload from decoded JSON."""

        version = int(data.get("version", 0))
        if version != PROBE_PACKET_VERSION:
            raise ValueError(f"unsupported probe packet version: {version}")
        return cls(
            version=version,
            run_id=str(data["run_id"]),
            sequence=int(data["sequence"]),
            root=int(data["root"]),
            current_node=int(data["current_node"]),
            next_telemetry_node=_optional_int(data.get("next_telemetry_node")),
            hop_count=int(data.get("hop_count", 0)),
            hop_limit=_optional_int(data.get("hop_limit")),
            visited=_int_list(data.get("visited", [])),
            path=_int_list(data.get("path", [])),
            telemetry_records=_record_list(data.get("telemetry_records", [])),
        )

    def mark_visited(self, node: int) -> None:
        """Record that ``node`` has been visited."""

        if node not in self.visited:
            self.visited.append(node)
            self.visited.sort()

    def add_telemetry_record(
        self,
        node: int,
        observed_time: float,
        links: list[dict[str, Any]],
    ) -> None:
        """Append one telemetry record collected by an emulation agent."""

        self.telemetry_records.append(
            {
                "node": node,
                "observed_time": observed_time,
                "links": links,
            }
        )


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    return int(value)


def _int_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        raise ValueError("probe packet list field must be a list")
    return [int(item) for item in value]


def _record_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        raise ValueError("telemetry_records must be a list")
    records: list[dict[str, Any]] = []
    for item in value:
        if not isinstance(item, dict):
            raise ValueError("telemetry record must be an object")
        records.append(dict(item))
    return records
