"""Run multiple randomized Mininet SRv6 experiment scenarios from one config."""

from __future__ import annotations

import argparse
import json
import random
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_PROJECT_SRC = _PROJECT_ROOT / "src"
if _PROJECT_SRC.exists() and str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

from adaptive_leo_traversal.constellation import (
    ConstellationConfig,
    make_constellation_topology,
)
from adaptive_leo_traversal.experiment_utils import safe_run_name, to_jsonable, write_jsonl
from adaptive_leo_traversal.models import Edge, TraversalStatus

DEFAULT_CONFIG_PATH = Path("config/mininet_batch_experiments.toml")


@dataclass(frozen=True, slots=True)
class MininetBatchConfig:
    """Configuration for a batch of Mininet scenarios."""

    scenario_count: int
    seed: int
    interrupt_statuses: tuple[str, ...]
    lab_config_path: str
    planes: int
    satellites_per_plane: int
    root: int
    duration: float
    slot_seconds: float
    delay_model: str
    delay_period_slots: int
    dynamic_topology_enabled: bool
    dynamic_topology_model: str
    dynamic_topology_period_slots: int
    algorithm_mode: str
    max_hop: int
    alpha: float
    observation_mode: str
    observation_ttl_seconds: float | None
    agent_enabled: bool
    probe_packet_validation: bool
    agent_udp_port: int
    validate_each_policy: bool
    failure_mode: str
    down_probability: float
    down_edges_per_scenario: int | None
    failure_start: float
    failure_end: float
    output_dir: str
    run_name: str | None
    write_stdout: bool
    dry_run: bool
    python: str
    continue_on_runner_error: bool


@dataclass(frozen=True, slots=True)
class MininetScenarioResult:
    scenario_id: int
    status: str
    interrupted: bool
    returncode: int
    run_dir: str
    failure_edges: tuple[Edge, ...]
    visited_count: int = 0
    total_nodes: int = 0
    hop_count: int = 0
    finish_time: float | None = None


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--config",
        type=Path,
        default=DEFAULT_CONFIG_PATH,
        help=f"batch config file, defaults to {DEFAULT_CONFIG_PATH}",
    )
    return parser.parse_args()


def load_config(path: Path) -> MininetBatchConfig:
    """Load a Mininet batch experiment config."""

    with path.open("rb") as file:
        raw = tomllib.load(file)

    scenario = raw.get("scenario", {})
    execution = raw.get("execution", {})
    constellation = raw.get("constellation", {})
    emulation = raw.get("emulation", {})
    delay = raw.get("delay", {})
    dynamic_topology = raw.get("dynamic_topology", {})
    algorithm = raw.get("algorithm", {})
    observation = raw.get("observation", {})
    agent = raw.get("agent", {})
    failure = raw.get("failure", {})
    output = raw.get("output", {})

    run_name = str(output.get("run_name", "")).strip()
    observation_ttl = observation.get("stale_after_seconds")
    down_count = failure.get("down_edges_per_scenario")
    config = MininetBatchConfig(
        scenario_count=int(scenario.get("count", 20)),
        seed=int(scenario.get("seed", 7)),
        interrupt_statuses=tuple(
            str(value)
            for value in scenario.get(
                "interrupt_statuses",
                [TraversalStatus.TEMPORARILY_UNREACHABLE.value, TraversalStatus.PARTIAL_RESULT.value],
            )
        ),
        lab_config_path=str(execution.get("lab_config", "config/mininet_srv6.toml")),
        planes=int(constellation.get("planes", 10)),
        satellites_per_plane=int(constellation.get("satellites_per_plane", 10)),
        root=int(algorithm.get("root", 0)),
        duration=float(emulation.get("duration", 120)),
        slot_seconds=float(emulation.get("slot_seconds", 1)),
        delay_model=str(delay.get("model", "propagation")),
        delay_period_slots=int(delay.get("period_slots", 8)),
        dynamic_topology_enabled=bool(dynamic_topology.get("enabled", True)),
        dynamic_topology_model=str(dynamic_topology.get("model", "rotating_seam")),
        dynamic_topology_period_slots=int(dynamic_topology.get("period_slots", 8)),
        algorithm_mode=str(algorithm.get("mode", "adaptive_traversal")),
        max_hop=int(algorithm.get("max_hop", 3000)),
        alpha=float(algorithm.get("alpha", 0.85)),
        observation_mode=str(observation.get("mode", "ping")),
        observation_ttl_seconds=None if observation_ttl is None else float(observation_ttl),
        agent_enabled=bool(agent.get("enabled", True)),
        probe_packet_validation=bool(agent.get("probe_packet_validation", True)),
        agent_udp_port=int(agent.get("udp_port", 5005)),
        validate_each_policy=bool(algorithm.get("validate_each_policy", True)),
        failure_mode=str(failure.get("mode", "probability")),
        down_probability=float(failure.get("down_probability", 0.1)),
        down_edges_per_scenario=None if down_count is None else int(down_count),
        failure_start=float(failure.get("start", 0)),
        failure_end=float(failure.get("end", emulation.get("duration", 120))),
        output_dir=str(output.get("base_dir", "logs/mininet-batch")),
        run_name=run_name or None,
        write_stdout=bool(output.get("write_stdout", True)),
        dry_run=bool(execution.get("dry_run", False)),
        python=str(execution.get("python", sys.executable)),
        continue_on_runner_error=bool(execution.get("continue_on_runner_error", True)),
    )
    validate_config(config, path)
    return config


def validate_config(config: MininetBatchConfig, path: Path) -> None:
    if config.scenario_count <= 0:
        raise ValueError(f"{path}: scenario.count must be positive")
    if config.planes <= 0 or config.satellites_per_plane <= 0:
        raise ValueError(f"{path}: constellation dimensions must be positive")
    if config.planes % 2 != 0:
        raise ValueError(f"{path}: constellation.planes must be even for adaptive traversal")
    if config.duration <= 0 or config.slot_seconds <= 0:
        raise ValueError(f"{path}: emulation duration and slot_seconds must be positive")
    if config.failure_mode not in {"probability", "fixed_count"}:
        raise ValueError(f"{path}: failure.mode must be 'probability' or 'fixed_count'")
    if not 0 <= config.down_probability <= 1:
        raise ValueError(f"{path}: failure.down_probability must be in [0, 1]")
    if config.down_edges_per_scenario is not None and config.down_edges_per_scenario < 0:
        raise ValueError(f"{path}: failure.down_edges_per_scenario must be non-negative")
    if config.failure_mode == "fixed_count" and config.down_edges_per_scenario is None:
        raise ValueError(
            f"{path}: failure.down_edges_per_scenario is required when failure.mode='fixed_count'"
        )
    for status in config.interrupt_statuses:
        if status not in {item.value for item in TraversalStatus}:
            raise ValueError(f"{path}: unknown interrupt status {status!r}")


def build_lab_command(
    config: MininetBatchConfig,
    run_name: str,
    output_dir: Path,
    failure_edges: tuple[Edge, ...],
) -> list[str]:
    command = [
        config.python,
        "-m",
        "emulation.mininet_srv6_lab",
        "--config",
        config.lab_config_path,
        "--planes",
        str(config.planes),
        "--satellites-per-plane",
        str(config.satellites_per_plane),
        "--root",
        str(config.root),
        "--duration",
        str(config.duration),
        "--slot-seconds",
        str(config.slot_seconds),
        "--delay-model",
        config.delay_model,
        "--delay-period-slots",
        str(config.delay_period_slots),
        "--dynamic-topology-model",
        config.dynamic_topology_model,
        "--dynamic-topology-period-slots",
        str(config.dynamic_topology_period_slots),
        "--algorithm-mode",
        config.algorithm_mode,
        "--max-hop",
        str(config.max_hop),
        "--alpha",
        str(config.alpha),
        "--observation-mode",
        config.observation_mode,
        "--agent-udp-port",
        str(config.agent_udp_port),
        "--failure-start",
        str(config.failure_start),
        "--failure-end",
        str(config.failure_end),
        "--output-dir",
        str(output_dir),
        "--run-name",
        run_name,
        "--no-cli",
    ]
    if failure_edges:
        command.extend(
            [
                "--failure-edges",
                ";".join(f"{u},{v}" for u, v in failure_edges),
            ]
        )
    command.append("--dynamic-topology" if config.dynamic_topology_enabled else "--no-dynamic-topology")
    command.append("--enable-agents" if config.agent_enabled else "--disable-agents")
    command.append(
        "--probe-packet-validation"
        if config.probe_packet_validation
        else "--no-probe-packet-validation"
    )
    if config.observation_ttl_seconds is not None:
        command.extend(["--observation-ttl-seconds", str(config.observation_ttl_seconds)])
    if not config.validate_each_policy:
        command.append("--no-validate-each-policy")
    if config.dry_run:
        command.append("--dry-run")
    return command


def select_failure_edges(
    config: MininetBatchConfig,
    rng: random.Random,
) -> tuple[Edge, ...]:
    constellation = ConstellationConfig(
        planes=config.planes,
        satellites_per_plane=config.satellites_per_plane,
        intra_plane_wrap=False,
        inter_plane_links=True,
        inter_plane_wrap=False,
    )
    topology = make_constellation_topology(constellation)
    edges = sorted(topology.edges)
    if config.failure_mode == "fixed_count":
        assert config.down_edges_per_scenario is not None
        return tuple(sorted(rng.sample(edges, k=min(config.down_edges_per_scenario, len(edges)))))
    return tuple(edge for edge in edges if rng.random() < config.down_probability)


def summarize_run_dir(run_dir: Path, total_nodes: int) -> dict[str, Any]:
    traversal_path = run_dir / "traversal_events.jsonl"
    if not traversal_path.exists():
        return {
            "status": "no_traversal_log",
            "visited_count": 0,
            "total_nodes": total_nodes,
            "hop_count": 0,
            "finish_time": None,
        }
    events = [
        json.loads(line)
        for line in traversal_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    if not events:
        return {
            "status": "empty_traversal_log",
            "visited_count": 0,
            "total_nodes": total_nodes,
            "hop_count": 0,
            "finish_time": None,
        }
    final = events[-1]
    visited = final.get("visited") or []
    return {
        "status": str(final.get("status")),
        "visited_count": len(visited),
        "total_nodes": total_nodes,
        "hop_count": int(final.get("hop_count") or 0),
        "finish_time": final.get("time"),
    }


def main() -> None:
    args = parse_args()
    config = load_config(args.config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    batch_name = safe_run_name(config.run_name or timestamp)
    batch_dir = Path(config.output_dir) / batch_name
    batch_dir.mkdir(parents=True, exist_ok=True)
    results_path = batch_dir / "scenarios.jsonl"
    summary_path = batch_dir / "summary.txt"
    config_path = batch_dir / "batch_config.json"
    output_lines: list[str] = []

    def emit(line: str = "") -> None:
        output_lines.append(line)
        if config.write_stdout:
            print(line, flush=True)

    total_nodes = config.planes * config.satellites_per_plane
    rng = random.Random(config.seed)
    results: list[MininetScenarioResult] = []
    config_path.write_text(
        json.dumps(to_jsonable({"config_file": str(args.config), "config": config}), indent=2) + "\n",
        encoding="utf-8",
    )

    emit(f"config: {args.config}")
    emit(f"batch_dir: {batch_dir}")
    emit(f"scenarios: {config.scenario_count}")
    emit(f"nodes: {total_nodes}")
    emit("scenario status                  visited hops failures returncode run_dir")

    with results_path.open("w", encoding="utf-8") as results_file:
        for scenario_id in range(1, config.scenario_count + 1):
            scenario_rng = random.Random(rng.randrange(2**32))
            failure_edges = select_failure_edges(config, scenario_rng)
            run_name = safe_run_name(f"{batch_name}_s{scenario_id:04d}")
            run_dir = batch_dir / run_name
            command = build_lab_command(config, run_name, batch_dir, failure_edges)
            completed = subprocess.run(
                command,
                cwd=Path(__file__).resolve().parents[1],
                check=False,
                capture_output=True,
                text=True,
            )
            (batch_dir / f"{run_name}.stdout.log").write_text(
                completed.stdout + completed.stderr,
                encoding="utf-8",
            )
            summary = (
                {
                    "status": "dry_run",
                    "visited_count": 0,
                    "total_nodes": total_nodes,
                    "hop_count": 0,
                    "finish_time": None,
                }
                if config.dry_run
                else summarize_run_dir(run_dir, total_nodes)
            )
            status = "runner_error" if completed.returncode != 0 else str(summary["status"])
            interrupted = status in config.interrupt_statuses
            result = MininetScenarioResult(
                scenario_id=scenario_id,
                status=status,
                interrupted=interrupted,
                returncode=completed.returncode,
                run_dir=str(run_dir),
                failure_edges=failure_edges,
                visited_count=int(summary["visited_count"]),
                total_nodes=int(summary["total_nodes"]),
                hop_count=int(summary["hop_count"]),
                finish_time=summary["finish_time"],
            )
            results.append(result)
            write_jsonl(results_file, result)
            emit(
                f"{scenario_id:>8} {status:<23} "
                f"{result.visited_count:>3}/{result.total_nodes:<3} "
                f"{result.hop_count:>4} "
                f"{len(failure_edges):>8} "
                f"{completed.returncode:>10} "
                f"{run_dir}"
            )
            if interrupted:
                emit(f"         interrupted by {status}; starting next scenario")
            if completed.returncode != 0 and not config.continue_on_runner_error:
                break

    counts: dict[str, int] = {}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    finished = counts.get(TraversalStatus.FINISHED.value, 0)
    interrupted_count = sum(1 for result in results if result.interrupted)
    emit("")
    emit("Summary")
    emit("-------")
    emit(f"scenarios: {len(results)}")
    emit(f"finished: {finished}")
    emit(f"interrupted: {interrupted_count}")
    emit(f"status_counts: {dict(sorted(counts.items()))}")
    summary_path.write_text("\n".join(output_lines) + "\n", encoding="utf-8")
    if config.write_stdout:
        print(f"summary: {summary_path}", flush=True)
        print(f"scenarios: {results_path}", flush=True)


if __name__ == "__main__":
    main()
