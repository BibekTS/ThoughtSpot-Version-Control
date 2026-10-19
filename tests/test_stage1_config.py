"""Tests for Stage 1: config validation."""
import os
import tempfile
import pytest
import yaml

from scripts.stage1_config import load_and_validate_config, ConfigError


def write_config(tmp_path, data):
    path = os.path.join(str(tmp_path), f"{data['org_key']}.yml")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        yaml.dump(data, f)
    return path


def test_valid_config(sample_org_config, tmp_path):
    path = write_config(tmp_path, sample_org_config)
    cfg = load_and_validate_config(path)
    assert cfg["org_key"] == "sales"


def test_custom_migration_tag_name_is_accepted(sample_org_config, tmp_path):
    """The <org_key>_readyformigration convention is recommended, not enforced."""
    sample_org_config["migration_tag"] = "Custom_ReadyForMigration"
    path = write_config(tmp_path, sample_org_config)
    assert load_and_validate_config(path)["migration_tag"] == "Custom_ReadyForMigration"


def test_blank_migration_tag_fails(sample_org_config, tmp_path):
    sample_org_config["migration_tag"] = "  "
    path = write_config(tmp_path, sample_org_config)
    with pytest.raises(ConfigError, match="migration_tag"):
        load_and_validate_config(path)


def test_same_url_and_org_fails(sample_org_config, tmp_path):
    sample_org_config["target"]["base_url"] = sample_org_config["source"]["base_url"]
    sample_org_config["target"]["org_id"] = sample_org_config["source"]["org_id"]
    path = write_config(tmp_path, sample_org_config)
    with pytest.raises(ConfigError, match="source and target"):
        load_and_validate_config(path)


def test_same_url_different_org_ok(sample_org_config, tmp_path):
    """Same-cluster org-to-org migration must be allowed."""
    sample_org_config["target"]["base_url"] = sample_org_config["source"]["base_url"]
    sample_org_config["target"]["org_id"] = 99  # different org
    path = write_config(tmp_path, sample_org_config)
    cfg = load_and_validate_config(path)
    assert cfg is not None


def test_missing_target_value_is_allowed(sample_org_config, tmp_path):
    """A blank target_value means the variable must already exist in the target (Stage 8)."""
    del sample_org_config["variables"]["database"]["target_value"]
    path = write_config(tmp_path, sample_org_config)
    assert load_and_validate_config(path) is not None


def test_variable_block_without_a_name_fails(sample_org_config, tmp_path):
    del sample_org_config["variables"]["database"]["ts_variable_name"]
    path = write_config(tmp_path, sample_org_config)
    with pytest.raises(ConfigError, match="ts_variable_name"):
        load_and_validate_config(path)


def test_invalid_dependency_mode(sample_org_config, tmp_path):
    sample_org_config["dependency_mode"] = "invalid_mode"
    path = write_config(tmp_path, sample_org_config)
    with pytest.raises(ConfigError, match="dependency_mode"):
        load_and_validate_config(path)
