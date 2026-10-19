#!/usr/bin/env python3
"""
Stage 7: Promote TML from ts-dev to ts-prod via PR.

Creates branch promote/<org_key>/<run_id> from ts-prod,
copies from ts-dev only this run's files under orgs/<org_key>/ (the ones stamped with the
run's TIMESTAMP; the whole org folder when TIMESTAMP is not set),
opens a PR into ts-prod, and asks GitHub to auto-merge it when checks pass. If GitHub
refuses (for example "Allow auto-merge" is off in the repository settings), the run says so
and the PR body is rewritten to say a person has to merge it.

Dry run (DRY_RUN=true): the PR is still opened so the export can be inspected, but it
is titled "[DRY RUN]" and auto-merge is NOT enabled, so dry-run content never lands on
ts-prod unless a person merges it.

Stage 7b (stage7b_wait_for_merge) then waits for the PR to be approved and merged before the
import job starts. Approval is enforced by branch protection on ts-prod.

Needs a token with contents:write + pull-requests:write. The workflow passes it as GH_TOKEN: a
GitHub App's token when the repository has GH_APP_CLIENT_ID / GH_APP_PRIVATE_KEY (so the PR is
authored by the app and a person can approve it), else GH_AUTOMATION_TOKEN, else GITHUB_TOKEN.
GITHUB_TOKEN cannot merge its own PRs, and nobody can approve a PR they authored; see
docs/runbook.md, "Approving the promote PRs as a solo maintainer".
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


def run_files(listing: str, timestamp: str) -> list[str]:
    """Paths from a `git ls-tree -r --name-only` listing that belong to this run.

    Exports are written as <stem>__<TIMESTAMP>.<ext>, so a run's files are the ones carrying
    its TIMESTAMP. Older runs' files already live on ts-dev and are not part of this PR.
    """
    marker = f"__{timestamp}."
    return [p for p in listing.splitlines() if p.strip() and marker in Path(p).name]


_WAIT_NOTE = ("The `import` job waits for this PR to be approved and merged (Stage 7b), then "
              "imports the merged content. If the PR is closed without merging, nothing is "
              "imported and the migration tag stays.")


def _pr_body(org_key: str, run_id: str, counts_lines: str, dry_run: bool,
             auto_merge_failed: bool = False) -> str:
    dry_banner = ""
    if dry_run:
        dry_banner = textwrap.dedent("""\
            > **DRY RUN.** Auto-merge is not enabled on this PR. It exists so the export
            > can be inspected. Close it, or merge it only if you intend to promote this
            > content.

        """)
        what_this_is = ("This PR holds the exports from a dry run (TML, plus Spotter memory and "
                        "NL instructions where present).")
        wait_note = ("Nothing waits for this PR on a dry run: the run validates only and "
                     "imports nothing.")
    elif auto_merge_failed:
        what_this_is = ("This PR records what this run promoted to `ts-prod` (TML, plus Spotter "
                        "memory and NL instructions where present). **Auto-merge could not be "
                        "enabled** (GitHub refused it; see the run's Stage 7 log), so this PR "
                        "stays open until a person merges it. Turn on \"Allow auto-merge\" in "
                        "the repository settings for it to merge itself.")
        wait_note = _WAIT_NOTE
    else:
        what_this_is = ("This PR records what this run promoted to `ts-prod` (TML, plus Spotter "
                        "memory and NL instructions where present). Auto-merge is enabled, so "
                        "GitHub merges it once it has been approved and its other merge "
                        "requirements are met.")
        wait_note = _WAIT_NOTE
    # Dedent the template BEFORE substituting: counts_lines is multi-line, and substituting
    # first leaves its later lines unindented, which defeats dedent and makes GitHub render
    # the whole body as an indented code block.
    return dry_banner + textwrap.dedent("""\
        ## ThoughtSpot TML Promotion

        **Org:** `{org_key}`  
        **Run:** `{run_id}`

        ### Manifest summary
        {counts_lines}

        {what_this_is}

        {wait_note}
    """).format(org_key=org_key, run_id=run_id, counts_lines=counts_lines,
                what_this_is=what_this_is, wait_note=wait_note)


def promote(org_key: str, run_id: str, manifest: list[dict], repo_root: str = ".",
            dry_run: bool = False, result: dict | None = None) -> str:
    """Create promote branch, apply this run's files, open PR. Returns PR URL.

    `result`, when given, is filled with {"pr_url", "files", "auto_merge"} for the artifact.
    """
    result = result if result is not None else {}
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

    timestamp = os.environ.get("TIMESTAMP", "")
    if timestamp:
        listing = run(["git", "ls-tree", "-r", "--name-only", f"origin/{DEV_BRANCH}", org_path],
                      capture=True).stdout
        files = run_files(listing, timestamp)
        if not files:
            print(f"[STAGE 7] No files stamped {timestamp} under '{org_path}' on {DEV_BRANCH} "
                  "— nothing to promote.")
            return ""
        run(["git", "checkout", f"origin/{DEV_BRANCH}", "--", *files])
        add_paths = files
        result["files"] = len(files)
    else:
        # Not running inside the workflow: no run stamp, so take the whole org folder.
        run(["git", "checkout", f"origin/{DEV_BRANCH}", "--", org_path])
        add_paths = [org_path]

    status = run(["git", "status", "--porcelain", *add_paths], capture=True)
    if not status.stdout.strip():
        print(f"[STAGE 7] No changes to promote for org '{org_key}'.")
        return ""

    commit_msg = f"chore: promote TML for org '{org_key}' run {run_id}"
    run(["git", "add", *add_paths])
    run(["git", "commit", "-m", commit_msg])
    run(["git", "push", "-u", "origin", promote_branch],
        env={"GIT_ASKPASS": "echo", "GIT_TOKEN": gh_token,
             "GH_TOKEN": gh_token})

    # Count manifest by type
    counts: dict[str, int] = {}
    for e in manifest:
        counts[e["type"]] = counts.get(e["type"], 0) + 1
    counts_lines = "\n".join(f"- {t}: {n}" for t, n in sorted(counts.items()))

    pr_body = _pr_body(org_key, run_id, counts_lines, dry_run)

    created = run(
        ["gh", "pr", "create",
         "--repo", GH_REPO,
         "--head", promote_branch,
         "--base", PROD_BRANCH,
         "--title", f"{'[DRY RUN] ' if dry_run else ''}[{org_key}] Promote TML run {run_id}",
         "--body", pr_body],
        env={"GH_TOKEN": gh_token},
        capture=True,
    )
    pr_url = created.stdout.strip()
    print(f"[STAGE 7] PR created: {pr_url}")
    result["pr_url"] = pr_url

    if dry_run:
        print("[STAGE 7] DRY_RUN=true — auto-merge NOT enabled on the promote PR.")
        result["auto_merge"] = False
    else:
        merged = run(["gh", "pr", "merge", pr_url, "--auto", "--squash"],
                     env={"GH_TOKEN": gh_token}, check=False, capture=True)
        if merged.returncode == 0:
            print("[STAGE 7] Auto-merge enabled on the promote PR.")
            result["auto_merge"] = True
        else:
            lines = (merged.stderr or merged.stdout or "").strip().splitlines()
            reason = lines[0] if lines else f"gh exited {merged.returncode}"
            print(f"[STAGE 7][WARN] Could not enable auto-merge on {pr_url}: {reason}. "
                  "The PR stays open for a person to merge.", file=sys.stderr)
            print(f"::warning::Stage 7 could not enable auto-merge on {pr_url}: {reason}")
            result["auto_merge"] = False
            result["auto_merge_error"] = reason
            # The body was written assuming it would work; say what actually happened.
            run(["gh", "pr", "edit", pr_url, "--body",
                 _pr_body(org_key, run_id, counts_lines, dry_run, auto_merge_failed=True)],
                env={"GH_TOKEN": gh_token}, check=False, capture=True)

    return pr_url


def main():
    org_key = os.environ["ORG_KEY"]
    run_id = os.environ.get("RUN_ID", "local")
    manifest_path = os.environ.get("MANIFEST_OUT", f"/tmp/ts_migration_{org_key}_manifest.json")
    repo_root = os.environ.get("REPO_ROOT", ".")
    manifest = json.loads(Path(manifest_path).read_text())
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    # Resolved before promote() changes directory, so the artifact lands where the workflow looks.
    out_path = Path(os.environ.get("PROMOTE_OUT", "stage7_promote.json")).resolve()
    result: dict = {}
    promote(org_key, run_id, manifest, repo_root, dry_run=dry_run, result=result)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
