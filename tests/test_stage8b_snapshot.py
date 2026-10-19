"""Stage 8b: save what the target holds before the import overwrites it."""
import json
from unittest.mock import MagicMock

import pytest

from scripts import stage8b_snapshot as s8b


def _entry(obj_id, typ="LIVEBOARD", folder="liveboards"):
    return {"obj_id": obj_id, "name": obj_id.title(), "type": typ, "folder": folder}


def _client(existing, tml=None, export_error=None):
    client = MagicMock()
    client.find_guid_by_obj_id.side_effect = lambda typ, oid: f"g-{oid}" if oid in existing else None
    if export_error:
        client.export_tml.side_effect = export_error
    else:
        client.export_tml.side_effect = lambda md, **k: [
            {"edoc": json.dumps(tml or {"obj_id": md[0]["identifier"], "liveboard": {}})}]
    return client


def test_an_existing_object_is_saved_under_its_folder_and_obj_id(tmp_path):
    res = s8b.snapshot_objects(_client({"a"}), [_entry("a")], "o", tmp_path)
    assert res[0]["status"] == "captured"
    saved = tmp_path / "o" / "liveboards" / "a.tml"
    assert json.loads(saved.read_text())["obj_id"] == "g-a"


def test_an_object_not_in_the_target_is_new_and_nothing_is_written(tmp_path):
    res = s8b.snapshot_objects(_client(set()), [_entry("a")], "o", tmp_path)
    assert res[0]["status"] == "new"
    assert not list(tmp_path.rglob("*.tml"))


def test_an_unreadable_object_is_failed_not_fatal_to_the_others(tmp_path):
    client = _client({"a", "b"})
    ok = client.export_tml.side_effect

    def flaky(md, **k):
        if md[0]["identifier"] == "g-a":
            raise RuntimeError("HTTP 500")
        return ok(md, **k)

    client.export_tml.side_effect = flaky
    res = s8b.snapshot_objects(client, [_entry("a"), _entry("b")], "o", tmp_path)
    assert [(r["obj_id"], r["status"]) for r in res] == [("a", "failed"), ("b", "captured")]
    assert "HTTP 500" in res[0]["error"]


def test_an_empty_export_is_a_failure(tmp_path):
    client = _client({"a"})
    client.export_tml.side_effect = None
    client.export_tml.return_value = []
    assert s8b.snapshot_objects(client, [_entry("a")], "o", tmp_path)[0]["status"] == "failed"


def test_types_that_are_not_exported_on_their_own_are_skipped(tmp_path):
    client = _client({"f"})
    res = s8b.snapshot_objects(client, [_entry("f", "FEEDBACK", "feedback")], "o", tmp_path)
    assert res[0]["status"] == "skipped"
    client.find_guid_by_obj_id.assert_not_called()


def test_the_target_is_only_read(tmp_path):
    client = _client({"a"})
    s8b.snapshot_objects(client, [_entry("a")], "o", tmp_path)
    assert not client.import_tml.called and not client.assign_tags.called


# ---- main -------------------------------------------------------------------------------- #

def _run_main(tmp_path, monkeypatch, client, required=None, exported_status="success"):
    files = {"config.json": {"target": {"base_url": "https://t", "org_id": 2}},
             "manifest.json": [_entry("a"), _entry("b")],
             "export.json": [{"obj_id": "a", "status": "success"},
                             {"obj_id": "b", "status": exported_status}]}
    for name, body in files.items():
        (tmp_path / name).write_text(json.dumps(body))
    monkeypatch.setattr(s8b, "TSClient", lambda *a, **k: client)
    client.authenticate = lambda: None
    monkeypatch.setattr(s8b, "resolve_secret", lambda n: "x")
    for k, v in {"ORG_KEY": "o", "CONFIG_OUT": "config.json", "MANIFEST_OUT": "manifest.json",
                 "EXPORT_OUT": "export.json", "SNAPSHOT_OUT": "snap.json",
                 "SNAPSHOT_DIR": "snapshot"}.items():
        monkeypatch.setenv(k, v)
    if required is not None:
        monkeypatch.setenv("SNAPSHOT_REQUIRED", required)
    monkeypatch.chdir(tmp_path)
    s8b.main()
    return json.loads((tmp_path / "snap.json").read_text())


def test_main_snapshots_only_objects_whose_export_succeeded(tmp_path, monkeypatch):
    out = _run_main(tmp_path, monkeypatch, _client({"a", "b"}), exported_status="failed")
    assert [r["obj_id"] for r in out] == ["a"]


def test_main_stops_the_run_when_a_copy_cannot_be_saved(tmp_path, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run_main(tmp_path, monkeypatch, _client({"a"}, export_error=RuntimeError("boom")))
    assert exc.value.code == 1
    assert json.loads((tmp_path / "snap.json").read_text())[0]["status"] == "failed"


def test_main_can_be_told_to_carry_on(tmp_path, monkeypatch):
    out = _run_main(tmp_path, monkeypatch, _client({"a"}, export_error=RuntimeError("boom")),
                    required="false")
    assert out[0]["status"] == "failed"
