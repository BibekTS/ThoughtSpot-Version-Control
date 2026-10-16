"""Stage 7: a dry run must open the promote PR but never enable auto-merge."""
from types import SimpleNamespace

import pytest

from scripts import stage7_promote as s7

MANIFEST = [{"type": "LOGICAL_TABLE", "folder": "models", "name": "M", "obj_id": "m1",
             "source_guid": "g1"}]
PR_URL = "https://github.com/o/r/pull/99"


@pytest.fixture
def calls(monkeypatch, tmp_path):
    """Replace run() so no git/gh command really executes; record each command."""
    monkeypatch.chdir(tmp_path)  # promote() chdirs; restore afterwards
    monkeypatch.setenv("GH_AUTOMATION_TOKEN", "tok")
    recorded = []

    def fake_run(cmd, check=True, capture=False, env=None):
        recorded.append(cmd)
        out = ""
        if cmd[:2] == ["git", "ls-tree"]:
            out = "040000 tree abc\torgs/o/"        # org exists on ts-dev
        elif cmd[:2] == ["git", "status"]:
            out = " M orgs/o/x.tml"                    # something to promote
        elif cmd[:3] == ["gh", "pr", "create"]:
            out = PR_URL + "\n"
        return SimpleNamespace(stdout=out, returncode=0)

    monkeypatch.setattr(s7, "run", fake_run)
    return recorded


def _gh(calls, sub):
    return [c for c in calls if c[:2] == ["gh", "pr"] and c[2] == sub]


def _arg(cmd, flag):
    return cmd[cmd.index(flag) + 1]


def test_live_run_enables_auto_merge(calls, tmp_path):
    url = s7.promote("o", "run1", MANIFEST, str(tmp_path), dry_run=False)
    assert url == PR_URL
    merge = _gh(calls, "merge")
    assert len(merge) == 1 and "--auto" in merge[0] and merge[0][3] == PR_URL
    create = _gh(calls, "create")[0]
    assert _arg(create, "--title") == "[o] Promote TML run run1"
    assert "DRY RUN" not in _arg(create, "--body")


def test_dry_run_opens_pr_but_never_enables_auto_merge(calls, tmp_path, capsys):
    url = s7.promote("o", "run1", MANIFEST, str(tmp_path), dry_run=True)
    assert url == PR_URL                       # PR still opened for inspection
    assert _gh(calls, "merge") == []           # the actual guarantee
    create = _gh(calls, "create")[0]
    assert _arg(create, "--title").startswith("[DRY RUN] ")
    body = _arg(create, "--body")
    assert "DRY RUN" in body and "Auto-merge is not enabled" in body
    assert "auto-merge NOT enabled" in capsys.readouterr().out


def test_dry_run_default_is_off(calls, tmp_path):
    s7.promote("o", "run1", MANIFEST, str(tmp_path))
    assert len(_gh(calls, "merge")) == 1


def test_main_reads_dry_run_env(calls, tmp_path, monkeypatch):
    mf = tmp_path / "manifest.json"
    mf.write_text('[{"type":"LOGICAL_TABLE","folder":"models","name":"M","obj_id":"m1","source_guid":"g1"}]')
    for k, v in dict(ORG_KEY="o", RUN_ID="r", MANIFEST_OUT=str(mf), REPO_ROOT=str(tmp_path)).items():
        monkeypatch.setenv(k, v)
    monkeypatch.setenv("DRY_RUN", "TRUE")      # case-insensitive, as in the other stages
    s7.main()
    assert _gh(calls, "merge") == [] and _gh(calls, "create")


def test_empty_manifest_still_does_nothing_in_dry_run(calls, tmp_path):
    assert s7.promote("o", "run1", [], str(tmp_path), dry_run=True) == ""
    assert calls == []


# --- PR body must not claim that merging the PR triggers the import ----------------

def _body(calls):
    return _arg(_gh(calls, "create")[0], "--body")


@pytest.mark.parametrize("dry_run", [False, True])
def test_body_does_not_claim_merging_triggers_import(calls, tmp_path, dry_run):
    s7.promote("o", "run1", MANIFEST, str(tmp_path), dry_run=dry_run)
    body = _body(calls)
    assert "Merge this PR to trigger" not in body
    assert "does **not** wait for this PR" in body
    assert "`prod` environment's required reviewers" in body


def test_live_body_describes_auto_merge_and_dry_body_does_not(calls, tmp_path):
    s7.promote("o", "run1", MANIFEST, str(tmp_path), dry_run=False)
    live = _body(calls)
    assert "Auto-merge is enabled" in live and "imports nothing" not in live
    calls.clear()
    s7.promote("o", "run1", MANIFEST, str(tmp_path), dry_run=True)
    dry = _body(calls)
    assert "imports nothing" in dry and "Auto-merge is enabled" not in dry
    assert "Auto-merge is not enabled" in dry     # the dry-run banner


def test_body_keeps_org_run_and_manifest_summary(calls, tmp_path):
    s7.promote("o", "run1", MANIFEST, str(tmp_path))
    body = _body(calls)
    assert "**Org:** `o`" in body and "**Run:** `run1`" in body
    assert "- LOGICAL_TABLE: 1" in body


MULTI = [{"type": "LOGICAL_TABLE", "folder": "tables", "name": "T", "obj_id": "t1", "source_guid": "g"},
         {"type": "LOGICAL_TABLE", "folder": "models", "name": "M", "obj_id": "m1", "source_guid": "g"},
         {"type": "ANSWER", "folder": "answers", "name": "A", "obj_id": "a1", "source_guid": "g"},
         {"type": "LIVEBOARD", "folder": "liveboards", "name": "L", "obj_id": "l1", "source_guid": "g"}]


@pytest.mark.parametrize("dry_run", [False, True])
def test_body_is_not_indented_so_github_does_not_render_a_code_block(calls, tmp_path, dry_run):
    # Regression: with 2+ object types the multi-line manifest list defeated textwrap.dedent
    # and every line of the real PR body (e.g. run 38745048765) was indented 8 spaces.
    s7.promote("o", "run1", MULTI, str(tmp_path), dry_run=dry_run)
    lines = [l for l in _body(calls).splitlines() if l.strip()]
    indented = [l for l in lines if l[0].isspace()]
    assert indented == [], indented
    assert "- ANSWER: 1" in lines and "- LIVEBOARD: 1" in lines and "- LOGICAL_TABLE: 2" in lines
    assert any(l.startswith("## ThoughtSpot TML Promotion") for l in lines)


# ---------------------------------------------------------------- only this run's files
LISTING = "\n".join([
    "orgs/o/models/a__A-1__T1.table.tml",
    "orgs/o/models/a__A-1__T0.table.tml",             # an older run
    "orgs/o/memory/memory__T1.yaml",
    "orgs/o/memory/memory__T0.yaml",
    "orgs/o/instructions/instructions__T1.json",
    "orgs/o/tables/t__T-1__T10.table.tml",            # T10 must not match T1
])


def test_run_files_picks_only_the_runs_timestamp():
    assert s7.run_files(LISTING, "T1") == [
        "orgs/o/models/a__A-1__T1.table.tml",
        "orgs/o/memory/memory__T1.yaml",
        "orgs/o/instructions/instructions__T1.json",
    ]
    assert s7.run_files(LISTING, "T9") == []
    assert s7.run_files("", "T1") == []


@pytest.fixture
def stamped(calls, monkeypatch):
    """Like `calls`, but the listing of ts-dev files is real-looking and TIMESTAMP is set."""
    monkeypatch.setenv("TIMESTAMP", "T1")
    inner = s7.run

    def fake_run(cmd, check=True, capture=False, env=None):
        if cmd[:3] == ["git", "ls-tree", "-r"]:
            calls.append(cmd)
            return SimpleNamespace(stdout=LISTING, returncode=0)
        return inner(cmd, check=check, capture=capture, env=env)

    monkeypatch.setattr(s7, "run", fake_run)
    return calls


def test_only_this_runs_files_are_checked_out_and_committed(stamped, tmp_path):
    s7.promote("o", "run1", MANIFEST, str(tmp_path))
    want = ["orgs/o/models/a__A-1__T1.table.tml", "orgs/o/memory/memory__T1.yaml",
            "orgs/o/instructions/instructions__T1.json"]
    checkout = next(c for c in stamped if c[:2] == ["git", "checkout"] and "--" in c)
    assert checkout[checkout.index("--") + 1:] == want
    add = next(c for c in stamped if c[:2] == ["git", "add"])
    assert add[2:] == want
    assert not any(c[:3] == ["git", "checkout", "origin/ts-dev"] and c[-1] == "orgs/o/" for c in stamped)


def test_no_files_for_this_run_opens_no_pr(stamped, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("TIMESTAMP", "T9")
    assert s7.promote("o", "run1", MANIFEST, str(tmp_path)) == ""
    assert _gh(stamped, "create") == []
    assert "No files stamped T9" in capsys.readouterr().out


def test_without_a_timestamp_the_whole_org_folder_is_used(calls, tmp_path, monkeypatch):
    monkeypatch.delenv("TIMESTAMP", raising=False)
    s7.promote("o", "run1", MANIFEST, str(tmp_path))
    assert ["git", "checkout", "origin/ts-dev", "--", "orgs/o/"] in calls


# ---------------------------------------------------------------- honest auto-merge reporting
@pytest.fixture
def refusing(monkeypatch, calls):
    """gh pr merge --auto fails the way it did on the real run."""
    inner = s7.run

    def fake_run(cmd, check=True, capture=False, env=None):
        if cmd[:3] == ["gh", "pr", "merge"]:
            calls.append(cmd)
            return SimpleNamespace(stdout="", returncode=1, stderr=(
                "GraphQL: Auto merge is not allowed for this repository (enablePullRequestAutoMerge)\n"))
        return inner(cmd, check=check, capture=capture, env=env)

    monkeypatch.setattr(s7, "run", fake_run)
    return calls


def test_a_refused_auto_merge_is_reported_and_the_body_corrected(refusing, tmp_path, capsys):
    result = {}
    url = s7.promote("o", "run1", MANIFEST, str(tmp_path), result=result)
    assert url == PR_URL                                        # the PR still exists
    cap = capsys.readouterr()
    assert "Could not enable auto-merge" in cap.err and "not allowed for this repository" in cap.err
    assert "::warning::Stage 7 could not enable auto-merge" in cap.out
    assert result["auto_merge"] is False and "not allowed" in result["auto_merge_error"]
    edit = _gh(refusing, "edit")[0]
    body = _arg(edit, "--body")
    assert "Auto-merge could not be enabled" in body and "stays open until a person merges it" in body
    assert "Auto-merge is enabled" not in body


def test_a_successful_auto_merge_leaves_the_body_alone(calls, tmp_path, capsys):
    result = {}
    s7.promote("o", "run1", MANIFEST, str(tmp_path), result=result)
    assert result == {"pr_url": PR_URL, "auto_merge": True}
    assert _gh(calls, "edit") == []
    assert "Auto-merge enabled on the promote PR" in capsys.readouterr().out


def test_main_writes_the_promote_artifact(calls, tmp_path, monkeypatch):
    mf = tmp_path / "manifest.json"
    mf.write_text('[{"type":"LOGICAL_TABLE","folder":"models","name":"M","obj_id":"m1","source_guid":"g1"}]')
    out = tmp_path / "stage7_promote.json"
    for k, v in dict(ORG_KEY="o", RUN_ID="r", MANIFEST_OUT=str(mf), REPO_ROOT=str(tmp_path),
                     PROMOTE_OUT=str(out)).items():
        monkeypatch.setenv(k, v)
    s7.main()
    import json
    assert json.loads(out.read_text()) == {"pr_url": PR_URL, "auto_merge": True}
