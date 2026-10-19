"""The promote PR is opened by a GitHub App when one is configured, so its owner can approve it."""
from pathlib import Path

import pytest
import yaml

WORKFLOW = Path(__file__).resolve().parent.parent / ".github/workflows/org-migration.yml"
TOKEN = "${{ steps.app.outputs.token || secrets.GH_AUTOMATION_TOKEN || github.token }}"


@pytest.fixture(scope="module")
def workflow():
    return yaml.safe_load(WORKFLOW.read_text())


@pytest.fixture(scope="module")
def steps(workflow):
    return workflow["jobs"]["promote"]["steps"]


def _step(steps, needle):
    return next(s for s in steps if needle in s.get("name", "") or needle in s.get("run", ""))


def test_the_app_token_step_comes_first_and_only_runs_when_configured(steps):
    app = steps[0]
    assert app["id"] == "app" and app["uses"].startswith("actions/create-github-app-token@")
    assert app["if"] == "${{ vars.GH_APP_CLIENT_ID != '' }}"
    assert app["with"]["client-id"] == "${{ vars.GH_APP_CLIENT_ID }}"
    assert app["with"]["private-key"] == "${{ secrets.GH_APP_PRIVATE_KEY }}"


def test_the_token_is_scoped_to_this_repository(steps):
    assert not {"owner", "repositories"} & set(steps[0]["with"])


def test_the_branch_push_and_pr_creation_use_the_app_token_first(steps):
    assert _step(steps, "Checkout ts-prod")["with"]["token"] == TOKEN
    assert _step(steps, "stage7_promote")["env"]["GH_TOKEN"] == TOKEN


def test_the_long_wait_uses_the_job_token_not_the_hour_long_app_token(steps):
    wait = _step(steps, "stage7b_wait_for_merge")
    assert wait["env"]["GH_TOKEN"] == "${{ github.token }}"


def test_no_job_level_token_overrides_the_per_step_choice(workflow):
    assert "GH_TOKEN" not in workflow["jobs"]["promote"]["env"]


def test_the_app_key_is_a_declared_optional_secret(workflow):
    secrets = workflow[True]["workflow_call"]["secrets"]     # YAML reads the key `on` as True
    assert secrets["GH_APP_PRIVATE_KEY"]["required"] is False
    assert secrets["GH_AUTOMATION_TOKEN"]["required"] is False
