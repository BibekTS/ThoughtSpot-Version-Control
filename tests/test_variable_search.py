"""Variable search must send filters where the API reads them and page through results."""
from unittest.mock import MagicMock

from scripts import stage8_prod_prep as s8
from scripts.lib.ts_client import TSClient


def _client(pages):
    c = TSClient.__new__(TSClient)
    c.bodies = []

    def post_json(path, body):
        c.bodies.append(body)
        return pages.pop(0)

    c.post_json = post_json
    return c


def test_filters_go_in_variable_details_and_size_is_explicit():
    c = _client([[]])
    c.search_variables(variable_type="TABLE_MAPPING", name_pattern="bat_db_var")
    body = c.bodies[0]
    assert body["variable_details"] == [{"type": "TABLE_MAPPING", "name_pattern": "bat_db_var"}]
    assert body["record_size"] == 100 and body["record_offset"] == 0
    assert body["response_content"] == "METADATA"
    assert "name_pattern" not in body and "variable_type" not in body


def test_no_filters_sends_no_variable_details():
    c = _client([[]])
    c.search_variables()
    assert "variable_details" not in c.bodies[0]


def test_pages_until_a_short_page():
    c = _client([[{"name": f"v{i}"} for i in range(100)], [{"name": "bat_db_var"}]])
    out = c.search_variables()
    assert len(out) == 101 and out[-1]["name"] == "bat_db_var"
    assert [b["record_offset"] for b in c.bodies] == [0, 100]


def test_stage8_reads_variable_type_from_the_response(capsys):
    t = MagicMock()
    t.search_variables.side_effect = [[], [{"name": "v", "variable_type": "FORMULA_VARIABLE"}]]
    cfg = {"variables": {"database": {"ts_variable_name": "v", "target_value": "X"}, "schema": {}}}
    issues = s8.sync_variables(t, cfg, set())
    assert any(i["severity"] == "ERROR" and "exists in target with type" in i["message"]
               for i in issues)
