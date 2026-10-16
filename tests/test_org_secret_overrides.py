"""An org can use its own credentials; every stage that authenticates must honor that."""
import json
import re
from pathlib import Path

import pytest
import yaml

from scripts import stage2_auth as s2
from scripts.lib.secrets import resolve_secret, secret_env_name

ROOT = Path(__file__).resolve().parent.parent
BASES = ("TS_SOURCE_USERNAME", "TS_SOURCE_SECRET_KEY", "TS_TARGET_USERNAME", "TS_TARGET_SECRET_KEY")


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for b in BASES:
        for name in (b, f"{b}__OVERRIDE", f"{b}_SALES"):
            monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("ORG_KEY", "sales")


def test_the_shared_secret_is_the_default(monkeypatch):
    monkeypatch.setenv("TS_SOURCE_SECRET_KEY", "shared")
    assert resolve_secret("TS_SOURCE_SECRET_KEY") == "shared"
    assert secret_env_name("TS_SOURCE_SECRET_KEY") == "TS_SOURCE_SECRET_KEY"


def test_the_workflow_override_beats_the_shared_secret(monkeypatch):
    monkeypatch.setenv("TS_SOURCE_SECRET_KEY", "shared")
    monkeypatch.setenv("TS_SOURCE_SECRET_KEY__OVERRIDE", "workflow")
    assert resolve_secret("TS_SOURCE_SECRET_KEY") == "workflow"


def test_an_org_suffixed_variable_beats_everything(monkeypatch):
    monkeypatch.setenv("TS_SOURCE_SECRET_KEY", "shared")
    monkeypatch.setenv("TS_SOURCE_SECRET_KEY__OVERRIDE", "workflow")
    monkeypatch.setenv("TS_SOURCE_SECRET_KEY_SALES", "local")
    assert resolve_secret("TS_SOURCE_SECRET_KEY") == "local"
    assert resolve_secret("TS_SOURCE_SECRET_KEY", "other") == "workflow"     # another org's suffix is ignored


def test_an_empty_override_falls_back(monkeypatch):
    """The workflow passes an empty string when the org has no override secret."""
    monkeypatch.setenv("TS_SOURCE_SECRET_KEY", "shared")
    monkeypatch.setenv("TS_SOURCE_SECRET_KEY__OVERRIDE", "")
    assert resolve_secret("TS_SOURCE_SECRET_KEY") == "shared"


def test_nothing_set_raises_keyerror():
    with pytest.raises(KeyError):
        resolve_secret("TS_SOURCE_SECRET_KEY")


def test_stage2_authenticates_each_side_with_its_own_credentials(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("TS_SOURCE_USERNAME", "src-user")
    monkeypatch.setenv("TS_SOURCE_SECRET_KEY", "src-shared")
    monkeypatch.setenv("TS_TARGET_USERNAME", "tgt-user")
    monkeypatch.setenv("TS_TARGET_SECRET_KEY", "tgt-shared")
    monkeypatch.setenv("TS_TARGET_SECRET_KEY__OVERRIDE", "tgt-sales")          # only the target is overridden
    cfg = tmp_path / "c.json"
    cfg.write_text(json.dumps({"org_key": "sales", "source": {"base_url": "s", "org_id": 1},
                               "target": {"base_url": "t", "org_id": 2}}))
    monkeypatch.setenv("CONFIG_OUT", str(cfg))
    used = []

    class FakeClient:
        def __init__(self, base_url, org_id, username, secret_key, **_):
            used.append((base_url, username, secret_key))

        def authenticate(self):
            pass

    monkeypatch.setattr(s2, "TSClient", FakeClient)
    s2.main()
    assert used == [("s", "src-user", "src-shared"), ("t", "tgt-user", "tgt-sales")]
    out = capsys.readouterr().out
    assert "TS_TARGET_SECRET_KEY__OVERRIDE for TS_TARGET_SECRET_KEY" in out
    assert "tgt-sales" not in out and "src-shared" not in out                  # names only, never values


def test_no_stage_reads_the_ts_credentials_directly():
    """Every stage that logs in must go through resolve_secret, or the override is skipped."""
    offenders = []
    for p in (ROOT / "scripts").glob("stage*.py"):
        if re.search(r'os\.environ(\.get)?\(?\[?\s*["\']TS_(SOURCE|TARGET)_(USERNAME|SECRET_KEY)', p.read_text()):
            offenders.append(p.name)
    assert offenders == []


def _jobs():
    return yaml.safe_load((ROOT / ".github/workflows/org-migration.yml").read_text())["jobs"]


def test_every_job_that_gets_the_credentials_also_gets_the_org_overrides():
    checked = 0
    for name, job in _jobs().items():
        env = job.get("env") or {}
        if "TS_SOURCE_SECRET_KEY" not in env:
            continue
        checked += 1
        for base in BASES:
            expr = env.get(f"{base}__OVERRIDE", "")
            assert f"secrets[format('{base}_{{0}}', inputs.org_key)]" in expr, (name, base)
    assert checked == 3          # extract, import, untag_source


def test_the_caller_passes_all_secrets_so_the_override_names_resolve():
    caller = yaml.safe_load((ROOT / ".github/workflows/ts-migration.yml").read_text())
    assert caller["jobs"]["migrate"]["secrets"] == "inherit"
