import pytest

from adaptive_leo_traversal.models import normalize_edge
from adaptive_leo_traversal.topology import Topology, make_grid_topology


def test_make_grid_topology_6x6_nodes_and_edges() -> None:
    topology = make_grid_topology(6, 6)

    assert topology.nodes == set(range(36))
    assert len(topology.edges) == 60
    assert topology.has_edge(0, 1)
    assert topology.has_edge(0, 6)
    assert not topology.has_edge(0, 5)


def test_normalize_edge_avoids_undirected_duplicates() -> None:
    assert normalize_edge(3, 1) == (1, 3)
    assert len({normalize_edge(1, 3), normalize_edge(3, 1)}) == 1
    with pytest.raises(ValueError):
        normalize_edge(2, 2)


def test_topology_without_edges_removes_recent_down_edges() -> None:
    topology = Topology(nodes={0, 1, 2}, edges={(0, 1), (1, 2)})

    estimated = topology.without_edges({(1, 0)})

    assert not estimated.has_edge(0, 1)
    assert estimated.has_edge(1, 2)
