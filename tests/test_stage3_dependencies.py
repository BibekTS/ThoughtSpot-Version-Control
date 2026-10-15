"""Stage 3 must find what a tagged object is built on (its model, its tables), not nothing."""
import glob
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from scripts import stage3_discover as s3
from scripts.lib.report import RunReport
from scripts.lib.tml_utils import extract_refs
from scripts.stage1_config import ConfigError, load_and_validate_config


# --------------------------------------------------------------------------- fakes
def _ref(oid, name="n"):
    return {"id": "x", "name": name, "obj_id": oid}


# liveboard LB-1 -> model M-1 -> tables T-1, T-2 ; T-1 joins T-3 ; T-3 joins back to T-1 (cycle)
TML = {
    "g-lb": {"obj_id": "LB-1", "liveboard": {"visualizations": [
        {"answer": {"tables": [_ref("M-1", "Model")]}},
        {"answer": {"tables": [_ref("M-1", "Model")]}}]}},          # referenced twice
    "g-m":  {"obj_id": "M-1", "model": {"model_tables": [
        {"name": "T1", "obj_id": "T-1"}, {"name": "T2", "obj_id": "T-2"}]}},
    "g-t1": {"obj_id": "T-1", "table": {
        "connection": {"name": "Snow", "obj_id": "C-1"},
        "joins_with": [{"destination": {"name": "T3", "obj_id": "T-3"}}]}},
    "g-t2": {"obj_id": "T-2", "table": {"connection": {"name": "Snow", "obj_id": "C-1"}}},
    "g-t3": {"obj_id": "T-3", "table": {
        "connection": {"name": "Snow", "obj_id": "C-1"},
        "joins_with": [{"destination": {"name": "T1", "obj_id": "T-1"}}]}},
}
CATALOG = {
    "M-1": {"metadata_id": "g-m", "metadata_name": "Model", "metadata_obj_id": "M-1",
            "metadata_header": {"type": "WORKSHEET"}},
    "T-1": {"metadata_id": "g-t1", "metadata_name": "T1", "metadata_obj_id": "T-1",
            "metadata_header": {"type": "TABLE"}},
    "T-2": {"metadata_id": "g-t2", "metadata_name": "T2", "metadata_obj_id": "T-2",
            "metadata_header": {"type": "TABLE"}},
    "T-3": {"metadata_id": "g-t3", "metadata_name": "T3", "metadata_obj_id": "T-3",
            "metadata_header": {"type": "TABLE"}},
}


class FakeSource:
    def __init__(self, tml=TML, catalog=CATALOG, tagged=None, export_fail=()):
        self.tml, self.catalog, self.export_fail = tml, catalog, set(export_fail)
        self.tagged = tagged or {}
        self.exports, self.lookups, self.search_bodies = [], [], []

    def export_tml(self, metadata, **kw):
        guid = metadata[0]["identifier"]
        self.exports.append(guid)
        if guid in self.export_fail:
            raise RuntimeError("export boom")
        return [{"edoc": json.dumps(self.tml[guid])}]

    def post(self, path, body):
        assert path == "/metadata/search"
        self.search_bodies.append(body)
        resp = MagicMock(); resp.status_code = 200
        md = body["metadata"][0]
        if "obj_identifier" in md:
            self.lookups.append(md["obj_identifier"])
            item = self.catalog.get(md["obj_identifier"])
            resp.json.return_value = [item] if item else []
        else:
            resp.json.return_value = self.tagged.get(md["type"], [])
        return resp


class FakeTarget:
    def __init__(self, existing=()):
        self.existing = set(existing)

    def find_model_guid(self, obj_id):
        return f"tgt-{obj_id}" if obj_id in self.existing else None


def _report():
    return RunReport(org_key="o", run_id="r", mode="LIVE", source_url="s", source_org_id=1,
                     target_url="t", target_org_id=2, migration_tag="t")


def _tagged(obj_id, guid, name, kind="LIVEBOARD"):
    return {"type": kind, "folder": "liveboards", "name": name, "obj_id": obj_id,
            "source_guid": guid, "reason": "tagged", "other_tags": []}


def _by_id(manifest):
    return {e["obj_id"]: e for e in manifest}


LB = _tagged("LB-1", "g-lb", "LB")


# ------------------------------------------------------------------- include mode
def test_include_pulls_the_whole_dependency_chain(capsys):
    src = FakeSource()
    m = s3.apply_dependency_mode(src, [dict(LB)], "include", _report())
    by = _by_id(m)
    assert sorted(by) == ["LB-1", "M-1", "T-1", "T-2", "T-3"]
    assert by["LB-1"]["reason"] == "tagged"
    assert by["M-1"]["reason"] == "dependency_of:LB-1" and by["M-1"]["folder"] == "models"
    assert by["T-1"]["reason"] == "dependency_of:M-1" and by["T-1"]["folder"] == "tables"
    assert by["T-3"]["reason"] == "dependency_of:T-1"       # reached through a join
    assert all(by[o]["type"] == "LOGICAL_TABLE" for o in ("M-1", "T-1", "T-2", "T-3"))
    assert by["T-1"]["source_guid"] == "g-t1" and by["T-1"]["other_tags"] == []


def test_connections_are_not_treated_as_dependencies():
    m = s3.apply_dependency_mode(FakeSource(), [dict(LB)], "include", _report())
    assert "C-1" not in _by_id(m)


def test_cycles_and_repeated_references_do_not_loop_or_duplicate():
    src = FakeSource()
    m = s3.apply_dependency_mode(src, [dict(LB)], "include", _report())
    assert len(m) == len({e["obj_id"] for e in m}) == 5
    assert sorted(src.exports) == sorted(set(src.exports))      # each object exported once
    assert src.lookups.count("M-1") == 1                         # referenced twice, looked up once


def test_an_object_that_is_also_tagged_stays_tagged_and_is_not_duplicated():
    tagged_model = {**_tagged("M-1", "g-m", "Model", "LOGICAL_TABLE"), "folder": "models"}
    m = s3.apply_dependency_mode(FakeSource(), [dict(LB), tagged_model], "include", _report())
    assert [e["obj_id"] for e in m].count("M-1") == 1
    assert _by_id(m)["M-1"]["reason"] == "tagged"


def test_no_references_adds_nothing():
    tml = {"g-t2": TML["g-t2"]}
    t2 = {**_tagged("T-2", "g-t2", "T2", "LOGICAL_TABLE"), "folder": "tables"}
    assert s3.apply_dependency_mode(FakeSource(tml=tml), [t2], "include", _report()) == [t2]


def test_dependency_missing_from_source_is_warned_and_skipped(capsys):
    cat = {k: v for k, v in CATALOG.items() if k != "T-2"}
    m = s3.apply_dependency_mode(FakeSource(catalog=cat), [dict(LB)], "include", _report())
    assert "T-2" not in _by_id(m) and "T-1" in _by_id(m)
    err = capsys.readouterr().err
    assert "'T-2'" in err and "not found in the source" in err


def test_unreadable_dependencies_do_not_crash_discovery(capsys):
    m = s3.apply_dependency_mode(FakeSource(export_fail={"g-t1"}), [dict(LB)], "include", _report())
    assert {"M-1", "T-1", "T-2"} <= set(_by_id(m))               # T-1 itself is still pulled
    assert "T-3" not in _by_id(m)                                # but what it joins is unknown
    assert "Could not read dependencies" in capsys.readouterr().err


def test_dependency_exclude_is_never_pulled_or_looked_up():
    src = FakeSource()
    m = s3.apply_dependency_mode(src, [dict(LB)], "include", _report(), exclude={"T-2", "M-1"})
    assert sorted(_by_id(m)) == ["LB-1"]                         # M-1 excluded => nothing below it
    assert "M-1" not in src.lookups and "T-2" not in src.lookups


# ---------------------------------------------------------------- skip_if_exists
def test_skip_if_exists_leaves_out_what_the_target_already_has(capsys):
    m = s3.apply_dependency_mode(FakeSource(), [dict(LB)], "skip_if_exists", _report(),
                                 target_client=FakeTarget(existing={"T-1"}))
    assert sorted(_by_id(m)) == ["LB-1", "M-1", "T-2"]           # T-1 skipped, T-3 only via T-1
    assert "already exists in the target" in capsys.readouterr().out


def test_skip_if_exists_without_target_client_falls_back_to_include(capsys):
    m = s3.apply_dependency_mode(FakeSource(), [dict(LB)], "skip_if_exists", _report())
    assert len(m) == 5
    assert "treating as 'include'" in capsys.readouterr().err


# ---------------------------------------------------------------- require_tagged
def test_require_tagged_excludes_objects_with_untagged_dependencies_and_their_parents(capsys):
    model = {**_tagged("M-1", "g-m", "Model", "LOGICAL_TABLE"), "folder": "models"}
    rep = _report()
    m = s3.apply_dependency_mode(FakeSource(), [dict(LB), model], "require_tagged", rep)
    assert m == []                                               # model (untagged tables), then LB
    assert {r.obj_id for r in rep.failed} == {"M-1", "LB-1"}
    assert all(r.stage == "discover" for r in rep.failed)
    err = capsys.readouterr().err
    assert "'T1' (T-1)" in err and "depends on an object that was excluded" in err


def test_require_tagged_keeps_everything_when_all_dependencies_are_tagged():
    objs = [dict(LB), {**_tagged("M-1", "g-m", "Model", "LOGICAL_TABLE"), "folder": "models"}]
    objs += [{**_tagged(o, g, o, "LOGICAL_TABLE"), "folder": "tables"}
             for o, g in (("T-1", "g-t1"), ("T-2", "g-t2"), ("T-3", "g-t3"))]
    rep = _report()
    assert s3.apply_dependency_mode(FakeSource(), objs, "require_tagged", rep) == objs
    assert rep.failed == []


def test_require_tagged_honors_dependency_exclude():
    model = {**_tagged("M-1", "g-m", "Model", "LOGICAL_TABLE"), "folder": "models"}
    rep = _report()
    m = s3.apply_dependency_mode(FakeSource(), [dict(LB), model], "require_tagged", rep,
                                 exclude={"T-1", "T-2"})
    assert [e["obj_id"] for e in m] == ["LB-1", "M-1"] and rep.failed == []


def test_require_tagged_keeps_objects_whose_dependencies_cannot_be_read(capsys):
    m = s3.apply_dependency_mode(FakeSource(export_fail={"g-lb"}), [dict(LB)], "require_tagged", _report())
    assert [e["obj_id"] for e in m] == ["LB-1"]
    assert "keeping it" in capsys.readouterr().err


def test_unknown_mode_changes_nothing():
    assert s3.apply_dependency_mode(FakeSource(), [dict(LB)], "bogus", _report()) == [LB]


# ------------------------------------------------------------- discover() end to end
def test_discover_finds_dependencies_and_never_relies_on_dependent_objects():
    lb_row = {"metadata_id": "g-lb", "metadata_name": "LB", "metadata_obj_id": "LB-1",
              "metadata_header": {}, "tags": []}
    src = FakeSource(tagged={"LIVEBOARD": [lb_row]})
    cfg = {"migration_tag": "t", "dependency_mode": "include", "dependency_exclude": ["T-3"]}
    m = s3.discover(src, cfg, _report())
    assert sorted(_by_id(m)) == ["LB-1", "M-1", "T-1", "T-2"]    # T-3 excluded via config
    assert _by_id(m)["LB-1"]["reason"] == "tagged"
    # the search only ever asks for tagged objects and per-obj_id lookups
    assert not any("include_dependent_objects" in b for b in src.search_bodies)


# --------------------------------------------------------------------- Stage 1
def _write_cfg(tmp_path, extra=""):
    p = tmp_path / "c.yml"
    p.write_text("org_key: o\nmigration_tag: t\nsource: {base_url: https://a, org_id: 1}\n"
                 "target: {base_url: https://b, org_id: 2}\n" + extra)
    return str(p)


def test_stage1_accepts_missing_or_valid_dependency_exclude(tmp_path):
    assert "dependency_exclude" not in load_and_validate_config(_write_cfg(tmp_path))
    ok = load_and_validate_config(_write_cfg(tmp_path, "dependency_exclude: [TSBIServer-eaab6de7]\n"))
    assert ok["dependency_exclude"] == ["TSBIServer-eaab6de7"]


@pytest.mark.parametrize("bad", ["dependency_exclude: abc\n", "dependency_exclude: [1, 2]\n",
                                 "dependency_exclude: ['']\n"])
def test_stage1_rejects_malformed_dependency_exclude(tmp_path, bad):
    with pytest.raises(ConfigError, match="dependency_exclude"):
        load_and_validate_config(_write_cfg(tmp_path, bad))


# ------------------------------------------------------------------ extract_refs
def test_extract_refs_finds_each_reference_shape_and_skips_self_and_connection():
    tml = {"obj_id": "ME", "table": {
        "connection": {"name": "C", "obj_id": "CONN"},
        "joins_with": [{"destination": {"name": "J", "obj_id": "J-1"}}],
        "rls_rules": {"tables": [{"name": "me", "obj_id": "ME"}]}}}
    assert extract_refs(tml) == [{"obj_id": "J-1", "name": "J"}]
    assert [r["obj_id"] for r in extract_refs(TML["g-lb"])] == ["M-1"]      # de-duplicated
    assert [r["obj_id"] for r in extract_refs(TML["g-m"])] == ["T-1", "T-2"]
    assert extract_refs({"obj_id": "X", "answer": {}}) == []


REPO = Path(__file__).resolve().parent.parent


@pytest.mark.skipif(not glob.glob(str(REPO / "orgs/github/models/retail_apparel_with_ago__*[0-9a-f].table.tml")),
                    reason="real exported TML not present")
def test_extract_refs_on_real_exported_tml():
    """Real exports from the github org: a model lists its tables; no connection leaks in."""
    f = REPO / "orgs/github/models/retail_apparel_with_ago__RetailApparelwithAGOWithInstructions-b0d8d902.table.tml"
    refs = {r["obj_id"] for r in extract_refs(json.loads(f.read_text()))}
    assert {"DIM_RETAPP_PRODUCTS-ad7d476f", "DIM_RETAPP_STORES-8cd69308",
            "FACT_RETAPP_SALES-1277592f"} <= refs
    assert "RetailApparelwithAGOWithInstructions-b0d8d902" not in refs      # not itself
    for t in glob.glob(str(REPO / "orgs/github/tables/*[0-9a-f].table.tml")):
        d = json.loads(Path(t).read_text())
        conn = (d.get("table", {}).get("connection") or {}).get("obj_id")
        assert conn not in {r["obj_id"] for r in extract_refs(d)}
