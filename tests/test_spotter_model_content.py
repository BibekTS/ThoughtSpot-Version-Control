"""Tests for model memory file resolution (Stage 9b) and NL instructions (Stages 4c/9c)."""
import json
from pathlib import Path
from unittest.mock import MagicMock

from scripts.lib.artifacts import resolve_export_file
from scripts.stage4c_export_instructions import export_instructions
from scripts.stage9c_import_instructions import import_instructions


def _resp(status=200, body=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body if body is not None else {}
    r.text = json.dumps(body or {})
    return r


class TestResolveExportFile:
    def test_timestamp_picks_exact_file(self, tmp_path):
        (tmp_path / "memory__20260101T000000Z.yaml").write_text("old")
        (tmp_path / "memory__20260202T000000Z.yaml").write_text("this run")
        got = resolve_export_file(tmp_path, "memory", ".yaml", "20260202T000000Z")
        assert got.name == "memory__20260202T000000Z.yaml"

    def test_timestamp_never_falls_back_to_older_run(self, tmp_path):
        # This run's export was skipped; an older run's file must NOT be imported.
        (tmp_path / "memory__20260101T000000Z.yaml").write_text("stale")
        (tmp_path / "memory.yaml").write_text("legacy")
        assert resolve_export_file(tmp_path, "memory", ".yaml", "20260202T000000Z") is None

    def test_no_timestamp_uses_newest_then_legacy(self, tmp_path):
        (tmp_path / "memory.yaml").write_text("legacy")
        assert resolve_export_file(tmp_path, "memory", ".yaml").name == "memory.yaml"
        (tmp_path / "memory__20260101T000000Z.yaml").write_text("a")
        (tmp_path / "memory__20260202T000000Z.yaml").write_text("b")
        assert resolve_export_file(tmp_path, "memory", ".yaml").name == "memory__20260202T000000Z.yaml"

    def test_missing_directory_returns_none(self, tmp_path):
        assert resolve_export_file(tmp_path / "nope", "memory", ".yaml", "x") is None


MANIFEST = [
    {"folder": "models", "obj_id": "m1", "name": "Model 1", "source_guid": "g1"},
    {"folder": "tables", "obj_id": "t1", "name": "Table 1", "source_guid": "g2"},
]
INFO = [{"scope": "GLOBAL", "instructions": ["Use net sales", "Fiscal year starts Feb"]}]


class TestExportInstructions:
    def test_writes_timestamped_file_for_models_only(self, tmp_path):
        client = MagicMock()
        client.get_nl_instructions.return_value = _resp(200, {"nl_instructions_info": INFO})
        result = export_instructions(client, MANIFEST, "org", tmp_path, "TS1")

        client.get_nl_instructions.assert_called_once_with("g1")  # table skipped
        assert result["skipped"] is False
        out = tmp_path / "orgs/org/instructions/instructions__TS1.json"
        data = json.loads(out.read_text())
        assert data["models"] == [{"obj_id": "m1", "name": "Model 1", "nl_instructions_info": INFO}]

    def test_empty_instructions_skipped(self, tmp_path):
        client = MagicMock()
        client.get_nl_instructions.return_value = _resp(
            200, {"nl_instructions_info": [{"scope": "GLOBAL", "instructions": ["  "]}]})
        result = export_instructions(client, MANIFEST, "org", tmp_path, "TS1")
        assert result["skipped"] and result["reason"] == "no_instructions"
        assert not (tmp_path / "orgs").exists()

    def test_403_is_non_blocking_skip(self, tmp_path):
        client = MagicMock()
        client.get_nl_instructions.return_value = _resp(403)
        result = export_instructions(client, MANIFEST, "org", tmp_path, "TS1")
        assert result["skipped"] and result["reason"] == "spotter_not_enabled"

    def test_one_model_failing_does_not_stop_others(self, tmp_path):
        manifest = MANIFEST + [{"folder": "models", "obj_id": "m2", "name": "M2", "source_guid": "g3"}]
        client = MagicMock()
        client.get_nl_instructions.side_effect = [
            _resp(500), _resp(200, {"nl_instructions_info": INFO})]
        result = export_instructions(client, manifest, "org", tmp_path, "TS1")
        assert result["exported_count"] == 1
        assert result["failed"][0]["obj_id"] == "m1"

    def test_no_models(self, tmp_path):
        result = export_instructions(MagicMock(), [MANIFEST[1]], "org", tmp_path, "TS1")
        assert result["reason"] == "no_models"


PAYLOAD = {"models": [{"obj_id": "m1", "name": "Model 1", "nl_instructions_info": INFO}]}


class TestImportInstructions:
    def _client(self, existing_info, set_resp=None):
        c = MagicMock()
        c.find_model_guid.return_value = "target-guid"
        c.get_nl_instructions.return_value = _resp(200, {"nl_instructions_info": existing_info})
        c.set_nl_instructions.return_value = set_resp or _resp(200, {"success": True})
        return c

    def test_applies_using_target_guid(self):
        c = self._client([])
        result = import_instructions(c, PAYLOAD, dry_run=False)
        c.set_nl_instructions.assert_called_once_with("target-guid", INFO)
        assert result["applied"] == 1 and result["failed"] == 0

    def test_dry_run_never_writes(self):
        c = self._client([{"scope": "GLOBAL", "instructions": ["something else"]}])
        result = import_instructions(c, PAYLOAD, dry_run=True)
        c.set_nl_instructions.assert_not_called()
        assert result["results"][0]["status"] == "dry_run"
        assert result["results"][0]["existing_count"] == 1

    def test_identical_target_is_not_rewritten(self):
        c = self._client(INFO)
        result = import_instructions(c, PAYLOAD, dry_run=False)
        c.set_nl_instructions.assert_not_called()
        assert result["results"][0]["status"] == "unchanged"

    def test_model_missing_in_target_is_skipped(self):
        c = self._client([])
        c.find_model_guid.return_value = None
        result = import_instructions(c, PAYLOAD, dry_run=False)
        c.set_nl_instructions.assert_not_called()
        assert result["results"][0]["reason"] == "model_not_in_target"

    def test_set_failure_recorded_not_raised(self):
        c = self._client([], set_resp=_resp(403, {"error": "no"}))
        result = import_instructions(c, PAYLOAD, dry_run=False)
        assert result["failed"] == 1 and result["results"][0]["status"] == "failed"

    def test_403_on_read_skips_whole_stage(self):
        c = self._client([])
        c.get_nl_instructions.return_value = _resp(403)
        result = import_instructions(c, PAYLOAD, dry_run=False)
        assert result["skipped"] and result["reason"] == "spotter_not_enabled"
        c.set_nl_instructions.assert_not_called()


# --- Models without memories / instructions (real API "empty" responses) ------ #
import pytest
from scripts.lib.artifacts import memory_is_empty
from scripts.stage4b_export_memory import export_memory


class TestEmptyMemory:
    @pytest.mark.parametrize("text", ["memories: []\n", "memories: []", "", "  \n", "memories:\n", "{}\n"])
    def test_empty_payloads(self, text):
        assert memory_is_empty(text) is True

    def test_real_memories_not_empty(self):
        assert memory_is_empty("memories:\n- type: RULE\n  content:\n    rule_definition: x\n") is False

    def test_unparseable_is_not_treated_as_empty(self):
        assert memory_is_empty("memories: [unclosed") is False

    def test_export_skips_api_empty_response(self, tmp_path):
        client = MagicMock()
        client.post.return_value = _resp(200, {"content": "memories: []\n"})
        result = export_memory(client, MANIFEST, "org", tmp_path, "TS1")
        assert result["skipped"] and result["reason"] == "empty_memory"
        assert not (tmp_path / "orgs").exists()  # no file => 9b has nothing to import

    def test_export_writes_when_memories_present(self, tmp_path):
        client = MagicMock()
        body = "memories:\n- type: RULE\n  content:\n    rule_definition: x\n"
        client.post.return_value = _resp(200, {"content": body})
        result = export_memory(client, MANIFEST, "org", tmp_path, "TS1")
        assert result["skipped"] is False
        assert (tmp_path / "orgs/org/memory/memory__TS1.yaml").read_text() == body

    def test_import_stage_never_sends_empty_payload(self, tmp_path, monkeypatch):
        """Even if an empty file exists, 9b must not call the (replacing) import API."""
        from scripts import stage9b_import_memory as m
        d = tmp_path / "orgs/o/memory"; d.mkdir(parents=True)
        (d / "memory__TS1.yaml").write_text("memories: []\n")
        out = tmp_path / "out.json"
        for k, v in dict(ORG_KEY="o", REPO_ROOT=str(tmp_path), TIMESTAMP="TS1",
                         MEMORY_IMPORT_OUT=str(out)).items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(m, "TSClient", MagicMock(side_effect=AssertionError("must not connect")))
        m.main()
        assert json.loads(out.read_text())["reason"] == "empty_memory"


class TestEmptyInstructions:
    def test_api_empty_list_exports_nothing(self, tmp_path):
        client = MagicMock()
        client.get_nl_instructions.return_value = _resp(200, {"nl_instructions_info": []})
        result = export_instructions(client, MANIFEST, "org", tmp_path, "TS1")
        assert result["skipped"] and result["reason"] == "no_instructions"
        assert not (tmp_path / "orgs").exists()

    def test_mixed_models_only_exports_those_with_instructions(self, tmp_path):
        manifest = MANIFEST + [{"folder": "models", "obj_id": "m2", "name": "M2", "source_guid": "g3"}]
        client = MagicMock()
        client.get_nl_instructions.side_effect = [
            _resp(200, {"nl_instructions_info": []}),
            _resp(200, {"nl_instructions_info": INFO}),
        ]
        result = export_instructions(client, manifest, "org", tmp_path, "TS1")
        data = json.loads((tmp_path / "orgs/org/instructions/instructions__TS1.json").read_text())
        assert [m["obj_id"] for m in data["models"]] == ["m2"]
        assert result["exported_count"] == 1


class TestNeverClearTarget:
    def test_empty_instructions_in_export_never_call_set(self):
        c = MagicMock()
        c.find_model_guid.return_value = "target-guid"
        for info in ([], [{"scope": "GLOBAL", "instructions": []}],
                     [{"scope": "GLOBAL", "instructions": ["", "  "]}]):
            payload = {"models": [{"obj_id": "m1", "name": "M", "nl_instructions_info": info}]}
            result = import_instructions(c, payload, dry_run=False)
            assert result["results"][0]["reason"] == "no_source_instructions"
        c.set_nl_instructions.assert_not_called()
        c.get_nl_instructions.assert_not_called()

    def test_models_absent_from_export_are_never_touched(self):
        c = MagicMock()
        result = import_instructions(c, {"models": []}, dry_run=False)
        c.find_model_guid.assert_not_called()
        c.set_nl_instructions.assert_not_called()
        assert result["applied"] == 0
