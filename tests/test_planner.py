from adaptive_leo_traversal.delay_table import DelayTable
from adaptive_leo_traversal.planner import (
    path_cost,
    remaining_path,
    remaining_path_cost,
    shortest_path,
)
from adaptive_leo_traversal.topology import Topology


def _weighted_topology() -> tuple[Topology, DelayTable]:
    topology = Topology(nodes={0, 1, 2, 3}, edges={(0, 1), (1, 2), (0, 3), (3, 2)})
    table = DelayTable.from_constant_delay(topology, period_slots=1, delay=1.0)
    table.set_delay(0, 0, 1, 5.0)
    table.set_delay(0, 1, 2, 5.0)
    table.set_delay(0, 0, 3, 1.0)
    table.set_delay(0, 3, 2, 1.0)
    return topology, table


def test_dijkstra_finds_shortest_path() -> None:
    topology, table = _weighted_topology()

    path, cost = shortest_path(topology, table, slot=0, source=0, target=2)

    assert path == [0, 3, 2]
    assert cost == 2.0


def test_path_helpers() -> None:
    topology, table = _weighted_topology()
    path = [0, 3, 2]

    assert path_cost(path, table, 0) == 2.0
    assert remaining_path(path, 1) == [3, 2]
    assert remaining_path_cost(path, 1, table, 0) == 1.0
