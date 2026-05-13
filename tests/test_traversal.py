from adaptive_leo_traversal.delay_table import DelayTable
from adaptive_leo_traversal.models import LinkState, ProbeState, TraversalStatus
from adaptive_leo_traversal.observations import LinkObservationTable
from adaptive_leo_traversal.topology import Topology
from adaptive_leo_traversal.traversal import AdaptiveTraversalEngine


def up_provider(u: int, v: int, now: float) -> LinkState:
    return LinkState.UP


def down_edges_provider(edges: set[tuple[int, int]]):
    normalized = {tuple(sorted(edge)) for edge in edges}

    def provider(u: int, v: int, now: float) -> LinkState:
        return LinkState.DOWN if tuple(sorted((u, v))) in normalized else LinkState.UP

    return provider


def test_initial_path_uses_current_slot_delay_table() -> None:
    topology = Topology(nodes={0, 1, 2}, edges={(0, 1), (1, 2), (0, 2)})
    table = DelayTable.from_constant_delay(topology, period_slots=2, delay=1.0)
    table.set_delay(1, 0, 1, 10.0)
    table.set_delay(1, 1, 2, 10.0)
    table.set_delay(1, 0, 2, 1.0)
    engine = AdaptiveTraversalEngine(
        topology,
        table,
        root=0,
        obs_ttl=10.0,
        max_hop=10,
        cycle_route=(0, 2, 1, 0),
    )
    probe = engine.initialize_probe(now=1.0)

    result = engine.on_probe_arrival(probe, 0, now=1.0, physical_link_state_provider=up_provider)

    assert result.status is TraversalStatus.RUNNING
    assert result.path == [0, 2]
    assert result.next_hop == 2


def test_cycle_route_selects_next_unvisited_target() -> None:
    topology = Topology(nodes={0, 1, 2}, edges={(0, 1), (0, 2), (1, 2)})
    table = DelayTable.from_constant_delay(topology, period_slots=1, delay=1.0)
    table.set_delay(0, 0, 1, 0.1)
    engine = AdaptiveTraversalEngine(
        topology,
        table,
        root=0,
        obs_ttl=10.0,
        max_hop=10,
        cycle_route=(0, 2, 1, 0),
    )
    probe = engine.initialize_probe(now=0.0)

    result = engine.on_probe_arrival(probe, 0, now=0.0, physical_link_state_provider=up_provider)

    assert result.status is TraversalStatus.RUNNING
    assert result.path == [0, 2]
    assert result.next_hop == 2


def test_next_hop_down_triggers_hard_replan() -> None:
    topology = Topology(nodes={0, 1, 2}, edges={(0, 1), (0, 2), (2, 1)})
    table = DelayTable.from_constant_delay(topology, period_slots=1, delay=1.0)
    table.set_delay(0, 0, 1, 0.1)
    engine = AdaptiveTraversalEngine(
        topology,
        table,
        root=0,
        obs_ttl=10.0,
        max_hop=10,
        cycle_route=(0, 2, 1, 0),
    )
    probe = ProbeState(root=0, current_node=0, visited={0}, current_path=[0, 1], path_index=0)

    result = engine.on_probe_arrival(
        probe,
        0,
        now=0.0,
        physical_link_state_provider=down_edges_provider({(0, 1)}),
    )

    assert result.status is TraversalStatus.RUNNING
    assert result.path == [0, 2]
    assert result.next_hop == 2


def test_slot_change_soft_replan_uses_new_weights_when_better() -> None:
    topology = Topology(nodes={0, 1, 2, 3}, edges={(0, 1), (1, 2), (1, 3), (3, 2)})
    table = DelayTable.from_constant_delay(topology, period_slots=2, delay=1.0)
    table.set_delay(1, 1, 2, 10.0)
    table.set_delay(1, 1, 3, 1.0)
    table.set_delay(1, 3, 2, 1.0)
    engine = AdaptiveTraversalEngine(
        topology,
        table,
        root=0,
        obs_ttl=10.0,
        max_hop=10,
        cycle_route=(0, 1, 2, 3, 0),
        alpha=0.85,
    )
    probe = ProbeState(
        root=0,
        current_node=1,
        visited={0, 1, 3},
        current_path=[0, 1, 2],
        path_index=1,
        last_slot=0,
    )

    result = engine.on_probe_arrival(probe, 1, now=1.0, physical_link_state_provider=up_provider)

    assert result.path == [1, 3, 2]
    assert result.next_hop == 3


def test_soft_replan_only_switches_when_candidate_beats_alpha_threshold() -> None:
    topology = Topology(nodes={0, 1, 2, 3}, edges={(0, 1), (1, 2), (1, 3), (3, 2)})
    table = DelayTable.from_constant_delay(topology, period_slots=1, delay=1.0)
    table.set_delay(0, 1, 2, 10.0)
    table.set_delay(0, 1, 3, 8.0)
    table.set_delay(0, 3, 2, 1.0)
    engine = AdaptiveTraversalEngine(
        topology,
        table,
        root=0,
        obs_ttl=10.0,
        max_hop=10,
        cycle_route=(0, 1, 2, 3, 0),
        alpha=0.85,
    )
    probe = ProbeState(
        root=0,
        current_node=1,
        visited={0, 1, 3},
        current_path=[0, 1, 2],
        path_index=1,
        last_slot=0,
    )

    result = engine.on_probe_arrival(probe, 1, now=0.0, physical_link_state_provider=up_provider)

    assert result.path == [0, 1, 2]
    assert result.next_hop == 2


def test_link_recovery_up_rejoins_estimated_topology() -> None:
    topology = Topology(nodes={0, 1}, edges={(0, 1)})
    table = DelayTable.from_constant_delay(topology, period_slots=1, delay=1.0)
    obs = LinkObservationTable()
    obs.update((0, 1), LinkState.DOWN, observed_time=0.0, ttl=10.0)
    engine = AdaptiveTraversalEngine(
        topology,
        table,
        root=0,
        obs_ttl=10.0,
        max_hop=10,
        cycle_route=(0, 1, 0),
    )
    probe = ProbeState(root=0, current_node=0, link_obs_table=obs, last_slot=table.slot_at(1.0))

    result = engine.on_probe_arrival(probe, 0, now=1.0, physical_link_state_provider=up_provider)

    assert probe.link_obs_table.recent_down_edges(1.0) == set()
    assert result.status is TraversalStatus.RUNNING
    assert result.next_hop == 1


def test_link_recovery_detected_even_when_down_edge_count_is_unchanged() -> None:
    topology = Topology(nodes={0, 1, 2, 3}, edges={(0, 1), (0, 2), (1, 2), (0, 3)})
    table = DelayTable.from_constant_delay(topology, period_slots=1, delay=1.0)
    table.set_delay(0, 0, 1, 10.0)
    table.set_delay(0, 0, 2, 1.0)
    table.set_delay(0, 1, 2, 1.0)
    engine = AdaptiveTraversalEngine(
        topology,
        table,
        root=0,
        obs_ttl=10.0,
        max_hop=10,
        cycle_route=(0, 1, 2, 3, 0),
        alpha=0.85,
    )
    probe = ProbeState(
        root=0,
        current_node=0,
        link_obs_table=LinkObservationTable(),
        visited={0, 2, 3},
        current_path=[0, 1],
        path_index=0,
        last_slot=0,
    )
    probe.link_obs_table.update((0, 2), LinkState.DOWN, observed_time=0.0, ttl=10.0)

    result = engine.on_probe_arrival(
        probe,
        0,
        now=1.0,
        physical_link_state_provider=down_edges_provider({(0, 3)}),
    )

    assert probe.link_obs_table.recent_down_edges(1.0) == {(0, 3)}
    assert result.path == [0, 2, 1]
    assert result.next_hop == 2


def test_finishes_after_all_nodes_are_visited_and_probe_returns_root() -> None:
    topology = Topology(nodes={0, 1}, edges={(0, 1)})
    table = DelayTable.from_constant_delay(topology, period_slots=1, delay=1.0)
    engine = AdaptiveTraversalEngine(
        topology,
        table,
        root=0,
        obs_ttl=10.0,
        max_hop=10,
        cycle_route=(0, 1, 0),
    )
    probe = engine.initialize_probe(now=0.0)

    first = engine.on_probe_arrival(probe, 0, now=0.0, physical_link_state_provider=up_provider)
    second = engine.on_probe_arrival(probe, first.next_hop or 1, now=1.0, physical_link_state_provider=up_provider)
    third = engine.on_probe_arrival(probe, second.next_hop or 0, now=2.0, physical_link_state_provider=up_provider)

    assert first.next_hop == 1
    assert second.next_hop == 0
    assert third.status is TraversalStatus.FINISHED


def test_unvisited_node_unreachable_returns_temporarily_unreachable() -> None:
    topology = Topology(nodes={0, 1}, edges={(0, 1)})
    table = DelayTable.from_constant_delay(topology, period_slots=1, delay=1.0)
    engine = AdaptiveTraversalEngine(
        topology,
        table,
        root=0,
        obs_ttl=10.0,
        max_hop=10,
        cycle_route=(0, 1, 0),
    )
    probe = engine.initialize_probe(now=0.0)

    result = engine.on_probe_arrival(
        probe,
        0,
        now=0.0,
        physical_link_state_provider=down_edges_provider({(0, 1)}),
    )

    assert result.status is TraversalStatus.TEMPORARILY_UNREACHABLE


def test_exceeding_max_hop_returns_partial_result() -> None:
    topology = Topology(nodes={0, 1}, edges={(0, 1)})
    table = DelayTable.from_constant_delay(topology, period_slots=1, delay=1.0)
    engine = AdaptiveTraversalEngine(
        topology,
        table,
        root=0,
        obs_ttl=10.0,
        max_hop=0,
        cycle_route=(0, 1, 0),
    )
    probe = engine.initialize_probe(now=0.0)

    result = engine.on_probe_arrival(probe, 0, now=0.0, physical_link_state_provider=up_provider)

    assert result.status is TraversalStatus.PARTIAL_RESULT
