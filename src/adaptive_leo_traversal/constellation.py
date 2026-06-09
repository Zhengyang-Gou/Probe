"""Configurable LEO constellation topology and delay helpers."""

from __future__ import annotations

from dataclasses import dataclass
from math import cos, pi, radians, sin, sqrt

from adaptive_leo_traversal.delay_table import DelayTable
from adaptive_leo_traversal.models import Edge, normalize_edge
from adaptive_leo_traversal.topology import Topology


SPEED_OF_LIGHT_KM_PER_MS = 299.792458


@dataclass(frozen=True, slots=True)
class ConstellationConfig:
    """A rectangular Walker-like constellation mapped to integer node IDs."""

    planes: int
    satellites_per_plane: int
    intra_plane_wrap: bool = False
    inter_plane_links: bool = True
    inter_plane_wrap: bool = False

    def __post_init__(self) -> None:
        if self.planes <= 0:
            raise ValueError("planes must be positive")
        if self.satellites_per_plane <= 0:
            raise ValueError("satellites_per_plane must be positive")

    @property
    def node_count(self) -> int:
        return self.planes * self.satellites_per_plane

    def node_at(self, plane: int, satellite: int) -> int:
        if not 0 <= plane < self.planes:
            raise ValueError("plane index out of range")
        if not 0 <= satellite < self.satellites_per_plane:
            raise ValueError("satellite index out of range")
        return plane * self.satellites_per_plane + satellite

    def coordinates_of(self, node: int) -> tuple[int, int]:
        if not 0 <= node < self.node_count:
            raise ValueError("node index out of range")
        return divmod(node, self.satellites_per_plane)


@dataclass(frozen=True, slots=True)
class DelayModelConfig:
    """Parameters for building a periodic link delay table."""

    model: str = "constant"
    period_slots: int = 1
    constant_delay_ms: float = 5.0
    altitude_km: float = 550.0
    inclination_deg: float = 53.0
    earth_radius_km: float = 6371.0
    min_delay_ms: float = 0.1

    def __post_init__(self) -> None:
        if self.model not in {"constant", "propagation"}:
            raise ValueError("delay model must be 'constant' or 'propagation'")
        if self.period_slots <= 0:
            raise ValueError("period_slots must be positive")
        if self.constant_delay_ms < 0:
            raise ValueError("constant_delay_ms must be non-negative")
        if self.altitude_km < 0:
            raise ValueError("altitude_km must be non-negative")
        if self.earth_radius_km <= 0:
            raise ValueError("earth_radius_km must be positive")
        if self.min_delay_ms < 0:
            raise ValueError("min_delay_ms must be non-negative")


@dataclass(frozen=True, slots=True)
class DynamicTopologyConfig:
    """Parameters for time-varying link availability."""

    enabled: bool = False
    model: str = "static"
    period_slots: int = 1

    def __post_init__(self) -> None:
        if self.model not in {"static", "rotating_seam"}:
            raise ValueError("dynamic topology model must be 'static' or 'rotating_seam'")
        if self.period_slots <= 0:
            raise ValueError("period_slots must be positive")


def make_constellation_topology(config: ConstellationConfig) -> Topology:
    """Create a constellation graph from plane and satellite counts."""

    nodes = set(range(config.node_count))
    edges: set[Edge] = set()

    for plane in range(config.planes):
        for satellite in range(config.satellites_per_plane):
            current = config.node_at(plane, satellite)
            if satellite + 1 < config.satellites_per_plane:
                edges.add(normalize_edge(current, config.node_at(plane, satellite + 1)))
            elif config.intra_plane_wrap and config.satellites_per_plane > 1:
                edges.add(normalize_edge(current, config.node_at(plane, 0)))

            if config.inter_plane_links:
                if plane + 1 < config.planes:
                    edges.add(normalize_edge(current, config.node_at(plane + 1, satellite)))
                elif config.inter_plane_wrap and config.planes > 1:
                    edges.add(normalize_edge(current, config.node_at(0, satellite)))

    return Topology(nodes=nodes, edges=edges)


def build_constellation_delay_table(
    topology: Topology,
    constellation: ConstellationConfig,
    delay: DelayModelConfig,
) -> DelayTable:
    """Build a periodic delay table for a constellation topology."""

    if delay.model == "constant":
        return DelayTable.from_constant_delay(
            topology,
            period_slots=delay.period_slots,
            delay=delay.constant_delay_ms,
        )

    table = DelayTable(period_slots=delay.period_slots)
    for slot in range(delay.period_slots):
        for u, v in topology.edges:
            distance = _distance_km(
                _satellite_position(constellation, delay, u, slot),
                _satellite_position(constellation, delay, v, slot),
            )
            propagation = distance / SPEED_OF_LIGHT_KM_PER_MS
            table.set_delay(slot, u, v, max(delay.min_delay_ms, propagation))
    return table


def scheduled_down_edges(
    topology: Topology,
    constellation: ConstellationConfig,
    dynamic: DynamicTopologyConfig,
    slot: int,
) -> set[Edge]:
    """Return topology edges unavailable in a periodic dynamic slot."""

    if not dynamic.enabled or dynamic.model == "static":
        return set()

    normalized_slot = slot % dynamic.period_slots
    seam_satellite = normalized_slot % constellation.satellites_per_plane
    down: set[Edge] = set()
    for u, v in topology.edges:
        u_plane, u_sat = constellation.coordinates_of(u)
        v_plane, v_sat = constellation.coordinates_of(v)
        is_inter_plane = u_sat == v_sat and u_plane != v_plane
        if is_inter_plane and u_sat == seam_satellite:
            down.add(normalize_edge(u, v))
    return down


def _satellite_position(
    constellation: ConstellationConfig,
    delay: DelayModelConfig,
    node: int,
    slot: int,
) -> tuple[float, float, float]:
    plane, satellite = constellation.coordinates_of(node)
    radius = delay.earth_radius_km + delay.altitude_km
    raan = 2 * pi * plane / constellation.planes
    phase = 2 * pi * (satellite / constellation.satellites_per_plane)
    anomaly = phase + 2 * pi * (slot / delay.period_slots)
    inclination = radians(delay.inclination_deg)

    x_orbit = radius * cos(anomaly)
    y_orbit = radius * sin(anomaly) * cos(inclination)
    z = radius * sin(anomaly) * sin(inclination)
    x = x_orbit * cos(raan) - y_orbit * sin(raan)
    y = x_orbit * sin(raan) + y_orbit * cos(raan)
    return (x, y, z)


def _distance_km(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    return sqrt(sum((left - right) ** 2 for left, right in zip(a, b)))
