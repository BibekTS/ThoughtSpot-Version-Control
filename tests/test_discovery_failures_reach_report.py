"""Problems found while discovering objects must reach the run report, not just the log."""
import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml

from scripts import stage11_report as s11
from scripts import stage3_discover as s3
from tests.test_stage3_dependencies import CATALOG, FakeSource, TML, _report, _tagged

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "org-migration.yml"
LB = _tagged("LB-1", "g-lb", "LB")


# ------------------------------------------------------------------- Stage 3 records them
def test_a_dependency_missing_from_the_source_is_a_failure_not_just_a_warning():
    cat = {k: v for k, v in CATALOG.items() if k != "T-2"}
    rep = _report()
    s3.apply_dependency_mode(FakeSource(catalog=cat), [dict(LB)], "include", rep)
    [f] = rep.failed
    assert (f.obj_id, f.stage, f.status) == ("T-2", "discover", "failed")
    assert f.reason.startswith("dependency_missing") and "M-1" in f.error
    assert "dependency_exclude" in f.suggested_action


def test_unreadable_dependencies_are_a_failure_on_the_parent():
    rep = _report()
    s3.apply_dependency_mode(FakeSource(export_fail={"g-t1"}), [dict(LB)], "include", rep)
    [f] = rep.failed
    assert (f.obj_id, f.reason, f.stage) == ("T-1", "dependencies_unreadable", "discover")


def test_excluded_and_skipped_dependencies_are_not_failures():
    rep = _report()
    s3.apply_dependency_mode(FakeSource(), [dict(LB)], "include", rep, exclude={"T-2"})
    assert rep.failed == []


# ---------------------------------------------------- Stage 3 main() writes the failures file
def _run_stage3(tmp_path, tagged, cfg_extra=None, monkeypatch=None):
    import lib.ts_client as client_module           # the module Stage 3 takes TSClient from
    cfg = {"org_key": "o", "migration_tag": "t", "dependency_mode": "include",
           "source": {"base_url": "https://s", "org_id": 1},
           "target": {"base_url": "https://t", "org_id": 2}, **(cfg_extra or {})}
    (tmp_path / "cfg.json").write_text(json.dumps(cfg))
    src = FakeSource(tagged=tagged)
    src.authenticate = lambda: None
    env = dict(ORG_KEY="o", CONFIG_OUT=str(tmp_path / "cfg.json"),
               MANIFEST_OUT=str(tmp_path / "manifest.json"),
               DISCOVER_OUT=str(tmp_path / "discover.json"),
               TS_SOURCE_USERNAME="u", TS_SOURCE_SECRET_KEY="k")
    with patch.dict(os.environ, env), patch.object(client_module, "TSClient", lambda *a, **k: src):
        s3.main()
    return (json.loads((tmp_path / "manifest.json").read_text()),
            json.loads((tmp_path / "discover.json").read_text()))


def test_stage3_main_writes_object_without_obj_id_to_the_failures_file(tmp_path):
    tagged = {"LIVEBOARD": [{"metadata_id": "g-x", "metadata_name": "No Id", "metadata_obj_id": "",
                             "metadata_header": {}, "tags": []}]}
    manifest, failures = _run_stage3(tmp_path, tagged)
    assert manifest == []                                   # excluded, as before
    assert [(f["name"], f["reason"], f["stage"], f["status"]) for f in failures] == \
           [("No Id", "missing_obj_id", "discover", "failed")]


def test_stage3_main_writes_an_empty_list_when_nothing_failed(tmp_path):
    row = {"metadata_id": "g-lb", "metadata_name": "LB", "metadata_obj_id": "LB-1",
           "metadata_header": {}, "tags": []}
    manifest, failures = _run_stage3(tmp_path, {"LIVEBOARD": [row]})
    assert len(manifest) == 5 and failures == []


# ----------------------------------------------------------- Stage 11 reports and goes red
def _discover_failure(**kw):
    return {"obj_type": "LOGICAL_TABLE", "name": "T2", "obj_id": "T-2", "source_guid": "",
            "reason": "dependency_missing: needed by M-1", "other_tags": [], "stage": "discover",
            "status": "failed", "error": "not found", "endpoint": "", "http_status": 0,
            "ts_error_code": "", "tml_path": "", "suggested_action": "check", **kw}


CFG = {"org_key": "o", "migration_tag": "t", "source": {"base_url": "s", "org_id": 1},
       "target": {"base_url": "t", "org_id": 2}}


def test_build_report_includes_discovery_failures():
    rep = s11.build_report(CFG, "r", False, [], [], [], {}, [], None, [_discover_failure()])
    assert [(r.obj_id, r.stage, r.status) for r in rep.failed] == [("T-2", "discover", "failed")]


def test_build_report_tolerates_unknown_and_missing_fields():
    rep = s11.build_report(CFG, "r", False, [], [], [], {}, [], None,
                           [{"name": "X", "obj_type": "ANSWER", "obj_id": "x", "future_field": 1}])
    assert rep.failed[0].name == "X"


def test_stage11_main_turns_red_and_writes_the_failure(tmp_path, monkeypatch):
    (tmp_path / "cfg.json").write_text(json.dumps(CFG))
    (tmp_path / "discover.json").write_text(json.dumps([_discover_failure()]))
    for k, v in dict(ORG_KEY="o", RUN_ID="r1", TIMESTAMP="TS1", REPO_ROOT=str(tmp_path),
                     CONFIG_OUT=str(tmp_path / "cfg.json"), DISCOVER_OUT=str(tmp_path / "discover.json"),
                     MANIFEST_OUT=str(tmp_path / "n1"), EXPORT_OUT=str(tmp_path / "n2"),
                     IMPORT_OUT=str(tmp_path / "n3"), POSTCHECK_OUT=str(tmp_path / "n4"),
                     PROD_PREP_OUT=str(tmp_path / "n5"), MEMORY_EXPORT_OUT=str(tmp_path / "n6"),
                     MEMORY_IMPORT_OUT=str(tmp_path / "n7"), INSTRUCTIONS_EXPORT_OUT=str(tmp_path / "n8"),
                     INSTRUCTIONS_IMPORT_OUT=str(tmp_path / "n9")).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    with pytest.raises(SystemExit) as e:
        s11.main()
    assert e.value.code == 1
    failures = json.loads((tmp_path / "reports/o/TS1_r1/failures.json").read_text())
    assert [(f["obj_id"], f["stage"], f["error"]) for f in failures] == [("T-2", "discover", "not found")]


def test_stage11_main_is_green_without_a_discover_file(tmp_path, monkeypatch):
    (tmp_path / "cfg.json").write_text(json.dumps(CFG))
    for k, v in dict(ORG_KEY="o", RUN_ID="r1", TIMESTAMP="TS1", REPO_ROOT=str(tmp_path),
                     CONFIG_OUT=str(tmp_path / "cfg.json"), DISCOVER_OUT=str(tmp_path / "missing"),
                     MANIFEST_OUT=str(tmp_path / "n1"), EXPORT_OUT=str(tmp_path / "n2"),
                     IMPORT_OUT=str(tmp_path / "n3"), POSTCHECK_OUT=str(tmp_path / "n4"),
                     PROD_PREP_OUT=str(tmp_path / "n5"), MEMORY_EXPORT_OUT=str(tmp_path / "n6"),
                     MEMORY_IMPORT_OUT=str(tmp_path / "n7"), INSTRUCTIONS_EXPORT_OUT=str(tmp_path / "n8"),
                     INSTRUCTIONS_IMPORT_OUT=str(tmp_path / "n9")).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    s11.main()                                              # no SystemExit


# -------------------------------------------------------------------- the workflow wiring
def test_workflow_carries_the_discovery_failures_file_from_extract_to_report():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]
    assert jobs["extract"]["env"]["DISCOVER_OUT"] == "stage3_discover_failures.json"
    assert jobs["report"]["env"]["DISCOVER_OUT"] == "stage3_discover_failures.json"
    paths = "\n".join(str(st.get("with", {}).get("path", ""))
                      for st in jobs["extract"]["steps"] if "upload-artifact" in str(st.get("uses", "")))
    assert "stage3_discover_failures.json" in paths
