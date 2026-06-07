from pathlib import Path

from emulation.mininet_srv6_lab import (
    MininetSrv6Config,
    _build_lab_state,
    _render_node_setup_commands,
    _render_underlay_route_commands,
    config_from_args,
    load_config,
)


def test_mininet_srv6_config_file_loads() -> None:
    config = load_config(Path("config/mininet_srv6.toml"))

    assert config.rows == 4
    assert config.cols == 4
    assert config.duration == 20
    assert config.slot_seconds == 1
    assert config.default_delay_ms == 5
    assert config.decap_table == "254"
    assert config.failure_edge == (0, 1)
    assert config.failure_start == 5
    assert config.failure_end == 15
    assert config.enable_cli is True


def test_cli_args_override_toml() -> None:
    config = config_from_args(
        [
            "--config",
            "config/mininet_srv6.toml",
            "--rows",
            "2",
            "--cols",
            "3",
            "--duration",
            "7",
            "--failure-edge",
            "1,2",
            "--decap-table",
            "100",
            "--no-cli",
        ]
    )

    assert config.rows == 2
    assert config.cols == 3
    assert config.duration == 7
    assert config.failure_edge == (1, 2)
    assert config.decap_table == "100"
    assert config.enable_cli is False


def test_target_node_sid_uses_end_dt6() -> None:
    config = MininetSrv6Config(rows=2, cols=2)
    state = _build_lab_state(config)

    target_commands = _render_node_setup_commands(config, state, state.target)
    transit_commands = _render_node_setup_commands(config, state, 1)

    assert [
        "ip",
        "-6",
        "route",
        "replace",
        "fc00:0:3::1/128",
        "encap",
        "seg6local",
        "action",
        "End.DT6",
        "table",
        "254",
        "dev",
        "lo",
    ] in target_commands
    assert [
        "ip",
        "-6",
        "route",
        "replace",
        "fc00:0:1::1/128",
        "encap",
        "seg6local",
        "action",
        "End",
        "dev",
        "lo",
    ] in transit_commands


def test_underlay_includes_service_return_routes() -> None:
    config = MininetSrv6Config(rows=2, cols=2)
    state = _build_lab_state(config)

    commands = _render_underlay_route_commands(config, state, source=1)

    assert [
        "ip",
        "-6",
        "route",
        "replace",
        "2001:db8:100:0::1/128",
        "via",
        "2001:db8:e:1::1",
        "dev",
        "r1-r0",
    ] in commands


def test_underlay_service_routes_follow_effective_topology() -> None:
    config = MininetSrv6Config(rows=2, cols=2)
    state = _build_lab_state(config)
    effective_topology = state.topology.without_edges({(0, 1)})

    commands = _render_underlay_route_commands(
        config,
        state,
        source=1,
        topology=effective_topology,
    )

    assert [
        "ip",
        "-6",
        "route",
        "replace",
        "2001:db8:100:0::1/128",
        "via",
        "2001:db8:e:3::4",
        "dev",
        "r1-r3",
    ] in commands
