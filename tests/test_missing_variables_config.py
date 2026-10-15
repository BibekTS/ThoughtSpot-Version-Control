"""A config without (or with a partial) `variables` block must not crash Stages 1, 5, 8, 9."""
import json
from unittest.mock import MagicMock

import pytest

from scripts import stage1_config as s1
from scripts import stage5_sanitize as s5
from scripts import stage8_prod_prep as s8
from scripts import stage9_import as s9
from scripts.lib.variables import variable_config

BASE = {"org_key": "o", "display_name": "O", "migration_tag": "t",
        "source": {"base_url": "https://s", "org_id": 1}, "target": {"base_url": "https://t", "org_id": 2}}
CONFIGS = {
    "no block": dict(BASE),
    "null block": {**BASE, "variables": None},
    "empty block": {**BASE, "variables": {}},
    "null entries": {**BASE, "variables": {"database": None, "schema": None, "extra": None}},
    "name only": {**BASE, "variables": {"database": {"ts_variable_name": "d"},
                                        "schema": {"ts_variable_name": "s"}}},
}


def test_variable_config_fills_every_key():
    assert variable_config(BASE, "database") == {"ts_variable_name": "", "source_value": "",
                                                 "target_value": ""}
    cfg = {"variables": {"database": {"ts_variable_name": "d", "target_value": None}}}
    assert variable_config(cfg, "database") == {"ts_variable_name": "d", "source_value": "",
                                                "target_value": ""}


@pytest.mark.parametrize("name", CONFIGS)
def test_stage9_imports_a_table_without_variable_config(name, tmp_path):
    (tmp_path / "t.tml").write_text(json.dumps({"table": {"db": "DB", "schema": "SCH"}}))
    entry = {"obj_id": "T-1", "name": "T", "type": "LOGICAL_TABLE", "folder": "tables",
             "tml_path": "t.tml", "other_tags": [], "reason": "tagged"}
    c = MagicMock()
    c.import_tml.return_value = [{"response": {"status": {"status_code": "OK"}}}]
    c.find_guid_by_obj_id.return_value = "g"
    res = s9.import_tier(c, [entry], CONFIGS[name], tmp_path, set())
    assert res[0]["status"] == "success"
    if name != "name only":
        c.parameterize_fields.assert_not_called()      # nothing configured, nothing to bind


@pytest.mark.parametrize("name", CONFIGS)
def test_stage5_sanitizes_without_variable_config(name, tmp_path):
    out = s5.sanitize_all([], [], CONFIGS[name], tmp_path)
    assert out is not None


@pytest.mark.parametrize("name", CONFIGS)
def test_stage1_accepts_the_config(name, tmp_path):
    import yaml
    path = tmp_path / "o.yml"
    path.write_text(yaml.safe_dump(CONFIGS[name]))
    assert s1.load_and_validate_config(str(path))["org_key"] == "o"


@pytest.mark.parametrize("name", CONFIGS)
def test_stage8_tolerates_the_config(name):
    t = MagicMock()
    t.search_variables.return_value = []
    s8.sync_variables(t, CONFIGS[name], set())
