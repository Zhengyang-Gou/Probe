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
from dataclasses import dataclass, replace
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO

_PROJECT_SRC = Path(__file__).resolve().parents[1] / "src"
if _PROJECT_SRC.exists() and str(_PROJECT_SRC) not in sys.path:
    sys.path.insert(0, str(_PROJECT_SRC))

from adaptive_leo_traversal.delay_table import DelayTable
from adaptive_leo_traversal.linux_srv6 import (
    render_enable_srv6_sysctls,
    render_end_dt6_sid_route,
    render_node_sid_route,
    render_plain_ipv6_route,
    render_srv6_encap_route,
)
from adaptive_leo_traversal.models import Edge, normalize_edge
from adaptive_leo_traversal.planner import shortest_path
from adaptive_leo_traversal.srv6_policy import encode_node_sid_policy
from adaptive_leo_traversal.srv6_sid import SRv6SidAllocator
from adaptive_leo_traversal.tc_netem import (
    edge_iface_name,
    render_tc_loss100,
    render_tc_netem,
    render_tc_show,
)
from adaptive_leo_traversal.topology import Topology, make_grid_topology


@dataclass(frozen=True, slots=True)
class MininetSrv6Config:
    rows: int = 4
    cols: int = 4
    root: int = 0
    duration: float = 20.0
    slot_seconds: float = 1.0
    locator_prefix: str = "fc00:0"
    decap_table: str = "254"
    dst_prefix_base: str = "2001:db8"
    default_delay_ms: float = 5.0
    failure_edge: Edge | None = None
    failure_start: float = 0.0
    failure_end: float = 0.0
    enable_cli: bool = True
    dry_run: bool = False
    verbose: bool = False
    tcpdump: bool = False


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
    path: list[int]
    segments: list[str]
    first_hop_dev: str
    command: list[str]


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

    def write(self, message: str) -> None:
        timestamp = datetime.now().isoformat(timespec="seconds")
        self.log_file.write(f"[{timestamp}] {message}\n")
        self.log_file.flush()


class DryRunPrinter:
    def __init__(self, stream: TextIO = sys.stdout) -> None:
        self.stream = stream

    def run(self, node_name: str, argv: list[str]) -> None:
        print(f"{node_name}$ {shlex.join(argv)}", file=self.stream)

    def note(self, message: str) -> None:
        print(f"# {message}", file=self.stream)


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a Mininet SRv6/tc emulation lab.")
    parser.add_argument("--config", type=Path, default=None)
    parser.add_argument("--rows", type=int, default=None)
    parser.add_argument("--cols", type=int, default=None)
    parser.add_argument("--root", type=int, default=None)
    parser.add_argument("--duration", type=float, default=None)
    parser.add_argument("--slot-seconds", type=float, default=None)
    parser.add_argument("--locator-prefix", type=str, default=None)
    parser.add_argument("--decap-table", type=str, default=None)
    parser.add_argument("--dst-prefix-base", type=str, default=None)
    parser.add_argument("--failure-edge", type=str, default=None, help='failure edge as "u,v"')
    parser.add_argument("--failure-start", type=float, default=None)
    parser.add_argument("--failure-end", type=float, default=None)
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
        config = replace(config, rows=int(grid["rows"]))
    if "cols" in grid:
        config = replace(config, cols=int(grid["cols"]))

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

    failure = _table(data, "failure")
    if "edge" in failure:
        config = replace(config, failure_edge=_parse_failure_edge(failure["edge"]))
    if "start" in failure:
        config = replace(config, failure_start=float(failure["start"]))
    if "end" in failure:
        config = replace(config, failure_end=float(failure["end"]))

    _validate_config(config)
    return config


def main(argv: list[str] | None = None) -> int:
    config = config_from_args(argv)
    if os.geteuid() != 0:
        print("Mininet/SRv6/tc emulation requires root. Run with sudo.", file=sys.stderr)
        return 1

    try:
        if config.dry_run:
            run_dry_run(config)
        else:
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

    for u, v in sorted(state.topology.edges):
        printer.run(f"r{u}", render_tc_netem(edge_iface_name(u, v), delay_ms=config.default_delay_ms))
        printer.run(f"r{v}", render_tc_netem(edge_iface_name(v, u), delay_ms=config.default_delay_ms))

    active = _failure_active(config, 0.0)
    effective_topology = _effective_topology(state.topology, config.failure_edge, active)
    policy = _render_policy(config, state, effective_topology)
    printer.run(f"r{config.root}", policy.command)
    _print_validation_commands(printer, config, state, policy)


def run_mininet_lab(config: MininetSrv6Config) -> None:
    from mininet.cli import CLI
    from mininet.link import TCLink
    from mininet.log import setLogLevel
    from mininet.net import Mininet

    setLogLevel("info" if config.verbose else "warning")
    state = _build_lab_state(config)
    log_dir = Path("logs")
    log_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = log_dir / f"mininet_srv6_{timestamp}.log"
    policy_path = log_dir / f"policy_updates_{timestamp}.jsonl"
    tc_path = log_dir / f"tc_updates_{timestamp}.jsonl"

    net: Any | None = None
    tcpdump_proc: subprocess.Popen[str] | None = None
    with (
        log_path.open("w", encoding="utf-8") as log_file,
        policy_path.open("w", encoding="utf-8") as policy_log,
        tc_path.open("w", encoding="utf-8") as tc_log,
    ):
        runner = NodeCommandRunner(log_file, verbose=config.verbose)
        runner.write(f"log file: {log_path}")
        runner.write(f"policy updates: {policy_path}")
        runner.write(f"tc updates: {tc_path}")

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

            _apply_underlay_routes(config, state, state.topology, hosts, runner)

            _apply_default_delay(config, state, hosts, runner, tc_log, now=0.0)
            failure_is_active = _failure_active(config, 0.0)
            if failure_is_active:
                _apply_failure_qdisc(
                    config,
                    hosts,
                    runner,
                    tc_log,
                    now=0.0,
                    active=True,
                    reason="failure_active",
                )

            effective_topology = _effective_topology(
                state.topology, config.failure_edge, failure_is_active
            )
            current_policy = _install_policy(
                config,
                state,
                effective_topology,
                hosts,
                runner,
                policy_log,
                now=0.0,
                reason="initial",
            )

            if config.tcpdump:
                tcpdump_proc = _start_tcpdump(hosts[config.root], current_policy.first_hop_dev, log_file)

            _run_validation(config, state, hosts, runner, current_policy)
            current_path = current_policy.path

            elapsed = 0.0
            while elapsed < config.duration:
                sleep_for = min(config.slot_seconds, config.duration - elapsed)
                if sleep_for > 0:
                    time.sleep(sleep_for)
                elapsed = round(elapsed + sleep_for, 6)

                active = _failure_active(config, elapsed)
                if active != failure_is_active:
                    _apply_failure_qdisc(
                        config,
                        hosts,
                        runner,
                        tc_log,
                        now=elapsed,
                        active=active,
                        reason="failure_active" if active else "failure_recovered",
                    )
                    failure_is_active = active
                    _apply_underlay_routes(
                        config,
                        state,
                        _effective_topology(state.topology, config.failure_edge, active),
                        hosts,
                        runner,
                    )

                effective_topology = _effective_topology(state.topology, config.failure_edge, active)
                candidate = _render_policy(config, state, effective_topology)
                if candidate.path != current_path:
                    reason = "failure_active" if active else "failure_recovered"
                    current_policy = _install_policy(
                        config,
                        state,
                        effective_topology,
                        hosts,
                        runner,
                        policy_log,
                        now=elapsed,
                        reason=reason,
                    )
                    _run_validation(config, state, hosts, runner, current_policy)
                    current_path = current_policy.path

            runner.write(f"finished timed run; logs: {log_path}, {policy_path}, {tc_path}")
            print(f"Mininet SRv6 log: {log_path}")
            print(f"Policy updates: {policy_path}")
            print(f"tc updates: {tc_path}")

            if config.enable_cli:
                runner.write("entering Mininet CLI")
                CLI(net)
        finally:
            if tcpdump_proc is not None:
                _stop_tcpdump(tcpdump_proc, runner)
            if net is not None:
                net.stop()


def _build_lab_state(config: MininetSrv6Config) -> LabState:
    topology = make_grid_topology(config.rows, config.cols)
    if config.failure_edge is not None and config.failure_edge not in topology.edges:
        raise ValueError(f"failure edge {config.failure_edge} is not in the grid topology")
    delay_table = DelayTable.from_constant_delay(topology, period_slots=1, delay=config.default_delay_ms)
    allocator = SRv6SidAllocator(config.locator_prefix)
    link_infos = {
        edge: LinkInfo(edge=edge, link_id=index)
        for index, edge in enumerate(sorted(topology.edges), start=1)
    }
    target = config.rows * config.cols - 1
    return LabState(
        topology=topology,
        delay_table=delay_table,
        allocator=allocator,
        link_infos=link_infos,
        target=target,
    )


def _render_node_setup_commands(
    config: MininetSrv6Config,
    state: LabState,
    node: int,
) -> list[list[str]]:
    ifaces = [edge_iface_name(node, neighbor) for neighbor in sorted(state.topology.neighbors(node))]
    commands = [["ip", "link", "set", "lo", "up"]]
    commands.extend(render_enable_srv6_sysctls(ifaces))
    commands.append(["ip", "-6", "route", "del", "default"])
    for neighbor in sorted(state.topology.neighbors(node)):
        iface = edge_iface_name(node, neighbor)
        commands.append(["ip", "link", "set", iface, "up"])
        commands.append(["ip", "-6", "addr", "add", _link_addr(config, state, node, neighbor), "dev", iface])
    commands.append(["ip", "-6", "addr", "add", _service_addr(config, node), "dev", "lo"])
    if node == state.target:
        commands.append(
            render_end_dt6_sid_route(
                state.allocator.node_sid(node),
                lookup_table=config.decap_table,
            )
        )
    else:
        commands.append(render_node_sid_route(state.allocator.node_sid(node)))
    return commands


def _render_underlay_route_commands(
    config: MininetSrv6Config,
    state: LabState,
    source: int,
    topology: Topology | None = None,
) -> list[list[str]]:
    route_topology = topology or state.topology
    commands: list[list[str]] = []
    for target in sorted(route_topology.nodes):
        if target == source:
            continue
        path, _ = shortest_path(route_topology, state.delay_table, slot=0, source=source, target=target)
        if path is None or len(path) < 2:
            raise RuntimeError(f"no underlay path from {source} to {target}")
        next_hop = path[1]
        dev = edge_iface_name(source, next_hop)
        via = _link_addr(config, state, next_hop, source, with_prefix=False)
        sid_prefix = f"{state.allocator.node_sid(target)}/128"
        commands.append(render_plain_ipv6_route(sid_prefix, via=via, dev=dev))
        commands.append(render_plain_ipv6_route(_service_addr(config, target), via=via, dev=dev))
    return commands


def _apply_underlay_routes(
    config: MininetSrv6Config,
    state: LabState,
    topology: Topology,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
) -> None:
    for node in sorted(topology.nodes):
        for command in _render_underlay_route_commands(config, state, node, topology=topology):
            runner.run(hosts[node], command)


def _render_policy(
    config: MininetSrv6Config,
    state: LabState,
    topology: Topology,
) -> PolicyRender:
    path, cost = shortest_path(
        topology,
        state.delay_table,
        slot=0,
        source=config.root,
        target=state.target,
    )
    if path is None:
        raise RuntimeError(f"no SRv6 policy path from {config.root} to {state.target}")
    if len(path) < 2:
        raise RuntimeError("SRv6 policy path must contain at least one hop")
    policy = encode_node_sid_policy(path, state.allocator, path_cost=cost)
    first_hop_dev = edge_iface_name(path[0], path[1])
    command = render_srv6_encap_route(
        dst_prefix=_service_addr(config, state.target),
        segments=policy.segments,
        dev=first_hop_dev,
    )
    return PolicyRender(
        path=path,
        segments=policy.segments,
        first_hop_dev=first_hop_dev,
        command=command,
    )


def _install_policy(
    config: MininetSrv6Config,
    state: LabState,
    topology: Topology,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    policy_log: TextIO,
    now: float,
    reason: str,
) -> PolicyRender:
    rendered = _render_policy(config, state, topology)
    runner.run(hosts[config.root], rendered.command)
    _write_jsonl(
        policy_log,
        {
            "time": now,
            "source": config.root,
            "target": state.target,
            "path": rendered.path,
            "segments": rendered.segments,
            "first_hop_dev": rendered.first_hop_dev,
            "reason": reason,
        },
    )
    return rendered


def _apply_default_delay(
    config: MininetSrv6Config,
    state: LabState,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    tc_log: TextIO,
    now: float,
) -> None:
    for u, v in sorted(state.topology.edges):
        _apply_edge_delay(config, hosts, runner, tc_log, now, u, v, "initial_delay")


def _apply_failure_qdisc(
    config: MininetSrv6Config,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    tc_log: TextIO,
    now: float,
    active: bool,
    reason: str,
) -> None:
    if config.failure_edge is None:
        return
    u, v = config.failure_edge
    if active:
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
        _apply_edge_delay(config, hosts, runner, tc_log, now, u, v, reason)


def _apply_edge_delay(
    config: MininetSrv6Config,
    hosts: dict[int, Any],
    runner: NodeCommandRunner,
    tc_log: TextIO,
    now: float,
    u: int,
    v: int,
    reason: str,
) -> None:
    interfaces = [edge_iface_name(u, v), edge_iface_name(v, u)]
    runner.run(hosts[u], render_tc_netem(interfaces[0], delay_ms=config.default_delay_ms))
    runner.run(hosts[v], render_tc_netem(interfaces[1], delay_ms=config.default_delay_ms))
    _write_jsonl(
        tc_log,
        {
            "time": now,
            "edge": [u, v],
            "interfaces": interfaces,
            "action": "delay",
            "delay_ms": config.default_delay_ms,
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
    target_addr = _service_addr(config, state.target, with_prefix=False)
    source_addr = _service_addr(config, config.root, with_prefix=False)
    runner.run(hosts[config.root], ["ping", "-6", "-I", source_addr, "-c", "2", target_addr])
    runner.run(hosts[config.root], ["ip", "-6", "route", "get", target_addr, "from", source_addr])
    runner.run(hosts[config.root], ["ip", "-6", "route", "show"])
    runner.run(hosts[state.target], ["ip", "-6", "addr", "show", "lo"])
    runner.run(hosts[config.root], render_tc_show(policy.first_hop_dev))


def _print_validation_commands(
    printer: DryRunPrinter,
    config: MininetSrv6Config,
    state: LabState,
    policy: PolicyRender,
) -> None:
    target_addr = _service_addr(config, state.target, with_prefix=False)
    source_addr = _service_addr(config, config.root, with_prefix=False)
    printer.run(f"r{config.root}", ["ping", "-6", "-I", source_addr, "-c", "2", target_addr])
    printer.run(f"r{config.root}", ["ip", "-6", "route", "get", target_addr, "from", source_addr])
    printer.run(f"r{config.root}", ["ip", "-6", "route", "show"])
    printer.run(f"r{state.target}", ["ip", "-6", "addr", "show", "lo"])
    printer.run(f"r{config.root}", render_tc_show(policy.first_hop_dev))


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


def _effective_topology(
    topology: Topology,
    failure_edge: Edge | None,
    failure_active: bool,
) -> Topology:
    if failure_edge is None or not failure_active:
        return topology
    return topology.without_edges({failure_edge})


def _failure_active(config: MininetSrv6Config, now: float) -> bool:
    return (
        config.failure_edge is not None
        and config.failure_start <= now < config.failure_end
        and config.failure_end > config.failure_start
    )


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


def _merge_cli_args(
    config: MininetSrv6Config,
    args: argparse.Namespace,
) -> MininetSrv6Config:
    updates: dict[str, Any] = {}
    for attr in [
        "rows",
        "cols",
        "root",
        "duration",
        "slot_seconds",
        "locator_prefix",
        "decap_table",
        "dst_prefix_base",
        "failure_start",
        "failure_end",
    ]:
        value = getattr(args, attr)
        if value is not None:
            updates[attr] = value

    if args.failure_edge is not None:
        updates["failure_edge"] = _parse_failure_edge(args.failure_edge)
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


def _table(data: dict[str, Any], name: str) -> dict[str, Any]:
    value = data.get(name, {})
    if not isinstance(value, dict):
        raise ValueError(f"[{name}] must be a TOML table")
    return value


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
