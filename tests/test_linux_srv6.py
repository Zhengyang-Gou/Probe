import pytest

from adaptive_leo_traversal.linux_srv6 import (
    render_enable_srv6_sysctls,
    render_end_dt6_sid_route,
    render_node_sid_route,
    render_plain_ipv6_route,
    render_srv6_encap_route,
)


def test_render_node_sid_route_adds_128_prefix() -> None:
    assert render_node_sid_route("fc00:0:4::1") == [
        "ip",
        "-6",
        "route",
        "replace",
        "fc00:0:4::1/128",
        "encap",
        "seg6local",
        "action",
        "End",
        "dev",
        "lo",
    ]


def test_render_end_dt6_sid_route_decaps_to_lookup_table() -> None:
    assert render_end_dt6_sid_route("fc00:0:f::1") == [
        "ip",
        "-6",
        "route",
        "replace",
        "fc00:0:f::1/128",
        "encap",
        "seg6local",
        "action",
        "End.DT6",
        "table",
        "254",
        "dev",
        "lo",
    ]


def test_render_srv6_encap_route_renders_segments_csv() -> None:
    command = render_srv6_encap_route(
        dst_prefix="2001:db8:100:f::1/128",
        segments=["fc00:0:4::1", "fc00:0:8::1"],
        dev="r0-r4",
    )

    assert command == [
        "ip",
        "-6",
        "route",
        "replace",
        "2001:db8:100:f::1/128",
        "encap",
        "seg6",
        "mode",
        "encap",
        "segs",
        "fc00:0:4::1,fc00:0:8::1",
        "dev",
        "r0-r4",
    ]


def test_render_srv6_encap_route_rejects_empty_segments() -> None:
    with pytest.raises(ValueError):
        render_srv6_encap_route(
            dst_prefix="2001:db8:100:f::1/128",
            segments=[],
            dev="r0-r4",
        )


def test_render_enable_srv6_sysctls_includes_interfaces() -> None:
    assert render_enable_srv6_sysctls(["r0-r1"]) == [
        ["sysctl", "-w", "net.ipv6.conf.all.forwarding=1"],
        ["sysctl", "-w", "net.ipv6.conf.all.seg6_enabled=1"],
        ["sysctl", "-w", "net.ipv6.conf.default.seg6_enabled=1"],
        ["sysctl", "-w", "net.ipv6.conf.r0-r1.seg6_enabled=1"],
    ]


def test_render_plain_ipv6_route_supports_via_and_direct() -> None:
    assert render_plain_ipv6_route("fc00:0:4::1/128", "2001:db8:e:1::5", "r0-r4") == [
        "ip",
        "-6",
        "route",
        "replace",
        "fc00:0:4::1/128",
        "via",
        "2001:db8:e:1::5",
        "dev",
        "r0-r4",
    ]
    assert render_plain_ipv6_route("fc00:0:4::1/128", None, "r0-r4") == [
        "ip",
        "-6",
        "route",
        "replace",
        "fc00:0:4::1/128",
        "dev",
        "r0-r4",
    ]
