"""Cycle-route helpers for grid traversal policies."""

from __future__ import annotations


def make_grid_hamiltonian_cycle(rows: int, cols: int) -> list[int]:
    """Return a Hamiltonian cycle for an even-row rectangular grid rooted at 0."""

    if rows < 2 or cols < 2:
        return [0]
    if rows % 2 != 0:
        raise ValueError("cycle route requires an even number of rows")

    def node_at(row: int, col: int) -> int:
        return row * cols + col

    route = [node_at(0, 0)]
    for row in range(1, rows):
        route.append(node_at(row, 0))

    for col in range(1, cols):
        if col % 2 == 1:
            rows_iter = range(rows - 1, 0, -1)
        else:
            rows_iter = range(1, rows)
        for row in rows_iter:
            route.append(node_at(row, col))

    for col in range(cols - 1, 0, -1):
        route.append(node_at(0, col))

    route.append(node_at(0, 0))
    return route


def cycle_targets(route: tuple[int, ...] | list[int], visited: set[int]) -> list[int]:
    """Return unvisited cycle nodes in route order, excluding repeated nodes."""

    targets: list[int] = []
    seen: set[int] = set()
    for node in route:
        if node in seen or node in visited:
            continue
        targets.append(node)
        seen.add(node)
    return targets
