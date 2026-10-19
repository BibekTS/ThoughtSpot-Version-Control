"""Tests for Stage 5: sanitize exported TML and collect what Stage 8 needs."""
import json

from scripts.stage5_sanitize import sanitize_all

CFG = {
    "org_key": "sales", "migration_tag": "sales_readyformigration",
    "variables": {"database": {"ts_variable_name": "ts_db", "source_value": "DEV_DB"},
                  "schema": {"ts_variable_name": "ts_schema", "source_value": "DEV_SCHEMA"}},
}


def _export(tmp_path, obj_id, tml, folder="tables", typ="LOGICAL_TABLE", status="success"):
    rel = f"orgs/sales/{folder}/{obj_id}.tml"
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(tml))
    return {"obj_id": obj_id, "name": obj_id, "type": typ, "folder": folder,
            "status": status, "tml_path": rel}


def test_literal_source_db_and_schema_are_warned_about(tmp_path, sample_table_tml):
    res = _export(tmp_path, "T-1", sample_table_tml)
    out = sanitize_all([res], [], CFG, tmp_path)
    assert {w["field"] for w in out["warnings"]} == {"db", "schema"}
    assert {w["literal_value"] for w in out["warnings"]} == {"DEV_DB", "DEV_SCHEMA"}


def test_variable_bound_values_are_not_warned_about(tmp_path):
    tml = {"table": {"db": "${ts_db}", "schema": "${ts_schema}"}}
    out = sanitize_all([_export(tmp_path, "T-1", tml)], [], CFG, tmp_path)
    assert out["warnings"] == []


def test_non_table_objects_are_not_checked_for_db_and_schema(tmp_path, sample_liveboard_tml):
    res = _export(tmp_path, "L-1", sample_liveboard_tml, folder="liveboards", typ="LIVEBOARD")
    assert sanitize_all([res], [], CFG, tmp_path)["warnings"] == []


def test_tml_is_rewritten_without_guid_and_migration_tag(tmp_path, sample_table_tml):
    res = _export(tmp_path, "T-1", sample_table_tml)
    out = sanitize_all([res], [], CFG, tmp_path)
    written = json.loads((tmp_path / res["tml_path"]).read_text())
    assert "guid" not in written
    assert written["tags"] == ["finance"]
    assert out["results"][0]["sanitize_status"] == "ok"


def test_connection_names_and_ts_var_references_are_collected(tmp_path, sample_table_tml):
    sample_table_tml["table"]["rls_rules"] = {"rules": [{"expr": "[region] = ts_var(region_filter)"}]}
    out = sanitize_all([_export(tmp_path, "T-1", sample_table_tml)], [], CFG, tmp_path)
    assert out["connection_names"] == ["Snowflake_Prod"]
    assert out["var_sync_set"] == ["region_filter"]


def test_failed_missing_and_unparseable_exports_are_skipped_not_fatal(tmp_path):
    failed = {"obj_id": "A", "name": "A", "type": "LIVEBOARD", "folder": "liveboards",
              "status": "failed", "tml_path": ""}
    missing = {"obj_id": "B", "name": "B", "type": "LIVEBOARD", "folder": "liveboards",
               "status": "success", "tml_path": "orgs/sales/liveboards/nope.tml"}
    bad = _export(tmp_path, "C", {}, folder="liveboards", typ="LIVEBOARD")
    (tmp_path / bad["tml_path"]).write_text("not json")
    out = sanitize_all([failed, missing, bad], [], CFG, tmp_path)
    assert [r["sanitize_status"] for r in out["results"]] == [
        "skipped_no_tml", "skipped_file_missing", "failed"]
