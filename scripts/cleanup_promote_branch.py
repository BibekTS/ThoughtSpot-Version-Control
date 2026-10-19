#!/usr/bin/env python3
"""
Delete promote/<org>/<run_id> branches once their pull request is finished with.

Stage 7 creates one such branch per run and nothing removed it, so they piled up. Three callers:

  * Stage 7b, right after the promote PR merged or was closed unmerged;
  * cleanup-promote-branches.yml, when any promote PR is closed (covers dry-run PRs closed by
    hand, and a merge that Stage 7b was not around to see);
  * the same workflow's manual "sweep", which clears branches left over from earlier runs.

A branch is only deleted when it
  - is named promote/<org>/<run_id> (nothing else, whatever it is called by the caller), and
  - has no open pull request.
Anything else is left alone. A branch that is already gone counts as success, so racing callers
are harmless.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys

BRANCH_RE = re.compile(r"^promote/[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


def run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def is_promote_branch(name: str) -> bool:
    return bool(BRANCH_RE.match(name or ""))


def has_open_pr(repo: str, branch: str) -> bool:
    r = run(["gh", "pr", "list", "--repo", repo, "--head", branch, "--state", "open",
             "--json", "number"])
    if r.returncode != 0:
        raise RuntimeError(f"could not list PRs for {branch}: {(r.stderr or r.stdout).strip()}")
    return bool(json.loads(r.stdout or "[]"))


def delete_branch(repo: str, branch: str) -> str:
    """One of: deleted, gone, refused (not a promote branch), kept (open PR), failed."""
    if not is_promote_branch(branch):
        print(f"[CLEANUP] Not touching '{branch}': not a promote/<org>/<run_id> branch.",
              file=sys.stderr)
        return "refused"
    try:
        if has_open_pr(repo, branch):
            print(f"[CLEANUP] Keeping {branch}: it still has an open pull request.")
            return "kept"
    except RuntimeError as exc:
        print(f"[CLEANUP][WARN] {exc}", file=sys.stderr)
        return "failed"
    r = run(["gh", "api", "-X", "DELETE", f"repos/{repo}/git/refs/heads/{branch}"])
    if r.returncode == 0:
        print(f"[CLEANUP] Deleted {branch}.")
        return "deleted"
    out = f"{r.stderr}\n{r.stdout}"
    if "Reference does not exist" in out or "Not Found" in out or "HTTP 404" in out:
        print(f"[CLEANUP] {branch} was already gone.")
        return "gone"
    print(f"[CLEANUP][WARN] Could not delete {branch}: {out.strip()[:200]}", file=sys.stderr)
    return "failed"


def list_promote_branches(repo: str) -> list[str]:
    r = run(["gh", "api", "--paginate", f"repos/{repo}/branches?per_page=100", "--jq",
             ".[].name"])
    if r.returncode != 0:
        raise RuntimeError(f"could not list branches: {(r.stderr or r.stdout).strip()}")
    return sorted(n for n in r.stdout.split() if is_promote_branch(n))


def sweep(repo: str, apply: bool) -> dict[str, int]:
    """Delete (or, without `apply`, list) every promote branch that has no open PR."""
    counts: dict[str, int] = {}
    for branch in list_promote_branches(repo):
        if apply:
            outcome = delete_branch(repo, branch)
        else:
            try:
                outcome = "kept" if has_open_pr(repo, branch) else "would delete"
            except RuntimeError as exc:
                print(f"[CLEANUP][WARN] {exc}", file=sys.stderr)
                outcome = "failed"
            print(f"[CLEANUP] {outcome}: {branch}")
        counts[outcome] = counts.get(outcome, 0) + 1
    print(f"[CLEANUP] Sweep {'result' if apply else 'preview'}: {counts or 'nothing to do'}.")
    return counts


def main() -> None:
    repo = os.environ.get("REPO") or os.environ["GITHUB_REPOSITORY"]
    if os.environ.get("MODE", "delete") == "sweep":
        counts = sweep(repo, apply=os.environ.get("DRY_RUN", "true").lower() != "true")
        sys.exit(1 if counts.get("failed") else 0)
    outcome = delete_branch(repo, os.environ.get("BRANCH", ""))
    sys.exit(1 if outcome == "failed" else 0)


if __name__ == "__main__":
    main()
