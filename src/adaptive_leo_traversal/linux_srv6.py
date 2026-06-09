"""Linux SRv6 iproute2 command rendering helpers."""

from __future__ import annotations

import shlex
import subprocess


def render_enable_srv6_sysctls(ifaces: list[str] | None = None) -> list[list[str]]:
    """Return sysctl commands that enable IPv6 forwarding and SRv6 processing."""

    commands = [
        ["sysctl", "-w", "net.ipv6.conf.all.forwarding=1"],
        ["sysctl", "-w", "net.ipv6.conf.default.forwarding=1"],
        ["sysctl", "-w", "net.ipv6.conf.all.seg6_enabled=1"],
        ["sysctl", "-w", "net.ipv6.conf.default.seg6_enabled=1"],
        ["sysctl", "-w", "net.ipv6.conf.lo.seg6_enabled=1"],
        ["sysctl", "-w", "net.ipv6.conf.all.seg6_require_hmac=0"],
        ["sysctl", "-w", "net.ipv6.conf.default.seg6_require_hmac=0"],
        ["sysctl", "-w", "net.ipv6.conf.lo.seg6_require_hmac=0"],
    ]
    for iface in ifaces or []:
        iface = _require_non_empty(iface, "iface")
        commands.append(["sysctl", "-w", f"net.ipv6.conf.{iface}.forwarding=1"])
        commands.append(["sysctl", "-w", f"net.ipv6.conf.{iface}.seg6_enabled=1"])
        commands.append(["sysctl", "-w", f"net.ipv6.conf.{iface}.seg6_require_hmac=0"])
    return commands


def render_node_sid_route(
    sid: str,
    dev: str = "lo",
    table: str | None = None,
) -> list[str]:
    """Render an iproute2 seg6local End route for a Node SID."""

    sid_prefix = _with_default_prefixlen(_require_non_empty(sid, "sid"), 128)
    dev = _require_non_empty(dev, "dev")
    command = [
        "ip",
        "-6",
        "route",
        "replace",
        sid_prefix,
        "encap",
        "seg6local",
        "action",
        "End",
        "dev",
        dev,
    ]
    if table is not None:
        command.extend(["table", _require_non_empty(table, "table")])
    return command


def render_end_dt6_sid_route(
    sid: str,
    lookup_table: str = "254",
    dev: str = "lo",
) -> list[str]:
    """Render a seg6local End.DT6 route that decapsulates into an IPv6 table."""

    sid_prefix = _with_default_prefixlen(_require_non_empty(sid, "sid"), 128)
    lookup_table = _require_non_empty(lookup_table, "lookup_table")
    dev = _require_non_empty(dev, "dev")
    return [
        "ip",
        "-6",
        "route",
        "replace",
        sid_prefix,
        "encap",
        "seg6local",
        "action",
        "End.DT6",
        "table",
        lookup_table,
        "dev",
        dev,
    ]


def render_srv6_encap_route(
    dst_prefix: str,
    segments: list[str],
    dev: str,
    via: str | None = None,
    mode: str = "encap",
    table: str | None = None,
) -> list[str]:
    """Render an iproute2 SRv6 encapsulation route."""

    dst_prefix = _require_non_empty(dst_prefix, "dst_prefix")
    dev = _require_non_empty(dev, "dev")
    if mode not in {"encap", "inline"}:
        raise ValueError("mode must be 'encap' or 'inline'")
    if not segments:
        raise ValueError("segments must be non-empty")

    cleaned_segments = [_require_non_empty(segment, "segment") for segment in segments]
    command = [
        "ip",
        "-6",
        "route",
        "replace",
        dst_prefix,
        "encap",
        "seg6",
        "mode",
        mode,
        "segs",
        ",".join(cleaned_segments),
    ]
    if via is not None:
        command.extend(["via", _require_non_empty(via, "via")])
    command.extend(["dev", dev])
    if table is not None:
        command.extend(["table", _require_non_empty(table, "table")])
    return command


def render_plain_ipv6_route(dst_prefix: str, via: str | None, dev: str) -> list[str]:
    """Render a plain IPv6 route, optionally using a next hop."""

    dst_prefix = _require_non_empty(dst_prefix, "dst_prefix")
    dev = _require_non_empty(dev, "dev")
    command = ["ip", "-6", "route", "replace", dst_prefix]
    if via is not None:
        command.extend(["via", _require_non_empty(via, "via")])
    command.extend(["dev", dev])
    return command


def run_cmd(argv: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a command safely and capture stdout/stderr."""

    if not argv:
        raise ValueError("argv must be non-empty")

    result = subprocess.run(argv, check=False, capture_output=True, text=True)
    if check and result.returncode != 0:
        command = shlex.join(argv)
        raise RuntimeError(
            "command failed\n"
            f"command: {command}\n"
            f"returncode: {result.returncode}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )
    return result


def _with_default_prefixlen(value: str, prefixlen: int) -> str:
    if "/" in value:
        return value
    return f"{value}/{prefixlen}"


def _require_non_empty(value: str, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be non-empty")
    return value.strip()
