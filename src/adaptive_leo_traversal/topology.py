"""Base topology utilities."""

from __future__ import annotations

from dataclasses import dataclass, field

from adaptive_leo_traversal.models import Edge, normalize_edge


@dataclass(frozen=True, slots=True)
class Topology:
    """An immutable undirected graph with normalized edges."""

    nodes: set[int]
    edges: set[Edge]
    _adjacency: dict[int, frozenset[int]] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        normalized_nodes = set(self.nodes)
        normalized_edges = {normalize_edge(u, v) for u, v in self.edges}
        for u, v in normalized_edges:
            if u not in normalized_nodes or v not in normalized_nodes:
                raise ValueError(f"edge {(u, v)} references a node outside the topology")
        adjacency: dict[int, set[int]] = {node: set() for node in normalized_nodes}
        for u, v in normalized_edges:
            adjacency[u].add(v)
            adjacency[v].add(u)
        object.__setattr__(self, "nodes", normalized_nodes)
        object.__setattr__(self, "edges", normalized_edges)
        object.__setattr__(
            self,
            "_adjacency",
            {node: frozenset(neighbors) for node, neighbors in adjacency.items()},
        )

    def neighbors(self, node: int) -> set[int]:
        """Return all adjacent nodes in the undirected graph."""

        if node not in self.nodes:
            raise KeyError(f"unknown node: {node}")
        return set(self._adjacency[node])

    def has_edge(self, u: int, v: int) -> bool:
        """Return whether the undirected edge exists."""

        return normalize_edge(u, v) in self.edges

    def without_edges(self, edges_to_remove: set[Edge]) -> "Topology":
        """Return a topology with the given undirected edges removed."""

        normalized = {normalize_edge(u, v) for u, v in edges_to_remove}
        return Topology(nodes=set(self.nodes), edges=self.edges - normalized)


def make_grid_topology(rows: int, cols: int, wrap: bool = False) -> Topology:
    """Create a row-major rectangular grid topology."""

    if rows <= 0 or cols <= 0:
        raise ValueError("rows and cols must be positive")

    nodes = set(range(rows * cols))
    edges: set[Edge] = set()

    def node_at(row: int, col: int) -> int:
        return row * cols + col

    for row in range(rows):
        for col in range(cols):
            current = node_at(row, col)
            if col + 1 < cols:
                edges.add(normalize_edge(current, node_at(row, col + 1)))
            elif wrap and cols > 1:
                edges.add(normalize_edge(current, node_at(row, 0)))

            if row + 1 < rows:
                edges.add(normalize_edge(current, node_at(row + 1, col)))
            elif wrap and rows > 1:
                edges.add(normalize_edge(current, node_at(0, col)))

    return Topology(nodes=nodes, edges=edges)
