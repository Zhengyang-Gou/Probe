"""Time-slot delay table for undirected links."""

from __future__ import annotations

from dataclasses import dataclass, field
from math import floor

from adaptive_leo_traversal.models import Edge, normalize_edge
from adaptive_leo_traversal.topology import Topology


@dataclass(slots=True)
class DelayTable:
    """Periodic per-edge delay storage."""

    period_slots: int
    _delays: list[dict[Edge, float]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.period_slots <= 0:
            raise ValueError("period_slots must be positive")
        self._delays = [dict() for _ in range(self.period_slots)]

    def slot_at(self, time: float) -> int:
        """Map a simulation time to a periodic slot."""

        return floor(time) % self.period_slots

    def get_delay(self, slot: int, u: int, v: int) -> float:
        """Return the delay for an undirected edge in a slot."""

        normalized_slot = slot % self.period_slots
        edge = normalize_edge(u, v)
        try:
            return self._delays[normalized_slot][edge]
        except KeyError as exc:
            raise KeyError(f"no delay configured for edge {edge} in slot {normalized_slot}") from exc

    def set_delay(self, slot: int, u: int, v: int, delay: float) -> None:
        """Set the delay for an undirected edge in a slot."""

        if delay < 0:
            raise ValueError("delay must be non-negative")
        self._delays[slot % self.period_slots][normalize_edge(u, v)] = delay

    @classmethod
    def from_constant_delay(
        cls, topology: Topology, period_slots: int, delay: float
    ) -> "DelayTable":
        """Create a table where every topology edge has the same delay in every slot."""

        table = cls(period_slots=period_slots)
        for slot in range(period_slots):
            for u, v in topology.edges:
                table.set_delay(slot, u, v, delay)
        return table
