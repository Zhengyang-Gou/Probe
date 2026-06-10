"""Level 2 Linux/Mininet/SRv6 emulation entrypoint."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
import tomllib
from dataclasses import asdict, dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

_PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
if _PROJECT_SRC.exists() and str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))
_PROJECT_ROOT = Path(__file__).resolve().parents[1]

from adaptive_leo_traversal.constellation import (
    ConstellationConfig,
    DelayModelConfig,
    DynamicTopologyConfig,
    build_constellation_delay_table,
    make_constellation_topology,
    scheduled_down_edges,
)
from adaptive_leo_traversal.delay_table import DelayTable
from adaptive_leo_traversal.cycle import make_grid_hamiltonian_cycle
from adaptive_leo_traversal.linux_srv6 import (
    render_enable_srv6_sysctls,
    render_end_dt6_sid_route,
    render_local_ipv6_route,
    render_node_sid_route,
    render_plain_ipv6_route,
    render_srv6_encap_route,
)
from adaptive_leo_traversal.models import Edge, LinkState, TraversalResult, TraversalStatus, normalize_edge
from adaptive_leo_traversal.planner import shortest_path
from adaptive_leo_traversal.srv6_sid import SRv6SidAllocator
from adaptive_leo_traversal.tc_netem import (
    edge_iface_name,
    render_tc_loss100,
    render_tc_netem,
    render_tc_show,
)
from adaptive_leo_traversal.topology import Topology
from adaptive_leo_traversal.traversal import AdaptiveTraversalEngine, PhysicalLinkStateProvider


ALGORITHM_MODES = {"root_target", "adaptive_traversal"}
OBSERVATION_MODES = {"configured", "ping"}
DELAY_MODELS = {"constant", "propagation"}
DYNAMIC_TOPOLOGY_MODELS = {"static", "rotating_seam"}
DEFAULT_PLANES = 4
DEFAULT_SATELLITES_PER_PLANE = 4


@dataclass(frozen=True, slots=True)
class MininetSrv6Config:
    rows: int = DEFAULT_PLANES
    cols: int = DEFAULT_SATELLITES_PER_PLANE
    planes: int = DEFAULT_PLANES
    satellites_per_plane: int = DEFAULT_SATELLITES_PER_PLANE
    intra_plane_wrap: bool = False
    inter_plane_links: bool = True
    inter_plane_wrap: bool = False
    root: int = 0
    duration: float = 20.0
    slot_seconds: float = 1.0
    locator_prefix: str = "fc00:0"
    decap_table: str = "255"
    dst_prefix_base: str = "2001:db8"
    default_delay_ms: float = 5.0
    delay_model: str = "constant"
    delay_period_slots: int = 1
    altitude_km: float = 550.0
    inclination_deg: float = 53.0
    min_delay_ms: float = 0.1
    dynamic_topology_enabled: bool = False
    dynamic_topology_model: str = "static"
    dynamic_topology_period_slots: int = 1
    failure_edge: Edge | None = None
    failure_edges: tuple[Edge, ...] = ()
    failure_start: float = 0.0
    failure_end: float = 0.0
    algorithm_mode: str = "root_target"
    max_hop: int = 500
    alpha: float = 0.85
    validate_each_policy: bool = True
    observation_mode: str = "configured"
    observation_ttl_seconds: float | None = None
    agent_enabled: bool = False
    probe_packet_validation: bool = False
    agent_udp_port: int = 5005
    output_dir: str = "logs/mininet"
    run_name: str | None = None
    enable_cli: bool = True
    dry_run: bool = False
    verbose: bool = False
    tcpdump: bool = False

    def __post_init__(self) -> None:
        """Keep legacy rows/cols and constellation scale in sync for direct callers."""

        grid_changed = (self.rows, self.cols) != (
            DEFAULT_PLANES,
            DEFAULT_SATELLITES_PER_PLANE,
        )
        constellation_changed = (self.planes, self.satellites_per_plane) != (
            DEFAULT_PLANES,
            DEFAULT_SATELLITES_PER_PLANE,
        )
        if grid_changed and not constellation_changed:
            object.__setattr__(self, "planes", self.rows)
            object.__setattr__(self, "satellites_per_plane", self.cols)
        elif constellation_changed and not grid_changed:
            object.__setattr__(self, "rows", self.planes)
            object.__setattr__(self, "cols", self.satellites_per_plane)


@dataclass(frozen=True, slots=True)
class LinkInfo:
    edge: Edge
    link_id: int


@dataclass(frozen=True, slots=True)
class LabState:
    topology: Topology
    delay_table: DelayTable
    allocator: SRv6SidAllocator
    link_infos: dict[Edge, LinkInfo]
    target: int


@dataclass(frozen=True, slots=True)
class PolicyRender:
    source: int
    target: int
    path: list[int]
    segments: list[str]
    first_hop_dev: str
    command: list[str]
    cost: float | None = None


class NodeCommandRunner:
    """Centralized Mininet node command logging."""

    def __init__(self, log_file: TextIO, verbose: bool = False) -> None:
        self.log_file = log_file
        self.verbose = verbose

    def run(self, node: Any, argv: list[str]) -> str:
        command = shlex.join(argv)
        node_name = getattr(node, "name", str(node))
        self.write(f"{node_name}$ {command}")
        output = node.cmd(command)
        if output:
            self.write(output.rstrip())
        if self.verbose and output:
            print(output, end="" if output.endswith("\n") else "\n")
        return output

    def pexec(self, node: Any, argv: list[str]) -> tuple[str, str, int]:
        command = shlex.join(argv)
        node_name = getattr(node, "name", str(node))
        self.write(f"{node_name}$ {command}")
        if hasattr(node, "pexec"):
            stdout, stderr, returncode = node.pexec(argv)
        else:
            completed = subprocess.run(argv, check=False, capture_output=True, text=True)
            stdout, stderr, returncode = completed.stdout, completed.stderr, completed.returncode
        if isinstance(stdout, bytes):
            stdout = stdout.decode("utf-8", errors="replace")
        if isinstance(stderr, bytes):
            stderr = stderr.decode("utf-8", errors="replace")
        output = "".join(part for part in [stdout, stderr] if part)
        if output:
            self.write(output.rstrip())
        if self.verbose and output:
            print(output, end="" if output.endswith("\n") else "\n")
        return stdout, stderr, int(returncode)

    def write(self, message: str) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        self.log_file.write(f"[{timestamp}] {message}\n")
        self.log_file.flush()


class DryRunPrinter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self.stream = stream or sys.stdout

    def run(self, node_name: str, argv: list[str]) -> None:
        print(f"{node_name}$ {shlex.join(argv)}", file=self.stream)

    def note(self, message: str) -> None:
        print(f"# {message}", file=self.stream)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Mininet SRv6/tc emulation lab.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--rows", type=int, default=None)
    parser.add_argument("--cols", type=int, default=None)
    parser.add_argument("--planes", type=int, default=None)
    parser.add_argument("--satellites-per-plane", type=int, default=None)
    parser.add_argument("--root", type=int, default=None)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--slot-seconds", type=float, default=None)
    parser.add_argument("--locator-prefix", type=str, default=None)
    parser.add_argument("--decap-table", type=str, default=None)
    parser.add_argument("--dst-prefix-base", type=str, default=None)
    parser.add_argument("--delay-model", choices=sorted(DELAY_MODELS), default=None)
    parser.add_argument("--delay-period-slots", type=int, default=None)
    parser.add_argument("--altitude-km", type=float, default=None)
    parser.add_argument("--inclination-deg", type=float, default=None)
    parser.add_argument("--failure-edge", type=str, default=None, help='failure edge as "u,v"')
    parser.add_argument(
        "--failure-edges",
        type=str,
        default=None,
        help='multiple failure edges as "u,v;a,b"',
    )
    parser.add_argument("--failure-start", type=float, default=None)
    parser.add_argument("--failure-end", type=float, default=None)
    parser.add_argument("--dynamic-topology", action="store_true", default=None)
    parser.add_argument(
        "--no-dynamic-topology",
        dest="dynamic_topology",
        action="store_false",
        default=None,
    )
    parser.add_argument("--dynamic-topology-model", choices=sorted(DYNAMIC_TOPOLOGY_MODELS), default=None)
    parser.add_argument("--dynamic-topology-period-slots", type=int, default=None)
    parser.add_argument("--algorithm-mode", choices=sorted(ALGORITHM_MODES), default=None)
    parser.add_argument("--max-hop", type=int, default=None)
    parser.add_argument("--alpha", type=float, default=None)
    parser.add_argument("--observation-mode", choices=sorted(OBSERVATION_MODES), default=None)
    parser.add_argument("--observation-ttl-seconds", type=float, default=None)
    parser.add_argument("--enable-agents", action="store_true", default=None)
    parser.add_argument("--disable-agents", dest="enable_agents", action="store_false", default=None)
    parser.add_argument("--probe-packet-validation", action="store_true", default=None)
    parser.add_argument(
        "--no-probe-packet-validation",
        dest="probe_packet_validation",
        action="store_false",
        default=None,
    )
    parser.add_argument("--agent-udp-port", type=int, default=None)
    parser.add_argument("--output-dir", type=str, default=None)
    parser.add_argument("--run-name", type=str, default=None)
    parser.add_argument("--no-validate-each-policy", action="store_true", default=None)
    parser.add_argument("--no-cli", action="store_true", default=None)
    parser.add_argument("--dry-run", action="store_true", default=None)
    parser.add_argument("--verbose", action="store_true", default=None)
    parser.add_argument("--tcpdump", action="store_true", default=None)
    return parser


def config_from_args(argv: list[str] | None = None) -> MininetSrv6Config:
    args = build_arg_parser().parse_args(argv)
    config = load_config(args.config) if args.config else MininetSrv6Config()
    config = _merge_cli_args(config, args)
    _validate_config(config)
    return config


def load_config(path: str | Path) -> MininetSrv6Config:
    """Load a TOML config file for the Mininet SRv6 lab."""

    config_path = Path(path)
    data = tomllib.loads(config_path.read_text(encoding="utf-8"))
    config = MininetSrv6Config()

    grid = _table(data, "grid")
    if "rows" in grid:
        rows = int(grid["rows"])
        config = replace(config, rows=rows, planes=rows)
    if "cols" in grid:
        cols = int(grid["cols"])
        config = replace(config, cols=cols, satellites_per_plane=cols)

    constellation = _table(data, "constellation")
    if "planes" in constellation:
        planes = int(constellation["planes"])
        config = replace(config, planes=planes, rows=planes)
    if "satellites_per_plane" in constellation:
        satellites_per_plane = int(constellation["satellites_per_plane"])
        config = replace(
            config,
            satellites_per_plane=satellites_per_plane,
            cols=satellites_per_plane,
        )
    if "intra_plane_wrap" in constellation:
        config = replace(config, intra_plane_wrap=bool(constellation["intra_plane_wrap"]))
    if "inter_plane_links" in constellation:
        config = replace(config, inter_plane_links=bool(constellation["inter_plane_links"]))
    if "inter_plane_wrap" in constellation:
        config = replace(config, inter_plane_wrap=bool(constellation["inter_plane_wrap"]))

    srv6 = _table(data, "srv6")
    if "locator_prefix" in srv6:
        config = replace(config, locator_prefix=str(srv6["locator_prefix"]))
    if "decap_table" in srv6:
        config = replace(config, decap_table=str(srv6["decap_table"]))

    emulation = _table(data, "emulation")
    if "duration" in emulation:
        config = replace(config, duration=float(emulation["duration"]))
    if "slot_seconds" in emulation:
        config = replace(config, slot_seconds=float(emulation["slot_seconds"]))
    if "default_delay_ms" in emulation:
        config = replace(config, default_delay_ms=float(emulation["default_delay_ms"]))
    if "enable_cli" in emulation:
        config = replace(config, enable_cli=bool(emulation["enable_cli"]))

    delay = _table(data, "delay")
    if "model" in delay:
        config = replace(config, delay_model=str(delay["model"]))
    if "period_slots" in delay:
        config = replace(config, delay_period_slots=int(delay["period_slots"]))
    if "constant_delay_ms" in delay:
        config = replace(config, default_delay_ms=float(delay["constant_delay_ms"]))
    if "altitude_km" in delay:
        config = replace(config, altitude_km=float(delay["altitude_km"]))
    if "inclination_deg" in delay:
        config = replace(config, inclination_deg=float(delay["inclination_deg"]))
    if "min_delay_ms" in delay:
        config = replace(config, min_delay_ms=float(delay["min_delay_ms"]))

    dynamic_topology = _table(data, "dynamic_topology")
    if "enabled" in dynamic_topology:
        config = replace(config, dynamic_topology_enabled=bool(dynamic_topology["enabled"]))
    if "model" in dynamic_topology:
        config = replace(config, dynamic_topology_model=str(dynamic_topology["model"]))
    if "period_slots" in dynamic_topology:
        config = replace(config, dynamic_topology_period_slots=int(dynamic_topology["period_slots"]))

    failure = _table(data, "failure")
    if "edge" in failure:
        config = replace(config, failure_edge=_parse_failure_edge(failure["edge"]))
    if "edges" in failure:
        config = replace(config, failure_edges=_parse_failure_edges(failure["edges"]))
    if "start" in failure:
        config = replace(config, failure_start=float(failure["start"]))
    if "end" in failure:
        config = replace(config, failure_end=float(failure["end"]))

    algorithm = _table(data, "algorithm")
    if "mode" in algorithm:
        config = replace(config, algorithm_mode=str(algorithm["mode"]))
    if "max_hop" in algorithm:
        config = replace(config, max_hop=int(algorithm["max_hop"]))
    if "alpha" in algorithm:
        config = replace(config, alpha=float(algorithm["alpha"]))
    if "validate_each_policy" in algorithm:
        config = replace(config, validate_each_policy=bool(algorithm["validate_each_policy"]))

    observation = _table(data, "observation")
    if "mode" in observation:
        config = replace(config, observation_mode=str(observation["mode"]))
    if "stale_after_seconds" in observation:
        config = replace(config, observation_ttl_seconds=float(observation["stale_after_seconds"]))

    agent = _table(data, "agent")
    if "enabled" in agent:
        config = replace(config, agent_enabled=bool(agent["enabled"]))
    if "probe_packet_validation" in agent:
        config = replace(config, probe_packet_validation=bool(agent["probe_packet_validation"]))
    if "udp_port" in agent:
        config = replace(config, agent_udp_port=int(agent["udp_port"]))

    output = _table(data, "output")
    if "base_dir" in output:
        config = replace(config, output_dir=str(output["base_dir"]))
    if "run_name" in output:
        value = str(output["run_name"]).strip()
        config = replace(config, run_name=value or None)

    _validate_config(config)
    return config


def main(argv: list[str] | None = None) -> int:
    config = config_from_args(argv)
    if config.dry_run:
        run_dry_run(config)
        return 0

    if os.geteuid() != 0:
        print("Mininet/SRv6/tc emulation requires root. Run with sudo.", file=sys.stderr)
        return 1

    try:
        run_mininet_lab(config)
    except ModuleNotFoundError as exc:
        if exc.name and exc.name.startswith("mininet"):
            print(
                "Mininet Python API import failed. Install apt packages and ensure sudo uses "
                "the expected Python environment.",
                file=sys.stderr,
            )
            print(
                "Try: sudo apt install -y mininet openvswitch-switch iproute2 "
                "iputils-ping tcpdump",
                file=sys.stderr,
            )
            return 1
        raise
    except Exception as exc:
        print(f"Mininet SRv6 lab failed: {exc}", file=sys.stderr)
        print("Suggested cleanup/recovery:", file=sys.stderr)
        print("  sudo mn -c", file=sys.stderr)
        print("  sudo systemctl restart openvswitch-switch", file=sys.stderr)
        return 1
    return 0


def run_dry_run(config: MininetSrv6Config) -> None:
    """Print the commands that would be applied without importing or starting Mininet."""

    state = _build_lab_state(config)
    printer = DryRunPrinter()
    printer.note(f"algorithm mode: {config.algorithm_mode}")
    printer.note(f"hosts: {', '.join(f'r{node}' for node in sorted(state.topology.nodes))}")
    for u, v in sorted(state.topology.edges):
        printer.note(
            f"link r{u}<->r{v}: {edge_iface_name(u, v)} <-> {edge_iface_name(v, u)}"
        )

    for node in sorted(state.topology.nodes):
        for command in _render_node_setup_commands(config, state, node):
            printer.run(f"r{node}", command)

    for node in sorted(state.topology.nodes):
        for command in _render_underlay_route_commands(config, state, node):
            printer.run(f"r{node}", command)

    _print_link_impairments(printer, config, state, now=0.0, reason="initial_link_state")

    if config.algorithm_mode == "adaptive_traversal":
        _print_adaptive_traversal_dry_run(config, state, printer)
        return

    effective_topology = state.topology.without_edges(_down_edges_at(config, state, 0.0))
    policy = _render_root_target_policy(config, state, effective_topology)
    printer.run(f"r{config.root}", policy.command)
    _print_validation_commands(printer, config, state, policy)


def _print_adaptive_traversal_dry_run(
    config: MininetSrv6Config,
    state: LabState,
    printer: DryRunPrinter,
) -> None:
    engine = _build_adaptive_engine(config, state)
    provider = _build_physical_link_state_provider(config, state)
    probe = engine.initialize_probe(0.0)
    elapsed = 0.0
    last_down_edges = _down_edges_at(config, state, elapsed)

    printer.note(f"hamiltonian cycle: {list(engine.cycle_route)}")
    while elapsed <= config.duration:
        down_edges = _down_edges_at(config, state, elapsed)
        if down_edges != last_down_edges:
            _print_link_impairments(printer, config, state, elapsed, reason="link_state_changed")
            last_down_edges = down_edges

        current_node = probe.current_node
        result = engine.on_probe_arrival(probe, current_node, elapsed, provider)
        remaining_path = _remaining_policy_path(result, current_node)
        if remaining_path is not None:
            policy = _render_policy_for_path(config, state, remaining_path, cost=result.cost)
            printer.note(
                f"t={elapsed:g}s status={result.status.value} "
                f"source=r{policy.source} target=r{policy.target} "
                f"next_hop={result.next_hop}"
            )
            printer.run(f"r{policy.source}", policy.command)
            if config.validate_each_policy:
                _print_validation_commands(printer, config, state, policy)
        else:
            printer.note(
                f"t={elapsed:g}s status={result.status.value} "
                f"current=r{current_node} next_hop={result.next_hop}"
            )

        if result.status is not TraversalStatus.RUNNING:
            break
        if result.next_hop is None:
            break
        probe.current_node = result.next_hop
        elapsed = round(elapsed + config.slot_seconds, 6)


def _print_link_impairments(
    printer: DryRunPrinter,
    config: MininetSrv6Config,
    state: LabState,
    now: float,
    reason: str,
) -> None:
    down_edges = _down_edges_at(config, state, now)
    printer.note(f"{reason}: slot={state.delay_table.slot_at(now)} down_edges={sorted(down_edges)}")
    for u, v in sorted(state.topology.edges):
        if normalize_edge(u, v) in down_edges:
            printer.run(f"r{u}", render_tc_loss100(edge_iface_name(u, v)))
            printer.run(f"r{v}", render_tc_loss100(edge_iface_name(v, u)))
        else:
            delay_ms = _edge_delay_ms(config, state, now, u, v)
            printer.run(f"r{u}", render_tc_netem(edge_iface_name(u, v), delay_ms=delay_ms))
            printer.run(f"r{v}", render_tc_netem(edge_iface_name(v, u), delay_ms=delay_ms))


def run_mininet_lab(config: MininetSrv6Config) -> None:
    from mininet.cli import CLI
    from mininet.link import TCLink
    from mininet.log import setLogLevel
    from mininet.net import Mininet

    setLogLevel("info" if config.verbose else "warning")
    state = _build_lab_state(config)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_dir = _make_run_dir(Path(config.output_dir), config.run_name, timestamp)
    log_path = run_dir / "mininet_srv6.log"
    policy_path = run_dir / "policy_updates.jsonl"
    tc_path = run_dir / "tc_updates.jsonl"
    traversal_path = run_dir / "traversal_events.jsonl"

    net: Any | None = None
    tcpdump_proc: subprocess.Popen[str] | None = None
    agent_procs: list[subprocess.Popen[str]] = []
    with (
        log_path.open("w", encoding="utf-8") as log_file,
        policy_path.open("w", encoding="utf-8") as policy_log,
        tc_path.open("w", encoding="utf-8") as tc_log,
        traversal_path.open("w", encoding="utf-8") as traversal_log,
    ):
        runner = NodeCommandRunner(log_file, verbose=config.verbose)
        runner.write(f"log file: {log_path}")
        runner.write(f"policy updates: {policy_path}")
        runner.write(f"tc updates: {tc_path}")
        runner.write(f"traversal events: {traversal_path}")
        _write_json_file(
            run_dir / "run_config.json",
            {
                "run_dir": str(run_dir),
                "timestamp": timestamp,
                "config": _config_payload(config),
            },
        )

        try:
            net = Mininet(controller=None, link=TCLink, build=False, autoSetMacs=True)
            hosts = {node: net.addHost(f"r{node}", ip=None) for node in sorted(state.topology.nodes)}
            for u, v in sorted(state.topology.edges):
                net.addLink(
                    hosts[u],
                    hosts[v],
                    intfName1=edge_iface_name(u, v),
                    intfName2=edge_iface_name(v, u),
                )
            net.build()
            net.start()

            for node in sorted(state.topology.nodes):
                for command in _render_node_setup_commands(config, state, node):
                    runner.run(hosts[node], command)

            _apply_underlay_routes(config, state, state.topology, hosts, runner, slot=0)
            if config.agent_enabled:
                agent_procs = _start_node_agents(config, state, hosts, run_dir, runner)

            if config.algorithm_mode == "adaptive_traversal":
                tcpdump_proc = _run_adaptive_traversal_lab(
                    config,
                    state,
                    hosts,
                    runner,
                    policy_log,
                    tc_log,
                    traversal_log,
                    log_file,
                )
            else:
                tcpdump_proc = _run_root_target_lab(
                    config,
                    state,
                    hosts,
                    runner,
                    policy_log,
                    tc_log,
                    log_file,
                )

            runner.write(
                f"finished timed run; logs: {log_path}, {policy_path}, {tc_path}, {traversal_path}"
            )
            print(f"Run directory: {run_dir}")
            print(f"Mininet SRv6 log: {log_path}")
            print(f"Policy updates: {policy_path}")
            print(f"tc updates: {tc_path}")
            print(f"Traversal events: {traversal_path}")

            if config.enable_cli:
                runner.write("entering Mininet CLI")
                CLI(net)
        finally:
            if tcpdump_proc is not None:
                _stop_tcpdump(tcpdump_proc, runner)
            _stop_node_agents(agent_procs, runner)
            if net is not None:
                net.stop()


def _run_root_target_lab(
    config: MininetSrv6Config,
    state: LabState,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    policy_log: TextIO,
    tc_log: TextIO,
    log_file: TextIO,
) -> subprocess.Popen[str] | None:
    tcpdump_proc: subprocess.Popen[str] | None = None
    active_down_edges = _down_edges_at(config, state, 0.0)
    active_slot = state.delay_table.slot_at(0.0)
    _apply_link_impairments(config, state, hosts, runner, tc_log, now=0.0, reason="initial_link_state")
    effective_topology = state.topology.without_edges(active_down_edges)
    _apply_underlay_routes(config, state, effective_topology, hosts, runner, slot=active_slot)
    current_policy = _install_rendered_policy(
        config,
        state,
        hosts,
        runner,
        policy_log,
        _render_root_target_policy(config, state, effective_topology, slot=active_slot),
        now=0.0,
        reason="initial",
    )

    if config.tcpdump:
        tcpdump_proc = _start_tcpdump(hosts[current_policy.source], current_policy.first_hop_dev, log_file)

    if config.validate_each_policy:
        _run_validation(config, state, hosts, runner, current_policy)
    current_path = current_policy.path

    elapsed = 0.0
    while elapsed < config.duration:
        sleep_for = min(config.slot_seconds, config.duration - elapsed)
        if sleep_for > 0:
            time.sleep(sleep_for)
        elapsed = round(elapsed + sleep_for, 6)

        down_edges = _down_edges_at(config, state, elapsed)
        slot = state.delay_table.slot_at(elapsed)
        _apply_link_impairments(config, state, hosts, runner, tc_log, now=elapsed, reason="slot_update")
        effective_topology = state.topology.without_edges(down_edges)
        if down_edges != active_down_edges or slot != active_slot:
            _apply_underlay_routes(config, state, effective_topology, hosts, runner, slot=slot)
            active_down_edges = down_edges
            active_slot = slot
        candidate = _render_root_target_policy(config, state, effective_topology, slot=slot)
        if candidate.path != current_path:
            current_policy = _install_rendered_policy(
                config,
                state,
                hosts,
                runner,
                policy_log,
                candidate,
                now=elapsed,
                reason="topology_or_delay_update",
            )
            if config.validate_each_policy:
                _run_validation(config, state, hosts, runner, current_policy)
            current_path = current_policy.path

    return tcpdump_proc


def _run_adaptive_traversal_lab(
    config: MininetSrv6Config,
    state: LabState,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    policy_log: TextIO,
    tc_log: TextIO,
    traversal_log: TextIO,
    log_file: TextIO,
) -> subprocess.Popen[str] | None:
    tcpdump_proc: subprocess.Popen[str] | None = None
    engine = _build_adaptive_engine(config, state)
    provider = (
        _build_ping_link_state_provider(config, state, hosts, runner)
        if config.observation_mode == "ping"
        else _build_physical_link_state_provider(config, state)
    )
    probe = engine.initialize_probe(0.0)
    active_down_edges = _down_edges_at(config, state, 0.0)
    active_slot = state.delay_table.slot_at(0.0)
    _apply_link_impairments(config, state, hosts, runner, tc_log, now=0.0, reason="initial_link_state")
    _apply_underlay_routes(
        config,
        state,
        state.topology.without_edges(active_down_edges),
        hosts,
        runner,
        slot=active_slot,
    )

    runner.write(f"adaptive traversal cycle: {list(engine.cycle_route)}")
    elapsed = 0.0
    installed_policy_key: tuple[int, int, tuple[int, ...]] | None = None
    while elapsed <= config.duration:
        transition_reason = ""
        down_edges = _down_edges_at(config, state, elapsed)
        slot = state.delay_table.slot_at(elapsed)
        _apply_link_impairments(config, state, hosts, runner, tc_log, now=elapsed, reason="slot_update")
        link_state_changed = down_edges != active_down_edges
        delay_slot_changed = slot != active_slot
        if link_state_changed or delay_slot_changed:
            reasons = []
            if link_state_changed:
                reasons.append("link_state_changed")
            if delay_slot_changed:
                reasons.append("delay_slot_changed")
            transition_reason = "+".join(reasons)
            _apply_underlay_routes(
                config,
                state,
                state.topology.without_edges(down_edges),
                hosts,
                runner,
                slot=slot,
            )
            active_down_edges = down_edges
            active_slot = slot

        current_node = probe.current_node
        result = engine.on_probe_arrival(probe, current_node, elapsed, provider)
        remaining_path = _remaining_policy_path(result, current_node)
        policy: PolicyRender | None = None
        if remaining_path is not None:
            policy = _render_policy_for_path(config, state, remaining_path, cost=result.cost)
            policy_key = (policy.source, policy.target, tuple(policy.path))
            if policy_key != installed_policy_key:
                reason = transition_reason or result.message or (
                    "initial" if installed_policy_key is None else "traversal_step"
                )
                _install_rendered_policy(
                    config,
                    state,
                    hosts,
                    runner,
                    policy_log,
                    policy,
                    now=elapsed,
                    reason=reason,
                )
                installed_policy_key = policy_key
                if config.tcpdump and tcpdump_proc is None:
                    tcpdump_proc = _start_tcpdump(
                        hosts[policy.source],
                        policy.first_hop_dev,
                        log_file,
                    )
                if config.validate_each_policy:
                    _run_validation(config, state, hosts, runner, policy)

        _write_traversal_event(
            traversal_log,
            now=elapsed,
            current_node=current_node,
            result=result,
            remaining_path=remaining_path,
            policy=policy,
            failure_active=_failure_active(config, elapsed),
            reason=transition_reason,
        )

        if result.status is not TraversalStatus.RUNNING:
            break
        if result.next_hop is None:
            break
        probe.current_node = result.next_hop

        sleep_for = min(config.slot_seconds, config.duration - elapsed)
        if sleep_for <= 0:
            break
        time.sleep(sleep_for)
        elapsed = round(elapsed + sleep_for, 6)

    return tcpdump_proc


def _start_node_agents(
    config: MininetSrv6Config,
    state: LabState,
    hosts: dict[int, Any],
    run_dir: Path,
    runner: NodeCommandRunner,
) -> list[subprocess.Popen[str]]:
    procs: list[subprocess.Popen[str]] = []
    for node in sorted(state.topology.nodes):
        neighbors = ",".join(str(neighbor) for neighbor in sorted(state.topology.neighbors(node)))
        log_path = (run_dir / f"agent_r{node}.jsonl").resolve()
        stderr_path = (run_dir / f"agent_r{node}.stderr.log").resolve()
        argv = [
            "python3",
            str((_PROJECT_ROOT / "emulation" / "node_agent.py").resolve()),
            "--node-id",
            str(node),
            "--port",
            str(config.agent_udp_port),
            "--neighbors",
            neighbors,
            "--log",
            str(log_path),
        ]
        runner.write(f"starting agent r{node}: {shlex.join(argv)}")
        with stderr_path.open("ab") as stderr_file:
            procs.append(hosts[node].popen(argv, stdout=stderr_file, stderr=stderr_file))
    time.sleep(0.2)
    for node, proc in zip(sorted(state.topology.nodes), procs, strict=True):
        returncode = proc.poll()
        if returncode is None:
            continue
        stderr_path = (run_dir / f"agent_r{node}.stderr.log").resolve()
        diagnostics = _read_short_text(stderr_path)
        runner.write(f"agent r{node} exited during startup with code {returncode}")
        if diagnostics:
            runner.write(f"agent r{node} diagnostics: {diagnostics}")
        raise RuntimeError(f"agent r{node} failed to start; see {stderr_path}")
    return procs


def _stop_node_agents(
    procs: list[subprocess.Popen[str]],
    runner: NodeCommandRunner,
) -> None:
    for proc in procs:
        if proc.poll() is None:
            proc.terminate()
    for proc in procs:
        if proc.poll() is not None:
            continue
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            runner.write("killing unresponsive node agent")
            proc.kill()
            proc.wait(timeout=2)


def _build_lab_state(config: MininetSrv6Config) -> LabState:
    _validate_config(config)
    constellation = _constellation_config(config)
    topology = make_constellation_topology(constellation)
    for edge in _configured_failure_edges(config):
        if edge not in topology.edges:
            raise ValueError(f"failure edge {edge} is not in the constellation topology")
    delay_table = build_constellation_delay_table(
        topology,
        constellation,
        _delay_model_config(config),
    )
    allocator = SRv6SidAllocator(config.locator_prefix)
    link_infos = {
        edge: LinkInfo(edge=edge, link_id=index)
        for index, edge in enumerate(sorted(topology.edges), start=1)
    }
    target = constellation.node_count - 1
    return LabState(
        topology=topology,
        delay_table=delay_table,
        allocator=allocator,
        link_infos=link_infos,
        target=target,
    )


def _constellation_config(config: MininetSrv6Config) -> ConstellationConfig:
    return ConstellationConfig(
        planes=config.planes,
        satellites_per_plane=config.satellites_per_plane,
        intra_plane_wrap=config.intra_plane_wrap,
        inter_plane_links=config.inter_plane_links,
        inter_plane_wrap=config.inter_plane_wrap,
    )


def _delay_model_config(config: MininetSrv6Config) -> DelayModelConfig:
    return DelayModelConfig(
        model=config.delay_model,
        period_slots=config.delay_period_slots,
        constant_delay_ms=config.default_delay_ms,
        altitude_km=config.altitude_km,
        inclination_deg=config.inclination_deg,
        min_delay_ms=config.min_delay_ms,
    )


def _dynamic_topology_config(config: MininetSrv6Config) -> DynamicTopologyConfig:
    return DynamicTopologyConfig(
        enabled=config.dynamic_topology_enabled,
        model=config.dynamic_topology_model,
        period_slots=config.dynamic_topology_period_slots,
    )


def _build_adaptive_engine(
    config: MininetSrv6Config,
    state: LabState,
) -> AdaptiveTraversalEngine:
    cycle_route = tuple(make_grid_hamiltonian_cycle(config.planes, config.satellites_per_plane))
    return AdaptiveTraversalEngine(
        base_topology=state.topology,
        delay_table=state.delay_table,
        root=config.root,
        max_hop=config.max_hop,
        cycle_route=cycle_route,
        alpha=config.alpha,
        observation_ttl=config.observation_ttl_seconds,
    )


def _build_physical_link_state_provider(
    config: MininetSrv6Config,
    state: LabState,
) -> PhysicalLinkStateProvider:
    def provider(u: int, v: int, now: float) -> LinkState:
        if normalize_edge(u, v) in _down_edges_at(config, state, now):
            return LinkState.DOWN
        return LinkState.UP

    return provider


def _build_ping_link_state_provider(
    config: MininetSrv6Config,
    state: LabState,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
) -> PhysicalLinkStateProvider:
    def provider(u: int, v: int, now: float) -> LinkState:
        if normalize_edge(u, v) in _scheduled_down_edges_at(config, state, now):
            return LinkState.DOWN
        source = _link_addr(config, state, u, v, with_prefix=False)
        target = _link_addr(config, state, v, u, with_prefix=False)
        _, _, returncode = runner.pexec(
            hosts[u],
            ["ping", "-6", "-I", source, "-c", "1", "-W", "1", target],
        )
        return LinkState.UP if returncode == 0 else LinkState.DOWN

    return provider


def _down_edges_at(config: MininetSrv6Config, state: LabState, now: float) -> set[Edge]:
    down = _scheduled_down_edges_at(config, state, now)
    if _failure_active(config, now):
        down.update(_configured_failure_edges(config))
    return down


def _scheduled_down_edges_at(
    config: MininetSrv6Config,
    state: LabState,
    now: float,
) -> set[Edge]:
    slot = state.delay_table.slot_at(now)
    return scheduled_down_edges(
        state.topology,
        _constellation_config(config),
        _dynamic_topology_config(config),
        slot,
    )


def _edge_delay_ms(config: MininetSrv6Config, state: LabState, now: float, u: int, v: int) -> float:
    return state.delay_table.get_delay(state.delay_table.slot_at(now), u, v)


def _remaining_policy_path(
    result: TraversalResult,
    current_node: int,
) -> list[int] | None:
    if not result.path or len(result.path) < 2:
        return None
    try:
        index = result.path.index(current_node)
    except ValueError:
        return None
    remaining = result.path[index:]
    if len(remaining) < 2:
        return None
    return remaining


def _write_traversal_event(
    traversal_log: TextIO,
    now: float,
    current_node: int,
    result: TraversalResult,
    remaining_path: list[int] | None,
    policy: PolicyRender | None,
    failure_active: bool,
    reason: str,
) -> None:
    payload: dict[str, Any] = {
        "time": now,
        "status": result.status.value,
        "current_node": current_node,
        "next_hop": result.next_hop,
        "next_telemetry_node": result.probe.next_telemetry_node,
        "path": result.path,
        "remaining_path": remaining_path,
        "visited": sorted(result.probe.visited),
        "hop_count": result.probe.hop_count,
        "hop_limit": result.probe.hop_limit,
        "failure_active": failure_active,
        "message": result.message,
        "reason": reason,
    }
    if policy is not None:
        payload.update(
            {
                "policy_source": policy.source,
                "policy_target": policy.target,
                "segments": policy.segments,
                "first_hop_dev": policy.first_hop_dev,
            }
        )
    _write_jsonl(traversal_log, payload)


def _render_node_setup_commands(
    config: MininetSrv6Config,
    state: LabState,
    node: int,
) -> list[list[str]]:
    ifaces = [edge_iface_name(node, neighbor) for neighbor in sorted(state.topology.neighbors(node))]
    sid_dev = ifaces[0] if ifaces else "lo"
    commands = [["ip", "link", "set", "lo", "up"]]
    commands.extend(render_enable_srv6_sysctls(ifaces))
    commands.append(["ip", "-6", "route", "del", "default"])
    for neighbor in sorted(state.topology.neighbors(node)):
        iface = edge_iface_name(node, neighbor)
        commands.append(["ip", "link", "set", iface, "up"])
        commands.append(["ip", "-6", "addr", "add", _link_addr(config, state, node, neighbor), "dev", iface])
    commands.append(["ip", "-6", "addr", "add", _service_addr(config, node), "dev", "lo"])
    # Keep the service loopback explicit for local delivery after decap.
    commands.append(render_local_ipv6_route(_service_addr(config, node), dev="lo", table=config.decap_table))
    commands.append(render_node_sid_route(state.allocator.node_sid(node), dev=sid_dev))
    commands.append(
        render_end_dt6_sid_route(
            state.allocator.decap_sid(node),
            lookup_table=config.decap_table,
            dev=sid_dev,
        )
    )
    return commands


def _render_underlay_route_commands(
    config: MininetSrv6Config,
    state: LabState,
    source: int,
    topology: Topology | None = None,
    slot: int = 0,
) -> list[list[str]]:
    route_topology = topology or state.topology
    commands: list[list[str]] = []
    for target in sorted(route_topology.nodes):
        if target == source:
            continue
        if route_topology.has_edge(source, target):
            next_hop = target
        else:
            path, _ = shortest_path(
                route_topology,
                state.delay_table,
                slot=slot,
                source=source,
                target=target,
            )
            if path is None or len(path) < 2:
                raise RuntimeError(f"no underlay path from {source} to {target}")
            next_hop = path[1]
        dev = edge_iface_name(source, next_hop)
        via = _link_addr(config, state, next_hop, source, with_prefix=False)
        for sid in [state.allocator.node_sid(target), state.allocator.decap_sid(target)]:
            commands.append(render_plain_ipv6_route(f"{sid}/128", via=via, dev=dev))
        commands.append(render_plain_ipv6_route(_service_addr(config, target), via=via, dev=dev))
    return commands


def _apply_underlay_routes(
    config: MininetSrv6Config,
    state: LabState,
    topology: Topology,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    slot: int = 0,
) -> None:
    for node in sorted(topology.nodes):
        for command in _render_underlay_route_commands(
            config,
            state,
            node,
            topology=topology,
            slot=slot,
        ):
            runner.run(hosts[node], command)


def _render_root_target_policy(
    config: MininetSrv6Config,
    state: LabState,
    topology: Topology,
    slot: int = 0,
) -> PolicyRender:
    path, cost = shortest_path(
        topology,
        state.delay_table,
        slot=slot,
        source=config.root,
        target=state.target,
    )
    if path is None:
        raise RuntimeError(f"no SRv6 policy path from {config.root} to {state.target}")
    if len(path) < 2:
        raise RuntimeError("SRv6 policy path must contain at least one hop")
    return _render_policy_for_path(config, state, path, cost=cost)


def _render_policy_for_path(
    config: MininetSrv6Config,
    state: LabState,
    path: list[int],
    cost: float | None = None,
) -> PolicyRender:
    if len(path) < 2:
        raise RuntimeError("SRv6 policy path must contain at least one hop")
    for source, target in zip(path, path[1:]):
        if not state.topology.has_edge(source, target):
            raise RuntimeError(f"SRv6 policy path contains non-adjacent hop {source}->{target}")

    source = path[0]
    target = path[-1]
    segments = [state.allocator.node_sid(node) for node in path[1:-1]]
    segments.append(state.allocator.decap_sid(target))
    first_hop_dev = edge_iface_name(path[0], path[1])
    first_hop_via = _link_addr(config, state, path[1], path[0], with_prefix=False)
    command = render_srv6_encap_route(
        dst_prefix=_service_addr(config, target),
        segments=segments,
        dev=first_hop_dev,
        via=first_hop_via,
    )
    return PolicyRender(
        source=source,
        target=target,
        path=path,
        segments=segments,
        first_hop_dev=first_hop_dev,
        command=command,
        cost=cost,
    )


def _install_rendered_policy(
    config: MininetSrv6Config,
    state: LabState,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    policy_log: TextIO,
    policy: PolicyRender,
    now: float,
    reason: str,
) -> PolicyRender:
    runner.run(hosts[policy.source], policy.command)
    _write_jsonl(
        policy_log,
        {
            "time": now,
            "source": policy.source,
            "target": policy.target,
            "path": policy.path,
            "segments": policy.segments,
            "first_hop_dev": policy.first_hop_dev,
            "cost": policy.cost,
            "reason": reason,
        },
    )
    return policy


def _apply_link_impairments(
    config: MininetSrv6Config,
    state: LabState,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    tc_log: TextIO,
    now: float,
    reason: str,
) -> None:
    down_edges = _down_edges_at(config, state, now)
    for u, v in sorted(state.topology.edges):
        edge = normalize_edge(u, v)
        if edge in down_edges:
            interfaces = [edge_iface_name(u, v), edge_iface_name(v, u)]
            runner.run(hosts[u], render_tc_loss100(interfaces[0]))
            runner.run(hosts[v], render_tc_loss100(interfaces[1]))
            _write_jsonl(
                tc_log,
                {
                    "time": now,
                    "edge": [u, v],
                    "interfaces": interfaces,
                    "action": "loss100",
                    "reason": reason,
                },
            )
        else:
            _apply_edge_delay(
                config,
                hosts,
                runner,
                tc_log,
                now,
                u,
                v,
                reason,
                delay_ms=_edge_delay_ms(config, state, now, u, v),
            )


def _apply_edge_delay(
    config: MininetSrv6Config,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    tc_log: TextIO,
    now: float,
    u: int,
    v: int,
    reason: str,
    delay_ms: float | None = None,
) -> None:
    interfaces = [edge_iface_name(u, v), edge_iface_name(v, u)]
    actual_delay_ms = config.default_delay_ms if delay_ms is None else delay_ms
    runner.run(hosts[u], render_tc_netem(interfaces[0], delay_ms=actual_delay_ms))
    runner.run(hosts[v], render_tc_netem(interfaces[1], delay_ms=actual_delay_ms))
    _write_jsonl(
        tc_log,
        {
            "time": now,
            "edge": [u, v],
            "interfaces": interfaces,
            "action": "delay",
            "delay_ms": actual_delay_ms,
            "reason": reason,
        },
    )


def _run_validation(
    config: MininetSrv6Config,
    state: LabState,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    policy: PolicyRender,
) -> None:
    target_addr = _service_addr(config, policy.target, with_prefix=False)
    source_addr = _service_addr(config, policy.source, with_prefix=False)
    runner.run(hosts[policy.source], ["ping", "-6", "-I", source_addr, "-c", "2", "-W", "1", target_addr])
    runner.run(hosts[policy.source], ["ip", "-6", "route", "get", target_addr, "from", source_addr])
    runner.run(hosts[policy.source], ["ip", "-6", "route", "show"])
    runner.run(hosts[policy.target], ["ip", "-6", "addr", "show", "lo"])
    runner.run(hosts[policy.source], render_tc_show(policy.first_hop_dev))
    if config.probe_packet_validation:
        _run_probe_packet_validation(config, hosts, runner, policy)


def _run_probe_packet_validation(
    config: MininetSrv6Config,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    policy: PolicyRender,
) -> None:
    target_addr = _service_addr(config, policy.target, with_prefix=False)
    source_addr = _service_addr(config, policy.source, with_prefix=False)
    _, _, returncode = runner.pexec(
        hosts[policy.source],
        [
            "python3",
            str((_PROJECT_ROOT / "emulation" / "probe_client.py").resolve()),
            "--run-id",
            "mininet",
            "--sequence",
            str(int(time.time() * 1000)),
            "--root",
            str(config.root),
            "--source-node",
            str(policy.source),
            "--target-node",
            str(policy.target),
            "--src",
            source_addr,
            "--dst",
            target_addr,
            "--port",
            str(config.agent_udp_port),
            "--path",
            ",".join(str(node) for node in policy.path),
            "--hop-count",
            str(max(0, len(policy.path) - 1)),
            "--hop-limit",
            str(config.max_hop),
        ],
    )
    if returncode != 0:
        runner.write(f"probe packet validation failed for r{policy.source}->r{policy.target}")


def _print_validation_commands(
    printer: DryRunPrinter,
    config: MininetSrv6Config,
    state: LabState,
    policy: PolicyRender,
) -> None:
    target_addr = _service_addr(config, policy.target, with_prefix=False)
    source_addr = _service_addr(config, policy.source, with_prefix=False)
    printer.run(f"r{policy.source}", ["ping", "-6", "-I", source_addr, "-c", "2", "-W", "1", target_addr])
    printer.run(f"r{policy.source}", ["ip", "-6", "route", "get", target_addr, "from", source_addr])
    printer.run(f"r{policy.source}", ["ip", "-6", "route", "show"])
    printer.run(f"r{policy.target}", ["ip", "-6", "addr", "show", "lo"])
    printer.run(f"r{policy.source}", render_tc_show(policy.first_hop_dev))
    if config.probe_packet_validation:
        printer.run(
            f"r{policy.source}",
            [
                "python3",
                str((_PROJECT_ROOT / "emulation" / "probe_client.py").resolve()),
                "--run-id",
                "dry-run",
                "--sequence",
                "0",
                "--root",
                str(config.root),
                "--source-node",
                str(policy.source),
                "--target-node",
                str(policy.target),
                "--src",
                source_addr,
                "--dst",
                target_addr,
                "--port",
                str(config.agent_udp_port),
                "--path",
                ",".join(str(node) for node in policy.path),
                "--hop-count",
                str(max(0, len(policy.path) - 1)),
                "--hop-limit",
                str(config.max_hop),
            ],
        )


def _start_tcpdump(host: Any, iface: str, log_file: TextIO) -> subprocess.Popen[str]:
    log_file.write(f"starting tcpdump on {host.name}:{iface}\n")
    log_file.flush()
    return host.popen(
        ["tcpdump", "-i", iface, "-vv", "ip6"],
        stdout=log_file,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _stop_tcpdump(proc: subprocess.Popen[str], runner: NodeCommandRunner) -> None:
    runner.write("stopping tcpdump")
    proc.terminate()
    try:
        proc.wait(timeout=3)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=3)


def _failure_active(config: MininetSrv6Config, now: float) -> bool:
    return (
        bool(_configured_failure_edges(config))
        and config.failure_start <= now < config.failure_end
        and config.failure_end > config.failure_start
    )


def _configured_failure_edges(config: MininetSrv6Config) -> set[Edge]:
    edges = set(config.failure_edges)
    if config.failure_edge is not None:
        edges.add(config.failure_edge)
    return edges


def _link_addr(
    config: MininetSrv6Config,
    state: LabState,
    node: int,
    neighbor: int,
    with_prefix: bool = True,
) -> str:
    edge = normalize_edge(node, neighbor)
    link_id = state.link_infos[edge].link_id
    address = f"{config.dst_prefix_base}:e:{link_id:x}::{node + 1}"
    return f"{address}/64" if with_prefix else address


def _service_addr(config: MininetSrv6Config, node: int, with_prefix: bool = True) -> str:
    address = f"{config.dst_prefix_base}:100:{node:x}::1"
    return f"{address}/128" if with_prefix else address


def _write_jsonl(handle: TextIO, payload: dict[str, Any]) -> None:
    handle.write(json.dumps(payload, sort_keys=True) + "\n")
    handle.flush()


def _write_json_file(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _read_short_text(path: Path, limit: int = 2000) -> str:
    if not path.exists():
        return ""
    text = path.read_text(encoding="utf-8", errors="replace").strip()
    if len(text) <= limit:
        return text
    return text[-limit:]


def _config_payload(config: MininetSrv6Config) -> dict[str, Any]:
    return asdict(config)


def _make_run_dir(base_dir: Path, run_name: str | None, timestamp: str) -> Path:
    base_dir.mkdir(parents=True, exist_ok=True)
    name = _safe_run_name(run_name or timestamp)
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


def _safe_run_name(value: str) -> str:
    cleaned = "".join(char if char.isalnum() or char in {"-", "_", "."} else "_" for char in value)
    cleaned = cleaned.strip("._-")
    return cleaned or "run"


def _merge_cli_args(
    config: MininetSrv6Config,
    args: argparse.Namespace,
) -> MininetSrv6Config:
    updates: dict[str, Any] = {}
    for attr in [
        "rows",
        "cols",
        "planes",
        "satellites_per_plane",
        "root",
        "duration",
        "slot_seconds",
        "locator_prefix",
        "decap_table",
        "dst_prefix_base",
        "delay_model",
        "delay_period_slots",
        "altitude_km",
        "inclination_deg",
        "failure_start",
        "failure_end",
        "dynamic_topology_model",
        "dynamic_topology_period_slots",
        "algorithm_mode",
        "max_hop",
        "alpha",
        "observation_mode",
        "observation_ttl_seconds",
        "agent_udp_port",
        "output_dir",
        "run_name",
    ]:
        value = getattr(args, attr)
        if value is not None:
            updates[attr] = value

    if "rows" in updates:
        updates["planes"] = updates["rows"]
    if "cols" in updates:
        updates["satellites_per_plane"] = updates["cols"]
    if "planes" in updates:
        updates["rows"] = updates["planes"]
    if "satellites_per_plane" in updates:
        updates["cols"] = updates["satellites_per_plane"]
    if args.failure_edge is not None:
        updates["failure_edge"] = _parse_failure_edge(args.failure_edge)
    if args.failure_edges is not None:
        updates["failure_edges"] = _parse_failure_edges(args.failure_edges)
    if args.dynamic_topology is not None:
        updates["dynamic_topology_enabled"] = args.dynamic_topology
    if args.enable_agents is not None:
        updates["agent_enabled"] = args.enable_agents
    if args.probe_packet_validation is not None:
        updates["probe_packet_validation"] = args.probe_packet_validation
    if args.no_validate_each_policy is not None:
        updates["validate_each_policy"] = not args.no_validate_each_policy
    if args.no_cli is not None:
        updates["enable_cli"] = not args.no_cli
    if args.dry_run is not None:
        updates["dry_run"] = args.dry_run
    if args.verbose is not None:
        updates["verbose"] = args.verbose
    if args.tcpdump is not None:
        updates["tcpdump"] = args.tcpdump
    return replace(config, **updates)


def _validate_config(config: MininetSrv6Config) -> None:
    if config.rows <= 0 or config.cols <= 0:
        raise ValueError("rows and cols must be positive")
    if config.planes <= 0 or config.satellites_per_plane <= 0:
        raise ValueError("planes and satellites_per_plane must be positive")
    if config.rows != config.planes or config.cols != config.satellites_per_plane:
        raise ValueError("rows/cols must match planes/satellites_per_plane")
    if not 0 <= config.root < config.rows * config.cols:
        raise ValueError("root must be within the grid node range")
    if config.duration < 0:
        raise ValueError("duration must be non-negative")
    if config.slot_seconds <= 0:
        raise ValueError("slot_seconds must be positive")
    if config.default_delay_ms < 0:
        raise ValueError("default_delay_ms must be non-negative")
    if not config.locator_prefix.strip():
        raise ValueError("locator_prefix must be non-empty")
    if not config.decap_table.strip():
        raise ValueError("decap_table must be non-empty")
    if not config.dst_prefix_base.strip():
        raise ValueError("dst_prefix_base must be non-empty")
    if config.failure_start < 0 or config.failure_end < 0:
        raise ValueError("failure_start and failure_end must be non-negative")
    if config.delay_model not in DELAY_MODELS:
        modes = ", ".join(sorted(DELAY_MODELS))
        raise ValueError(f"delay_model must be one of: {modes}")
    if config.delay_period_slots <= 0:
        raise ValueError("delay_period_slots must be positive")
    if config.altitude_km < 0:
        raise ValueError("altitude_km must be non-negative")
    if config.min_delay_ms < 0:
        raise ValueError("min_delay_ms must be non-negative")
    if config.dynamic_topology_model not in DYNAMIC_TOPOLOGY_MODELS:
        modes = ", ".join(sorted(DYNAMIC_TOPOLOGY_MODELS))
        raise ValueError(f"dynamic_topology_model must be one of: {modes}")
    if config.dynamic_topology_period_slots <= 0:
        raise ValueError("dynamic_topology_period_slots must be positive")
    if config.algorithm_mode not in ALGORITHM_MODES:
        modes = ", ".join(sorted(ALGORITHM_MODES))
        raise ValueError(f"algorithm_mode must be one of: {modes}")
    if config.max_hop <= 0:
        raise ValueError("max_hop must be positive")
    if not 0 < config.alpha <= 1:
        raise ValueError("alpha must be in the interval (0, 1]")
    if (
        config.algorithm_mode == "adaptive_traversal"
        and (config.rows < 2 or config.cols < 2 or config.rows % 2 != 0)
    ):
        raise ValueError("adaptive_traversal requires a grid with rows >= 2, cols >= 2, and even rows")
    if config.algorithm_mode == "adaptive_traversal" and not config.inter_plane_links:
        raise ValueError("adaptive_traversal requires inter_plane_links=true")
    if config.observation_mode not in OBSERVATION_MODES:
        modes = ", ".join(sorted(OBSERVATION_MODES))
        raise ValueError(f"observation_mode must be one of: {modes}")
    if config.observation_ttl_seconds is not None and config.observation_ttl_seconds < 0:
        raise ValueError("observation_ttl_seconds must be non-negative")
    if config.agent_udp_port <= 0:
        raise ValueError("agent_udp_port must be positive")
    if config.probe_packet_validation and not config.agent_enabled:
        raise ValueError("probe_packet_validation requires agent_enabled")
    if not config.output_dir.strip():
        raise ValueError("output_dir must be non-empty")


def _parse_failure_edge(value: object) -> Edge:
    if isinstance(value, str):
        parts = [part.strip() for part in value.split(",")]
    elif isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        raise ValueError('failure edge must be "u,v" or a two-item list')

    if len(parts) != 2:
        raise ValueError('failure edge must contain exactly two nodes, e.g. "0,1"')
    return normalize_edge(int(parts[0]), int(parts[1]))


def _parse_failure_edges(value: object) -> tuple[Edge, ...]:
    if isinstance(value, str):
        raw_edges: list[object] = [part.strip() for part in value.split(";") if part.strip()]
    elif isinstance(value, (list, tuple)):
        raw_edges = list(value)
    else:
        raise ValueError('failure edges must be "u,v;a,b" or a list of two-item lists')
    return tuple(sorted({_parse_failure_edge(edge) for edge in raw_edges}))


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a TOML table")
    return value


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
