import pytest

from adaptive_leo_traversal.linux_srv6 import (
    NetemConfig,
    build_seg6_policy_cmd,
    build_tc_netem_cmd,
    host_addr_for_node,
    host_prefix_for_node,
    path_to_sid_list,
    router_name,
    sid_for_node,
)


def test_deterministic_names_and_addresses() -> None:
    assert router_name(0) == "r0"
    assert sid_for_node(0) == "fc00:1::1"
    assert sid_for_node(15) == "fc00:10::1"
    assert host_prefix_for_node(0) == "2001:db8:1::/64"
    assert host_addr_for_node(0) == "2001:db8:1::2"


def test_path_to_sid_list_excludes_ingress() -> None:
    assert path_to_sid_list([0, 2, 5]) == ["fc00:3::1", "fc00:6::1"]
    assert path_to_sid_list([0]) == []


def test_build_seg6_policy_cmd() -> None:
    command = build_seg6_policy_cmd(
        "2001:db8:6::/64",
        ["fc00:3::1", "fc00:6::1"],
        "r0-eth1",
    )

    assert command == (
        "ip -6 route replace 2001:db8:6::/64 encap seg6 mode encap "
        "segs fc00:3::1,fc00:6::1 dev r0-eth1"
    )


def test_build_tc_netem_cmd() -> None:
    command = build_tc_netem_cmd("r1-eth0", NetemConfig(delay_ms=20, jitter_ms=5, loss_percent=1))

    assert command == "tc qdisc replace dev r1-eth0 root netem delay 20ms 5ms loss 1%"


def test_build_tc_netem_rejects_invalid_jitter() -> None:
    with pytest.raises(ValueError, match="jitter_ms requires delay_ms"):
        build_tc_netem_cmd("r1-eth0", NetemConfig(jitter_ms=5))
