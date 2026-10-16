"""
The report job must actually commit reports to ts-dev.

These run Stage 11's real main() to produce the report folder, then run the real
"Commit reports to ts-dev" step from the workflow (as bash) against a temporary repo
whose `origin` is a local bare repo. That exercises the whole chain: the folder name
Stage 11 writes, the folder name the workflow looks for, the commit and the push.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import stage11_report as s11

WORKFLOW = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "org-migration.yml"
pytestmark = pytest.mark.skipif(shutil.which("git") is None or shutil.which("bash") is None,
                                reason="needs git and bash")


def _step():
    wf = yaml.safe_load(WORKFLOW.read_text())
    return next(s for s in wf["jobs"]["report"]["steps"] if s.get("name") == "Commit reports to ts-dev")


def _git(cwd, *args, env=None):
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True,
                          env=env).stdout


@pytest.fixture
def repos(tmp_path):
    """A working clone on ts-dev whose origin is a bare repo."""
    home = tmp_path / "home"; home.mkdir()
    env = {**os.environ, "HOME": str(home), "GIT_CONFIG_NOSYSTEM": "1",
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t"}
    bare = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(bare), env=env)
    work = tmp_path / "work"; work.mkdir()
    _git(work, "init", "-q", env=env)
    _git(work, "checkout", "-q", "-b", "ts-dev", env=env)
    (work / ".gitkeep").write_text("")
    _git(work, "add", ".gitkeep", env=env)
    _git(work, "commit", "-q", "-m", "init", env=env)
    _git(work, "remote", "add", "origin", str(bare), env=env)
    _git(work, "push", "-q", "-u", "origin", "ts-dev", env=env)
    return SimpleNs(work=work, bare=bare, env=env, tmp=tmp_path)


class SimpleNs:
    def __init__(self, **kw):
        self.__dict__.update(kw)


def _write_report(repos, monkeypatch, org="o", ts="20261015T133749Z", run_id="777-1"):
    """Run Stage 11's real main() inside the working clone."""
    cfg = repos.tmp / "cfg.json"
    cfg.write_text(json.dumps({
        "org_key": org, "migration_tag": "t",
        "source": {"base_url": "https://s", "org_id": 1},
        "target": {"base_url": "https://t", "org_id": 2}}))
    for k, v in dict(ORG_KEY=org, RUN_ID=run_id, TIMESTAMP=ts, REPO_ROOT=str(repos.work),
                     CONFIG_OUT=str(cfg), MANIFEST_OUT=str(repos.tmp / "none1.json"),
                     EXPORT_OUT=str(repos.tmp / "none2.json"), IMPORT_OUT=str(repos.tmp / "none3.json"),
                     POSTCHECK_OUT=str(repos.tmp / "none4.json"),
                     PROD_PREP_OUT=str(repos.tmp / "none5.json")).items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    s11.main()
    return f"reports/{org}/{ts}_{run_id}"


def _run_step(repos, org="o", ts="20261015T133749Z", run_id="777-1"):
    env = {**repos.env, "ORG_KEY": org, "TIMESTAMP": ts, "RUN_ID": run_id}
    return subprocess.run(["bash", "-e", "-c", _step()["run"]], cwd=repos.work, env=env,
                          capture_output=True, text=True)


def _remote_files(repos):
    return _git(repos.bare, "ls-tree", "-r", "--name-only", "ts-dev").split()


def test_step_runs_even_when_stage_11_exited_red():
    # Stage 11 exits non-zero when anything failed; the commit must still happen then.
    assert _step().get("if") == "always()"


def test_report_folder_name_matches_what_the_workflow_looks_for(repos, monkeypatch):
    rel = _write_report(repos, monkeypatch)
    assert (repos.work / rel).is_dir(), "Stage 11 wrote a differently named folder"


def test_reports_are_committed_and_pushed_to_ts_dev(repos, monkeypatch):
    rel = _write_report(repos, monkeypatch)
    r = _run_step(repos)
    assert r.returncode == 0, r.stderr
    files = _remote_files(repos)
    assert any(f.startswith(rel + "/") for f in files), files
    assert "Pushed reports to ts-dev" in r.stdout


def test_second_stage_11_invocation_does_not_create_a_second_folder(repos, monkeypatch):
    # Two report generations in one run (e.g. a manual re-run) with a fixed timestamp write
    # the same folder.
    _write_report(repos, monkeypatch)
    before = sorted(p.name for p in (repos.work / "reports" / "o").iterdir())
    _write_report(repos, monkeypatch)
    after = sorted(p.name for p in (repos.work / "reports" / "o").iterdir())
    assert before == after and len(after) == 1


def test_missing_report_dir_is_a_warning_not_a_failure(repos):
    r = _run_step(repos)
    assert r.returncode == 0
    assert "::warning::No report directory" in r.stdout
    assert _remote_files(repos) == [".gitkeep"]


def test_nothing_to_commit_is_not_a_failure(repos, monkeypatch):
    _write_report(repos, monkeypatch)
    assert _run_step(repos).returncode == 0
    r = _run_step(repos)                       # same files again: nothing new
    assert r.returncode == 0 and "No report changes to commit" in r.stdout


def test_push_rejected_then_rebased_and_retried(repos, monkeypatch):
    rel = _write_report(repos, monkeypatch)
    # Another org pushes to ts-dev after this job checked it out.
    other = repos.tmp / "other"
    _git(repos.tmp, "clone", "-q", "-b", "ts-dev", str(repos.bare), str(other), env=repos.env)
    (other / "orgs").mkdir(); (other / "orgs" / "x.tml").write_text("x")
    _git(other, "add", "-A", env=repos.env)
    _git(other, "commit", "-q", "-m", "other org", env=repos.env)
    _git(other, "push", "-q", "origin", "ts-dev", env=repos.env)

    r = _run_step(repos)
    assert r.returncode == 0, r.stderr
    files = _remote_files(repos)
    assert "orgs/x.tml" in files and any(f.startswith(rel + "/") for f in files), files
    assert "Push rejected" in r.stdout
