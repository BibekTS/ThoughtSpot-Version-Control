#!/usr/bin/env python3
"""
Tag the ts-prod commit a clean migration imported, so it can be redeployed later.

Creates the annotated tag migrate/<org_key>/<run_id> on MERGE_SHA and pushes it. The redeploy
workflow (ts-redeploy.yml) takes such a tag as its `ref`, so every clean migration leaves a
named restore point.

Only called after a live run in which the import succeeded and the report found no failures,
so a tag always marks content that really reached the target. Re-running is harmless (the same
tag on the same commit is left alone); the same tag on a different commit is an error.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from redeploy_prepare import PROD_REF, git


def tag_name(org_key: str, run_id: str) -> str:
    return f"migrate/{org_key}/{run_id}"


def create_tag(org_key: str, run_id: str, sha: str, repo_root: Path,
               prod_ref: str = PROD_REF, remote: str = "origin") -> str:
    """Create and push the tag; returns its name. Raises RuntimeError when it must not."""
    if not sha:
        raise RuntimeError("No merge commit to tag.")
    if git(["merge-base", "--is-ancestor", sha, prod_ref], repo_root, check=False).returncode != 0:
        raise RuntimeError(f"{sha[:12]} is not part of {prod_ref}'s history; refusing to tag it.")

    tag = tag_name(org_key, run_id)
    existing = git(["rev-parse", "--verify", "--quiet", f"refs/tags/{tag}^{{commit}}"],
                   repo_root, check=False)
    if existing.returncode == 0:
        if existing.stdout.strip() == sha:
            print(f"[TAG] {tag} already marks {sha[:12]}.")
            return tag
        raise RuntimeError(f"{tag} already exists on a different commit "
                           f"({existing.stdout.strip()[:12]}).")

    git(["tag", "-a", tag, sha, "-m",
         f"ThoughtSpot migration of org '{org_key}', run {run_id}. "
         f"Restore point: redeploy it with ts-redeploy.yml (ref: {tag})."], repo_root)
    git(["push", remote, f"refs/tags/{tag}"], repo_root)
    print(f"[TAG] Pushed {tag} -> {sha[:12]}.")
    return tag


def main() -> None:
    try:
        create_tag(os.environ["ORG_KEY"], os.environ["RUN_ID"],
                   os.environ.get("MERGE_SHA", "").strip(),
                   Path(os.environ.get("REPO_ROOT", ".")).resolve(),
                   os.environ.get("PROD_REF", PROD_REF))
    except RuntimeError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
