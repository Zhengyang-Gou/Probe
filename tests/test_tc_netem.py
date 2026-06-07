import pytest

from adaptive_leo_traversal.tc_netem import (
    edge_iface_name,
    render_tc_delete,
    render_tc_loss100,
    render_tc_netem,
    render_tc_show,
)


def test_render_tc_netem_delay_and_loss() -> None:
    assert render_tc_netem("r0-r1", delay_ms=5, loss_percent=1) == [
        "tc",
        "qdisc",
        "replace",
        "dev",
        "r0-r1",
        "root",
        "netem",
        "delay",
        "5.000ms",
        "loss",
        "1.000%",
    ]


def test_render_tc_loss100() -> None:
    assert render_tc_loss100("r0-r1") == [
        "tc",
        "qdisc",
        "replace",
        "dev",
        "r0-r1",
        "root",
        "netem",
        "loss",
        "100.000%",
    ]


def test_edge_iface_name() -> None:
    assert edge_iface_name(0, 4) == "r0-r4"


def test_render_tc_delete_and_show() -> None:
    assert render_tc_delete("r0-r1") == ["tc", "qdisc", "del", "dev", "r0-r1", "root"]
    assert render_tc_show("r0-r1") == ["tc", "qdisc", "show", "dev", "r0-r1"]


def test_render_tc_netem_rejects_empty_impairment() -> None:
    with pytest.raises(ValueError):
        render_tc_netem("r0-r1")


def test_render_tc_netem_validates_ranges() -> None:
    with pytest.raises(ValueError):
        render_tc_netem("r0-r1", delay_ms=-1)
    with pytest.raises(ValueError):
        render_tc_netem("r0-r1", loss_percent=100.1)
    with pytest.raises(ValueError):
        render_tc_netem("r0-r1", rate_mbit=0)
