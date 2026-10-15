"""A failed memory / NL-instructions migration must show up in the report and keep the tag."""
import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import yaml

from scripts import stage10_postcheck as s10
from scripts import stage11_report as s11
from scripts.lib.spotter_results import spotter_outcome

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "org-migration.yml"

OK_MEM_EXPORT = {"skipped": False, "model_count": 2}
OK_MEM_IMPORT = {"skipped": False, "dry_run": False, "validation_status": "SUCCESS",
                 "apply_status": "SUCCESS", "applied": True}
OK_INS_EXPORT = {"skipped": False, "exported_count": 1, "failed": []}
OK_INS_IMPORT = {"skipped": False, "dry_run": False, "applied": 1, "failed": 0,
                 "results": [{"obj_id": "M-1", "name": "M", "status": "applied"}]}


def outcome(me=OK_MEM_EXPORT, mi=OK_MEM_IMPORT, ie=OK_INS_EXPORT, ii=OK_INS_IMPORT):
    return spotter_outcome(me, mi, ie, ii)


# ------------------------------------------------------------------ what counts as a failure
def test_all_good_is_not_a_failure():
    o = outcome()
    assert o["failures"] == [] and o["notes"] == []
    assert not o["blocked_all_models"] and o["blocked_obj_ids"] == set()


def test_no_results_at_all_is_not_a_failure():
    o = spotter_outcome({}, {}, {}, {})
    assert o["failures"] == [] and not o["blocked_all_models"]


@pytest.mark.parametrize("me", [
    {"skipped": True, "reason": "no_models"}, {"skipped": True, "reason": "empty_memory"}])
def test_nothing_to_migrate_is_not_a_failure(me):
    assert outcome(me=me)["failures"] == []


def test_empty_memory_and_no_file_on_import_are_not_failures():
    for reason in ("empty_memory", "no_memory_file"):
        assert outcome(mi={"skipped": True, "reason": reason})["failures"] == []
    assert outcome(ie={"skipped": True, "reason": "no_instructions", "failed": []})["failures"] == []


def test_spotter_not_enabled_is_a_note_not_a_failure():
    o = outcome(me={"skipped": True, "reason": "spotter_not_enabled", "http_status": 403},
                mi={"skipped": True, "reason": "Spotter not enabled or insufficient permissions"},
                ie={"skipped": True, "reason": "spotter_not_enabled", "failed": []},
                ii={"skipped": True, "reason": "spotter_not_enabled"})
    assert o["failures"] == [] and len(o["notes"]) == 4
    assert not o["blocked_all_models"]


def test_model_not_in_target_is_not_a_second_failure():
    ii = {"results": [{"obj_id": "M-1", "name": "M", "status": "skipped",
                       "reason": "model_not_in_target"}]}
    assert outcome(ii=ii)["failures"] == []


@pytest.mark.parametrize("reason", ["api_error", "http_error"])
def test_memory_export_error_is_a_failure_for_every_model(reason):
    o = outcome(me={"skipped": True, "reason": reason, "error": "boom", "http_status": 500})
    assert [f["stage"] for f in o["failures"]] == ["spotter_memory_export"]
    assert o["blocked_all_models"]


def test_memory_validation_failure_is_a_failure_even_in_a_dry_run():
    mi = {"skipped": False, "dry_run": True, "validation_status": "VALIDATION_FAILED", "applied": False,
          "validation_failures": [{"message": "bad rule", "line_number": 3}]}
    o = outcome(mi=mi)
    assert o["failures"][0]["stage"] == "spotter_memory_import"
    assert "bad rule" in o["failures"][0]["error"] and o["blocked_all_models"]


def test_dry_run_memory_that_validated_is_fine():
    mi = {"skipped": False, "dry_run": True, "validation_status": "SUCCESS", "applied": False}
    assert outcome(mi=mi)["failures"] == []


def test_live_memory_not_applied_is_a_failure():
    mi = {"skipped": False, "dry_run": False, "validation_status": "SUCCESS",
          "apply_status": "FAILED", "applied": False}
    o = outcome(mi=mi)
    assert "not applied (status FAILED)" in o["failures"][0]["error"] and o["blocked_all_models"]


def test_instructions_failures_are_per_model_and_block_only_that_model():
    ie = {"skipped": False, "failed": [{"obj_id": "M-2", "name": "Two", "error": "HTTP 500"}]}
    ii = {"results": [{"obj_id": "M-1", "name": "One", "status": "failed", "http_status": 403,
                       "error": "no privilege"}, {"obj_id": "M-3", "name": "Three", "status": "applied"}]}
    o = outcome(ie=ie, ii=ii)
    assert {f["obj_id"] for f in o["failures"]} == {"M-1", "M-2"}
    assert o["blocked_obj_ids"] == {"M-1", "M-2"} and not o["blocked_all_models"]


def test_instruction_export_failures_count_even_when_the_export_was_skipped():
    ie = {"skipped": True, "reason": "no_instructions",
          "failed": [{"obj_id": "M-2", "name": "Two", "error": "HTTP 500"}]}
    assert [f["obj_id"] for f in outcome(ie=ie)["failures"]] == ["M-2"]


# ------------------------------------------------------------------ Stage 10 keeps the tag
MANIFEST = [
    {"obj_id": "M-1", "type": "LOGICAL_TABLE", "folder": "models", "name": "One",
     "source_guid": "g1", "reason": "tagged"},
    {"obj_id": "M-2", "type": "LOGICAL_TABLE", "folder": "models", "name": "Two",
     "source_guid": "g2", "reason": "tagged"},
    {"obj_id": "L-1", "type": "LIVEBOARD", "folder": "liveboards", "name": "LB",
     "source_guid": "g3", "reason": "tagged"},
]
IMPORTED = [{"obj_id": o, "status": "success"} for o in ("M-1", "M-2", "L-1")]


def _removed_guids(src):
    return [m["identifier"] for m in src.unassign_tags.call_args.args[0]]


def test_stage10_removes_every_tag_when_nothing_is_blocked(capsys):
    src = MagicMock()
    out = s10.cleanup_tags(src, MANIFEST, IMPORTED, [], "tag")
    assert sorted(_removed_guids(src)) == ["g1", "g2", "g3"] and out["removed"] == 3
    assert out["kept_for_spotter"] == 0


def test_stage10_keeps_the_tag_of_a_model_whose_instructions_failed(capsys):
    src = MagicMock()
    out = s10.cleanup_tags(src, MANIFEST, IMPORTED, [], "tag", blocked_ids={"M-1"})
    assert sorted(_removed_guids(src)) == ["g2", "g3"]
    assert out["removed"] == 2 and out["retained"] == 1 and out["kept_for_spotter"] == 1
    assert "Kept the migration tag on 1 object(s)" in capsys.readouterr().out


def test_stage10_blocking_ignores_objects_that_did_not_import_anyway():
    src = MagicMock()
    imported = [{"obj_id": "M-1", "status": "failed"}, {"obj_id": "M-2", "status": "success"},
                {"obj_id": "L-1", "status": "success"}]
    out = s10.cleanup_tags(src, MANIFEST, imported, [], "tag", blocked_ids={"M-1"})
    assert out["kept_for_spotter"] == 0 and out["retained"] == 1        # M-1 retained for TML failure


def test_stage10_main_blocks_every_model_when_memory_failed(tmp_path, monkeypatch):
    files = {"cfg.json": {"source": {"base_url": "s", "org_id": 1}, "target": {"base_url": "t", "org_id": 2},
                          "migration_tag": "tag"},
             "manifest.json": MANIFEST, "import.json": IMPORTED,
             "mi.json": {"skipped": False, "dry_run": False, "validation_status": "SUCCESS",
                         "apply_status": "FAILED", "applied": False}}
    for name, data in files.items():
        (tmp_path / name).write_text(json.dumps(data))
    env = dict(ORG_KEY="o", DRY_RUN="false", CONFIG_OUT=str(tmp_path / "cfg.json"),
               MANIFEST_OUT=str(tmp_path / "manifest.json"), IMPORT_OUT=str(tmp_path / "import.json"),
               MEMORY_IMPORT_OUT=str(tmp_path / "mi.json"), POSTCHECK_OUT=str(tmp_path / "pc.json"),
               MEMORY_EXPORT_OUT=str(tmp_path / "none1"), INSTRUCTIONS_EXPORT_OUT=str(tmp_path / "none2"),
               INSTRUCTIONS_IMPORT_OUT=str(tmp_path / "none3"),
               TS_TARGET_USERNAME="u", TS_TARGET_SECRET_KEY="k", TS_SOURCE_USERNAME="u",
               TS_SOURCE_SECRET_KEY="k")
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    client = MagicMock()
    monkeypatch.setattr(s10, "TSClient", lambda *a, **k: client)
    monkeypatch.setattr(s10, "post_check", lambda *a, **k: {"by_type": {}, "mismatches": []})
    s10.main()
    assert _removed_guids(client) == ["g3"]            # only the liveboard; both models kept
    assert json.loads((tmp_path / "pc.json").read_text())["tag_cleanup"]["kept_for_spotter"] == 2


# ------------------------------------------------------------------ Stage 11 reports it
def _report(spotter):
    cfg = {"org_key": "o", "migration_tag": "t", "source": {"base_url": "s", "org_id": 1},
           "target": {"base_url": "t", "org_id": 2}}
    return s11.build_report(cfg, "r", False, [], [], [], {}, [], spotter)


def test_stage11_lists_spotter_failures_and_they_make_the_run_red():
    ie = {"skipped": False, "failed": [{"obj_id": "M-2", "name": "Two", "error": "HTTP 500"}]}
    rep = _report(outcome(ie=ie, mi={"skipped": False, "dry_run": False, "apply_status": "FAILED",
                                     "validation_status": "SUCCESS", "applied": False}))
    kinds = {(r.obj_type, r.stage) for r in rep.failed}
    assert kinds == {("SPOTTER_INSTRUCTIONS", "spotter_instructions_export"),
                     ("SPOTTER_MEMORY", "spotter_memory_import")}
    assert all(r.status == "failed" and r.error and r.suggested_action for r in rep.failed)


def test_stage11_notes_go_to_the_unsupported_section_without_failing():
    rep = _report(outcome(mi={"skipped": True, "reason": "Spotter not enabled or insufficient permissions"}))
    assert rep.failed == []
    assert any("Spotter memory" == u["name"] for u in rep.unsupported)


def test_stage11_without_spotter_results_is_unchanged():
    assert _report(None).failed == []


# ------------------------------------------------------------------ the workflow passes the files
def _job_env(job):
    return yaml.safe_load(WORKFLOW.read_text())["jobs"][job]["env"]


@pytest.mark.parametrize("job", ["untag_source", "report"])
def test_jobs_that_judge_the_run_are_told_where_the_spotter_results_are(job):
    env = _job_env(job)
    assert env["MEMORY_EXPORT_OUT"] == "stage4b_memory_export.json"
    assert env["MEMORY_IMPORT_OUT"] == "stage9b_memory_import.json"
    assert env["INSTRUCTIONS_EXPORT_OUT"] == "stage4c_instructions_export.json"
    assert env["INSTRUCTIONS_IMPORT_OUT"] == "stage9c_instructions_import.json"


def test_the_files_those_jobs_read_are_uploaded_by_the_jobs_that_write_them():
    jobs = yaml.safe_load(WORKFLOW.read_text())["jobs"]

    def uploaded(job):
        return "\n".join(str(st.get("with", {}).get("path", ""))
                         for st in jobs[job]["steps"] if "upload-artifact" in str(st.get("uses", "")))
    assert "stage4b_memory_export.json" in uploaded("extract")
    assert "stage4c_instructions_export.json" in uploaded("extract")
    assert "stage9b_memory_import.json" in uploaded("import")
    assert "stage9c_instructions_import.json" in uploaded("import")


# ------------------------------------------------- older clusters lack the beta endpoints
from scripts import stage4b_export_memory as s4b            # noqa: E402
from scripts import stage4c_export_instructions as s4c      # noqa: E402
from scripts import stage9b_import_memory as s9b            # noqa: E402
from scripts import stage9c_import_instructions as s9c      # noqa: E402


def _resp(status, body=None):
    r = MagicMock(status_code=status, text="")
    r.json.return_value = body or {}
    return r


MODELS = [{"folder": "models", "obj_id": "M-1", "name": "One", "source_guid": "g1"}]


@pytest.mark.parametrize("status", [404, 405, 501])
def test_4b_missing_memory_endpoint_is_a_skip_and_a_note_not_a_failure(status, tmp_path):
    c = MagicMock(); c.post.return_value = _resp(status)
    res = s4b.export_memory(c, MODELS, "o", tmp_path)
    assert res == {"skipped": True, "reason": "endpoint_unavailable", "http_status": status}
    o = spotter_outcome(res, {}, {}, {})
    assert o["failures"] == [] and len(o["notes"]) == 1 and not o["blocked_all_models"]


def test_4b_a_real_server_error_is_still_a_failure(tmp_path):
    c = MagicMock(); c.post.return_value = _resp(500)
    res = s4b.export_memory(c, MODELS, "o", tmp_path)
    assert res["reason"] == "http_error"
    assert spotter_outcome(res, {}, {}, {})["blocked_all_models"]


@pytest.mark.parametrize("status", [404, 405, 501])
def test_4c_missing_instructions_endpoint_is_a_skip_and_a_note(status, tmp_path):
    c = MagicMock(); c.get_nl_instructions.return_value = _resp(status)
    res = s4c.export_instructions(c, MODELS, "o", tmp_path)
    assert res["skipped"] and res["reason"] == "endpoint_unavailable"
    o = spotter_outcome({}, {}, res, {})
    assert o["failures"] == [] and len(o["notes"]) == 1


def test_4c_a_real_server_error_is_still_a_per_model_failure(tmp_path):
    c = MagicMock(); c.get_nl_instructions.return_value = _resp(500)
    res = s4c.export_instructions(c, MODELS, "o", tmp_path)
    assert [f["obj_id"] for f in spotter_outcome({}, {}, res, {})["failures"]] == ["M-1"]


@pytest.mark.parametrize("status", [404, 405, 501])
def test_9b_missing_memory_endpoint_is_a_skip_and_a_note(status):
    c = MagicMock(); c.post.return_value = _resp(status)
    res = s9b.import_memory(c, "memories:\n- type: RULE\n", dry_run=False)
    assert res["skipped"] is True and "not available on this cluster" in res["reason"]
    o = spotter_outcome({}, res, {}, {})
    assert o["failures"] == [] and len(o["notes"]) == 1 and not o["blocked_all_models"]


def test_9b_a_real_server_error_is_still_a_failure():
    c = MagicMock(); c.post.return_value = _resp(500)
    res = s9b.import_memory(c, "memories:\n- type: RULE\n", dry_run=False)
    assert spotter_outcome({}, res, {}, {})["blocked_all_models"]


@pytest.mark.parametrize("status", [404, 405, 501])
def test_9c_missing_instructions_endpoint_is_a_skip_and_a_note(status):
    c = MagicMock(); c.find_model_guid.return_value = "tg"; c.get_nl_instructions.return_value = _resp(status)
    payload = {"models": [{"obj_id": "M-1", "name": "One",
                           "nl_instructions_info": [{"scope": "GLOBAL", "instructions": ["x"]}]}]}
    res = s9c.import_instructions(c, payload, dry_run=False)
    assert res["skipped"] and res["reason"] == "endpoint_unavailable"
    c.set_nl_instructions.assert_not_called()
    assert spotter_outcome({}, {}, {}, res)["failures"] == []
