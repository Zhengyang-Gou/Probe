from pathlib import Path

from emulation.mininet_srv6_lab import (
    MininetSrv6Config,
    _build_lab_state,
    _make_run_dir,
    _remaining_policy_path,
    _render_node_setup_commands,
    _render_policy_for_path,
    _render_underlay_route_commands,
    config_from_args,
    load_config,
    main,
    run_dry_run,
)
from adaptive_leo_traversal.models import ProbeState, TraversalResult, TraversalStatus


def test_mininet_srv6_config_file_loads() -> None:
    config = load_config(Path("config/mininet_srv6.toml"))

    assert config.rows == 4
    assert config.cols == 4
    assert config.planes == 4
    assert config.satellites_per_plane == 4
    assert config.duration == 40
    assert config.slot_seconds == 1
    assert config.default_delay_ms == 5
    assert config.delay_model == "propagation"
    assert config.delay_period_slots == 8
    assert config.dynamic_topology_enabled is True
    assert config.dynamic_topology_model == "rotating_seam"
    assert config.observation_mode == "ping"
    assert config.observation_ttl_seconds == 2
    assert config.agent_enabled is True
    assert config.probe_packet_validation is True
    assert config.output_dir == "logs/mininet"
    assert config.run_name is None
    assert config.decap_table == "254"
    assert config.algorithm_mode == "adaptive_traversal"
    assert config.max_hop == 500
    assert config.alpha == 0.85
    assert config.validate_each_policy is True
    assert config.failure_edge == (5, 9)
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
            "--planes",
            "2",
            "--satellites-per-plane",
            "3",
            "--duration",
            "7",
            "--failure-edge",
            "1,2",
            "--decap-table",
            "100",
            "--algorithm-mode",
            "root_target",
            "--max-hop",
            "50",
            "--alpha",
            "0.9",
            "--delay-model",
            "constant",
            "--observation-mode",
            "configured",
            "--no-dynamic-topology",
            "--disable-agents",
            "--no-probe-packet-validation",
            "--output-dir",
            "logs/test-mininet",
            "--run-name",
            "demo",
            "--no-validate-each-policy",
            "--no-cli",
        ]
    )

    assert config.rows == 2
    assert config.cols == 3
    assert config.planes == 2
    assert config.satellites_per_plane == 3
    assert config.duration == 7
    assert config.failure_edge == (1, 2)
    assert config.decap_table == "100"
    assert config.algorithm_mode == "root_target"
    assert config.max_hop == 50
    assert config.alpha == 0.9
    assert config.delay_model == "constant"
    assert config.observation_mode == "configured"
    assert config.dynamic_topology_enabled is False
    assert config.agent_enabled is False
    assert config.probe_packet_validation is False
    assert config.output_dir == "logs/test-mininet"
    assert config.run_name == "demo"
    assert config.validate_each_policy is False
    assert config.enable_cli is False


def test_make_run_dir_allocates_unique_folder(tmp_path) -> None:
    first = _make_run_dir(tmp_path, "my run", "20260609_120000")
    second = _make_run_dir(tmp_path, "my run", "20260609_120000")

    assert first.name == "my_run"
    assert second.name == "my_run_2"
    assert first.is_dir()
    assert second.is_dir()


def test_dry_run_does_not_require_root(monkeypatch) -> None:
    calls = []

    monkeypatch.setattr("emulation.mininet_srv6_lab.run_dry_run", lambda config: calls.append(config))
    monkeypatch.setattr("emulation.mininet_srv6_lab.os.geteuid", lambda: 1000)

    status = main(["--dry-run"])

    assert status == 0
    assert len(calls) == 1


def test_each_node_has_transit_and_decap_sid_routes() -> None:
    config = MininetSrv6Config(rows=2, cols=2)
    state = _build_lab_state(config)

    commands = _render_node_setup_commands(config, state, 1)

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
    ] in commands
    assert [
        "ip",
        "-6",
        "route",
        "replace",
        "fc00:d:1::1/128",
        "encap",
        "seg6local",
        "action",
        "End.DT6",
        "table",
        "254",
        "dev",
        "lo",
    ] in commands


def test_programmatic_constellation_scale_controls_mininet_topology() -> None:
    config = MininetSrv6Config(
        planes=2,
        satellites_per_plane=3,
        algorithm_mode="root_target",
    )

    state = _build_lab_state(config)

    assert config.rows == 2
    assert config.cols == 3
    assert state.topology.nodes == {0, 1, 2, 3, 4, 5}
    assert state.target == 5


def test_render_policy_for_path_uses_decap_sid_as_final_segment() -> None:
    config = MininetSrv6Config(rows=2, cols=2)
    state = _build_lab_state(config)

    policy = _render_policy_for_path(config, state, [0, 1, 3], cost=2.0)

    assert policy.source == 0
    assert policy.target == 3
    assert policy.segments == ["fc00:0:1::1", "fc00:d:3::1"]
    assert policy.command == [
        "ip",
        "-6",
        "route",
        "replace",
        "2001:db8:100:3::1/128",
        "encap",
        "seg6",
        "mode",
        "encap",
        "segs",
        "fc00:0:1::1,fc00:d:3::1",
        "dev",
        "r0-r1",
    ]


def test_underlay_includes_service_return_routes() -> None:
    config = MininetSrv6Config(rows=2, cols=2)
    state = _build_lab_state(config)

    commands = _render_underlay_route_commands(config, state, source=1)

    assert [
        "ip",
        "-6",
        "route",
        "replace",
        "fc00:d:0::1/128",
        "via",
        "2001:db8:e:1::1",
        "dev",
        "r1-r0",
    ] in commands
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


def test_underlay_prefers_direct_route_to_adjacent_segment() -> None:
    config = MininetSrv6Config(rows=2, cols=3, delay_model="propagation")
    state = _build_lab_state(config)

    commands = _render_underlay_route_commands(config, state, source=0, slot=0)

    assert [
        "ip",
        "-6",
        "route",
        "replace",
        "fc00:0:1::1/128",
        "via",
        "2001:db8:e:1::2",
        "dev",
        "r0-r1",
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


def test_remaining_policy_path_uses_current_node_suffix() -> None:
    probe = ProbeState(root=0, current_node=1)
    result = TraversalResult(
        TraversalStatus.RUNNING,
        probe,
        next_hop=2,
        path=[0, 1, 2, 3],
    )

    assert _remaining_policy_path(result, current_node=1) == [1, 2, 3]


def test_adaptive_dry_run_prints_hamiltonian_policy(capsys) -> None:
    config = MininetSrv6Config(
        rows=2,
        cols=2,
        duration=0,
        algorithm_mode="adaptive_traversal",
        validate_each_policy=False,
    )

    run_dry_run(config)

    output = capsys.readouterr().out
    assert "# hamiltonian cycle: [0, 2, 3, 1, 0]" in output
    assert (
        "r0$ ip -6 route replace 2001:db8:100:2::1/128 "
        "encap seg6 mode encap segs fc00:d:2::1 dev r0-r2"
    ) in output
