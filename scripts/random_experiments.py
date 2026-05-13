"""Run randomized link-failure experiments for adaptive LEO traversal."""

from __future__ import annotations

import argparse
import random
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from math import cos, inf, pi
from pathlib import Path
from statistics import mean

from adaptive_leo_traversal import (
    AdaptiveTraversalEngine,
    DelayTable,
    TraversalStatus,
    make_grid_hamiltonian_cycle,
    make_grid_topology,
)
from adaptive_leo_traversal.simulation import StaticLinkStateProvider
from adaptive_leo_traversal.topology import Topology

ActualDelayProvider = Callable[[int, int, float], float]

DEFAULT_CONFIG_PATH = Path("config/random_experiments.toml")


@dataclass(frozen=True, slots=True)
class ExperimentResult:
    """Summary metrics for one randomized run."""

    run_id: int
    status: TraversalStatus
    visited_count: int
    total_nodes: int
    hop_count: int
    total_delay: float
    finish_time: float
    down_edges: int
    mean_active_down_edges: float
    max_active_down_edges: int


@dataclass(frozen=True, slots=True)
class ExperimentConfig:
    """Configuration loaded from the experiment TOML file."""

    runs: int
    rows: int
    cols: int
    seed: int
    period_slots: int
    delay_model: str
    min_delay: float
    max_delay: float
    intra_delay: float
    inter_min_delay: float
    inter_max_delay: float
    down_probability: float
    obs_ttl: float
    max_hop: int | None
    alpha: float
    step_time: float


def build_random_delay_table(
    topology: Topology,
    period_slots: int,
    rng: random.Random,
    min_delay: float,
    max_delay: float,
) -> DelayTable:
    """Create a periodic delay table with random per-slot edge weights."""

    table = DelayTable(period_slots=period_slots)
    for slot in range(period_slots):
        for u, v in topology.edges:
            table.set_delay(slot, u, v, rng.uniform(min_delay, max_delay))
    return table


def build_leo_delay_table(
    topology: Topology,
    rows: int,
    cols: int,
    period_slots: int,
    intra_delay: float,
    inter_min_delay: float,
    inter_max_delay: float,
) -> DelayTable:
    """Create a structured LEO-like delay table.

    Vertical grid edges model intra-plane links and stay almost constant.
    Horizontal grid edges model inter-plane links and vary periodically:
    lower at high latitudes, higher around equatorial separation.
    """

    if intra_delay < 0 or inter_min_delay < 0 or inter_max_delay < 0:
        raise ValueError("delays must be non-negative")
    if inter_max_delay < inter_min_delay:
        raise ValueError("inter_max_delay must be greater than or equal to inter_min_delay")

    table = DelayTable(period_slots=period_slots)
    for slot in range(period_slots):
        for u, v in topology.edges:
            delay = leo_link_delay_at_time(
                u=u,
                v=v,
                now=float(slot),
                rows=rows,
                cols=cols,
                period_slots=period_slots,
                intra_delay=intra_delay,
                inter_min_delay=inter_min_delay,
                inter_max_delay=inter_max_delay,
            )
            table.set_delay(slot, u, v, delay)
    return table


def leo_link_delay_at_time(
    u: int,
    v: int,
    now: float,
    rows: int,
    cols: int,
    period_slots: int,
    intra_delay: float,
    inter_min_delay: float,
    inter_max_delay: float,
) -> float:
    """Return the continuous-time physical delay for one LEO grid link."""

    if is_intra_plane_edge(u, v, cols):
        return intra_delay
    if is_inter_plane_edge(u, v, cols):
        row = min(u, v) // cols
        orbit_phase = (now % period_slots) / period_slots
        latitude_phase = orbit_phase + row / rows
        equator_factor = abs(cos(2.0 * pi * latitude_phase))
        return inter_min_delay + (inter_max_delay - inter_min_delay) * equator_factor
    return intra_delay


def build_leo_actual_delay_provider(
    rows: int,
    cols: int,
    period_slots: int,
    intra_delay: float,
    inter_min_delay: float,
    inter_max_delay: float,
) -> ActualDelayProvider:
    """Create the continuous physical delay model used for experiment metrics."""

    def actual_delay(u: int, v: int, now: float) -> float:
        return leo_link_delay_at_time(
            u=u,
            v=v,
            now=now,
            rows=rows,
            cols=cols,
            period_slots=period_slots,
            intra_delay=intra_delay,
            inter_min_delay=inter_min_delay,
            inter_max_delay=inter_max_delay,
        )

    return actual_delay


def build_slot_actual_delay_provider(delay_table: DelayTable) -> ActualDelayProvider:
    """Use the algorithm's slot delay table as the physical delay model."""

    def actual_delay(u: int, v: int, now: float) -> float:
        return delay_table.get_delay(delay_table.slot_at(now), u, v)

    return actual_delay


def is_intra_plane_edge(u: int, v: int, cols: int) -> bool:
    """Return whether a grid edge is vertical, used as same-orbit-plane."""

    return u % cols == v % cols


def is_inter_plane_edge(u: int, v: int, cols: int) -> bool:
    """Return whether a grid edge is horizontal, used as cross-orbit-plane."""

    return u // cols == v // cols


def build_random_provider(
    topology: Topology,
    rng: random.Random,
    down_probability: float,
) -> StaticLinkStateProvider:
    """Create permanent down intervals for a random subset of topology edges."""

    provider = StaticLinkStateProvider()
    for u, v in topology.edges:
        if rng.random() >= down_probability:
            continue
        provider.add_down_interval(u, v, start=0.0, end=inf)
    return provider


def count_active_down_edges(provider: StaticLinkStateProvider, now: float) -> int:
    """Return how many configured links are physically down at ``now``."""

    return sum(
        1
        for intervals in provider.down_intervals.values()
        if any(start <= now < end for start, end in intervals)
    )


def run_one_experiment(
    run_id: int,
    topology: Topology,
    delay_table: DelayTable,
    actual_delay_provider: ActualDelayProvider,
    provider: StaticLinkStateProvider,
    obs_ttl: float,
    max_hop: int,
    alpha: float,
    step_time: float,
    cycle_route: list[int],
) -> ExperimentResult:
    """Run one simulation while accumulating actual hop delay."""

    engine = AdaptiveTraversalEngine(
        base_topology=topology,
        delay_table=delay_table,
        root=0,
        obs_ttl=obs_ttl,
        max_hop=max_hop,
        alpha=alpha,
        cycle_route=tuple(cycle_route),
    )
    probe = engine.initialize_probe(now=0.0)
    now = 0.0
    total_delay = 0.0
    active_down_samples: list[int] = []
    status = TraversalStatus.RUNNING

    while status is TraversalStatus.RUNNING:
        current_node = probe.current_node
        active_down_samples.append(count_active_down_edges(provider, now))
        result = engine.on_probe_arrival(probe, current_node, now, provider)
        status = result.status

        if result.next_hop is not None:
            total_delay += actual_delay_provider(current_node, result.next_hop, now)
            probe.current_node = result.next_hop

        now += step_time

    return ExperimentResult(
        run_id=run_id,
        status=status,
        visited_count=len(probe.visited),
        total_nodes=len(topology.nodes),
        hop_count=probe.hop_count,
        total_delay=total_delay,
        finish_time=now,
        down_edges=len(provider.down_intervals),
        mean_active_down_edges=mean(active_down_samples) if active_down_samples else 0.0,
        max_active_down_edges=max(active_down_samples, default=0),
    )


def summarize(results: list[ExperimentResult]) -> str:
    """Format aggregate statistics."""

    finished = [result for result in results if result.status is TraversalStatus.FINISHED]
    status_counts = Counter(result.status.value for result in results)
    success_rate = len(finished) / len(results) if results else 0.0
    return "\n".join(
        [
            "",
            "Summary",
            "-------",
            f"runs: {len(results)}",
            f"finished: {len(finished)}",
            f"success_rate: {success_rate:.2%}",
            f"status_counts: {dict(sorted(status_counts.items()))}",
            f"mean_hops: {mean(result.hop_count for result in results):.2f}",
            f"mean_actual_delay: {mean(result.total_delay for result in results):.2f}",
            f"mean_finish_time: {mean(result.finish_time for result in results):.2f}",
            f"mean_visited: {mean(result.visited_count for result in results):.4f}",
            f"min_visited: {min(result.visited_count for result in results)}",
            f"mean_active_down_edges: {mean(result.mean_active_down_edges for result in results):.2f}",
            f"mean_max_active_down_edges: {mean(result.max_active_down_edges for result in results):.2f}",
        ]
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"experiment config file, defaults to {DEFAULT_CONFIG_PATH}",
    )
    return parser.parse_args()


def load_config(path: Path) -> ExperimentConfig:
    """Load experiment settings from a TOML file."""

    with path.open("rb") as file:
        raw = tomllib.load(file)

    experiment = raw.get("experiment", {})
    grid = raw.get("grid", {})
    delay = raw.get("delay", {})
    random_delay = raw.get("random_delay", {})
    leo_delay = raw.get("leo_delay", {})
    failure = raw.get("failure", {})
    traversal = raw.get("traversal", {})
    simulation = raw.get("simulation", {})

    max_hop = int(traversal.get("max_hop", 0))
    config = ExperimentConfig(
        runs=int(experiment.get("runs", 20)),
        rows=int(grid.get("rows", 10)),
        cols=int(grid.get("cols", 10)),
        seed=int(experiment.get("seed", 7)),
        period_slots=int(delay.get("period_slots", 8)),
        delay_model=str(delay.get("model", "leo")),
        min_delay=float(random_delay.get("min_delay", 0.8)),
        max_delay=float(random_delay.get("max_delay", 3.0)),
        intra_delay=float(leo_delay.get("intra_delay", 1.0)),
        inter_min_delay=float(leo_delay.get("inter_min_delay", 1.2)),
        inter_max_delay=float(leo_delay.get("inter_max_delay", 3.0)),
        down_probability=float(failure.get("down_probability", 0.18)),
        obs_ttl=float(traversal.get("obs_ttl", 4.0)),
        max_hop=None if max_hop == 0 else max_hop,
        alpha=float(traversal.get("alpha", 0.85)),
        step_time=float(simulation.get("step_time", 1.0)),
    )
    validate_config(config, path)
    return config


def validate_config(config: ExperimentConfig, path: Path) -> None:
    """Fail fast on invalid experiment settings."""

    if config.runs <= 0:
        raise ValueError(f"{path}: experiment.runs must be positive")
    if config.rows <= 0 or config.cols <= 0:
        raise ValueError(f"{path}: grid.rows and grid.cols must be positive")
    if config.rows % 2 != 0:
        raise ValueError(f"{path}: grid.rows must be even for the Hamiltonian cycle builder")
    if config.period_slots <= 0:
        raise ValueError(f"{path}: delay.period_slots must be positive")
    if config.delay_model not in {"leo", "random"}:
        raise ValueError(f"{path}: delay.model must be 'leo' or 'random'")
    if config.min_delay < 0 or config.max_delay < config.min_delay:
        raise ValueError(f"{path}: random_delay values must satisfy 0 <= min_delay <= max_delay")
    if config.intra_delay < 0 or config.inter_min_delay < 0:
        raise ValueError(f"{path}: leo_delay values must be non-negative")
    if config.inter_max_delay < config.inter_min_delay:
        raise ValueError(f"{path}: leo_delay.inter_max_delay must be >= inter_min_delay")
    if not 0 <= config.down_probability <= 1:
        raise ValueError(f"{path}: failure.down_probability must be in [0, 1]")
    if config.obs_ttl <= 0:
        raise ValueError(f"{path}: traversal.obs_ttl must be positive")
    if config.max_hop is not None and config.max_hop < 0:
        raise ValueError(f"{path}: traversal.max_hop must be non-negative")
    if not 0 < config.alpha <= 1:
        raise ValueError(f"{path}: traversal.alpha must be in (0, 1]")
    if config.step_time <= 0:
        raise ValueError(f"{path}: simulation.step_time must be positive")


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    rng = random.Random(config.seed)
    topology = make_grid_topology(config.rows, config.cols)
    max_hop = config.max_hop if config.max_hop is not None else config.rows * config.cols * 30
    cycle_route = make_grid_hamiltonian_cycle(config.rows, config.cols)
    node_width = max(2, len(str(len(topology.nodes))))
    results: list[ExperimentResult] = []

    print(f"config: {args.config}", flush=True)
    print(
        "run status                  visited   hops actual_delay down_edges active_down_mean active_down_max",
        flush=True,
    )
    for run_id in range(1, config.runs + 1):
        run_seed = rng.randrange(2**32)
        run_rng = random.Random(run_seed)
        if config.delay_model == "leo":
            delay_table = build_leo_delay_table(
                topology=topology,
                rows=config.rows,
                cols=config.cols,
                period_slots=config.period_slots,
                intra_delay=config.intra_delay,
                inter_min_delay=config.inter_min_delay,
                inter_max_delay=config.inter_max_delay,
            )
            actual_delay_provider = build_leo_actual_delay_provider(
                rows=config.rows,
                cols=config.cols,
                period_slots=config.period_slots,
                intra_delay=config.intra_delay,
                inter_min_delay=config.inter_min_delay,
                inter_max_delay=config.inter_max_delay,
            )
        else:
            delay_table = build_random_delay_table(
                topology,
                period_slots=config.period_slots,
                rng=run_rng,
                min_delay=config.min_delay,
                max_delay=config.max_delay,
            )
            actual_delay_provider = build_slot_actual_delay_provider(delay_table)
        provider = build_random_provider(
            topology,
            rng=run_rng,
            down_probability=config.down_probability,
        )
        result = run_one_experiment(
            run_id=run_id,
            topology=topology,
            delay_table=delay_table,
            actual_delay_provider=actual_delay_provider,
            provider=provider,
            obs_ttl=config.obs_ttl,
            max_hop=max_hop,
            alpha=config.alpha,
            step_time=config.step_time,
            cycle_route=cycle_route,
        )
        results.append(result)
        print(
            f"{result.run_id:>3} "
            f"{result.status.value:<23} "
            f"{result.visited_count:>{node_width}}/{result.total_nodes:<{node_width}} "
            f"{result.hop_count:>4} "
            f"{result.total_delay:>12.2f} "
            f"{result.down_edges:>10} "
            f"{result.mean_active_down_edges:>16.2f} "
            f"{result.max_active_down_edges:>15}",
            flush=True,
        )

    print(summarize(results))


if __name__ == "__main__":
    main()
