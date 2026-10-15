"""Stage 9 binds a table's database and schema to THEIR OWN variables, by GUID."""
import json
from unittest.mock import MagicMock

from scripts import stage9_import as s9
from scripts.lib.ts_client import ApiError

CFG = {"variables": {
    "database": {"source_value": "DEV_DB", "target_value": "PROD_DB", "ts_variable_name": "db_var"},
    "schema": {"source_value": "DEV_SCH", "target_value": "PROD_SCH", "ts_variable_name": "sch_var"}}}


def _client(guid="tgt-guid", parameterize_side_effect=None):
    c = MagicMock()
    c.import_tml.return_value = [{"response": {"status": {"status_code": "OK"}}}]
    c.find_guid_by_obj_id.return_value = guid
    if parameterize_side_effect:
        c.parameterize_fields.side_effect = parameterize_side_effect
    return c


def _table(tmp_path):
    (tmp_path / "t.table.tml").write_text(json.dumps({"table": {"db": "DEV_DB", "schema": "DEV_SCH"}}))
    return {"obj_id": "T-1", "name": "T", "type": "LOGICAL_TABLE", "folder": "tables",
            "tml_path": "t.table.tml", "other_tags": [], "reason": "tagged"}


def test_database_and_schema_are_bound_to_their_own_variables_by_guid(tmp_path):
    c = _client()
    res = s9.import_tier(c, [_table(tmp_path)], CFG, tmp_path, set())
    assert res[0]["status"] == "success" and "warnings" not in res[0]
    calls = [(k["table_identifier"], k["field_names"], k["variable_identifier"])
             for _, k in c.parameterize_fields.call_args_list]
    assert calls == [("tgt-guid", ["databaseName"], "db_var"),
                     ("tgt-guid", ["schemaName"], "sch_var")]        # NOT both to db_var
    c.find_guid_by_obj_id.assert_called_once_with("LOGICAL_TABLE", "T-1")


def test_a_failed_bind_is_reported_and_the_other_still_runs(tmp_path, capsys):
    c = _client(parameterize_side_effect=[ApiError("metadata/parameterize-fields", 400, "no var"), None])
    res = s9.import_tier(c, [_table(tmp_path)], CFG, tmp_path, set())
    assert res[0]["status"] == "success"                              # the import itself succeeded
    assert len(res[0]["warnings"]) == 1 and "databaseName -> 'db_var'" in res[0]["warnings"][0]
    assert c.parameterize_fields.call_count == 2
    out = capsys.readouterr()
    assert "Failed to bind databaseName" in out.err
    assert "Bound schemaName" in out.out and "Bound databaseName" not in out.out


def test_table_not_found_in_target_is_a_warning_not_a_crash(tmp_path):
    c = _client(guid=None)
    res = s9.import_tier(c, [_table(tmp_path)], CFG, tmp_path, set())
    assert res[0]["status"] == "success"
    assert "not found in the target" in res[0]["warnings"][0]
    c.parameterize_fields.assert_not_called()


def test_blank_variable_names_skip_binding_without_a_lookup():
    c = MagicMock()
    assert s9._parameterize_table(c, "T-1", "", "") == []
    c.find_guid_by_obj_id.assert_not_called()
    c.parameterize_fields.assert_not_called()


def test_only_the_configured_variable_is_bound():
    c = MagicMock(); c.find_guid_by_obj_id.return_value = "g"
    assert s9._parameterize_table(c, "T-1", "db_var", "") == []
    assert c.parameterize_fields.call_count == 1
    assert c.parameterize_fields.call_args.kwargs["field_names"] == ["databaseName"]


def test_non_table_objects_are_not_parameterized(tmp_path):
    (tmp_path / "m.tml").write_text(json.dumps({"model": {}}))
    c = _client()
    entry = {"obj_id": "M-1", "name": "M", "type": "LOGICAL_TABLE", "folder": "models",
             "tml_path": "m.tml", "other_tags": [], "reason": "tagged"}
    s9.import_tier(c, [entry], CFG, tmp_path, set())
    c.parameterize_fields.assert_not_called()
