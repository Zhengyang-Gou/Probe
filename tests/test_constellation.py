from adaptive_leo_traversal.constellation import (
    ConstellationConfig,
    DelayModelConfig,
    DynamicTopologyConfig,
    build_constellation_delay_table,
    make_constellation_topology,
    scheduled_down_edges,
)


def test_constellation_topology_uses_configurable_scale() -> None:
    config = ConstellationConfig(planes=2, satellites_per_plane=3)

    topology = make_constellation_topology(config)

    assert topology.nodes == {0, 1, 2, 3, 4, 5}
    assert topology.edges == {
        (0, 1),
        (1, 2),
        (3, 4),
        (4, 5),
        (0, 3),
        (1, 4),
        (2, 5),
    }


def test_propagation_delay_table_is_periodic_and_positive() -> None:
    constellation = ConstellationConfig(planes=2, satellites_per_plane=3)
    topology = make_constellation_topology(constellation)
    delay = DelayModelConfig(model="propagation", period_slots=3, min_delay_ms=0.1)

    table = build_constellation_delay_table(topology, constellation, delay)

    assert table.period_slots == 3
    assert table.get_delay(0, 0, 1) >= 0.1
    assert table.get_delay(3, 0, 1) == table.get_delay(0, 0, 1)


def test_rotating_seam_marks_one_satellite_column_down_per_slot() -> None:
    constellation = ConstellationConfig(planes=3, satellites_per_plane=4)
    topology = make_constellation_topology(constellation)
    dynamic = DynamicTopologyConfig(enabled=True, model="rotating_seam", period_slots=4)

    down = scheduled_down_edges(topology, constellation, dynamic, slot=1)

    assert down == {(1, 5), (5, 9)}
