"""Promote branches are deleted once their PR is finished with, and nothing else ever is."""
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml

from scripts import cleanup_promote_branch as cpb
from scripts import stage7_promote as s7
from scripts import stage7b_wait_for_merge as s7b

ROOT = Path(__file__).resolve().parent.parent
REPO = "o/r"


class FakeGh:
    """Stands in for the gh CLI: `open_prs` maps branch -> open PR count; `branches` is the repo's list."""

    def __init__(self, open_prs=None, branches=(), delete_result=None, list_fails=False):
        self.open_prs, self.branches = open_prs or {}, list(branches)
        self.delete_result = delete_result or (0, "", "")
        self.list_fails, self.deleted, self.calls = list_fails, [], []

    def __call__(self, cmd):
        self.calls.append(cmd)
        if cmd[:3] == ["gh", "pr", "list"]:
            if self.list_fails:
                return SimpleNamespace(returncode=1, stdout="", stderr="boom")
            n = self.open_prs.get(cmd[cmd.index("--head") + 1], 0)
            return SimpleNamespace(returncode=0, stdout=json.dumps([{"number": i} for i in range(n)]), stderr="")
        if cmd[:4] == ["gh", "api", "-X", "DELETE"]:
            code, out, err = self.delete_result
            if code == 0:
                self.deleted.append(cmd[4].split("/git/refs/heads/")[1])
            return SimpleNamespace(returncode=code, stdout=out, stderr=err)
        if cmd[:2] == ["gh", "api"]:
            return SimpleNamespace(returncode=0, stdout="\n".join(self.branches), stderr="")
        raise AssertionError(cmd)


@pytest.fixture
def gh(monkeypatch):
    def install(**kw):
        fake = FakeGh(**kw)
        monkeypatch.setattr(cpb, "run", fake)
        return fake
    return install


# ---- what counts as a promote branch ------------------------------------------------------ #

@pytest.mark.parametrize("name", ["promote/github/38926079201-1", "promote/my-org/1.2_3-4"])
def test_promote_branches_are_recognised(name):
    assert cpb.is_promote_branch(name)


@pytest.mark.parametrize("name", [
    "", "ts-prod", "ts-dev", "main", "claude/pr1-validation-gate", "promote", "promote/",
    "promote/github", "promote/github/", "promote//1", "promote/a/b/c", "xpromote/a/b",
    "promote/a/b c", "promote/a/../ts-prod", "refs/heads/promote/a/1", "promote/a/1;rm"])
def test_nothing_else_is_ever_treated_as_one(name):
    assert not cpb.is_promote_branch(name)


# ---- delete_branch ------------------------------------------------------------------------- #

def test_a_finished_promote_branch_is_deleted(gh):
    fake = gh()
    assert cpb.delete_branch(REPO, "promote/github/1-1") == "deleted"
    assert fake.deleted == ["promote/github/1-1"]


@pytest.mark.parametrize("name", ["ts-prod", "ts-dev", "claude/x", "promote/a/b/c", ""])
def test_a_branch_that_is_not_a_promote_branch_is_refused_without_any_call(gh, name):
    fake = gh()
    assert cpb.delete_branch(REPO, name) == "refused"
    assert fake.calls == []


def test_a_branch_with_an_open_pr_is_kept(gh):
    fake = gh(open_prs={"promote/github/1-1": 1})
    assert cpb.delete_branch(REPO, "promote/github/1-1") == "kept"
    assert fake.deleted == []


def test_if_open_prs_cannot_be_checked_nothing_is_deleted(gh):
    fake = gh(list_fails=True)
    assert cpb.delete_branch(REPO, "promote/github/1-1") == "failed"
    assert fake.deleted == []


@pytest.mark.parametrize("err", ["Reference does not exist", "gh: Not Found (HTTP 404)"])
def test_an_already_deleted_branch_counts_as_success(gh, err):
    gh(delete_result=(1, "", err))
    assert cpb.delete_branch(REPO, "promote/github/1-1") == "gone"


def test_any_other_delete_error_is_reported_as_failed(gh):
    gh(delete_result=(1, "", "HTTP 403: Resource not accessible"))
    assert cpb.delete_branch(REPO, "promote/github/1-1") == "failed"


# ---- sweep --------------------------------------------------------------------------------- #

BRANCHES = ["claude/x", "promote/github/1-1", "promote/github/2-1", "ts-dev", "ts-prod"]


def test_a_sweep_preview_deletes_nothing(gh, capsys):
    fake = gh(branches=BRANCHES, open_prs={"promote/github/2-1": 1})
    counts = cpb.sweep(REPO, apply=False)
    assert counts == {"would delete": 1, "kept": 1} and fake.deleted == []
    assert "would delete: promote/github/1-1" in capsys.readouterr().out


def test_a_sweep_deletes_only_finished_promote_branches(gh):
    fake = gh(branches=BRANCHES, open_prs={"promote/github/2-1": 1})
    counts = cpb.sweep(REPO, apply=True)
    assert fake.deleted == ["promote/github/1-1"] and counts == {"deleted": 1, "kept": 1}


def test_a_sweep_never_lists_other_branches_for_deletion(gh):
    fake = gh(branches=BRANCHES)
    cpb.sweep(REPO, apply=True)
    assert not set(fake.deleted) & {"ts-prod", "ts-dev", "claude/x"}


# ---- main ---------------------------------------------------------------------------------- #

def _main(monkeypatch, **env):
    for k in ("MODE", "BRANCH", "DRY_RUN"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("REPO", REPO)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    with pytest.raises(SystemExit) as exc:
        cpb.main()
    return exc.value.code


def test_main_deletes_the_named_branch(monkeypatch, gh):
    fake = gh()
    assert _main(monkeypatch, BRANCH="promote/github/1-1") == 0
    assert fake.deleted == ["promote/github/1-1"]


def test_main_sweep_defaults_to_a_preview(monkeypatch, gh):
    fake = gh(branches=BRANCHES)
    assert _main(monkeypatch, MODE="sweep") == 0 and fake.deleted == []


def test_main_sweep_with_dry_run_off_deletes(monkeypatch, gh):
    fake = gh(branches=BRANCHES)
    assert _main(monkeypatch, MODE="sweep", DRY_RUN="false") == 0
    assert fake.deleted == ["promote/github/1-1", "promote/github/2-1"]


def test_main_exits_non_zero_when_a_delete_fails(monkeypatch, gh):
    gh(delete_result=(1, "", "HTTP 403"))
    assert _main(monkeypatch, BRANCH="promote/github/1-1") == 1


# ---- the pipeline calls it ----------------------------------------------------------------- #

def test_stage_7_records_the_branch_it_created():
    import inspect
    assert 'result["branch"] = promote_branch' in inspect.getsource(s7.promote)


@pytest.fixture
def stage7b(tmp_path, monkeypatch):
    (tmp_path / "stage7_promote.json").write_text(json.dumps(
        {"pr_url": "https://x/pull/1", "branch": "promote/github/9-1"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setenv("GITHUB_REPOSITORY", REPO)
    deleted = []
    monkeypatch.setattr(s7b, "delete_branch", lambda repo, b: deleted.append((repo, b)) or "deleted")

    def run(status):
        monkeypatch.setattr(s7b, "wait_for_merge", lambda *a, **k: {"status": status, "pr_url": "u"})
        try:
            s7b.main()
        except SystemExit:
            pass
        return deleted
    return run


@pytest.mark.parametrize("status", ["merged", "closed"])
def test_the_branch_is_deleted_once_the_pr_is_finished_with(stage7b, status):
    assert stage7b(status) == [(REPO, "promote/github/9-1")]


@pytest.mark.parametrize("status", ["timeout", "error"])
def test_the_branch_stays_while_the_pr_is_still_open(stage7b, status):
    assert stage7b(status) == []


def test_a_cleanup_failure_never_fails_the_run(tmp_path, monkeypatch):
    (tmp_path / "stage7_promote.json").write_text(json.dumps(
        {"pr_url": "https://x/pull/1", "branch": "promote/github/9-1"}))
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("DRY_RUN", raising=False)
    monkeypatch.setattr(s7b, "wait_for_merge", lambda *a, **k: {"status": "merged", "pr_url": "u"})
    monkeypatch.setattr(s7b, "delete_branch", lambda *a: (_ for _ in ()).throw(RuntimeError("boom")))
    s7b.main()                                    # returns normally: the import must still run


def test_a_dry_run_cleans_up_nothing(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("DRY_RUN", "true")
    monkeypatch.setattr(s7b, "delete_branch", lambda *a: pytest.fail("must not delete"))
    s7b.main()


# ---- the workflow ---------------------------------------------------------------------------- #

@pytest.fixture(scope="module")
def wf():
    return yaml.safe_load((ROOT / ".github/workflows/cleanup-promote-branches.yml").read_text())


def test_the_workflow_only_acts_on_same_repo_promote_prs_or_a_manual_run(wf):
    cond = " ".join(str(wf["jobs"]["cleanup"]["if"]).split())
    assert "github.event_name == 'workflow_dispatch'" in cond
    assert "github.event.pull_request.head.repo.full_name == github.repository" in cond
    assert "startsWith(github.event.pull_request.head.ref, 'promote/')" in cond
    assert wf[True]["pull_request"]["types"] == ["closed"]


def test_the_sweep_is_a_preview_unless_unticked(wf):
    assert wf[True]["workflow_dispatch"]["inputs"]["dry_run"]["default"] is True


def test_the_workflow_uses_the_scripts_from_ts_prod_with_a_narrow_token(wf):
    job = wf["jobs"]["cleanup"]
    assert job["steps"][0]["with"]["ref"] == "ts-prod"
    assert wf["permissions"] == {"contents": "write", "pull-requests": "read"}
    assert job["env"]["GH_TOKEN"] == "${{ github.token }}"
