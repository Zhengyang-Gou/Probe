"""SRv6-aware simulation runner built around the adaptive traversal engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from statistics import mean
from typing import Any

from adaptive_leo_traversal.models import TraversalStatus
from adaptive_leo_traversal.srv6_models import SRv6ExperimentResult, SRv6PolicyEvent
from adaptive_leo_traversal.srv6_policy import encode_node_sid_policy
from adaptive_leo_traversal.srv6_sid import SRv6SidAllocator
from adaptive_leo_traversal.traversal import AdaptiveTraversalEngine, PhysicalLinkStateProvider

ActualDelayProvider = Callable[[int, int, float], float]


@dataclass(slots=True)
class SRv6SimulationRunner:
    """Run a traversal simulation and record SRv6 policy update metrics."""

    engine: AdaptiveTraversalEngine
    provider: PhysicalLinkStateProvider
    actual_delay_provider: ActualDelayProvider
    sid_allocator: SRv6SidAllocator
    start_time: float = 0.0
    step_time: float = 1.0
    base_srh_overhead_bytes: int = 8
    per_sid_overhead_bytes: int = 16

    def run(self, run_id: int = 1) -> tuple[SRv6ExperimentResult, list[SRv6PolicyEvent]]:
        """Run the simulation until traversal stops."""

        probe = self.engine.initialize_probe(self.start_time)
        now = self.start_time
        total_delay = 0.0
        status = TraversalStatus.RUNNING
        active_down_samples: list[int] = []
        last_path: list[int] | None = None
        policy_events: list[SRv6PolicyEvent] = []
        segment_lengths: list[int] = []
        srh_overheads: list[int] = []
        policy_updates = 0

        while status is TraversalStatus.RUNNING:
            current_node = probe.current_node
            active_down_samples.append(_count_active_down_edges(self.provider, now))
            result = self.engine.on_probe_arrival(probe, current_node, now, self.provider)
            status = result.status

            if result.path:
                current_path = list(result.path)
                if current_path != last_path:
                    policy = encode_node_sid_policy(
                        current_path,
                        sid_allocator=self.sid_allocator,
                        path_cost=result.cost,
                        base_srh_overhead_bytes=self.base_srh_overhead_bytes,
                        per_sid_overhead_bytes=self.per_sid_overhead_bytes,
                    )
                    policy_updates += 1
                    segment_lengths.append(policy.segment_count)
                    srh_overheads.append(policy.srh_overhead_bytes)
                    policy_events.append(
                        SRv6PolicyEvent(
                            run_id=run_id,
                            time=now,
                            slot=self.engine.delay_table.slot_at(now),
                            source=policy.source,
                            target=policy.target,
                            path=policy.path,
                            segments=policy.segments,
                            segment_count=policy.segment_count,
                            srh_overhead_bytes=policy.srh_overhead_bytes,
                            status=status.value,
                            reason=result.message,
                        )
                    )
                    last_path = current_path

            if result.next_hop is not None:
                total_delay += self.actual_delay_provider(current_node, result.next_hop, now)
                probe.current_node = result.next_hop

            now += self.step_time

        return (
            SRv6ExperimentResult(
                run_id=run_id,
                status=status,
                visited_count=len(probe.visited),
                total_nodes=len(self.engine.base_topology.nodes),
                hop_count=probe.hop_count,
                total_delay=total_delay,
                finish_time=now,
                down_edges=_configured_down_edges(self.provider),
                mean_active_down_edges=mean(active_down_samples) if active_down_samples else 0.0,
                max_active_down_edges=max(active_down_samples, default=0),
                srv6_enabled=True,
                srv6_policy_updates=policy_updates,
                mean_segment_list_length=mean(segment_lengths) if segment_lengths else 0.0,
                max_segment_list_length=max(segment_lengths, default=0),
                total_sid_processing_count=sum(segment_lengths),
                mean_srh_overhead_bytes=mean(srh_overheads) if srh_overheads else 0.0,
                max_srh_overhead_bytes=max(srh_overheads, default=0),
            ),
            policy_events,
        )


def _configured_down_edges(provider: PhysicalLinkStateProvider) -> int:
    down_intervals = getattr(provider, "down_intervals", None)
    if not isinstance(down_intervals, dict):
        return 0
    return len(down_intervals)


def _count_active_down_edges(provider: PhysicalLinkStateProvider, now: float) -> int:
    down_intervals = getattr(provider, "down_intervals", None)
    if not isinstance(down_intervals, dict):
        return 0
    return sum(1 for intervals in down_intervals.values() if _has_active_interval(intervals, now))


def _has_active_interval(intervals: Any, now: float) -> bool:
    try:
        return any(start <= now < end for start, end in intervals)
    except TypeError:
        return False
