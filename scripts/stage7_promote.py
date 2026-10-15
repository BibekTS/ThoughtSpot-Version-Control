#!/usr/bin/env python3
"""
Stage 7: Promote TML from ts-dev to ts-prod via PR.

Creates branch promote/<org_key>/<run_id> from ts-prod,
cherry-picks only orgs/<org_key>/ from ts-dev,
opens a PR into ts-prod, and auto-merges when checks pass.

Dry run (DRY_RUN=true): the PR is still opened so the export can be inspected, but it
is titled "[DRY RUN]" and auto-merge is NOT enabled, so dry-run content never lands on
ts-prod unless a person merges it.

Requires GH_AUTOMATION_TOKEN (PAT with contents:write + pull-requests:write).
GITHUB_TOKEN cannot merge its own PRs — use GH_AUTOMATION_TOKEN.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

PROD_BRANCH = os.environ.get("PROD_BRANCH", "ts-prod")
DEV_BRANCH = os.environ.get("DEV_BRANCH", "ts-dev")
GH_REPO = os.environ.get("GITHUB_REPOSITORY", "")


def run(cmd: list[str], check: bool = True, capture: bool = False,
        env: dict | None = None) -> subprocess.CompletedProcess:
    import os as _os
    merged_env = {**_os.environ, **(env or {})}
    return subprocess.run(cmd, check=check, capture_output=capture, text=True, env=merged_env)


def promote(org_key: str, run_id: str, manifest: list[dict], repo_root: str = ".",
            dry_run: bool = False) -> str:
    """Create promote branch, apply org TML changes, open PR. Returns PR URL."""
    gh_token = (
        os.environ.get("GH_AUTOMATION_TOKEN")
        or os.environ.get("GH_TOKEN")
        or os.environ.get("GITHUB_TOKEN", "")
    )
    if not gh_token:
        print("[ERROR] No GitHub token found (set GH_AUTOMATION_TOKEN, GH_TOKEN, or GITHUB_TOKEN).",
              file=sys.stderr)
        sys.exit(1)

    if not manifest:
        print(f"[STAGE 7] Manifest is empty — 0 objects discovered, nothing to promote.")
        return ""

    os.chdir(repo_root)
    run(["git", "config", "user.email", "github-actions@github.com"])
    run(["git", "config", "user.name", "GitHub Actions"])

    promote_branch = f"promote/{org_key}/{run_id}"
    run(["git", "fetch", "origin", PROD_BRANCH])
    run(["git", "fetch", "origin", DEV_BRANCH])
    run(["git", "checkout", "-B", promote_branch, f"origin/{PROD_BRANCH}"])

    # Check if org TML exists on ts-dev before attempting checkout
    org_path = f"orgs/{org_key}/"
    ls_result = run(["git", "ls-tree", f"origin/{DEV_BRANCH}", org_path],
                    capture=True, check=False)
    if not ls_result.stdout.strip():
        print(f"[STAGE 7] No TML found at '{org_path}' on {DEV_BRANCH} — nothing to promote.")
        return ""

    run(["git", "checkout", f"origin/{DEV_BRANCH}", "--", org_path])

    status = run(["git", "status", "--porcelain", org_path], capture=True)
    if not status.stdout.strip():
        print(f"[STAGE 7] No changes to promote for org '{org_key}'.")
        return ""

    commit_msg = f"chore: promote TML for org '{org_key}' run {run_id}"
    run(["git", "add", org_path])
    run(["git", "commit", "-m", commit_msg])
    run(["git", "push", "-u", "origin", promote_branch],
        env={"GIT_ASKPASS": "echo", "GIT_TOKEN": gh_token,
             "GH_TOKEN": gh_token})

    # Count manifest by type
    counts: dict[str, int] = {}
    for e in manifest:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    counts_lines = "\n".join(f"- {t}: {n}" for t, n in sorted(counts.items()))

    dry_banner = ""
    if dry_run:
        dry_banner = textwrap.dedent("""\
            > **DRY RUN.** Auto-merge is not enabled on this PR. It exists so the export
            > can be inspected. Close it, or merge it only if you intend to promote this
            > content.

        """)
    pr_body = dry_banner + textwrap.dedent(f"""\
        ## ThoughtSpot TML Promotion

        **Org:** `{org_key}`  
        **Run:** `{run_id}`

        ### Manifest summary
        {counts_lines}

        Merge this PR to trigger the `import` job which will load the TML into the
        production ThoughtSpot cluster (requires `prod` environment approval if configured).
    """)

    result = run(
        ["gh", "pr", "create",
         "--repo", GH_REPO,
         "--head", promote_branch,
         "--base", PROD_BRANCH,
         "--title", f"{'[DRY RUN] ' if dry_run else ''}[{org_key}] Promote TML run {run_id}",
         "--body", pr_body],
        env={"GH_TOKEN": gh_token},
        capture=True,
    )
    pr_url = result.stdout.strip()
    print(f"[STAGE 7] PR created: {pr_url}")

    if dry_run:
        print("[STAGE 7] DRY_RUN=true — auto-merge NOT enabled on the promote PR.")
    else:
        # Enable auto-merge
        run(["gh", "pr", "merge", pr_url, "--auto", "--squash"],
            env={"GH_TOKEN": gh_token}, check=False)

    return pr_url


def main():
    org_key = os.environ["ORG_KEY"]
    run_id = os.environ.get("RUN_ID", "local")
    manifest_path = os.environ.get("MANIFEST_OUT", f"/tmp/ts_migration_{org_key}_manifest.json")
    repo_root = os.environ.get("REPO_ROOT", ".")
    manifest = json.loads(Path(manifest_path).read_text())
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    promote(org_key, run_id, manifest, repo_root, dry_run=dry_run)


if __name__ == "__main__":
    main()
