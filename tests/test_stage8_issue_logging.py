"""Stage 8 must say what each issue is; a bare "2 errors" count is not actionable."""
import json

from scripts import stage8_prod_prep as s8


class TestFormatIssue:
    def test_variable_issue(self):
        line = s8.format_issue({"severity": "ERROR", "variable": "bat_db_var",
                                "message": "TABLE_MAPPING variable 'bat_db_var' not found"})
        assert line == ("[STAGE 8] ERROR [bat_db_var]: "
                        "TABLE_MAPPING variable 'bat_db_var' not found")

    def test_connection_and_object_subjects(self):
        assert "[Snowflake_Prod]" in s8.format_issue(
            {"severity": "ERROR", "connection": "Snowflake_Prod", "message": "missing"})
        assert "[obj_1]" in s8.format_issue(
            {"severity": "VALIDATION_FAILED", "obj_id": "obj_1", "message": "bad"})

    def test_no_subject_and_long_message_truncated(self):
        line = s8.format_issue({"severity": "ERROR", "message": "x" * 1000})
        assert line.startswith("[STAGE 8] ERROR: ") and line.endswith("...")
        assert len(line) < 400

    def test_newlines_flattened_and_missing_severity(self):
        line = s8.format_issue({"message": "a\nb"})
        assert "\n" not in line and "UNKNOWN" in line


class TestMainOutput:
    def _run(self, monkeypatch, tmp_path, issues):
        cfg = tmp_path / "cfg.json"; cfg.write_text(json.dumps(
            {"target": {"base_url": "https://t", "org_id": 1}, "variables": {}}))
        san = tmp_path / "san.json"; san.write_text("{}")
        exp = tmp_path / "exp.json"; exp.write_text("[]")
        out = tmp_path / "prep.json"
        for k, v in dict(ORG_KEY="o", CONFIG_OUT=str(cfg), SANITIZE_OUT=str(san),
                         EXPORT_OUT=str(exp), PROD_PREP_OUT=str(out), REPO_ROOT=str(tmp_path),
                         TS_TARGET_USERNAME="u", TS_TARGET_SECRET_KEY="k").items():
            monkeypatch.setenv(k, v)
        monkeypatch.setattr(s8, "TSClient", lambda *a, **k: type("C", (), {"authenticate": lambda self: None})())
        monkeypatch.setattr(s8, "sync_variables", lambda *a, **k: issues)
        monkeypatch.setattr(s8, "connection_preflight", lambda *a, **k: [])
        s8.main()
        return out

    def test_each_error_is_printed_and_wording_is_accurate(self, monkeypatch, tmp_path, capsys):
        issues = [
            {"severity": "ERROR", "variable": "bat_db_var", "message": "not found"},
            {"severity": "ERROR", "variable": "bat_schema_var", "message": "wrong type"},
            {"severity": "INFO", "variable": "f", "message": "formula note"},
        ]
        out = self._run(monkeypatch, tmp_path, issues)
        cap = capsys.readouterr()
        assert "[STAGE 8] ERROR [bat_db_var]: not found" in cap.err
        assert "[STAGE 8] ERROR [bat_schema_var]: wrong type" in cap.err
        assert "[STAGE 8] INFO [f]: formula note" in cap.out      # INFO is not an alarm
        assert "will be excluded" not in cap.err                   # the old, false claim
        assert "does not exclude objects" in cap.err
        assert len(json.loads(out.read_text())) == 3               # artifact unchanged

    def test_no_issues_prints_no_warning(self, monkeypatch, tmp_path, capsys):
        self._run(monkeypatch, tmp_path, [])
        cap = capsys.readouterr()
        assert "0 errors" in cap.out and "[WARN]" not in cap.err
