"""Adaptive traversal engine."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from math import isinf

from adaptive_leo_traversal.cycle import cycle_targets
from adaptive_leo_traversal.delay_table import DelayTable
from adaptive_leo_traversal.models import LinkState, ProbeState, TraversalResult, TraversalStatus
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
    obs_ttl: float
    max_hop: int
    cycle_route: tuple[int, ...]
    alpha: float = 0.85

    def __post_init__(self) -> None:
        if self.root not in self.base_topology.nodes:
            raise ValueError("root must be in base_topology")
        if self.obs_ttl <= 0:
            raise ValueError("obs_ttl must be positive")
        if self.max_hop < 0:
            raise ValueError("max_hop must be non-negative")
        if not 0 < self.alpha <= 1:
            raise ValueError("alpha must be in the interval (0, 1]")
        route_nodes = set(self.cycle_route)
        if self.root not in route_nodes:
            raise ValueError("cycle_route must include root")
        if not self.base_topology.nodes <= route_nodes:
            raise ValueError("cycle_route must include every topology node")

    def initialize_probe(self, now: float) -> ProbeState:
        """Create a probe at the root node."""

        return ProbeState(root=self.root, current_node=self.root, last_slot=self.delay_table.slot_at(now))

    def on_probe_arrival(
        self,
        probe: ProbeState,
        current_node: int,
        now: float,
        physical_link_state_provider: PhysicalLinkStateProvider,
    ) -> TraversalResult:
        """Handle one probe arrival and return the next routing action."""

        probe.current_node = current_node
        slot = self.delay_table.slot_at(now)
        previous_slot = probe.last_slot
        old_down_edges = probe.link_obs_table.recent_down_edges(now)

        probe.visited.add(current_node)
        self._observe_adjacent_links(probe, current_node, now, physical_link_state_provider)
        probe.link_obs_table.remove_expired(now)

        recent_down_edges = probe.link_obs_table.recent_down_edges(now)
        estimated_topology = self.base_topology.without_edges(recent_down_edges)

        if self._all_nodes_visited(probe) and current_node == self.root:
            probe.last_slot = slot
            return TraversalResult(TraversalStatus.FINISHED, probe, path=list(probe.current_path))

        if probe.hop_count >= self.max_hop:
            probe.last_slot = slot
            return TraversalResult(
                TraversalStatus.PARTIAL_RESULT,
                probe,
                path=list(probe.current_path),
                message="max_hop exceeded",
            )

        recovery_seen = bool(old_down_edges - recent_down_edges)
        if self._needs_hard_replan(probe, current_node, estimated_topology):
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

    def _observe_adjacent_links(
        self,
        probe: ProbeState,
        current_node: int,
        now: float,
        provider: PhysicalLinkStateProvider,
    ) -> None:
        for neighbor in self.base_topology.neighbors(current_node):
            state = provider(current_node, neighbor, now)
            probe.link_obs_table.update((current_node, neighbor), state, now, self.obs_ttl)

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
        if self._all_nodes_visited(probe):
            path, cost = shortest_path(topology, self.delay_table, slot, current_node, self.root)
            if path is None:
                probe.last_slot = slot
                return TraversalResult(
                    TraversalStatus.TEMPORARILY_UNREACHABLE,
                    probe,
                    message="root is temporarily unreachable",
                )
        else:
            path, cost = self._path_to_unvisited_target(probe, current_node, topology, slot)
            if path is None or isinf(cost):
                probe.last_slot = slot
                return TraversalResult(
                    TraversalStatus.TEMPORARILY_UNREACHABLE,
                    probe,
                    message="no unvisited node is currently reachable",
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
        if self._all_nodes_visited(probe):
            path, cost = shortest_path(topology, self.delay_table, slot, current_node, self.root)
        else:
            path, cost = self._path_to_unvisited_target(probe, current_node, topology, slot)
        if path is None or isinf(cost):
            return None
        return path, cost

    def _path_to_unvisited_target(
        self,
        probe: ProbeState,
        current_node: int,
        topology: Topology,
        slot: int,
    ) -> tuple[list[int] | None, float]:
        for target in cycle_targets(self.cycle_route, probe.visited):
            path, cost = shortest_path(topology, self.delay_table, slot, current_node, target)
            if path is not None and not isinf(cost):
                return path, cost
        return None, float("inf")

    def _forward_or_finish(self, probe: ProbeState) -> TraversalResult:
        next_hop = self._peek_next_hop(probe)
        if next_hop is None:
            if self._all_nodes_visited(probe) and probe.current_node == self.root:
                return TraversalResult(TraversalStatus.FINISHED, probe, path=list(probe.current_path))
            return TraversalResult(
                TraversalStatus.TEMPORARILY_UNREACHABLE,
                probe,
                path=list(probe.current_path),
                message="no next hop available",
            )

        probe.path_index += 1
        probe.hop_count += 1
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
