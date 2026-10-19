"""Import pinned to the promote PR's merge commit; clean migrations leave a restore-point tag."""
import json
import os
import subprocess
from pathlib import Path

import pytest
import yaml

from scripts import restore_merged_tml as rmt
from scripts import stage7b_wait_for_merge as s7b
from scripts import tag_release as tr

ROOT = Path(__file__).resolve().parent.parent
ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"]}


def git(repo, *args):
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True,
                          env={**ENV, "HOME": str(repo)}).stdout.strip()


def write(repo, rel, body):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body)


@pytest.fixture
def repo(tmp_path):
    """main: c1 = the run's files as first promoted; merge = reviewer-edited; later = another run."""
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "orgs/o/tables/a__A-1__20260101T000000Z.table.tml", '{"obj_id":"A-1","v":"first"}')
    git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "base")
    # the promote PR's squash-merge: the reviewer edited A-1 and removed B-1
    write(tmp_path, "orgs/o/tables/a__A-1__20260201T000000Z.table.tml", '{"obj_id":"A-1","v":"reviewed"}')
    git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "promote run 2")
    merge_sha = git(tmp_path, "rev-parse", "HEAD")
    # a later run lands on ts-prod while the import is still waiting for its turn
    write(tmp_path, "orgs/o/tables/c__C-1__20260301T000000Z.table.tml", '{"obj_id":"C-1"}')
    git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "promote run 3")
    return tmp_path, merge_sha


# ---- Stage 7b publishes the merge commit ------------------------------------------------- #

def _run_7b(tmp_path, monkeypatch, result):
    (tmp_path / "stage7_promote.json").write_text(json.dumps({"pr_url": "https://x/pull/1"}))
    out = tmp_path / "gh_output"
    for k in ("DRY_RUN",):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("GITHUB_OUTPUT", str(out))
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(s7b, "wait_for_merge", lambda *a, **k: result)
    try:
        s7b.main()
    except SystemExit:
        pass
    return out.read_text() if out.exists() else ""


def test_a_merged_pr_publishes_its_merge_commit(tmp_path, monkeypatch):
    out = _run_7b(tmp_path, monkeypatch, {"status": "merged", "pr_url": "u", "merge_commit": "abc123"})
    assert out == "merge_sha=abc123\n"


@pytest.mark.parametrize("result", [{"status": "closed", "pr_url": "u"},
                                    {"status": "timeout", "pr_url": "u"}])
def test_nothing_is_published_when_the_pr_did_not_merge(tmp_path, monkeypatch, result):
    assert _run_7b(tmp_path, monkeypatch, result) == ""


def test_a_dry_run_publishes_nothing(tmp_path, monkeypatch):
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setenv("GITHUB_OUTPUT", str(tmp_path / "gh_output"))
    monkeypatch.chdir(tmp_path)
    s7b.main()
    assert not (tmp_path / "gh_output").exists()


# ---- the import uses what was merged, not the pre-review artifact ------------------------- #

def _restore(repo, monkeypatch, merge_sha):
    monkeypatch.setenv("ORG_KEY", "o")
    monkeypatch.setenv("REPO_ROOT", str(repo))
    monkeypatch.setenv("MERGE_SHA", merge_sha)
    monkeypatch.setenv("IMPORT_SOURCE_OUT", str(repo / "import_source.json"))
    monkeypatch.setenv("GITHUB_STEP_SUMMARY", str(repo / "summary.md"))
    rmt.main()
    return json.loads((repo / "import_source.json").read_text())


def _files(repo):
    return sorted(p.name for p in (repo / "orgs/o").rglob("*") if p.is_file())


def test_the_workspace_is_replaced_with_the_merge_commits_files(repo, monkeypatch):
    path, merge_sha = repo
    # the downloaded artifact overlays the pre-review copy and a stray file
    write(path, "orgs/o/tables/a__A-1__20260201T000000Z.table.tml", '{"obj_id":"A-1","v":"PRE-REVIEW"}')
    write(path, "orgs/o/tables/stray__S-1.table.tml", "{}")
    info = _restore(path, monkeypatch, merge_sha)
    kept = (path / "orgs/o/tables/a__A-1__20260201T000000Z.table.tml").read_text()
    assert json.loads(kept)["v"] == "reviewed"
    assert _files(path) == ["a__A-1__20260101T000000Z.table.tml", "a__A-1__20260201T000000Z.table.tml"]
    assert info == {"ts_prod_commit": merge_sha, "from_merge_commit": True, "files": 2}


def test_a_later_runs_files_are_not_pulled_in(repo, monkeypatch):
    path, merge_sha = repo
    _restore(path, monkeypatch, merge_sha)
    assert not any("C-1" in n for n in _files(path))      # that landed after the merge


def test_the_commit_is_reported_in_the_job_summary(repo, monkeypatch):
    path, merge_sha = repo
    _restore(path, monkeypatch, merge_sha)
    assert merge_sha in (path / "summary.md").read_text()


def test_without_a_merge_commit_the_current_ts_prod_is_used(repo, monkeypatch):
    path, _ = repo
    info = _restore(path, monkeypatch, "")
    assert info["from_merge_commit"] is False and info["files"] == 3
    assert info["ts_prod_commit"] == git(path, "rev-parse", "HEAD")


def test_an_unknown_merge_commit_stops_the_job(repo, monkeypatch):
    path, _ = repo
    with pytest.raises(SystemExit) as exc:
        _restore(path, monkeypatch, "0" * 40)
    assert exc.value.code == 1


def test_an_org_with_no_files_at_the_commit_is_left_alone(repo, monkeypatch, capsys):
    path, merge_sha = repo
    monkeypatch.setenv("ORG_KEY", "other")
    monkeypatch.setenv("REPO_ROOT", str(path))
    monkeypatch.setenv("MERGE_SHA", merge_sha)
    monkeypatch.setenv("IMPORT_SOURCE_OUT", str(path / "import_source.json"))
    monkeypatch.delenv("GITHUB_STEP_SUMMARY", raising=False)
    rmt.main()
    assert json.loads((path / "import_source.json").read_text())["files"] == 0
    assert "no content for org 'other'" in capsys.readouterr().err


# ---- the restore-point tag ---------------------------------------------------------------- #

@pytest.fixture
def tagged_repo(repo, tmp_path_factory, monkeypatch):
    path, merge_sha = repo
    # The workflow runs `git config user.name/email` before the script; an annotated tag needs
    # a committer identity, so give the script the same one here (a CI runner has none).
    for key in ("GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL", "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL"):
        monkeypatch.setenv(key, ENV[key])
    remote = tmp_path_factory.mktemp("remote")
    git(remote, "init", "-q", "--bare")
    git(path, "remote", "add", "origin", str(remote))
    git(path, "push", "-q", "origin", "main")
    git(path, "fetch", "-q", "origin")
    return path, merge_sha, remote


def test_the_tag_marks_the_merge_commit_and_is_pushed(tagged_repo):
    path, merge_sha, remote = tagged_repo
    tag = tr.create_tag("o", "123-1", merge_sha, path, prod_ref="main")
    assert tag == "migrate/o/123-1"
    assert git(path, "rev-parse", f"{tag}^{{commit}}") == merge_sha
    assert git(remote, "rev-parse", f"refs/tags/{tag}^{{commit}}") == merge_sha
    assert "ts-redeploy" in git(path, "tag", "-n1", tag)


def test_tagging_twice_is_harmless(tagged_repo, capsys):
    path, merge_sha, _ = tagged_repo
    tr.create_tag("o", "123-1", merge_sha, path, prod_ref="main")
    tr.create_tag("o", "123-1", merge_sha, path, prod_ref="main")
    assert "already marks" in capsys.readouterr().out


def test_the_same_tag_on_a_different_commit_is_an_error(tagged_repo):
    path, merge_sha, _ = tagged_repo
    tr.create_tag("o", "123-1", merge_sha, path, prod_ref="main")
    with pytest.raises(RuntimeError, match="different commit"):
        tr.create_tag("o", "123-1", git(path, "rev-parse", "HEAD"), path, prod_ref="main")


def test_a_commit_outside_ts_prod_is_never_tagged(tagged_repo):
    path, _, _ = tagged_repo
    git(path, "checkout", "-qb", "side")
    write(path, "x.txt", "x")
    git(path, "add", "-A"); git(path, "commit", "-qm", "unreviewed")
    side = git(path, "rev-parse", "HEAD")
    with pytest.raises(RuntimeError, match="not part of main"):
        tr.create_tag("o", "9-1", side, path, prod_ref="main")


def test_an_empty_merge_sha_is_refused(tagged_repo):
    path, _, _ = tagged_repo
    with pytest.raises(RuntimeError, match="No merge commit"):
        tr.create_tag("o", "9-1", "", path, prod_ref="main")


# ---- workflow wiring ----------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def jobs():
    return yaml.safe_load((ROOT / ".github/workflows/org-migration.yml").read_text())["jobs"]


def test_promote_publishes_the_merge_commit_from_the_wait_step(jobs):
    assert jobs["promote"]["outputs"]["merge_sha"] == "${{ steps.wait.outputs.merge_sha }}"
    assert any(s.get("id") == "wait" and "stage7b" in s["run"] for s in jobs["promote"]["steps"])


def test_import_restores_from_the_merge_commit_before_anything_reads_the_tml(jobs):
    steps = jobs["import"]["steps"]
    names = [s.get("name", "") for s in steps]
    restore = next(i for i, s in enumerate(steps) if "restore_merged_tml" in s.get("run", ""))
    assert restore < names.index(next(n for n in names if n.startswith("Stage 8 —")))
    assert steps[restore]["env"]["MERGE_SHA"] == "${{ needs.promote.outputs.merge_sha }}"
    assert steps[restore]["if"] == "${{ !inputs.dry_run }}"
    checkout = steps[0]
    assert checkout["with"]["fetch-depth"] == 0 and checkout["with"]["ref"] == "ts-prod"


def test_the_tag_job_runs_only_after_a_clean_live_run(jobs):
    job = jobs["release_tag"]
    cond = " ".join(str(job["if"]).split())
    for needed in ("!inputs.dry_run", "needs.import.result == 'success'",
                   "needs.report.result == 'success'", "needs.promote.outputs.merge_sha != ''"):
        assert needed in cond
    assert set(job["needs"]) >= {"promote", "import", "report"}
    assert job["permissions"] == {"contents": "write"}
    assert job["continue-on-error"] is True
