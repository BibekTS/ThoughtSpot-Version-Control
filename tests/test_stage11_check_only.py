"""Stage 11: report generation never fails the job; `--check-only` is the only thing that does."""
import json

import pytest
import yaml

from scripts import stage11_report as s11

CFG = {"org_key": "o", "display_name": "O", "migration_tag": "t",
       "source": {"base_url": "s", "org_id": 1}, "target": {"base_url": "t", "org_id": 2}}
MANIFEST = [{"obj_id": "A-1", "name": "A", "type": "LOGICAL_TABLE", "reason": "tagged"}]


@pytest.fixture
def run(tmp_path, monkeypatch):
    def setup(import_status):
        files = {
            "CONFIG_OUT": CFG, "MANIFEST_OUT": MANIFEST,
            "EXPORT_OUT": [{"obj_id": "A-1", "status": "success", "tml_path": "a.tml"}],
            "IMPORT_OUT": [{"obj_id": "A-1", "status": import_status, "error": "boom"}],
        }
        for env, data in files.items():
            p = tmp_path / f"{env}.json"
            p.write_text(json.dumps(data))
            monkeypatch.setenv(env, str(p))
        for env in ("POSTCHECK_OUT", "PROD_PREP_OUT", "MEMORY_EXPORT_OUT", "MEMORY_IMPORT_OUT",
                    "INSTRUCTIONS_EXPORT_OUT", "INSTRUCTIONS_IMPORT_OUT", "DISCOVER_OUT"):
            monkeypatch.setenv(env, str(tmp_path / "missing.json"))
        monkeypatch.setenv("ORG_KEY", "o")
        monkeypatch.setenv("RUN_ID", "r1")
        monkeypatch.setenv("TIMESTAMP", "T1")
        monkeypatch.setenv("REPO_ROOT", str(tmp_path))
        monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(tmp_path / "summary.md"))
        return tmp_path
    return setup


def test_generating_reports_does_not_exit_nonzero_on_failures(run, capsys):
    root = run("failed")
    s11.main([])                                           # no SystemExit
    assert (root / "reports" / "o" / "T1_r1" / "migration_summary.txt").exists()
    assert "1 failures" in capsys.readouterr().err


def test_check_only_exits_1_on_failures_and_writes_nothing(run):
    root = run("failed")
    with pytest.raises(SystemExit) as e:
        s11.main(["--check-only"])
    assert e.value.code == 1
    assert not (root / "reports").exists()
    assert not (root / "summary.md").exists()


def test_check_only_passes_on_a_clean_run(run, capsys):
    root = run("success")
    s11.main(["--check-only"])
    assert "No failures" in capsys.readouterr().out
    assert not (root / "reports").exists()


def test_job_summary_is_written_once_per_run(run):
    root = run("success")
    s11.main([])
    s11.main(["--check-only"])
    assert (root / "summary.md").read_text().count("## ThoughtSpot Migration") == 1


def test_workflow_runs_check_only_last_and_even_after_failures():
    wf = yaml.safe_load(open(".github/workflows/org-migration.yml"))
    steps = wf["jobs"]["report"]["steps"]
    last = steps[-1]
    assert last["run"].endswith("stage11_report --check-only") and last["if"] == "always()"
    gen = next(s for s in steps if s.get("run") == "python -m scripts.stage11_report")
    assert steps.index(gen) < steps.index(last)
