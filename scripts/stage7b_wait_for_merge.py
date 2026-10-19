#!/usr/bin/env python3
"""
Stage 7b: Wait for the promote PR to be approved and merged into ts-prod.

The import job runs after this step, so nothing is imported into the target until a
reviewer has approved the PR (branch protection on ts-prod requires the approval) and
it has merged. Stage 7 asks GitHub to auto-merge the PR, so the merge normally follows
the approval on its own; if auto-merge was refused, a person merges it.

Outcome:
  merged                -> exit 0, merge commit recorded in stage7b_wait.json
  closed without merge  -> exit 1 (the import is skipped, the migration tag stays)
  timeout               -> exit 1 (the PR stays open; re-run once it is merged)

Skipped (exit 0) on a dry run, and when Stage 7 opened no PR (nothing new to promote).
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Callable

sys.path.insert(0, str(Path(__file__).parent))
from cleanup_promote_branch import delete_branch

MAX_CONSECUTIVE_ERRORS = 10


def get_pr_state(pr_url: str) -> dict:
    """Current PR state via gh: {"state": OPEN|CLOSED|MERGED, "merge_commit": sha|None}."""
    out = subprocess.run(
        ["gh", "pr", "view", pr_url, "--json", "state,mergeCommit"],
        check=True, capture_output=True, text=True,
    ).stdout
    data = json.loads(out)
    merge_commit = (data.get("mergeCommit") or {}).get("oid")
    return {"state": data.get("state", ""), "merge_commit": merge_commit}


def wait_for_merge(
    pr_url: str,
    timeout_s: float,
    interval_s: float,
    fetch: Callable[[str], dict] = get_pr_state,
    sleep: Callable[[float], None] = time.sleep,
    clock: Callable[[], float] = time.monotonic,
) -> dict:
    """Poll until the PR merges. Returns a result dict with status merged|closed|timeout."""
    deadline = clock() + timeout_s
    errors = 0
    last_state = ""
    while True:
        try:
            info = fetch(pr_url)
            errors = 0
        except Exception as exc:  # gh/network hiccup: retry, but not forever
            errors += 1
            print(f"[STAGE 7b][WARN] Could not read {pr_url} ({errors}/{MAX_CONSECUTIVE_ERRORS}): "
                  f"{exc}", file=sys.stderr)
            if errors >= MAX_CONSECUTIVE_ERRORS:
                return {"status": "error", "pr_url": pr_url, "error": str(exc)}
            info = None

        if info:
            state = info["state"]
            if state == "MERGED":
                return {"status": "merged", "pr_url": pr_url,
                        "merge_commit": info.get("merge_commit")}
            if state == "CLOSED":
                return {"status": "closed", "pr_url": pr_url}
            if state != last_state:
                print(f"[STAGE 7b] {pr_url} is {state or 'unknown'}; waiting for approval "
                      "and merge...")
                last_state = state

        if clock() >= deadline:
            return {"status": "timeout", "pr_url": pr_url}
        sleep(interval_s)


def write_output(name: str, value: str) -> None:
    """Expose a step output to later jobs (the import pins its TML to the merge commit)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if path:
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(f"{name}={value}\n")


def main() -> None:
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    promote_path = Path(os.environ.get("PROMOTE_OUT", "stage7_promote.json"))
    out_path = Path(os.environ.get("WAIT_OUT", "stage7b_wait.json"))

    if dry_run:
        print("[STAGE 7b] DRY_RUN=true — not waiting for the promote PR (it is never merged "
              "automatically on a dry run).")
        out_path.write_text(json.dumps({"status": "skipped", "reason": "dry run"}, indent=2))
        return

    promote = json.loads(promote_path.read_text()) if promote_path.exists() else {}
    pr_url = promote.get("pr_url", "")
    if not pr_url:
        print("[STAGE 7b] Stage 7 opened no PR (nothing new to promote) — not waiting.")
        out_path.write_text(json.dumps({"status": "skipped", "reason": "no PR"}, indent=2))
        return

    if promote.get("auto_merge") is False:
        print("::notice::Auto-merge is not enabled on the promote PR; a person must approve "
              f"and merge {pr_url} before the import starts.")

    timeout_min = float(os.environ.get("MERGE_WAIT_TIMEOUT_MIN", "330"))
    interval_s = float(os.environ.get("MERGE_WAIT_INTERVAL_S", "30"))
    print(f"[STAGE 7b] Waiting up to {timeout_min:g} min for {pr_url} to be approved and merged.")

    result = wait_for_merge(pr_url, timeout_min * 60, interval_s)
    out_path.write_text(json.dumps(result, indent=2), encoding="utf-8")

    # The PR is finished with once it merged or was closed; its branch is then just clutter.
    # Best effort: a failure here must never fail the run (the cleanup workflow is the backstop).
    branch = promote.get("branch", "")
    if result["status"] in ("merged", "closed") and branch:
        try:
            delete_branch(os.environ.get("GITHUB_REPOSITORY", ""), branch)
        except Exception as exc:
            print(f"[STAGE 7b][WARN] Could not clean up {branch}: {exc}", file=sys.stderr)

    if result["status"] == "merged":
        sha = result.get("merge_commit") or ""
        print(f"[STAGE 7b] Merged ({sha or 'unknown commit'}) — continuing to import.")
        write_output("merge_sha", sha)
        return
    reasons = {
        "closed": "was closed without merging",
        "timeout": f"was not merged within {timeout_min:g} minutes",
        "error": f"could not be read: {result.get('error')}",
    }
    print(f"[STAGE 7b][ERROR] Promote PR {pr_url} {reasons[result['status']]}. "
          "Nothing was imported; the migration tag is kept.", file=sys.stderr)
    sys.exit(1)


if __name__ == "__main__":
    main()
