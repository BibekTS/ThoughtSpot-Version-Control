"""Within a tier, an object imports after the same-tier objects it references."""
import json

from scripts import stage9_import as s9


def _entry(tmp_path, obj_id, refs=(), key="model_tables", bad=False):
    name = f"{obj_id}.tml"
    if bad:
        (tmp_path / name).write_text("{ not json")
    else:
        body = {"obj_id": obj_id, "model": {key: [{"name": r, "obj_id": r} for r in refs]}}
        (tmp_path / name).write_text(json.dumps(body))
    return {"obj_id": obj_id, "name": obj_id, "type": "LOGICAL_TABLE", "folder": "models",
            "tml_path": name}


def _ids(entries):
    return [e["obj_id"] for e in entries]


def test_a_dependency_in_the_same_tier_comes_first(tmp_path):
    entries = [_entry(tmp_path, "AGO", ["DIM"]), _entry(tmp_path, "DIM")]   # tagged first, as in Stage 3
    assert _ids(s9.order_by_dependencies(entries, tmp_path)) == ["DIM", "AGO"]


def test_chains_and_manifest_order_are_kept_otherwise(tmp_path):
    entries = [_entry(tmp_path, "A", ["B"]), _entry(tmp_path, "X"),
               _entry(tmp_path, "B", ["C"]), _entry(tmp_path, "C"), _entry(tmp_path, "Y")]
    assert _ids(s9.order_by_dependencies(entries, tmp_path)) == ["C", "B", "A", "X", "Y"]


def test_references_outside_the_tier_are_ignored(tmp_path):
    entries = [_entry(tmp_path, "M1", ["SOME-TABLE"]), _entry(tmp_path, "M2")]
    assert _ids(s9.order_by_dependencies(entries, tmp_path)) == ["M1", "M2"]


def test_a_cycle_does_not_loop_or_drop_objects(tmp_path):
    entries = [_entry(tmp_path, "A", ["B"]), _entry(tmp_path, "B", ["A"])]
    assert sorted(_ids(s9.order_by_dependencies(entries, tmp_path))) == ["A", "B"]


def test_unreadable_or_missing_tml_means_no_dependencies(tmp_path):
    entries = [_entry(tmp_path, "A", bad=True), {"obj_id": "B", "name": "B", "tml_path": ""},
               _entry(tmp_path, "C")]
    assert _ids(s9.order_by_dependencies(entries, tmp_path)) == ["A", "B", "C"]


def test_table_joins_are_ordered_too(tmp_path):
    a = {"obj_id": "T-A", "name": "A", "tml_path": "a.tml"}
    (tmp_path / "a.tml").write_text(json.dumps(
        {"obj_id": "T-A", "table": {"joins_with": [{"destination": {"name": "B", "obj_id": "T-B"}}]}}))
    b = {"obj_id": "T-B", "name": "B", "tml_path": "b.tml"}
    (tmp_path / "b.tml").write_text(json.dumps({"obj_id": "T-B", "table": {}}))
    assert _ids(s9.order_by_dependencies([a, b], tmp_path)) == ["T-B", "T-A"]
