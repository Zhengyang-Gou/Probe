import pytest
import importlib.util
import sys
from pathlib import Path

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "random_experiments.py"
SPEC = importlib.util.spec_from_file_location("random_experiments_for_test", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
random_experiments = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = random_experiments
SPEC.loader.exec_module(random_experiments)
build_random_provider = random_experiments.build_random_provider
load_config = random_experiments.load_config
make_run_dir = random_experiments.make_run_dir
main = random_experiments.main


def test_load_config_defaults_srv6_disabled(tmp_path) -> None:
    path = tmp_path / "experiment.toml"
    path.write_text(
        """
[experiment]
runs = 1

[grid]
rows = 2
cols = 2
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.srv6_enabled is False
    assert config.srv6_locator_prefix == "fc00:0"
    assert config.srv6_base_srh_overhead_bytes == 8
    assert config.srv6_per_sid_overhead_bytes == 16
    assert config.failure_mode == "probability"
    assert config.down_edges_per_scenario is None
    assert [status.value for status in config.interrupted_statuses] == [
        "temporarily_unreachable",
        "partial_result",
    ]
    assert config.output_dir == "logs/random"
    assert config.run_name is None
    assert config.write_stdout is True


def test_load_config_reads_scenario_and_fixed_count_failure(tmp_path) -> None:
    path = tmp_path / "experiment.toml"
    path.write_text(
        """
[scenario]
count = 7
seed = 42
interrupt_statuses = ["temporarily_unreachable"]

[grid]
rows = 10
cols = 10

[failure]
mode = "fixed_count"
down_edges_per_scenario = 12
down_probability = 0.25
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.runs == 7
    assert config.seed == 42
    assert config.rows == 10
    assert config.cols == 10
    assert config.failure_mode == "fixed_count"
    assert config.down_edges_per_scenario == 12
    assert config.down_probability == 0.25
    assert [status.value for status in config.interrupted_statuses] == [
        "temporarily_unreachable"
    ]


def test_load_config_reads_srv6_block(tmp_path) -> None:
    path = tmp_path / "experiment.toml"
    path.write_text(
        """
[experiment]
runs = 1

[grid]
rows = 2
cols = 2

[srv6]
enabled = true
locator_prefix = "fd00:1"
base_srh_overhead_bytes = 12
per_sid_overhead_bytes = 20

[output]
base_dir = "logs/custom-random"
run_name = "case 1"
write_stdout = false
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.srv6_enabled is True
    assert config.srv6_locator_prefix == "fd00:1"
    assert config.srv6_base_srh_overhead_bytes == 12
    assert config.srv6_per_sid_overhead_bytes == 20
    assert config.output_dir == "logs/custom-random"
    assert config.run_name == "case 1"
    assert config.write_stdout is False


def test_load_config_rejects_invalid_srv6_overheads(tmp_path) -> None:
    path = tmp_path / "experiment.toml"
    path.write_text(
        """
[experiment]
runs = 1

[grid]
rows = 2
cols = 2

[srv6]
base_srh_overhead_bytes = -1
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="srv6.base_srh_overhead_bytes"):
        load_config(path)


def test_load_config_rejects_fixed_count_without_edge_count(tmp_path) -> None:
    path = tmp_path / "experiment.toml"
    path.write_text(
        """
[experiment]
runs = 1

[grid]
rows = 2
cols = 2

[failure]
mode = "fixed_count"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="failure.down_edges_per_scenario"):
        load_config(path)


def test_fixed_count_random_provider_selects_exact_edge_count() -> None:
    topology = random_experiments.make_grid_topology(4, 4)
    rng = random_experiments.random.Random(1)

    provider = build_random_provider(
        topology,
        rng=rng,
        down_probability=0.0,
        down_edges_per_scenario=5,
    )

    assert len(provider.down_intervals) == 5


def test_make_run_dir_allocates_unique_folder(tmp_path) -> None:
    first = make_run_dir(tmp_path, "case 1", "20260609_120000")
    second = make_run_dir(tmp_path, "case 1", "20260609_120000")

    assert first.name == "case_1"
    assert second.name == "case_1_2"
    assert first.is_dir()
    assert second.is_dir()


def test_main_writes_output_files_from_config(tmp_path, monkeypatch) -> None:
    config_path = tmp_path / "experiment.toml"
    output_dir = tmp_path / "logs"
    config_path.write_text(
        f"""
[experiment]
runs = 1
seed = 1

[grid]
rows = 2
cols = 2

[delay]
model = "leo"
period_slots = 2

[failure]
down_probability = 0

[output]
base_dir = "{output_dir}"
run_name = "smoke"
write_stdout = false
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys, "argv", ["random_experiments.py", "--config", str(config_path)])

    main()

    run_dir = output_dir / "smoke"
    assert (run_dir / "summary.txt").is_file()
    assert (run_dir / "runs.jsonl").is_file()
    assert (run_dir / "run_config.json").is_file()
