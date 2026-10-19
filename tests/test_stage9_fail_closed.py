"""Stage 9 fails closed: partial responses, unreadable read-backs and unhandled objects."""
import json
from unittest.mock import MagicMock

import pytest

from scripts import stage9_import as s9
from scripts import stage3_discover as s3


def _status(code, msg=""):
    return {"response": {"status": {"status_code": code, "error_message": msg}}}


# ---- import status ---------------------------------------------------------------------- #

@pytest.mark.parametrize("resp,ok", [
    ([_status("OK")], True),
    ([_status("WARNING")], True),
    ([_status("OK"), _status("WARNING")], True),
    ([_status("OK"), _status("ERROR")], False),     # one OK must not hide an ERROR
    ([_status("ERROR"), _status("OK")], False),
    ([], False),                                      # nothing came back
    ({"object": []}, False),
    ({"object": [_status("OK")]}, True),
    ([{}], False),                                    # no status at all
])
def test_import_status_needs_every_object_ok(resp, ok):
    assert s9._parse_import_status(resp, "x") is ok


def test_error_text_comes_from_the_failing_object_and_empty_is_explained():
    assert s9._extract_error([_status("OK"), _status("ERROR", "bad join")]) == "bad join"
    assert "no objects" in s9._extract_error([])


def test_a_partly_failed_response_fails_the_object(tmp_path):
    (tmp_path / "t.tml").write_text(json.dumps({"obj_id": "T-1", "table": {}}))
    entry = {"obj_id": "T-1", "name": "T", "type": "LIVEBOARD", "folder": "liveboards",
             "tml_path": "t.tml", "other_tags": []}
    client = MagicMock()
    client.import_tml.return_value = [_status("OK"), _status("ERROR", "second object bad")]
    failed = set()
    res = s9.import_tier(client, [entry], {}, tmp_path, failed)
    assert res[0]["status"] == "failed" and res[0]["error"] == "second object bad"
    assert failed == {"T-1"}


# ---- safety gate ------------------------------------------------------------------------ #

def _gate(client):
    results = [{"obj_id": "T-1", "status": "success"}]
    failed = set()
    s9.safety_gate(client, results, "DEV_DB", "DEV_SCHEMA", failed)
    return results[0], failed


def test_gate_fails_a_table_whose_readback_raises():
    client = MagicMock()
    client.export_tml.side_effect = RuntimeError("HTTP 500")
    res, failed = _gate(client)
    assert res["status"] == "failed" and "HTTP 500" in res["error"]
    assert "not verified" in res["error"] and failed == {"T-1"}


@pytest.mark.parametrize("resp", [[], {"object": []}])
def test_gate_fails_a_table_whose_readback_is_empty(resp):
    client = MagicMock()
    client.export_tml.return_value = resp
    res, failed = _gate(client)
    assert res["status"] == "failed" and failed == {"T-1"}


def test_gate_fails_a_table_whose_readback_is_not_tml():
    client = MagicMock()
    client.export_tml.return_value = [{"edoc": "not json"}]
    res, _ = _gate(client)
    assert res["status"] == "failed"


def test_gate_still_passes_a_verified_table():
    client = MagicMock()
    client.export_tml.return_value = [{"edoc": json.dumps({"table": {"db": "P", "schema": "Q"}})}]
    res, failed = _gate(client)
    assert res["status"] == "success" and failed == set()


# ---- objects no tier handles ------------------------------------------------------------ #

def test_untiered_objects_are_failed_and_handled_ones_are_left_alone():
    manifest = [
        {"obj_id": "A", "name": "A", "type": "LIVEBOARD", "folder": "liveboards"},
        {"obj_id": "B", "name": "B", "type": "MYSTERY", "folder": "misc"},
    ]
    out = s9.untiered_results(manifest, [{"obj_id": "A", "status": "success"}])
    assert [(r["obj_id"], r["status"]) for r in out] == [("B", "failed")]
    assert "MYSTERY" in out[0]["error"]


def test_every_discoverable_type_and_folder_has_a_tier():
    """What Stage 3 can put in the manifest must be something Stage 9 imports."""
    table_tiers = {folder for folder, types, _ in s9.IMPORT_TIERS if "LOGICAL_TABLE" in types}
    assert set(s3._TABLE_SUBTYPE_FOLDER.values()) | {"tables"} <= table_tiers
    for meta_type in s3.SEARCH_TYPES:
        assert any(meta_type in types for _, types, _ in s9.IMPORT_TIERS), meta_type


# ---- stage 3 subtype warning ------------------------------------------------------------ #

def test_unrecognised_table_subtype_is_warned_about_and_treated_as_a_table(capsys):
    obj = {"metadata_name": "Odd", "metadata_header": {"type": "SOMETHING_NEW"}}
    assert s3._folder_for_object("LOGICAL_TABLE", obj) == "tables"
    assert "SOMETHING_NEW" in capsys.readouterr().err


def test_known_and_missing_subtypes_are_quiet(capsys):
    assert s3._folder_for_object("LOGICAL_TABLE", {"metadata_header": {"type": "WORKSHEET"}}) == "models"
    assert s3._folder_for_object("LOGICAL_TABLE", {}) == "tables"
    assert capsys.readouterr().err == ""
