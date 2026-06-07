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
load_config = random_experiments.load_config


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
""",
        encoding="utf-8",
    )

    config = load_config(path)

    assert config.srv6_enabled is True
    assert config.srv6_locator_prefix == "fd00:1"
    assert config.srv6_base_srh_overhead_bytes == 12
    assert config.srv6_per_sid_overhead_bytes == 20


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
