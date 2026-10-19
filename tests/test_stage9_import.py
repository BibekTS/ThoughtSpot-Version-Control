"""Tests for Stage 9: Path B substitution at import, the post-import safety gate, status parsing.

Dependency skipping, tier ordering and variable binding have their own modules
(test_stage9_dependency_skip.py, test_stage9_tier_ordering.py, test_stage9_parameterize.py).
"""
import json
from unittest.mock import MagicMock

from scripts import stage9_import as s9

CFG = {"variables": {
    "database": {"source_value": "DEV_DB", "target_value": "PROD_DB", "ts_variable_name": ""},
    "schema": {"source_value": "DEV_SCHEMA", "target_value": "PROD_SCHEMA", "ts_variable_name": ""}}}

OK = [{"response": {"status": {"status_code": "OK"}}}]
BAD = [{"response": {"status": {"status_code": "ERROR", "error_message": "no good"}}}]


def _entry(tmp_path, tml, obj_id="T-1", folder="tables"):
    rel = f"{obj_id}.tml"
    (tmp_path / rel).write_text(json.dumps(tml))
    return {"obj_id": obj_id, "name": obj_id, "type": "LOGICAL_TABLE", "folder": folder,
            "tml_path": rel, "other_tags": [], "reason": "tagged"}


class TestPathBSubstitution:
    def test_source_values_are_replaced_in_the_tml_that_is_sent(self, tmp_path, sample_table_tml):
        client = MagicMock()
        client.import_tml.return_value = OK
        s9.import_tier(client, [_entry(tmp_path, sample_table_tml)], CFG, tmp_path, set())
        sent = client.import_tml.call_args.args[0][0]
        assert "PROD_DB" in sent and "PROD_SCHEMA" in sent
        assert "DEV_DB" not in sent and "DEV_SCHEMA" not in sent

    def test_the_file_on_disk_is_never_rewritten(self, tmp_path, sample_table_tml):
        client = MagicMock()
        client.import_tml.return_value = OK
        entry = _entry(tmp_path, sample_table_tml)
        s9.import_tier(client, [entry], CFG, tmp_path, set())
        assert "DEV_DB" in (tmp_path / entry["tml_path"]).read_text()

    def test_models_are_sent_unchanged(self, tmp_path):
        client = MagicMock()
        client.import_tml.return_value = OK
        entry = _entry(tmp_path, {"model": {"db": "DEV_DB"}}, obj_id="M-1", folder="models")
        s9.import_tier(client, [entry], CFG, tmp_path, set())
        assert "DEV_DB" in client.import_tml.call_args.args[0][0]


class TestImportOutcome:
    def test_a_rejected_import_is_failed_with_the_api_message_and_remembered(self, tmp_path):
        client = MagicMock()
        client.import_tml.return_value = BAD
        failed = set()
        res = s9.import_tier(client, [_entry(tmp_path, {"table": {}})], CFG, tmp_path, failed)
        assert res[0]["status"] == "failed" and res[0]["error"] == "no good"
        assert failed == {"T-1"}

    def test_missing_tml_file_is_skipped(self, tmp_path):
        entry = _entry(tmp_path, {"table": {}})
        (tmp_path / entry["tml_path"]).unlink()
        res = s9.import_tier(MagicMock(), [entry], CFG, tmp_path, set())
        assert res[0] == {"obj_id": "T-1", "name": "T-1", "type": "LOGICAL_TABLE",
                          "status": "skipped", "reason": "tml file missing"}


class TestParseImportStatus:
    def test_ok_and_warning_count_as_success(self):
        for code in ("OK", "WARNING"):
            assert s9._parse_import_status(
                [{"response": {"status": {"status_code": code}}}], "x") is True

    def test_error_or_empty_is_failure(self):
        assert s9._parse_import_status(BAD, "x") is False
        assert s9._parse_import_status([], "x") is False

    def test_object_wrapper_is_unwrapped(self):
        assert s9._parse_import_status({"object": OK}, "x") is True


class TestSafetyGate:
    def _readback(self, db, schema):
        client = MagicMock()
        client.export_tml.return_value = [{"edoc": json.dumps({"table": {"db": db, "schema": schema}})}]
        return client

    def _run(self, client):
        results = [{"obj_id": "T-1", "status": "success"}]
        failed = set()
        s9.safety_gate(client, results, "DEV_DB", "DEV_SCHEMA", failed)
        return results[0], failed

    def test_a_table_still_holding_the_source_db_is_failed(self):
        res, failed = self._run(self._readback("DEV_DB", "PROD_SCHEMA"))
        assert res["status"] == "failed" and "DEV_DB" in res["error"]
        assert failed == {"T-1"}

    def test_a_table_still_holding_the_source_schema_is_failed(self):
        res, _ = self._run(self._readback("PROD_DB", "DEV_SCHEMA"))
        assert res["status"] == "failed"

    def test_target_values_pass(self):
        res, failed = self._run(self._readback("PROD_DB", "PROD_SCHEMA"))
        assert res["status"] == "success" and failed == set()

    def test_only_successful_imports_are_read_back(self):
        client = MagicMock()
        s9.safety_gate(client, [{"obj_id": "T-1", "status": "failed"}], "A", "B", set())
        client.export_tml.assert_not_called()
