"""Stage 8 validation failures gate the import (Stage 9) and reach the report (Stage 11)."""
import json
from types import SimpleNamespace

import pytest

from scripts import stage8_prod_prep as s8
from scripts import stage9_import as s9
from scripts import stage11_report as s11
from scripts.lib.prod_prep import blocked_objects
from scripts.stage1_config import ConfigError, load_and_validate_config

CFG = {
    "org_key": "o", "migration_tag": "o_ready",
    "source": {"base_url": "https://s", "org_id": 1},
    "target": {"base_url": "https://t", "org_id": 2},
    "variables": {"database": {"ts_variable_name": "db", "source_value": "A", "target_value": "B"}},
}


def _resp(*statuses_and_ids):
    return [{"response": {"status": {"status_code": s, "error_message": f"bad {i}"},
                          "header": {"obj_id_ref": i}}} for s, i in statuses_and_ids]


# ---- Stage 8: validation issues name the object ------------------------------------------ #

def test_validation_failure_names_object_from_response():
    client = SimpleNamespace(import_tml=lambda *a, **k: _resp(("OK", "a"), ("ERROR", "b")))
    issues = s8.validate_import(client, ["x", "y"], ["a", "b"])
    assert [(i["severity"], i["obj_id"]) for i in issues] == [("VALIDATION_FAILED", "b")]


def test_validation_failure_falls_back_to_position_when_response_has_no_obj_id():
    client = SimpleNamespace(import_tml=lambda *a, **k: _resp(("OK", ""), ("ERROR", "")))
    issues = s8.validate_import(client, ["x", "y"], ["a", "b"])
    assert issues[0]["obj_id"] == "b"


def test_no_position_fallback_when_counts_differ():
    client = SimpleNamespace(import_tml=lambda *a, **k: _resp(("ERROR", "")))
    issues = s8.validate_import(client, ["x", "y"], ["a", "b"])
    assert issues[0]["obj_id"] == ""


def test_blocked_objects_ignores_unattributed_and_other_severities():
    issues = [{"severity": "VALIDATION_FAILED", "obj_id": "b", "message": "m"},
              {"severity": "VALIDATION_FAILED", "obj_id": "", "message": "no object"},
              {"severity": "ERROR", "obj_id": "c", "message": "not validation"}]
    assert blocked_objects(issues) == {"b": "m"}


# ---- Stage 8: a dry run writes no variables ---------------------------------------------- #

class VarClient:
    def __init__(self, existing=()):
        self.existing, self.writes = list(existing), []

    def search_variables(self, name_pattern=None):
        return [v for v in self.existing if not name_pattern or name_pattern in v["name"]]

    def create_variable(self, *a, **k):
        self.writes.append(("create", a))

    def set_variable_org_value(self, *a, **k):
        self.writes.append(("set", a))


def test_dry_run_writes_nothing_and_reports_what_it_would_do():
    client = VarClient()
    issues = s8.sync_variables(client, CFG, set(), dry_run=True)
    assert client.writes == []
    msgs = [i["message"] for i in issues if i["severity"] == "INFO"]
    assert any("would create variable 'db'" in m for m in msgs)
    assert not [i for i in issues if i["severity"] == "ERROR"]


def test_dry_run_does_not_set_values_of_existing_variables():
    client = VarClient([{"name": "db", "variable_type": "TABLE_MAPPING"}])
    issues = s8.sync_variables(client, CFG, set(), dry_run=True)
    assert client.writes == []
    assert any("would set the org value" in i["message"] for i in issues)


def test_live_run_still_writes():
    client = VarClient()
    s8.sync_variables(client, CFG, set())
    assert [w[0] for w in client.writes] == ["create", "set"]


# ---- Stage 9: blocked objects are skipped, and so is what depends on them ---------------- #

def _entry(obj_id, **kw):
    return {"obj_id": obj_id, "name": obj_id, "type": "LIVEBOARD", "folder": "liveboards",
            "tml_path": f"orgs/o/{obj_id}.tml", **kw}


def _write_tml(tmp_path, obj_id, body):
    p = tmp_path / "orgs" / "o" / f"{obj_id}.tml"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"obj_id": obj_id, **body}))


def test_blocked_object_is_skipped_not_imported(tmp_path):
    _write_tml(tmp_path, "a", {"liveboard": {}})
    _write_tml(tmp_path, "b", {"liveboard": {}})
    imported = []

    def import_tml(strings, **k):
        imported.append(strings)
        return _resp(("OK", "a"))

    client = SimpleNamespace(import_tml=import_tml)
    failed = set()
    res = s9.import_tier(client, [_entry("a"), _entry("b")], CFG, tmp_path, failed,
                         blocked={"b": "bad column"})
    by_id = {r["obj_id"]: r for r in res}
    assert by_id["a"]["status"] == "success"
    assert by_id["b"]["status"] == "skipped"
    assert by_id["b"]["reason"] == "validation failed in Stage 8: bad column"
    assert len(imported) == 1 and "b" in failed


def test_object_depending_on_a_blocked_one_is_skipped_too(tmp_path):
    _write_tml(tmp_path, "T-1", {"table": {}})
    _write_tml(tmp_path, "M-1", {"model": {"model_tables": [{"name": "t", "obj_id": "T-1"}]}})
    client = SimpleNamespace(import_tml=lambda *a, **k: pytest.fail("nothing should import"))
    failed = set()
    tables = [{**_entry("T-1", type="LOGICAL_TABLE"), "folder": "tables"}]
    models = [{**_entry("M-1", type="LOGICAL_TABLE"), "folder": "models"}]
    res = s9.import_tier(client, tables, CFG, tmp_path, failed, blocked={"T-1": "bad"})
    res += s9.import_tier(client, models, CFG, tmp_path, failed, blocked={"T-1": "bad"})
    assert [r["status"] for r in res] == ["skipped", "skipped"]
    assert "dependency T-1 failed" in res[1]["reason"]


# ---- Stage 11: report ------------------------------------------------------------------- #

def _report(prod_prep, import_results=(), dry_run=False):
    manifest = [{"obj_id": "a", "name": "A", "type": "LIVEBOARD"},
                {"obj_id": "b", "name": "B", "type": "LIVEBOARD"}]
    return s11.build_report(CFG, "r1", dry_run, manifest,
                            [{"obj_id": "a", "status": "success", "tml_path": "a"},
                             {"obj_id": "b", "status": "success", "tml_path": "b"}],
                            list(import_results), {}, prod_prep)


def test_report_lists_blocked_object_as_skipped_with_error_reason():
    rep = _report([{"severity": "VALIDATION_FAILED", "obj_id": "b", "message": "bad"}],
                  [{"obj_id": "a", "status": "success"},
                   {"obj_id": "b", "status": "skipped",
                    "reason": "validation failed in Stage 8: bad"}])
    failed, error_skipped = s11.run_failures(rep)
    assert not failed and [r.obj_id for r in error_skipped] == ["b"]


def test_report_fails_a_validation_failure_with_no_import_result_even_on_a_dry_run():
    rep = _report([{"severity": "VALIDATION_FAILED", "obj_id": "b", "message": "bad"}],
                  dry_run=True)
    failed, _ = s11.run_failures(rep)
    assert [(r.obj_id, r.stage) for r in failed] == [("b", "validation")]


def test_report_counts_non_connection_stage8_errors():
    rep = _report([{"severity": "ERROR", "variable": "v", "message": "type mismatch"},
                   {"severity": "ERROR", "message": "Validate-only import failed: boom"},
                   {"severity": "VALIDATION_FAILED", "obj_id": "", "message": "unattributed"},
                   {"severity": "WARNING", "message": "not an error"},
                   {"severity": "INFO", "variable": "v", "message": "DRY RUN: would create"}],
                  [{"obj_id": "a", "status": "success"}, {"obj_id": "b", "status": "success"}])
    failed, _ = s11.run_failures(rep)
    assert sorted(r.obj_type for r in failed) == ["PROD_PREP", "PROD_PREP", "VARIABLE"]


# ---- Stage 1: validation_gate config ------------------------------------------------------ #

@pytest.mark.parametrize("value,ok", [("block", True), ("warn", True), ("off", False)])
def test_validation_gate_config(tmp_path, value, ok):
    import yaml
    p = tmp_path / "o.yml"
    p.write_text(yaml.safe_dump({**CFG, "validation_gate": value}))
    if ok:
        assert load_and_validate_config(str(p))["validation_gate"] == value
    else:
        with pytest.raises(ConfigError):
            load_and_validate_config(str(p))


# ---- Stage 9 main(): reads prod_prep.json and honors validation_gate ---------------------- #

@pytest.mark.parametrize("gate,imported", [("block", ["a"]), ("warn", ["a", "b"])])
def test_main_applies_the_gate(tmp_path, monkeypatch, gate, imported):
    for oid in ("a", "b"):
        _write_tml(tmp_path, oid, {"liveboard": {}})
    cfg = {**CFG, "validation_gate": gate}
    files = {
        "config.json": cfg,
        "export.json": [{"obj_id": o, "status": "success", "tml_path": f"orgs/o/{o}.tml"}
                        for o in ("a", "b")],
        "manifest.json": [{"obj_id": o, "name": o, "type": "LIVEBOARD", "folder": "liveboards"}
                          for o in ("a", "b")],
        "prod_prep.json": [{"severity": "VALIDATION_FAILED", "obj_id": "b", "message": "bad"}],
    }
    for name, body in files.items():
        (tmp_path / name).write_text(json.dumps(body))

    seen = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        def authenticate(self): pass
        def import_tml(self, docs, **k):
            seen.append(json.loads(docs[0])["obj_id"])
            return _resp(("OK", seen[-1]))

    monkeypatch.setattr(s9, "TSClient", FakeClient)
    monkeypatch.setattr(s9, "resolve_secret", lambda name: "x")
    for k, v in {"ORG_KEY": "o", "CONFIG_OUT": "config.json", "EXPORT_OUT": "export.json",
                 "MANIFEST_OUT": "manifest.json", "PROD_PREP_OUT": "prod_prep.json",
                 "IMPORT_OUT": "import.json", "REPO_ROOT": str(tmp_path),
                 "DRY_RUN": "false"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.chdir(tmp_path)
    s9.main()
    assert seen == imported
    out = {r["obj_id"]: r["status"] for r in json.loads((tmp_path / "import.json").read_text())}
    assert out["b"] == ("skipped" if gate == "block" else "success")
