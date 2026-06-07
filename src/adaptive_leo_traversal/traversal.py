"""Adaptive traversal engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from math import isinf

from adaptive_leo_traversal.delay_table import DelayTable
from adaptive_leo_traversal.models import (
    LinkObservation,
    LinkState,
    NodeTelemetryRecord,
    ProbeState,
    TraversalResult,
    TraversalStatus,
)
from adaptive_leo_traversal.planner import (
    is_next_hop_available,
    remaining_path_cost,
    shortest_path,
)
from adaptive_leo_traversal.topology import Topology

PhysicalLinkStateProvider = Callable[[int, int, float], LinkState]


@dataclass(slots=True)
class AdaptiveTraversalEngine:
    """Coordinates observations, estimated topology construction, and path planning."""

    base_topology: Topology
    delay_table: DelayTable
    root: int
    max_hop: int
    cycle_route: tuple[int, ...]
    alpha: float = 0.85
    _telemetry_successor: dict[int, int] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if self.root not in self.base_topology.nodes:
            raise ValueError("root must be in base_topology")
        if self.max_hop < 0:
            raise ValueError("max_hop must be non-negative")
        if not 0 < self.alpha <= 1:
            raise ValueError("alpha must be in the interval (0, 1]")
        route_nodes = set(self.cycle_route)
        if self.root not in route_nodes:
            raise ValueError("cycle_route must include root")
        if not self.base_topology.nodes <= route_nodes:
            raise ValueError("cycle_route must include every topology node")
        self._telemetry_successor = self._build_telemetry_successor()

    def initialize_probe(self, now: float) -> ProbeState:
        """Create a probe at the root node."""

        return ProbeState(
            root=self.root,
            current_node=self.root,
            next_telemetry_node=self.root,
            hop_limit=self.max_hop,
            last_slot=self.delay_table.slot_at(now),
        )

    def on_probe_arrival(
        self,
        probe: ProbeState,
        current_node: int,
        now: float,
        physical_link_state_provider: PhysicalLinkStateProvider,
    ) -> TraversalResult:
        """Handle one probe arrival and return the next routing action."""

        probe.current_node = current_node
        if probe.next_telemetry_node is None:
            probe.next_telemetry_node = self.root
        if probe.hop_limit is None:
            probe.hop_limit = self.max_hop
        slot = self.delay_table.slot_at(now)
        previous_slot = probe.last_slot
        old_down_edges = probe.link_obs_table.down_edges()

        if self._all_nodes_visited(probe) and current_node == self.root:
            probe.last_slot = slot
            return TraversalResult(TraversalStatus.FINISHED, probe, path=list(probe.current_path))

        target_updated = False
        if current_node == probe.next_telemetry_node:
            self._measure_telemetry_node(probe, current_node, now, physical_link_state_provider)
            probe.next_telemetry_node = self._next_telemetry_target(probe, current_node)
            target_updated = True
        else:
            self._record_failed_next_hop(probe, current_node, now, physical_link_state_provider)

        recent_down_edges = probe.link_obs_table.down_edges()
        estimated_topology = self.base_topology.without_edges(recent_down_edges)

        if self._all_nodes_visited(probe) and current_node == self.root:
            probe.last_slot = slot
            return TraversalResult(TraversalStatus.FINISHED, probe, path=list(probe.current_path))

        if probe.hop_count >= self.max_hop or probe.hop_limit <= 0:
            probe.last_slot = slot
            return TraversalResult(
                TraversalStatus.PARTIAL_RESULT,
                probe,
                path=list(probe.current_path),
                message="max_hop exceeded",
            )

        recovery_seen = bool(old_down_edges - recent_down_edges)
        if target_updated:
            direct_result = self._try_direct_forward_to_target(
                probe,
                current_node,
                now,
                physical_link_state_provider,
            )
            if direct_result is not None:
                probe.last_slot = slot
                return direct_result

        if target_updated or self._needs_hard_replan(probe, current_node, estimated_topology):
            planned = self._hard_replan(probe, current_node, estimated_topology, slot)
            if planned is not None:
                return planned
        else:
            self._maybe_soft_replan(
                probe=probe,
                current_node=current_node,
                topology=estimated_topology,
                slot=slot,
                previous_slot=previous_slot,
                recovery_seen=recovery_seen,
            )

        probe.last_slot = slot
        return self._forward_or_finish(probe)

    def run_until_done(
        self,
        provider: PhysicalLinkStateProvider,
        start_time: float = 0.0,
        step_time: float = 1.0,
    ) -> TraversalResult:
        """Run a simple synchronous simulation until the engine stops."""

        probe = self.initialize_probe(start_time)
        now = start_time
        result = TraversalResult(TraversalStatus.RUNNING, probe)
        while result.status is TraversalStatus.RUNNING:
            result = self.on_probe_arrival(probe, probe.current_node, now, provider)
            if result.next_hop is not None:
                probe.current_node = result.next_hop
            now += step_time
        return result

    def _build_telemetry_successor(self) -> dict[int, int]:
        route: list[int] = []
        seen: set[int] = set()
        for node in self.cycle_route:
            if node in seen:
                continue
            route.append(node)
            seen.add(node)
        if not route:
            return {}
        return {node: route[(index + 1) % len(route)] for index, node in enumerate(route)}

    def _observe_adjacent_links(
        self,
        probe: ProbeState,
        current_node: int,
        now: float,
        provider: PhysicalLinkStateProvider,
    ) -> list[LinkObservation]:
        observations: list[LinkObservation] = []
        for neighbor in self.base_topology.neighbors(current_node):
            state = provider(current_node, neighbor, now)
            probe.link_obs_table.update((current_node, neighbor), state, now)
            observation = probe.link_obs_table.current_observation((current_node, neighbor))
            if observation is not None:
                observations.append(observation)
        return observations

    def _measure_telemetry_node(
        self,
        probe: ProbeState,
        current_node: int,
        now: float,
        provider: PhysicalLinkStateProvider,
    ) -> None:
        if current_node in probe.visited:
            return
        probe.visited.add(current_node)
        observations = self._observe_adjacent_links(probe, current_node, now, provider)
        probe.telemetry_record.append(
            NodeTelemetryRecord(
                node=current_node,
                observed_time=now,
                links=observations,
            )
        )

    def _record_failed_next_hop(
        self,
        probe: ProbeState,
        current_node: int,
        now: float,
        provider: PhysicalLinkStateProvider,
    ) -> None:
        next_hop = self._peek_next_hop(probe)
        if next_hop is None:
            return
        if provider(current_node, next_hop, now) is LinkState.DOWN:
            probe.link_obs_table.update((current_node, next_hop), LinkState.DOWN, now)

    def _next_telemetry_target(self, probe: ProbeState, current_node: int) -> int:
        if self._all_nodes_visited(probe):
            return self.root
        target = self._telemetry_successor[current_node]
        checked: set[int] = set()
        while target in probe.visited:
            if target in checked:
                return self.root
            checked.add(target)
            target = self._telemetry_successor[target]
        return target

    def _try_direct_forward_to_target(
        self,
        probe: ProbeState,
        current_node: int,
        now: float,
        provider: PhysicalLinkStateProvider,
    ) -> TraversalResult | None:
        target = self._current_target(probe)
        if target == current_node or not self.base_topology.has_edge(current_node, target):
            return None

        state = provider(current_node, target, now)
        probe.link_obs_table.update((current_node, target), state, now)
        if state is not LinkState.UP:
            return None

        probe.current_path = [current_node, target]
        probe.path_index = 0
        return self._forward_or_finish(probe)

    def _all_nodes_visited(self, probe: ProbeState) -> bool:
        return self.base_topology.nodes <= probe.visited

    def _needs_hard_replan(
        self,
        probe: ProbeState,
        current_node: int,
        topology: Topology,
    ) -> bool:
        next_hop = self._peek_next_hop(probe)
        if not probe.current_path or next_hop is None:
            return True
        return not is_next_hop_available(topology, current_node, next_hop)

    def _hard_replan(
        self,
        probe: ProbeState,
        current_node: int,
        topology: Topology,
        slot: int,
    ) -> TraversalResult | None:
        target = self._current_target(probe)
        path, cost = shortest_path(topology, self.delay_table, slot, current_node, target)
        if path is None or isinf(cost):
            probe.last_slot = slot
            return TraversalResult(
                TraversalStatus.TEMPORARILY_UNREACHABLE,
                probe,
                message=f"telemetry target {target} is temporarily unreachable",
            )

        probe.current_path = path
        probe.path_index = 0
        probe.last_slot = slot
        return None

    def _maybe_soft_replan(
        self,
        probe: ProbeState,
        current_node: int,
        topology: Topology,
        slot: int,
        previous_slot: int | None,
        recovery_seen: bool,
    ) -> None:
        slot_changed = previous_slot is not None and previous_slot != slot
        should_consider = slot_changed or recovery_seen
        if not should_consider:
            return

        old_cost = remaining_path_cost(probe.current_path, probe.path_index, self.delay_table, slot)
        candidate = self._candidate_path(probe, current_node, topology, slot)
        if candidate is None:
            return
        candidate_path, candidate_cost = candidate
        if candidate_cost < self.alpha * old_cost:
            probe.current_path = candidate_path
            probe.path_index = 0

    def _candidate_path(
        self,
        probe: ProbeState,
        current_node: int,
        topology: Topology,
        slot: int,
    ) -> tuple[list[int], float] | None:
        path, cost = shortest_path(
            topology,
            self.delay_table,
            slot,
            current_node,
            self._current_target(probe),
        )
        if path is None or isinf(cost):
            return None
        return path, cost

    def _current_target(self, probe: ProbeState) -> int:
        if self._all_nodes_visited(probe):
            return self.root
        return probe.next_telemetry_node or self.root

    def _forward_or_finish(self, probe: ProbeState) -> TraversalResult:
        next_hop = self._peek_next_hop(probe)
        if next_hop is None:
            if self._all_nodes_visited(probe) and probe.current_node == self.root:
                return TraversalResult(
                    TraversalStatus.FINISHED,
                    probe,
                    path=list(probe.current_path),
                )
            return TraversalResult(
                TraversalStatus.TEMPORARILY_UNREACHABLE,
                probe,
                path=list(probe.current_path),
                message="no next hop available",
            )

        probe.path_index += 1
        probe.hop_count += 1
        if probe.hop_limit is not None:
            probe.hop_limit -= 1
        return TraversalResult(
            TraversalStatus.RUNNING,
            probe,
            next_hop=next_hop,
            path=list(probe.current_path),
        )

    def _peek_next_hop(self, probe: ProbeState) -> int | None:
        if not probe.current_path:
            return None
        if probe.path_index >= len(probe.current_path) - 1:
            return None
        return probe.current_path[probe.path_index + 1]
