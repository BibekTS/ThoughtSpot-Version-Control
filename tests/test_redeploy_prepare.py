"""Redeploy: refuse refs outside ts-prod, restore the org folder exactly, rebuild the run inputs."""
import json
import subprocess
from pathlib import Path

import pytest

from scripts import redeploy_prepare as rp


def git(repo, *args):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": __import__("os").environ["PATH"],
           "HOME": str(repo)}
    return subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True,
                          text=True, env=env).stdout.strip()


def write(repo, rel, body):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(body if isinstance(body, str) else json.dumps(body))


def tml(obj_id, kind="table", name=None, **extra):
    return {"obj_id": obj_id, kind: {"name": name or obj_id, **extra}}


@pytest.fixture
def repo(tmp_path):
    """main = ts-prod. c1 has T-1 and L-1; c2 updates T-1 and adds M-1; a side branch is unmerged."""
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "orgs/o/tables/t_one__T-1__20260101T000000Z.table.tml", tml("T-1", v=1))
    write(tmp_path, "orgs/o/liveboards/lb__L-1__20260101T000000Z.liveboard.tml",
          tml("L-1", "liveboard"))
    write(tmp_path, "orgs/o/memory/memory__20260101T000000Z.yaml", "memories: []\n")
    git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "c1"); git(tmp_path, "tag", "v1")
    write(tmp_path, "orgs/o/tables/t_one__T-1__20260201T000000Z.table.tml", tml("T-1", v=2))
    write(tmp_path, "orgs/o/models/m__M-1__20260201T000000Z.table.tml", tml("M-1", "model"))
    git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "c2"); git(tmp_path, "tag", "v2")
    git(tmp_path, "checkout", "-qb", "side")
    write(tmp_path, "orgs/o/tables/x__X-1__20260301T000000Z.table.tml", tml("X-1"))
    git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "unreviewed"); git(tmp_path, "tag", "unreviewed")
    git(tmp_path, "checkout", "-q", "main")
    return tmp_path


# ---- resolve_ref ------------------------------------------------------------------------- #

def test_tags_and_commits_on_ts_prod_resolve(repo):
    assert rp.resolve_ref("v1", repo, "main") == git(repo, "rev-parse", "v1^{commit}")
    sha = git(repo, "rev-parse", "v2")
    assert rp.resolve_ref(sha, repo, "main") == sha


def test_a_ref_outside_ts_prod_history_is_refused(repo):
    with pytest.raises(rp.RedeployError, match="not part of main"):
        rp.resolve_ref("unreviewed", repo, "main")


def test_unknown_and_blank_refs_are_refused(repo):
    with pytest.raises(rp.RedeployError, match="Cannot resolve"):
        rp.resolve_ref("nope", repo, "main")
    with pytest.raises(rp.RedeployError, match="required"):
        rp.resolve_ref("  ", repo, "main")


def test_a_ref_that_looks_like_an_option_is_not_treated_as_one(repo):
    with pytest.raises(rp.RedeployError):
        rp.resolve_ref("--all", repo, "main")


# ---- restore_org ------------------------------------------------------------------------- #

def test_restore_makes_the_org_folder_match_the_ref_exactly(repo):
    sha = rp.resolve_ref("v1", repo, "main")
    # the working tree is at v2 and also holds a stray local file under orgs/o
    write(repo, "orgs/o/tables/stray__S-1.table.tml", tml("S-1"))
    count = rp.restore_org("o", sha, repo)
    names = sorted(p.name for p in (repo / "orgs/o").rglob("*") if p.is_file())
    assert count == 3 and len(names) == 3
    assert not any("M-1" in n or "S-1" in n or "20260201" in n for n in names)


def test_restore_errors_when_the_org_has_no_content_at_the_ref(repo):
    sha = rp.resolve_ref("v1", repo, "main")
    with pytest.raises(rp.RedeployError, match="no content for org 'other'"):
        rp.restore_org("other", sha, repo)


# ---- build_inputs ------------------------------------------------------------------------ #

def test_newest_file_per_obj_id_wins_and_other_files_are_ignored(repo):
    manifest, results = rp.build_inputs("o", repo)
    by_id = {e["obj_id"]: e for e in manifest}
    assert set(by_id) == {"T-1", "L-1", "M-1"}                      # memory yaml is not an object
    assert results[[r["obj_id"] for r in results].index("T-1")]["tml_path"].endswith(
        "t_one__T-1__20260201T000000Z.table.tml")
    assert by_id["T-1"]["type"] == "LOGICAL_TABLE" and by_id["T-1"]["folder"] == "tables"
    assert by_id["L-1"]["type"] == "LIVEBOARD" and by_id["M-1"]["folder"] == "models"
    assert all(e["reason"] == "redeploy" and e["other_tags"] == [] for e in manifest)
    assert all(r["status"] == "success" for r in results)


def test_manifest_follows_import_order(repo):
    manifest, _ = rp.build_inputs("o", repo)
    assert [e["folder"] for e in manifest] == ["tables", "models", "liveboards"]


def test_legacy_file_without_a_timestamp_loses_to_a_timestamped_one(tmp_path):
    write(tmp_path, "orgs/o/tables/a__A-1.table.tml", tml("A-1", v="old"))
    write(tmp_path, "orgs/o/tables/a__A-1__20260101T000000Z.table.tml", tml("A-1", v="new"))
    _, results = rp.build_inputs("o", tmp_path)
    assert len(results) == 1 and "20260101T000000Z" in results[0]["tml_path"]


def test_obj_id_and_name_come_from_the_tml_not_the_file_name(tmp_path):
    # slugs can hold runs of underscores, so splitting the file name would misread this one
    write(tmp_path, "orgs/o/liveboards/a___b__data-41e28ff7__20260101T000000Z.liveboard.tml",
          tml("data-41e28ff7", "liveboard", name="A & B"))
    manifest, _ = rp.build_inputs("o", tmp_path)
    assert (manifest[0]["obj_id"], manifest[0]["name"]) == ("data-41e28ff7", "A & B")


def test_unusable_files_are_skipped_with_a_warning(tmp_path, capsys):
    write(tmp_path, "orgs/o/tables/ok__A-1.table.tml", tml("A-1"))
    write(tmp_path, "orgs/o/tables/bad__B-1.table.tml", "not json")
    write(tmp_path, "orgs/o/tables/noid__C-1.table.tml", {"table": {"name": "x"}})
    write(tmp_path, "orgs/o/tables/weird.txt", "x")
    manifest, _ = rp.build_inputs("o", tmp_path)
    assert [e["obj_id"] for e in manifest] == ["A-1"]
    err = capsys.readouterr().err
    assert "bad__B-1" in err and "noid__C-1" in err


# ---- main -------------------------------------------------------------------------------- #

def _run_main(repo, monkeypatch, ref):
    for k, v in {"ORG_KEY": "o", "REF": ref, "REPO_ROOT": str(repo), "PROD_REF": "main",
                 "MANIFEST_OUT": str(repo / "manifest.json"),
                 "EXPORT_OUT": str(repo / "export.json"),
                 "REDEPLOY_OUT": str(repo / "ref.json")}.items():
        monkeypatch.setenv(k, v)
    rp.main()


def test_main_writes_the_run_inputs_for_the_ref(repo, monkeypatch, capsys):
    _run_main(repo, monkeypatch, "v1")
    manifest = json.loads((repo / "manifest.json").read_text())
    assert {e["obj_id"] for e in manifest} == {"T-1", "L-1"}                  # M-1 is after v1
    info = json.loads((repo / "ref.json").read_text())
    assert info["ref"] == "v1" and info["objects"] == 2
    assert "2 object(s)" in capsys.readouterr().out


def test_main_exits_non_zero_for_a_ref_outside_ts_prod(repo, monkeypatch):
    with pytest.raises(SystemExit) as exc:
        _run_main(repo, monkeypatch, "unreviewed")
    assert exc.value.code == 1
    assert not (repo / "manifest.json").exists()
