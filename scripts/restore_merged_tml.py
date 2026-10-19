#!/usr/bin/env python3
"""
Import job, step 1: use the TML that was approved and merged into ts-prod.

The import job downloads the extract artifact, which also carries the extracted
orgs/<org>/ files. Those are the pre-review copy: a reviewer may have edited or removed files
in the promote PR. This replaces orgs/<org>/ with what ts-prod holds at the merge commit
(MERGE_SHA, from Stage 7b), so the import uses exactly what was approved, and nothing from
later runs. With no MERGE_SHA (Stage 7 opened no PR because ts-prod already had these files)
it uses the checked-out ts-prod.

The scripts keep running from the current ts-prod checkout; only the org's TML is pinned.
Writes import_source.json and a job-summary line saying which commit was imported.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from redeploy_prepare import RedeployError, git, restore_org


def main() -> None:
    org_key = os.environ["ORG_KEY"]
    merge_sha = os.environ.get("MERGE_SHA", "").strip()
    repo_root = Path(os.environ.get("REPO_ROOT", ".")).resolve()
    out_path = Path(os.environ.get("IMPORT_SOURCE_OUT", "import_source.json"))

    ref = merge_sha or "HEAD"
    r = git(["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], repo_root, check=False)
    if r.returncode != 0:
        print(f"::error::Cannot find the merge commit {ref} in the ts-prod checkout.",
              file=sys.stderr)
        sys.exit(1)
    sha = r.stdout.strip()

    try:
        count = restore_org(org_key, sha, repo_root)
    except RedeployError as exc:
        # Nothing for this org at that commit: nothing to restore, and nothing will import.
        print(f"[IMPORT] {exc} Leaving the workspace as it is.", file=sys.stderr)
        count = 0

    source = "the promote PR's merge commit" if merge_sha else "the current ts-prod"
    line = f"Importing TML from {source}: ts-prod @ {sha} ({count} file(s) for '{org_key}')."
    print(f"[IMPORT] {line}")
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a", encoding="utf-8") as fh:
            fh.write(f"{line}\n")
    out_path.write_text(json.dumps({"ts_prod_commit": sha, "from_merge_commit": bool(merge_sha),
                                    "files": count}, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
