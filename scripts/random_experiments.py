"""Run randomized link-failure experiments for adaptive LEO traversal."""

from __future__ import annotations

import argparse
import json
import random
import tomllib
from collections import Counter
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import datetime
from enum import Enum
from math import cos, inf, pi
from pathlib import Path
from statistics import mean

from adaptive_leo_traversal import (
    AdaptiveTraversalEngine,
    DelayTable,
    SRv6ExperimentResult,
    SRv6PolicyEvent,
    SRv6SidAllocator,
    TraversalStatus,
    make_grid_hamiltonian_cycle,
    make_grid_topology,
)
from adaptive_leo_traversal.simulation import StaticLinkStateProvider
from adaptive_leo_traversal.srv6_simulation import SRv6SimulationRunner
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
    max_hop: int | None
    alpha: float
    step_time: float
    srv6_enabled: bool
    srv6_locator_prefix: str
    srv6_base_srh_overhead_bytes: int
    srv6_per_sid_overhead_bytes: int
    output_dir: str
    run_name: str | None
    write_stdout: bool


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


def run_one_srv6_experiment(
    run_id: int,
    topology: Topology,
    delay_table: DelayTable,
    actual_delay_provider: ActualDelayProvider,
    provider: StaticLinkStateProvider,
    max_hop: int,
    alpha: float,
    step_time: float,
    cycle_route: list[int],
    sid_allocator: SRv6SidAllocator,
    base_srh_overhead_bytes: int = 8,
    per_sid_overhead_bytes: int = 16,
) -> tuple[SRv6ExperimentResult, list[SRv6PolicyEvent]]:
    """Run one simulation while recording SRv6 policy metrics."""

    engine = AdaptiveTraversalEngine(
        base_topology=topology,
        delay_table=delay_table,
        root=0,
        max_hop=max_hop,
        alpha=alpha,
        cycle_route=tuple(cycle_route),
    )
    runner = SRv6SimulationRunner(
        engine=engine,
        provider=provider,
        actual_delay_provider=actual_delay_provider,
        sid_allocator=sid_allocator,
        step_time=step_time,
        base_srh_overhead_bytes=base_srh_overhead_bytes,
        per_sid_overhead_bytes=per_sid_overhead_bytes,
    )
    return runner.run(run_id=run_id)


def summarize(results: list[ExperimentResult | SRv6ExperimentResult]) -> str:
    """Format aggregate statistics."""

    finished = [result for result in results if result.status is TraversalStatus.FINISHED]
    status_counts = Counter(result.status.value for result in results)
    success_rate = len(finished) / len(results) if results else 0.0

    def successful_mean(values: Callable[[ExperimentResult], float], precision: int = 2) -> str:
        if not finished:
            return "n/a"
        return f"{mean(values(result) for result in finished):.{precision}f}"

    return "\n".join(
        [
            "",
            "Summary",
            "-------",
            f"runs: {len(results)}",
            f"finished: {len(finished)}",
            f"success_rate: {success_rate:.2%}",
            f"status_counts: {dict(sorted(status_counts.items()))}",
            f"mean_hops: {successful_mean(lambda result: result.hop_count)}",
            f"mean_actual_delay: {successful_mean(lambda result: result.total_delay)}",
            f"mean_finish_time: {successful_mean(lambda result: result.finish_time)}",
            f"mean_visited: {successful_mean(lambda result: result.visited_count, precision=4)}",
            f"min_visited: {min(result.visited_count for result in results)}",
            (
                "mean_active_down_edges: "
                f"{successful_mean(lambda result: result.mean_active_down_edges)}"
            ),
            (
                "mean_max_active_down_edges: "
                f"{successful_mean(lambda result: result.max_active_down_edges)}"
            ),
        ]
    )


def summarize_srv6(results: list[SRv6ExperimentResult]) -> str:
    """Format aggregate statistics for SRv6-aware runs."""

    finished = [result for result in results if result.status is TraversalStatus.FINISHED]

    def successful_mean(values: Callable[[SRv6ExperimentResult], float], precision: int = 2) -> str:
        if not finished:
            return "n/a"
        return f"{mean(values(result) for result in finished):.{precision}f}"

    return "\n".join(
        [
            summarize(results),
            "",
            "SRv6 Summary",
            "------------",
            (
                "mean_srv6_policy_updates: "
                f"{successful_mean(lambda result: result.srv6_policy_updates)}"
            ),
            (
                "mean_segment_list_length: "
                f"{successful_mean(lambda result: result.mean_segment_list_length)}"
            ),
            (
                "mean_max_segment_list_length: "
                f"{successful_mean(lambda result: result.max_segment_list_length)}"
            ),
            (
                "mean_srh_overhead_bytes: "
                f"{successful_mean(lambda result: result.mean_srh_overhead_bytes)}"
            ),
            (
                "mean_total_sid_processing_count: "
                f"{successful_mean(lambda result: result.total_sid_processing_count)}"
            ),
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
    srv6 = raw.get("srv6", {})
    output = raw.get("output", {})

    max_hop = int(traversal.get("max_hop", 0))
    run_name = str(output.get("run_name", "")).strip()
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
        max_hop=None if max_hop == 0 else max_hop,
        alpha=float(traversal.get("alpha", 0.85)),
        step_time=float(simulation.get("step_time", 1.0)),
        srv6_enabled=bool(srv6.get("enabled", False)),
        srv6_locator_prefix=str(srv6.get("locator_prefix", "fc00:0")),
        srv6_base_srh_overhead_bytes=int(srv6.get("base_srh_overhead_bytes", 8)),
        srv6_per_sid_overhead_bytes=int(srv6.get("per_sid_overhead_bytes", 16)),
        output_dir=str(output.get("base_dir", "logs/random")),
        run_name=run_name or None,
        write_stdout=bool(output.get("write_stdout", True)),
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
    if config.max_hop is not None and config.max_hop < 0:
        raise ValueError(f"{path}: traversal.max_hop must be non-negative")
    if not 0 < config.alpha <= 1:
        raise ValueError(f"{path}: traversal.alpha must be in (0, 1]")
    if config.step_time <= 0:
        raise ValueError(f"{path}: simulation.step_time must be positive")
    if not config.srv6_locator_prefix:
        raise ValueError(f"{path}: srv6.locator_prefix must be non-empty")
    if config.srv6_base_srh_overhead_bytes < 0:
        raise ValueError(f"{path}: srv6.base_srh_overhead_bytes must be non-negative")
    if config.srv6_per_sid_overhead_bytes < 0:
        raise ValueError(f"{path}: srv6.per_sid_overhead_bytes must be non-negative")
    if not config.output_dir.strip():
        raise ValueError(f"{path}: output.base_dir must be non-empty")


def make_run_dir(base_dir: Path, run_name: str | None, timestamp: str) -> Path:
    """Create a unique directory for one experiment run."""

    base_dir.mkdir(parents=True, exist_ok=True)
    name = safe_run_name(run_name or timestamp)
    candidate = base_dir / name
    if not candidate.exists():
        candidate.mkdir()
        return candidate
    for suffix in range(2, 1000):
        candidate = base_dir / f"{name}_{suffix}"
        if not candidate.exists():
            candidate.mkdir()
            return candidate
    raise RuntimeError(f"could not allocate a run directory under {base_dir}")


def safe_run_name(value: str) -> str:
    """Return a filesystem-friendly run name."""

    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
    cleaned = cleaned.strip("._-")
    return cleaned or "run"


def write_json_file(path: Path, payload: object) -> None:
    """Write an indented JSON file."""

    path.write_text(json.dumps(to_jsonable(payload), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def write_jsonl(handle, payload: object) -> None:
    """Write one JSON object line."""

    handle.write(json.dumps(to_jsonable(payload), sort_keys=True) + "\n")
    handle.flush()


def to_jsonable(value: object) -> object:
    """Convert dataclasses and enums into JSON-serializable values."""

    if isinstance(value, Enum):
        return value.value
    if hasattr(value, "__dataclass_fields__"):
        return to_jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = make_run_dir(Path(config.output_dir), config.run_name, timestamp)
    summary_path = run_dir / "summary.txt"
    results_path = run_dir / "runs.jsonl"
    policy_events_path = run_dir / "policy_events.jsonl"
    config_path = run_dir / "run_config.json"
    output_lines: list[str] = []

    def emit(line: str = "") -> None:
        output_lines.append(line)
        if config.write_stdout:
            print(line, flush=True)

    rng = random.Random(config.seed)
    topology = make_grid_topology(config.rows, config.cols)
    max_hop = config.max_hop if config.max_hop is not None else config.rows * config.cols * 30
    cycle_route = make_grid_hamiltonian_cycle(config.rows, config.cols)
    node_width = max(2, len(str(len(topology.nodes))))
    results: list[ExperimentResult] = []
    srv6_results: list[SRv6ExperimentResult] = []
    sid_allocator = SRv6SidAllocator(config.srv6_locator_prefix)

    write_json_file(
        config_path,
        {
            "config_file": str(args.config),
            "run_dir": str(run_dir),
            "timestamp": timestamp,
            "config": config,
        },
    )
    emit(f"config: {args.config}")
    emit(f"output_dir: {run_dir}")
    if config.srv6_enabled:
        emit(
            "run status                  visited   hops actual_delay down_edges "
            "active_down_mean active_down_max srv6_updates mean_sid_len max_sid_len "
            "mean_srh_bytes max_srh_bytes"
        )
    else:
        emit(
            "run status                  visited   hops actual_delay down_edges "
            "active_down_mean active_down_max"
        )

    policy_events_file = None
    try:
        with results_path.open("w", encoding="utf-8") as results_file:
            if config.srv6_enabled:
                policy_events_file = policy_events_path.open("w", encoding="utf-8")
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
                if config.srv6_enabled:
                    result, policy_events = run_one_srv6_experiment(
                        run_id=run_id,
                        topology=topology,
                        delay_table=delay_table,
                        actual_delay_provider=actual_delay_provider,
                        provider=provider,
                        max_hop=max_hop,
                        alpha=config.alpha,
                        step_time=config.step_time,
                        cycle_route=cycle_route,
                        sid_allocator=sid_allocator,
                        base_srh_overhead_bytes=config.srv6_base_srh_overhead_bytes,
                        per_sid_overhead_bytes=config.srv6_per_sid_overhead_bytes,
                    )
                    srv6_results.append(result)
                    write_jsonl(results_file, result)
                    if policy_events_file is not None:
                        for event in policy_events:
                            write_jsonl(policy_events_file, event)
                    emit(
                        f"{result.run_id:>3} "
                        f"{result.status.value:<23} "
                        f"{result.visited_count:>{node_width}}/{result.total_nodes:<{node_width}} "
                        f"{result.hop_count:>4} "
                        f"{result.total_delay:>12.2f} "
                        f"{result.down_edges:>10} "
                        f"{result.mean_active_down_edges:>16.2f} "
                        f"{result.max_active_down_edges:>15} "
                        f"{result.srv6_policy_updates:>12} "
                        f"{result.mean_segment_list_length:>12.2f} "
                        f"{result.max_segment_list_length:>11} "
                        f"{result.mean_srh_overhead_bytes:>14.2f} "
                        f"{result.max_srh_overhead_bytes:>13}"
                    )
                else:
                    result = run_one_experiment(
                        run_id=run_id,
                        topology=topology,
                        delay_table=delay_table,
                        actual_delay_provider=actual_delay_provider,
                        provider=provider,
                        max_hop=max_hop,
                        alpha=config.alpha,
                        step_time=config.step_time,
                        cycle_route=cycle_route,
                    )
                    results.append(result)
                    write_jsonl(results_file, result)
                    emit(
                        f"{result.run_id:>3} "
                        f"{result.status.value:<23} "
                        f"{result.visited_count:>{node_width}}/{result.total_nodes:<{node_width}} "
                        f"{result.hop_count:>4} "
                        f"{result.total_delay:>12.2f} "
                        f"{result.down_edges:>10} "
                        f"{result.mean_active_down_edges:>16.2f} "
                        f"{result.max_active_down_edges:>15}"
                    )
    finally:
        if policy_events_file is not None:
            policy_events_file.close()

    emit(summarize_srv6(srv6_results) if config.srv6_enabled else summarize(results))
    summary_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    if config.write_stdout:
        print(f"summary: {summary_path}", flush=True)
        print(f"runs: {results_path}", flush=True)
        if config.srv6_enabled:
            print(f"policy events: {policy_events_path}", flush=True)


if __name__ == "__main__":
    main()
