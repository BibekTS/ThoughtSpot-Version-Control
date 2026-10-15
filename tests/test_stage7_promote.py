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
