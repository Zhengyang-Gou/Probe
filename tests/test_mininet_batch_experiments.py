import importlib.util
import random
import sys
from pathlib import Path

import pytest

SCRIPT_PATH = Path(__file__).resolve().parents[1] / "emulation" / "mininet_batch_experiments.py"
SPEC = importlib.util.spec_from_file_location("mininet_batch_experiments_for_test", SCRIPT_PATH)
assert SPEC is not None
assert SPEC.loader is not None
mininet_batch = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = mininet_batch
SPEC.loader.exec_module(mininet_batch)

build_lab_command = mininet_batch.build_lab_command
load_config = mininet_batch.load_config
select_failure_edges = mininet_batch.select_failure_edges


def test_load_config_reads_mininet_batch_settings(tmp_path) -> None:
    path = tmp_path / "batch.toml"
    path.write_text(
        """
[scenario]
count = 3
seed = 11
interrupt_statuses = ["temporarily_unreachable"]

[execution]
dry_run = true

[constellation]
planes = 10
satellites_per_plane = 10

[failure]
mode = "fixed_count"
down_edges_per_scenario = 12
start = 0
end = 60

[output]
base_dir = "logs/test-mininet-batch"
run_name = "batch"
write_stdout = false
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.scenario_count == 3
    assert config.seed == 11
    assert config.interrupt_statuses == ("temporarily_unreachable",)
    assert config.planes == 10
    assert config.satellites_per_plane == 10
    assert config.failure_mode == "fixed_count"
    assert config.down_edges_per_scenario == 12
    assert config.dry_run is True
    assert config.output_dir == "logs/test-mininet-batch"
    assert config.run_name == "batch"
    assert config.write_stdout is False


def test_load_config_rejects_fixed_count_without_count(tmp_path) -> None:
    path = tmp_path / "batch.toml"
    path.write_text(
        """
[failure]
mode = "fixed_count"
""",
        encoding="utf-8",
    )

    with pytest.raises(ValueError, match="failure.down_edges_per_scenario"):
        load_config(path)


def test_select_failure_edges_uses_fixed_count(tmp_path) -> None:
    path = tmp_path / "batch.toml"
    path.write_text(
        """
[constellation]
planes = 4
satellites_per_plane = 4

[failure]
mode = "fixed_count"
down_edges_per_scenario = 5
""",
        encoding="utf-8",
    )
    config = load_config(path)

    edges = select_failure_edges(config, random.Random(1))

    assert len(edges) == 5
    assert len(set(edges)) == 5


def test_build_lab_command_contains_multiple_failure_edges(tmp_path) -> None:
    path = tmp_path / "batch.toml"
    path.write_text(
        """
[execution]
dry_run = true

[failure]
mode = "fixed_count"
down_edges_per_scenario = 2
""",
        encoding="utf-8",
    )
    config = load_config(path)

    command = build_lab_command(
        config,
        run_name="scenario_0001",
        output_dir=tmp_path,
        failure_edges=((0, 1), (5, 9)),
    )

    assert "--failure-edges" in command
    assert "0,1;5,9" in command
    assert "--output-dir" in command
    assert str(tmp_path) in command
    assert "--run-name" in command
    assert "scenario_0001" in command
    assert "--dry-run" in command
