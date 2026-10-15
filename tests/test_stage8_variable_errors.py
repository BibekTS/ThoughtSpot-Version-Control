"""Stage 8 records variable-API errors as issues instead of crashing the whole stage."""
from unittest.mock import MagicMock

import pytest
import requests

from scripts import stage8_prod_prep as s8
from scripts.lib.ts_client import ApiError

CFG = {"variables": {"database": {"ts_variable_name": "v", "target_value": ""}, "schema": {}}}


def _http_error(status):
    r = requests.Response()
    r.status_code = status
    return requests.HTTPError(f"{status} Server Error", response=r)


def _client(probe=None, lookup=None):
    t = MagicMock()
    effects = []
    effects.append(probe if probe is not None else [])           # availability probe
    if lookup is not None:
        effects.append(lookup)
    t.search_variables.side_effect = effects
    return t


@pytest.mark.parametrize("exc", [_http_error(500), _http_error(502), ApiError("variables", 500, "x"),
                                 requests.ConnectionError("down")])
def test_a_failing_probe_is_an_error_issue_not_a_crash(exc):
    issues = s8.sync_variables(_client(probe=exc), CFG, set())
    assert [i["severity"] for i in issues] == ["ERROR"]
    assert "Variable sync skipped" in issues[0]["message"]


@pytest.mark.parametrize("exc", [_http_error(403), _http_error(404), _http_error(405),
                                 ApiError("variables", 501, "x"),
                                 RuntimeError("variables are not enabled")])
def test_unavailable_or_forbidden_stays_a_warning(exc):
    issues = s8.sync_variables(_client(probe=exc), CFG, set())
    assert [i["severity"] for i in issues] == ["WARNING"]


def test_a_failing_per_variable_lookup_is_reported_and_the_rest_continue():
    cfg = {"variables": {"database": {"ts_variable_name": "a", "target_value": ""},
                         "schema": {"ts_variable_name": "b", "target_value": ""}}}
    t = MagicMock()
    t.search_variables.side_effect = [[], _http_error(500), [{"name": "b", "variable_type": "TABLE_MAPPING"}]]
    issues = s8.sync_variables(t, cfg, set())
    assert [(i["variable"], i["severity"]) for i in issues] == [("a", "ERROR")]
    assert "Could not look up variable 'a'" in issues[0]["message"]
    assert t.search_variables.call_count == 3          # 'b' was still checked
