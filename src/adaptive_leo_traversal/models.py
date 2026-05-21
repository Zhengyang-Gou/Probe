"""Shared data models for adaptive traversal."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, TypeAlias

if TYPE_CHECKING:
    from adaptive_leo_traversal.observations import LinkObservationTable


Edge: TypeAlias = tuple[int, int]


class LinkState(Enum):
    """Observed or physical state of a link."""

    UP = "up"
    DOWN = "down"


class TraversalStatus(Enum):
    """High-level status returned by the traversal engine."""

    RUNNING = "running"
    FINISHED = "finished"
    TEMPORARILY_UNREACHABLE = "temporarily_unreachable"
    PARTIAL_RESULT = "partial_result"


def normalize_edge(u: int, v: int) -> Edge:
    """Return a canonical undirected edge representation."""

    if u == v:
        raise ValueError("self-loop edges are not supported")
    return (u, v) if u < v else (v, u)


def _new_link_observation_table() -> LinkObservationTable:
    from adaptive_leo_traversal.observations import LinkObservationTable

    return LinkObservationTable()


@dataclass(slots=True)
class LinkObservation:
    """A time-limited observation for one undirected edge."""

    edge: Edge
    state: LinkState
    observed_time: float
    ttl: float

    @property
    def expires_at(self) -> float:
        """Return the time at which this observation becomes stale."""

        return self.observed_time + self.ttl

    def is_expired(self, now: float) -> bool:
        """Return whether the observation has expired at ``now``."""

        return now >= self.expires_at


@dataclass(slots=True)
class NodeTelemetryRecord:
    """Telemetry collected when the probe reaches one requested telemetry node."""

    node: int
    observed_time: float
    links: list[LinkObservation]


@dataclass(slots=True)
class ProbeState:
    """Mutable packet state carried by the simulated probe."""

    root: int
    current_node: int
    next_telemetry_node: int | None = None
    visited: set[int] = field(default_factory=set)
    current_path: list[int] = field(default_factory=list)
    path_index: int = 0
    hop_count: int = 0
    hop_limit: int | None = None
    last_slot: int | None = None
    link_obs_table: LinkObservationTable = field(default_factory=_new_link_observation_table)
    telemetry_record: list[NodeTelemetryRecord] = field(default_factory=list)


@dataclass(slots=True)
class TraversalResult:
    """Result of handling one probe arrival."""

    status: TraversalStatus
    probe: ProbeState
    next_hop: int | None = None
    path: list[int] | None = None
    cost: float | None = None
    message: str = ""


@dataclass(frozen=True, slots=True)
class PathDecision:
    """A candidate routing decision produced by the planner."""

    path: list[int]
    cost: float
    target: int | None
