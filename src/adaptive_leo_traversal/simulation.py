"""Small simulation helpers for the adaptive traversal engine."""

from __future__ import annotations

from dataclasses import dataclass, field

from adaptive_leo_traversal.models import Edge, LinkState, TraversalResult, TraversalStatus, normalize_edge
from adaptive_leo_traversal.traversal import AdaptiveTraversalEngine


@dataclass(slots=True)
class StaticLinkStateProvider:
    """Physical link-state provider with optional down intervals."""

    down_intervals: dict[Edge, list[tuple[float, float]]] = field(default_factory=dict)

    def add_down_interval(self, u: int, v: int, start: float, end: float) -> None:
        """Mark an undirected link as down during ``[start, end)``."""

        if end <= start:
            raise ValueError("end must be greater than start")
        self.down_intervals.setdefault(normalize_edge(u, v), []).append((start, end))

    def __call__(self, u: int, v: int, now: float) -> LinkState:
        """Return the configured physical state at ``now``."""

        for start, end in self.down_intervals.get(normalize_edge(u, v), []):
            if start <= now < end:
                return LinkState.DOWN
        return LinkState.UP


def run_simulation(
    engine: AdaptiveTraversalEngine,
    provider: StaticLinkStateProvider,
    start_time: float = 0.0,
    step_time: float = 1.0,
) -> TraversalResult:
    """Run the helper simulation until traversal stops."""

    probe = engine.initialize_probe(start_time)
    now = start_time
    result = TraversalResult(TraversalStatus.RUNNING, probe)
    while result.status is TraversalStatus.RUNNING:
        result = engine.on_probe_arrival(probe, probe.current_node, now, provider)
        if result.next_hop is not None:
            probe.current_node = result.next_hop
        now += step_time
    return result
