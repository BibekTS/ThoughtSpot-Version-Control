"""A redeploy runs the normal Stages 5 and 9 on inputs rebuilt from git, and imports the OLD content."""
import json
import os
import subprocess

import pytest

from scripts import redeploy_prepare as rp
from scripts import stage5_sanitize as s5
from scripts import stage9_import as s9
from scripts import stage11_report as s11

CFG = {
    "org_key": "o", "migration_tag": "o_ready",
    "source": {"base_url": "https://s", "org_id": 1},
    "target": {"base_url": "https://t", "org_id": 2},
    "variables": {"database": {"ts_variable_name": "db", "source_value": "DEV_DB", "target_value": "PROD_DB"},
                  "schema": {"ts_variable_name": "sch", "source_value": "DEV_SCHEMA", "target_value": "PROD_SCHEMA"}},
}


def git(repo, *args):
    env = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t", "GIT_COMMITTER_NAME": "t",
           "GIT_COMMITTER_EMAIL": "t@t", "PATH": os.environ["PATH"], "HOME": str(repo)}
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def write(repo, rel, body):
    p = repo / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(body))


def table(version):
    return {"obj_id": "T-1", "table": {"name": "T", "db": "DEV_DB", "schema": "DEV_SCHEMA",
                                        "db_table": "T", "connection": {"name": "Snow"}, "version": version}}


@pytest.fixture
def run(tmp_path, monkeypatch):
    git(tmp_path, "init", "-q", "-b", "main")
    write(tmp_path, "orgs/o/tables/t__T-1__20260101T000000Z.table.tml", table("one"))
    git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "c1"); git(tmp_path, "tag", "v1")
    write(tmp_path, "orgs/o/tables/t__T-1__20260201T000000Z.table.tml", table("two"))
    write(tmp_path, "config.json", CFG)
    git(tmp_path, "add", "-A"); git(tmp_path, "commit", "-qm", "c2")

    imported = []

    class FakeClient:
        def __init__(self, *a, **k): pass
        def authenticate(self): pass
        def find_guid_by_obj_id(self, *a): return "tgt-guid"
        def parameterize_fields(self, **k): pass
        def import_tml(self, docs, **k):
            imported.append(json.loads(docs[0]))
            return [{"response": {"status": {"status_code": "OK"}}}]
        def export_tml(self, *a, **k):
            return [{"edoc": json.dumps({"table": {"db": "PROD_DB", "schema": "PROD_SCHEMA"}})}]

    monkeypatch.setattr(s9, "TSClient", FakeClient)
    monkeypatch.setattr(s9, "resolve_secret", lambda n: "x")
    for k, v in {"ORG_KEY": "o", "REF": "v1", "REPO_ROOT": str(tmp_path), "PROD_REF": "main",
                 "CONFIG_OUT": str(tmp_path / "config.json"),
                 "MANIFEST_OUT": str(tmp_path / "manifest.json"),
                 "EXPORT_OUT": str(tmp_path / "export.json"),
                 "SANITIZE_OUT": str(tmp_path / "sanitize.json"),
                 "IMPORT_OUT": str(tmp_path / "import.json"),
                 "REDEPLOY_OUT": str(tmp_path / "ref.json"), "DRY_RUN": "false"}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.delenv("TIMESTAMP", raising=False)
    monkeypatch.chdir(tmp_path)
    return tmp_path, imported


def test_redeploying_a_tag_imports_that_tags_content_not_the_latest(run):
    repo, imported = run
    rp.main()
    s5.main()
    s9.main()
    assert [t["table"]["version"] for t in imported] == ["one"]
    # Path B still applies: the source db/schema are swapped for the target's on the way in
    assert imported[0]["table"]["db"] == "PROD_DB"
    results = json.loads((repo / "import.json").read_text())
    assert [(r["obj_id"], r["status"]) for r in results] == [("T-1", "success")]


def test_stage_5_finds_the_connection_in_the_rebuilt_inputs(run):
    repo, _ = run
    rp.main()
    s5.main()
    assert json.loads((repo / "sanitize.json").read_text())["connection_names"] == ["Snow"]


def test_the_report_builds_from_a_redeploys_files(run):
    repo, _ = run
    rp.main()
    s5.main()
    s9.main()
    report = s11.build_report(
        CFG, "r1", False,
        json.loads((repo / "manifest.json").read_text()),
        json.loads((repo / "export.json").read_text()),
        json.loads((repo / "import.json").read_text()), {}, [])
    assert [r.obj_id for r in report.succeeded] == ["T-1"]
    assert s11.run_failures(report) == ([], [])
