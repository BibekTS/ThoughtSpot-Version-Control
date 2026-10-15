"""Stage 9 skips an object whose DEPENDENCIES failed - not a dependency whose consumer failed."""
import json
from unittest.mock import MagicMock

from scripts import stage9_import as s9

OK = [{"response": {"status": {"status_code": "OK"}}}]
BAD = [{"response": {"status": {"status_code": "ERROR", "error_message": "boom"}}}]


def _write(tmp_path, name, body):
    (tmp_path / name).write_text(json.dumps(body))
    return name


def _entry(tmp_path, obj_id, folder, otype, body, reason="tagged"):
    return {"obj_id": obj_id, "name": obj_id, "type": otype, "folder": folder,
            "tml_path": _write(tmp_path, f"{obj_id}.tml", {"obj_id": obj_id, **body}),
            "other_tags": [], "reason": reason}


def _client(failing=()):
    c = MagicMock()
    c.find_guid_by_obj_id.return_value = "g"

    def import_tml(docs, **_):
        return BAD if any(f'"obj_id": "{f}"' in docs[0] for f in failing) else OK

    c.import_tml.side_effect = import_tml
    return c


def _statuses(res):
    return {r["obj_id"]: r["status"] for r in res}


def test_consumer_of_a_failed_dependency_is_skipped_transitively(tmp_path):
    table = _entry(tmp_path, "T-1", "tables", "LOGICAL_TABLE", {"table": {}})
    other = _entry(tmp_path, "T-2", "tables", "LOGICAL_TABLE", {"table": {}})
    model = _entry(tmp_path, "M-1", "models", "LOGICAL_TABLE",
                   {"model": {"model_tables": [{"name": "T", "obj_id": "T-1"}]}})
    board = _entry(tmp_path, "L-1", "liveboards", "LIVEBOARD",
                   {"liveboard": {"visualizations": [
                       {"answer": {"tables": [{"name": "M", "obj_id": "M-1"}]}}]}})
    c, failed = _client(failing=["T-1"]), set()
    res = []
    for tier in ([table, other], [model], [board]):
        res += s9.import_tier(c, tier, {}, tmp_path, failed)
    assert _statuses(res) == {"T-1": "failed", "T-2": "success",
                              "M-1": "skipped", "L-1": "skipped"}
    skipped = {r["obj_id"]: r["reason"] for r in res if r["status"] == "skipped"}
    assert "T-1" in skipped["M-1"] and "M-1" in skipped["L-1"]
    imported = [d[0][0] for d in c.import_tml.call_args_list]
    assert not any('"obj_id": "M-1"' in d or '"obj_id": "L-1"' in d for d in imported)


def test_a_dependency_is_still_imported_when_its_consumer_failed(tmp_path):
    """The old check skipped an entry tagged dependency_of:<parent> if the parent failed."""
    dep = _entry(tmp_path, "T-1", "tables", "LOGICAL_TABLE", {"table": {}},
                 reason="dependency_of:M-1")
    c = _client()
    res = s9.import_tier(c, [dep], {}, tmp_path, {"M-1"})       # consumer M-1 already failed
    assert _statuses(res) == {"T-1": "success"}


def test_objects_with_no_failed_dependencies_import_normally(tmp_path):
    model = _entry(tmp_path, "M-1", "models", "LOGICAL_TABLE",
                   {"model": {"model_tables": [{"name": "T", "obj_id": "T-1"}]}})
    res = s9.import_tier(_client(), [model], {}, tmp_path, {"UNRELATED"})
    assert _statuses(res) == {"M-1": "success"}
