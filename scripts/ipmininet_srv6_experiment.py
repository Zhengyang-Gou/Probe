#!/usr/bin/env python3
"""Run an IPMininet + Linux SRv6 + tc experiment topology.

Run this script on a Linux host with IPMininet, Mininet, iproute2, and an SRv6-capable
kernel. It is intentionally kept outside the package because IPMininet is a system-level
optional dependency.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = PROJECT_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from adaptive_leo_traversal import (  # noqa: E402
    DelayTable,
    LinkState,
    make_grid_topology,
)
from adaptive_leo_traversal.linux_srv6 import (  # noqa: E402
    NetemConfig,
    build_seg6_policy_cmd,
    build_seg6local_end_cmd,
    build_tc_netem_cmd,
    host_addr_for_node,
    host_prefix_for_node,
    path_to_sid_list,
    router_name,
    sid_for_node,
)
from adaptive_leo_traversal.models import normalize_edge  # noqa: E402
from adaptive_leo_traversal.planner import shortest_path  # noqa: E402

try:
    from ipmininet.cli import IPCLI
    from ipmininet.ipnet import IPNet
    from ipmininet.iptopo import IPTopo
    from ipmininet.router.config import RouterConfig
except ImportError as exc:  # pragma: no cover - exercised on the target Linux host.
    raise SystemExit(
        "IPMininet is not installed. Install it on the remote Linux server first.\n"
        "Example: sudo pip3 install git+https://github.com/cnp3/ipmininet.git@v1.1"
    ) from exc


class GridSRv6Topo(IPTopo):
    """Grid of Linux routers with one source host and one destination host."""

    def build(self, rows: int = 3, cols: int = 3, **kwargs) -> None:
        topology = make_grid_topology(rows, cols)
        routers = {
            node: self.addRouter(router_name(node), config=RouterConfig)
            for node in sorted(topology.nodes)
        }

        source = self.addHost("hsrc")
        destination = self.addHost("hdst")
        self.addLink(source, routers[0])
        self.addLink(destination, routers[rows * cols - 1])

        for u, v in sorted(topology.edges):
            self.addLink(routers[u], routers[v])

        super().build(**kwargs)


@dataclass(frozen=True, slots=True)
class LinkEvent:
    """Scheduled physical-link event for both tc and the algorithm provider."""

    at: float
    u: int
    v: int
    state: LinkState


def node_cmd(net: IPNet, node_name: str, command: str) -> str:
    """Run one shell command inside an IPMininet node and print failures."""

    output = net[node_name].cmd(command)
    if output.strip():
        print(f"[{node_name}] {command}\n{output.strip()}")
    return output


def interface_between(net: IPNet, left: str, right: str) -> str:
    """Return the interface name on ``left`` connected to ``right``."""

    links = net[left].connectionsTo(net[right])
    if not links:
        raise RuntimeError(f"no link between {left} and {right}")
    left_intf, _right_intf = links[0]
    return str(left_intf)


def configure_host_addresses(net: IPNet, dst_node: int) -> None:
    """Configure stable host addresses and default routes."""

    hsrc_dev = interface_between(net, "hsrc", router_name(0))
    r0_host_dev = interface_between(net, router_name(0), "hsrc")
    hdst_dev = interface_between(net, "hdst", router_name(dst_node))
    rdst_host_dev = interface_between(net, router_name(dst_node), "hdst")

    node_cmd(net, "hsrc", f"ip -6 addr flush dev {hsrc_dev}")
    node_cmd(net, "hsrc", f"ip -6 addr add {host_addr_for_node(0)}/64 dev {hsrc_dev}")
    node_cmd(net, "hsrc", f"ip -6 route replace default via 2001:db8:1::1 dev {hsrc_dev}")

    node_cmd(net, router_name(0), f"ip -6 addr add 2001:db8:1::1/64 dev {r0_host_dev}")

    node_cmd(net, "hdst", f"ip -6 addr flush dev {hdst_dev}")
    node_cmd(net, "hdst", f"ip -6 addr add {host_addr_for_node(dst_node)}/64 dev {hdst_dev}")
    node_cmd(
        net,
        "hdst",
        f"ip -6 route replace default via 2001:db8:{dst_node + 1:x}::1 dev {hdst_dev}",
    )
    node_cmd(
        net,
        router_name(dst_node),
        f"ip -6 addr add 2001:db8:{dst_node + 1:x}::1/64 dev {rdst_host_dev}",
    )


def configure_router_srv6(net: IPNet, rows: int, cols: int) -> None:
    """Enable forwarding and install one End SID per router."""

    for node in range(rows * cols):
        name = router_name(node)
        sid = sid_for_node(node)
        node_cmd(net, name, "sysctl -w net.ipv6.conf.all.forwarding=1")
        node_cmd(net, name, "sysctl -w net.ipv6.conf.all.seg6_enabled=1")
        node_cmd(net, name, "sysctl -w net.ipv6.conf.default.seg6_enabled=1")
        node_cmd(net, name, f"ip -6 addr replace {sid}/128 dev lo")
        node_cmd(net, name, build_seg6local_end_cmd(sid))


def apply_link_tc(
    net: IPNet,
    u: int,
    v: int,
    config: NetemConfig,
    bidirectional: bool = True,
) -> None:
    """Apply tc/netem to one IPMininet router-router link."""

    left = router_name(u)
    right = router_name(v)
    left_dev = interface_between(net, left, right)
    node_cmd(net, left, build_tc_netem_cmd(left_dev, config))
    if bidirectional:
        right_dev = interface_between(net, right, left)
        node_cmd(net, right, build_tc_netem_cmd(right_dev, config))


def install_srv6_policy(net: IPNet, path: list[int], dst_node: int) -> None:
    """Install the ingress SRv6 policy on the first router in ``path``."""

    if len(path) < 2:
        raise ValueError("SRv6 policy path must contain at least two nodes")
    ingress = router_name(path[0])
    next_router = router_name(path[1])
    out_dev = interface_between(net, ingress, next_router)
    sid_list = path_to_sid_list(path)
    dst_prefix = host_prefix_for_node(dst_node)
    node_cmd(net, ingress, build_seg6_policy_cmd(dst_prefix, sid_list, out_dev))


def active_down_edges(events: list[LinkEvent], now: float) -> set[tuple[int, int]]:
    """Return the links whose latest event at ``now`` says they are down."""

    latest: dict[tuple[int, int], LinkState] = {}
    for event in sorted(events, key=lambda item: item.at):
        if event.at <= now:
            latest[normalize_edge(event.u, event.v)] = event.state
    return {edge for edge, state in latest.items() if state is LinkState.DOWN}


def install_shortest_srv6_policy(
    net: IPNet,
    rows: int,
    cols: int,
    delay_ms: float,
    down_edges: set[tuple[int, int]] | None = None,
) -> list[int]:
    """Install an SRv6 policy from r0 to the destination host prefix."""

    topology = make_grid_topology(rows, cols)
    if down_edges:
        topology = topology.without_edges(down_edges)
    delay_table = DelayTable.from_constant_delay(topology, period_slots=1, delay=delay_ms)
    dst_node = rows * cols - 1
    path, cost = shortest_path(topology, delay_table, slot=0, source=0, target=dst_node)
    if path is None:
        raise RuntimeError(f"destination node {dst_node} is unreachable")
    install_srv6_policy(net, path, dst_node)
    print(f"installed SRv6 policy path={path}, cost={cost:g}ms")
    return path


def run_policy_demo(
    net: IPNet,
    rows: int,
    cols: int,
    delay_ms: float,
    events: list[LinkEvent],
) -> None:
    """Apply scheduled link events and reinstall the shortest SRv6 policy."""

    now = 0.0
    install_shortest_srv6_policy(net, rows, cols, delay_ms, active_down_edges(events, now))

    for event in sorted(events, key=lambda item: item.at):
        time.sleep(max(0.0, event.at - now))
        now = event.at
        loss = 100.0 if event.state is LinkState.DOWN else 0.0
        apply_link_tc(net, event.u, event.v, NetemConfig(delay_ms=delay_ms, loss_percent=loss))
        install_shortest_srv6_policy(net, rows, cols, delay_ms, active_down_edges(events, now))
        print(f"event at {now:g}s: {event.u}-{event.v} {event.state.value}")


def parse_link_event(raw: str) -> LinkEvent:
    """Parse an event in the form ``time:u:v:down|up``."""

    at_text, u_text, v_text, state_text = raw.split(":", maxsplit=3)
    return LinkEvent(
        at=float(at_text),
        u=int(u_text),
        v=int(v_text),
        state=LinkState(state_text),
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--rows", type=int, default=3)
    parser.add_argument("--cols", type=int, default=3)
    parser.add_argument("--delay-ms", type=float, default=20.0)
    parser.add_argument("--loss-percent", type=float, default=0.0)
    parser.add_argument(
        "--event",
        action="append",
        default=[],
        help="scheduled link event: time:u:v:down|up, for example 10:1:2:down",
    )
    parser.add_argument("--no-cli", action="store_true", help="exit after setup instead of opening IPCLI")
    parser.add_argument(
        "--algorithm-demo",
        action="store_true",
        help="apply scheduled events and reinstall SRv6 policies from current shortest paths",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    if args.rows <= 0 or args.cols <= 0:
        raise SystemExit("--rows and --cols must be positive")

    topology = make_grid_topology(args.rows, args.cols)
    events = [parse_link_event(raw) for raw in args.event]
    net = IPNet(topo=GridSRv6Topo(rows=args.rows, cols=args.cols), allocate_IPs=True)

    try:
        net.start()
        dst_node = args.rows * args.cols - 1
        configure_router_srv6(net, args.rows, args.cols)
        configure_host_addresses(net, dst_node)

        for u, v in sorted(topology.edges):
            apply_link_tc(
                net,
                u,
                v,
                NetemConfig(delay_ms=args.delay_ms, loss_percent=args.loss_percent),
            )

        install_shortest_srv6_policy(net, args.rows, args.cols, args.delay_ms)

        print("Try in IPCLI: hsrc ping6 -c 3 " + host_addr_for_node(dst_node))
        print("Try in IPCLI: r0 ip -6 route")

        if args.algorithm_demo:
            run_policy_demo(net, args.rows, args.cols, args.delay_ms, events)

        if not args.no_cli:
            IPCLI(net)
    finally:
        net.stop()


if __name__ == "__main__":
    main()
