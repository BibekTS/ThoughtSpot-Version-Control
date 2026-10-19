"""Tests for Stage 8's connection preflight and its handling of existing variables.

Variable creation, dry runs and validate-only are covered by test_stage8_variable_errors.py,
test_missing_variables_config.py and test_validation_gate.py.
"""
from unittest.mock import MagicMock

from scripts.stage8_prod_prep import connection_preflight, sync_variables

CFG = {"variables": {"database": {"ts_variable_name": "ts_db_name", "target_value": "PROD_DB"}}}


class TestConnectionPreflight:
    def test_present_connection_has_no_issue(self):
        client = MagicMock()
        client.search_connections.return_value = [{"name": "Snowflake_Prod"}]
        assert connection_preflight(client, {"Snowflake_Prod"}) == []

    def test_missing_connection_is_an_error_naming_it(self):
        client = MagicMock()
        client.search_connections.return_value = []
        issues = connection_preflight(client, {"Missing_Conn"})
        assert [(i["severity"], i["connection"]) for i in issues] == [("ERROR", "Missing_Conn")]

    def test_only_an_exact_name_match_counts(self):
        client = MagicMock()
        client.search_connections.return_value = [{"name": "Snowflake_Prod_Copy"}]
        assert connection_preflight(client, {"Snowflake_Prod"})[0]["severity"] == "ERROR"

    def test_lookup_failure_is_an_error(self):
        client = MagicMock()
        client.search_connections.side_effect = RuntimeError("boom")
        issues = connection_preflight(client, {"C"})
        assert issues[0]["severity"] == "ERROR" and "boom" in issues[0]["message"]


class TestExistingVariables:
    def _client(self, existing):
        client = MagicMock()
        client.search_variables.return_value = existing
        return client

    def test_type_mismatch_is_an_error_and_nothing_is_written(self):
        client = self._client([{"name": "ts_db_name", "variable_type": "FORMULA_VARIABLE"}])
        issues = sync_variables(client, CFG, set())
        assert [i["severity"] for i in issues] == ["ERROR"]
        assert "FORMULA_VARIABLE" in issues[0]["message"]
        client.create_variable.assert_not_called()
        client.set_variable_org_value.assert_not_called()

    def test_existing_variable_of_the_right_type_only_gets_its_value_set(self):
        client = self._client([{"name": "ts_db_name", "variable_type": "TABLE_MAPPING"}])
        assert sync_variables(client, CFG, set()) == []
        client.create_variable.assert_not_called()
        client.set_variable_org_value.assert_called_once_with("ts_db_name", "PROD_DB")
