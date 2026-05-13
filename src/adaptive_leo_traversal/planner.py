"""Path-planning helpers for adaptive traversal."""

from __future__ import annotations

import heapq
from math import inf

from adaptive_leo_traversal.delay_table import DelayTable
from adaptive_leo_traversal.topology import Topology


def shortest_path(
    topology: Topology,
    delay_table: DelayTable,
    slot: int,
    source: int,
    target: int,
) -> tuple[list[int] | None, float]:
    """Find the minimum-delay path between two nodes using Dijkstra."""

    if source not in topology.nodes or target not in topology.nodes:
        raise KeyError("source and target must exist in topology")
    if source == target:
        return [source], 0.0

    distances: dict[int, float] = {source: 0.0}
    previous: dict[int, int] = {}
    queue: list[tuple[float, int]] = [(0.0, source)]
    settled: set[int] = set()

    while queue:
        current_distance, node = heapq.heappop(queue)
        if node in settled:
            continue
        settled.add(node)
        if node == target:
            return _reconstruct_path(previous, source, target), current_distance

        for neighbor in topology.neighbors(node):
            if neighbor in settled:
                continue
            candidate = current_distance + delay_table.get_delay(slot, node, neighbor)
            if candidate < distances.get(neighbor, inf):
                distances[neighbor] = candidate
                previous[neighbor] = node
                heapq.heappush(queue, (candidate, neighbor))

    return None, inf


def path_cost(path: list[int], delay_table: DelayTable, slot: int) -> float:
    """Return total delay for a path in the given slot."""

    return sum(delay_table.get_delay(slot, u, v) for u, v in zip(path, path[1:]))


def remaining_path(path: list[int], path_index: int) -> list[int]:
    """Return the not-yet-traversed suffix from the current path index."""

    if not path:
        return []
    safe_index = min(max(path_index, 0), len(path) - 1)
    return path[safe_index:]


def remaining_path_cost(
    path: list[int],
    path_index: int,
    delay_table: DelayTable,
    slot: int,
) -> float:
    """Return remaining path cost from ``path_index`` onward."""

    return path_cost(remaining_path(path, path_index), delay_table, slot)


def is_next_hop_available(topology: Topology, current_node: int, next_hop: int) -> bool:
    """Return whether the next hop edge exists in the estimated topology."""

    return topology.has_edge(current_node, next_hop)


def _reconstruct_path(previous: dict[int, int], source: int, target: int) -> list[int]:
    path = [target]
    while path[-1] != source:
        path.append(previous[path[-1]])
    path.reverse()
    return path
