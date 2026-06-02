"""Linux SRv6 and tc command helpers for real-network experiments."""

from __future__ import annotations

from dataclasses import dataclass


def router_name(node: int) -> str:
    """Return the IPMininet router name for an algorithm node ID."""

    if node < 0:
        raise ValueError("node must be non-negative")
    return f"r{node}"


def sid_for_node(node: int, locator_prefix: str = "fc00") -> str:
    """Return a deterministic SRv6 SID for an algorithm node ID."""

    if node < 0:
        raise ValueError("node must be non-negative")
    return f"{locator_prefix}:{node + 1:x}::1"


def host_prefix_for_node(node: int, prefix: str = "2001:db8") -> str:
    """Return a deterministic /64 host prefix attached to a router node."""

    if node < 0:
        raise ValueError("node must be non-negative")
    return f"{prefix}:{node + 1:x}::/64"


def host_addr_for_node(node: int, prefix: str = "2001:db8") -> str:
    """Return a deterministic host address attached to a router node."""

    if node < 0:
        raise ValueError("node must be non-negative")
    return f"{prefix}:{node + 1:x}::2"


def path_to_sid_list(path: list[int] | tuple[int, ...], locator_prefix: str = "fc00") -> list[str]:
    """Convert an explicit node path into an SRv6 segment list.

    The ingress node is excluded because it installs the policy. The last node is kept so
    it can execute the final endpoint behavior.
    """

    if len(path) < 2:
        return []
    return [sid_for_node(node, locator_prefix) for node in path[1:]]


def build_seg6local_end_cmd(sid: str, dev: str = "lo") -> str:
    """Build an endpoint route that consumes one SRv6 SID."""

    return f"ip -6 route replace {sid}/128 encap seg6local action End dev {dev}"


def build_seg6local_dx6_cmd(sid: str, nh6: str, dev: str) -> str:
    """Build a final endpoint route that decapsulates and forwards to an IPv6 next hop."""

    return f"ip -6 route replace {sid}/128 encap seg6local action End.DX6 nh6 {nh6} dev {dev}"


def build_seg6_policy_cmd(
    dst_prefix: str,
    sid_list: list[str] | tuple[str, ...],
    dev: str,
    mode: str = "encap",
) -> str:
    """Build an ingress SRv6 policy route for one destination prefix."""

    if not sid_list:
        raise ValueError("sid_list must contain at least one SID")
    segments = ",".join(sid_list)
    return f"ip -6 route replace {dst_prefix} encap seg6 mode {mode} segs {segments} dev {dev}"


def build_plain_ipv6_route_cmd(dst_prefix: str, nh6: str, dev: str) -> str:
    """Build a normal IPv6 route used by hosts or non-SRv6 fallbacks."""

    return f"ip -6 route replace {dst_prefix} via {nh6} dev {dev}"


@dataclass(frozen=True, slots=True)
class NetemConfig:
    """tc/netem parameters for one interface."""

    delay_ms: float | None = None
    jitter_ms: float | None = None
    loss_percent: float | None = None


def build_tc_netem_cmd(dev: str, config: NetemConfig) -> str:
    """Build a tc/netem replacement command."""

    parts = [f"tc qdisc replace dev {dev} root netem"]
    if config.delay_ms is not None:
        if config.delay_ms < 0:
            raise ValueError("delay_ms must be non-negative")
        delay = f"delay {config.delay_ms:g}ms"
        if config.jitter_ms is not None:
            if config.jitter_ms < 0:
                raise ValueError("jitter_ms must be non-negative")
            delay = f"{delay} {config.jitter_ms:g}ms"
        parts.append(delay)
    elif config.jitter_ms is not None:
        raise ValueError("jitter_ms requires delay_ms")

    if config.loss_percent is not None:
        if not 0 <= config.loss_percent <= 100:
            raise ValueError("loss_percent must be in [0, 100]")
        parts.append(f"loss {config.loss_percent:g}%")

    if len(parts) == 1:
        parts.append("delay 0ms")
    return " ".join(parts)


def build_tc_delete_cmd(dev: str) -> str:
    """Build a command that removes the root qdisc from an interface."""

    return f"tc qdisc del dev {dev} root"


def build_tc_tbf_cmd(dev: str, rate: str, burst: str = "32kbit", latency: str = "400ms") -> str:
    """Build a tc/tbf bandwidth limiter command."""

    return f"tc qdisc replace dev {dev} root tbf rate {rate} burst {burst} latency {latency}"
