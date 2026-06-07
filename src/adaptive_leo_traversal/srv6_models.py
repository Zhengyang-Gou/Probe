"""SRv6-aware simulation data models."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum

from adaptive_leo_traversal.models import TraversalStatus


class SidBehavior(Enum):
    """Supported SRv6 SID behavior labels for the Python simulation layer."""

    END = "End"
    END_X = "End.X"


@dataclass(slots=True)
class SRv6Policy:
    """A simulated SRv6 policy derived from an engine path decision."""

    source: int
    target: int
    path: list[int]
    segments: list[str]
    mode: str = "encap"
    path_cost: float | None = None
    srh_overhead_bytes: int = 0
    segment_count: int = 0


@dataclass(slots=True)
class SRv6PolicyEvent:
    """One SRv6 policy update observed during a simulation run."""

    run_id: int
    time: float
    slot: int
    source: int
    target: int
    path: list[int]
    segments: list[str]
    segment_count: int
    srh_overhead_bytes: int
    status: str
    reason: str = ""


@dataclass(frozen=True, slots=True)
class SRv6ExperimentResult:
    """Summary metrics for one SRv6-aware randomized run."""

    run_id: int
    status: TraversalStatus
    visited_count: int
    total_nodes: int
    hop_count: int
    total_delay: float
    finish_time: float
    down_edges: int
    mean_active_down_edges: float
    max_active_down_edges: int
    srv6_enabled: bool
    srv6_policy_updates: int
    mean_segment_list_length: float
    max_segment_list_length: int
    total_sid_processing_count: int
    mean_srh_overhead_bytes: float
    max_srh_overhead_bytes: int
