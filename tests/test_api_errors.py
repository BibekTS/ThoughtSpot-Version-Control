"""Failed API calls must not look like successes (tags, parameterize, variable values)."""
import json
from unittest.mock import MagicMock

import pytest

from scripts.lib.ts_client import ApiError, TSClient
from scripts import stage8_prod_prep as s8
from scripts import stage9_import as s9
from scripts import stage10_postcheck as s10


def _client(status=204, body="", json_body=None):
    c = TSClient("https://x", 1, "u", "k")
    c._token, c._token_expires_at = "t", 1e12
    c.authenticate = lambda: None          # a 401 makes the client re-authenticate; no network here
    resp = MagicMock(status_code=status, text=body)
    resp.json.return_value = json_body if json_body is not None else []
    c._session = MagicMock()
    c._session.request.return_value = resp
    return c


CALLS = {
    "assign_tags": lambda c: c.assign_tags([{"type": "ANSWER", "identifier": "g"}], ["t"]),
    "unassign_tags": lambda c: c.unassign_tags([{"type": "ANSWER", "identifier": "g"}], ["t"]),
    "parameterize_fields": lambda c: c.parameterize_fields("g", ["databaseName"], "v"),
    "set_variable_org_value": lambda c: c.set_variable_org_value("v", "1"),
}


@pytest.mark.parametrize("name", CALLS)
@pytest.mark.parametrize("status", [400, 401, 403, 404])
def test_helpers_raise_on_client_errors_with_the_status_and_body(name, status):
    c = _client(status=status, body="no such object")
    with pytest.raises(ApiError) as e:
        CALLS[name](c)
    assert e.value.status_code == status and "no such object" in str(e.value)


@pytest.mark.parametrize("name", CALLS)
@pytest.mark.parametrize("status", [200, 204])
def test_helpers_are_quiet_on_success(name, status):
    CALLS[name](_client(status=status))        # no exception


def test_find_guid_by_obj_id_asks_for_the_right_type_and_matches_exactly():
    c = _client(status=200, json_body=[
        {"metadata_id": "other", "metadata_obj_id": "other-id"},
        {"metadata_id": "g-1", "metadata_obj_id": "ANS-1"}])
    assert c.find_guid_by_obj_id("ANSWER", "ANS-1") == "g-1"
    sent = c._session.request.call_args.kwargs["json"]
    assert sent["metadata"] == [{"type": "ANSWER", "obj_identifier": "ANS-1"}]
    assert c.find_guid_by_obj_id("ANSWER", "nope") is None
    assert _client(status=404).find_guid_by_obj_id("ANSWER", "x") is None


def test_find_model_guid_still_works():
    c = _client(status=200, json_body=[{"metadata_id": "g-m", "metadata_obj_id": "M-1"}])
    assert c.find_model_guid("M-1") == "g-m"


# ---- callers now see the failures ------------------------------------------------------------
def test_stage10_does_not_claim_tags_were_removed_when_the_api_refused(capsys):
    src = MagicMock()
    src.unassign_tags.side_effect = ApiError("tags/unassign", 400, "bad identifier")
    manifest = [{"obj_id": "a", "type": "ANSWER", "name": "A", "source_guid": "g", "reason": "tagged"}]
    out = s10.cleanup_tags(src, manifest, [{"obj_id": "a", "status": "success"}], [], "tag")
    assert out["removed"] == 0 and out["retained"] == 0
    err = capsys.readouterr().err
    assert "NOT removed" in err and "HTTP 400" in err


def test_stage8_records_a_failed_variable_value_instead_of_logging_success(capsys):
    t = MagicMock()
    t.search_variables.side_effect = [[], [{"name": "v", "type": "TABLE_MAPPING"}]]  # probe, lookup
    t.set_variable_org_value.side_effect = ApiError("variables/update-values", 403, "nope")
    cfg = {"variables": {"database": {"ts_variable_name": "v", "target_value": "PROD"}, "schema": {}}}
    issues = s8.sync_variables(t, cfg, set())
    assert any(i["severity"] == "ERROR" and i["variable"] == "v"
               and "Failed to set value" in i["message"] for i in issues)
    assert "Set org value for variable" not in capsys.readouterr().out


def _import_ok(client, obj_id="ANS-1"):
    client.import_tml.return_value = [{"response": {"status": {"status_code": "OK"}}}]


def test_stage9_reapplies_tags_by_guid_not_obj_id(tmp_path):
    tml = tmp_path / "a.answer.tml"; tml.write_text(json.dumps({"answer": {}}))
    client = MagicMock(); _import_ok(client)
    client.find_guid_by_obj_id.return_value = "tgt-guid"
    entry = {"obj_id": "ANS-1", "name": "A", "type": "ANSWER", "folder": "answers",
             "tml_path": "a.answer.tml", "other_tags": ["Certified"], "reason": "tagged"}
    cfg = {"variables": {"database": {"source_value": "", "target_value": "", "ts_variable_name": "d"},
                         "schema": {"source_value": "", "target_value": "", "ts_variable_name": "s"}}}
    res = s9.import_tier(client, [entry], cfg, tmp_path, set())
    assert res[0]["status"] == "success"
    client.find_guid_by_obj_id.assert_called_once_with("ANSWER", "ANS-1")
    client.assign_tags.assert_called_once_with(
        [{"type": "ANSWER", "identifier": "tgt-guid"}], ["Certified"])


def test_stage9_tag_reapply_failure_is_reported_not_swallowed(tmp_path, capsys):
    tml = tmp_path / "a.answer.tml"; tml.write_text(json.dumps({"answer": {}}))
    client = MagicMock(); _import_ok(client)
    client.find_guid_by_obj_id.return_value = None            # not found in the target
    entry = {"obj_id": "ANS-1", "name": "A", "type": "ANSWER", "folder": "answers",
             "tml_path": "a.answer.tml", "other_tags": ["Certified"], "reason": "tagged"}
    cfg = {"variables": {"database": {"source_value": "", "target_value": "", "ts_variable_name": "d"},
                         "schema": {"source_value": "", "target_value": "", "ts_variable_name": "s"}}}
    res = s9.import_tier(client, [entry], cfg, tmp_path, set())
    assert res[0]["status"] == "success"                      # the import itself did succeed
    client.assign_tags.assert_not_called()
    assert "Failed to reapply tags for 'A'" in capsys.readouterr().err
