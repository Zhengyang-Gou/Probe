"""SRv6 SID allocation helpers for the pure Python simulation layer."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SRv6SidAllocator:
    """Allocate deterministic Node SIDs and Adj SIDs for integer node IDs."""

    locator_prefix: str = "fc00:0"

    def __post_init__(self) -> None:
        if not self.locator_prefix:
            raise ValueError("locator_prefix must be non-empty")

    def node_sid(self, node: int) -> str:
        """Return the Node SID assigned to ``node``."""

        self._validate_node(node, "node")
        return f"{self.locator_prefix}:{node:x}::1"

    def adj_sid(self, source: int, target: int) -> str:
        """Return the directional Adj SID assigned to ``source -> target``."""

        self._validate_node(source, "source")
        self._validate_node(target, "target")
        return f"{self._adj_locator_prefix()}:{source:x}:{target:x}::1"

    def _adj_locator_prefix(self) -> str:
        parts = self.locator_prefix.split(":")
        parts[-1] = "a"
        return ":".join(parts)

    @staticmethod
    def _validate_node(value: int, name: str) -> None:
        if type(value) is not int or value < 0:
            raise ValueError(f"{name} must be a non-negative integer")
