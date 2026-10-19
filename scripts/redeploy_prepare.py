#!/usr/bin/env python3
"""
Redeploy, step 1: rebuild a run's inputs from an approved ref on ts-prod.

Used by ts-redeploy.yml to put the target back into the state recorded in git (a rollback), or
to redeploy approved content without touching the source org. Instead of extracting from the
source, it:

  1. resolves REF (a tag, a commit, or a branch) and REFUSES it unless it is part of ts-prod's
     history, so content that was never reviewed and merged cannot be deployed this way;
  2. replaces orgs/<org_key>/ in the working tree with that ref's copy, exactly;
  3. rebuilds manifest.json and stage4_export.json (what Stage 3 and Stage 4 write) from the
     files, so the normal Stages 5, 8, 9, 9b and 9c run unchanged.

"State as of the ref" means the newest file for each object at that ref: ts-prod keeps every
run's files (<slug>__<obj_id>__<TIMESTAMP>.<ext>.tml), so an object's older versions are
ignored. Nothing is deleted in the target, so an object that was added after the ref stays.
Spotter memory and NL instructions are the newest files at the ref (the redeploy runs with no
TIMESTAMP).
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

PROD_REF = "origin/ts-prod"

# <slug>__<obj_id>[__<TIMESTAMP>].<ext>.tml ; legacy files have no timestamp.
FILE_RE = re.compile(r"^(?:.*?)(?:__(?P<ts>\d{8}T\d{6}Z))?\.(?P<ext>[a-z_]+)\.tml$")
EXT_TO_TYPE = {"table": "LOGICAL_TABLE", "answer": "ANSWER", "liveboard": "LIVEBOARD",
               "collection": "COLLECTION", "feedback": "FEEDBACK"}
# Import order of the folders (Stage 9 tiers); only used to give the manifest a stable order.
FOLDERS = ["tables", "views", "models", "collections", "answers", "liveboards", "feedback"]


class RedeployError(Exception):
    pass


def git(args: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=cwd, check=check, capture_output=True, text=True)


def resolve_ref(ref: str, repo_root: Path, prod_ref: str = PROD_REF) -> str:
    """Commit SHA for `ref`, provided it is on ts-prod's history. Raises RedeployError."""
    if not ref.strip():
        raise RedeployError("A ref (tag or commit on ts-prod) is required.")
    if ref.startswith("-"):
        raise RedeployError(f"'{ref}' is not a valid ref.")
    sha = None
    for candidate in (ref, f"origin/{ref}"):
        r = git(["rev-parse", "--verify", "--quiet", f"{candidate}^{{commit}}"], repo_root,
                check=False)
        if r.returncode == 0:
            sha = r.stdout.strip()
            break
    if not sha:
        raise RedeployError(f"Cannot resolve '{ref}' to a commit in this repository.")
    on_prod = git(["merge-base", "--is-ancestor", sha, prod_ref], repo_root, check=False)
    if on_prod.returncode != 0:
        raise RedeployError(
            f"'{ref}' ({sha[:12]}) is not part of {prod_ref}'s history. Only content that was "
            "reviewed and merged into ts-prod can be redeployed.")
    return sha


def restore_org(org_key: str, sha: str, repo_root: Path) -> int:
    """Make orgs/<org_key>/ match `sha` exactly. Returns the number of files at that ref."""
    path = f"orgs/{org_key}"
    listing = git(["ls-tree", "-r", "--name-only", sha, "--", path], repo_root).stdout
    files = [line for line in listing.splitlines() if line.strip()]
    if not files:
        raise RedeployError(f"There is no content for org '{org_key}' at {sha[:12]}.")
    target = repo_root / path
    if target.exists():
        shutil.rmtree(target)        # files added after the ref must not leak into the run
    git(["checkout", sha, "--", path], repo_root)
    return len(files)


def _object_of(tml: dict) -> tuple[str, dict] | None:
    for key, value in tml.items():
        if key != "obj_id" and isinstance(value, dict):
            return key, value
    return None


def build_inputs(org_key: str, repo_root: Path) -> tuple[list[dict], list[dict]]:
    """(manifest, export_results) from the newest TML per obj_id under orgs/<org_key>/."""
    best: dict[str, tuple[str, dict, dict]] = {}     # obj_id -> (ts, entry, export result)
    base = repo_root / "orgs" / org_key
    for folder in FOLDERS:
        for path in sorted((base / folder).glob("*.tml")) if (base / folder).is_dir() else []:
            m = FILE_RE.match(path.name)
            if not m or m.group("ext") not in EXT_TO_TYPE:
                print(f"[REDEPLOY][WARN] Ignoring {path.name}: unrecognised file name.",
                      file=sys.stderr)
                continue
            try:
                tml = json.loads(path.read_text(encoding="utf-8"))
            except Exception as exc:
                print(f"[REDEPLOY][WARN] Ignoring {path.name}: not valid TML ({exc}).",
                      file=sys.stderr)
                continue
            obj_id = tml.get("obj_id")
            if not obj_id:
                print(f"[REDEPLOY][WARN] Ignoring {path.name}: no obj_id in the TML.",
                      file=sys.stderr)
                continue
            obj = _object_of(tml)
            name = (obj[1].get("name") if obj else None) or path.name.split("__")[0]
            ts = m.group("ts") or ""
            rel = str(path.relative_to(repo_root))
            typ = EXT_TO_TYPE[m.group("ext")]
            entry = {"type": typ, "folder": folder, "name": name, "obj_id": obj_id,
                     "source_guid": "", "reason": "redeploy", "other_tags": []}
            result = {"obj_id": obj_id, "name": name, "type": typ, "folder": folder,
                      "tml_path": rel, "status": "success"}
            if obj_id not in best or ts > best[obj_id][0]:
                best[obj_id] = (ts, entry, result)

    order = {f: i for i, f in enumerate(FOLDERS)}
    items = sorted(best.values(), key=lambda t: (order[t[1]["folder"]], t[1]["name"].lower()))
    return [e for _, e, _ in items], [r for _, _, r in items]


def main():
    org_key = os.environ["ORG_KEY"]
    ref = os.environ.get("REF", "")
    repo_root = Path(os.environ.get("REPO_ROOT", ".")).resolve()
    manifest_path = Path(os.environ.get("MANIFEST_OUT", "manifest.json"))
    export_path = Path(os.environ.get("EXPORT_OUT", "stage4_export.json"))
    out_path = Path(os.environ.get("REDEPLOY_OUT", "redeploy_ref.json"))

    try:
        sha = resolve_ref(ref, repo_root, os.environ.get("PROD_REF", PROD_REF))
        count = restore_org(org_key, sha, repo_root)
        manifest, results = build_inputs(org_key, repo_root)
    except RedeployError as exc:
        print(f"::error::{exc}", file=sys.stderr)
        sys.exit(1)
    if not manifest:
        print(f"::error::No importable TML for org '{org_key}' at {sha[:12]}.", file=sys.stderr)
        sys.exit(1)

    manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    export_path.write_text(json.dumps(results, indent=2), encoding="utf-8")
    out_path.write_text(json.dumps({"ref": ref, "sha": sha, "files_at_ref": count,
                                    "objects": len(manifest)}, indent=2), encoding="utf-8")
    by_folder: dict[str, int] = {}
    for e in manifest:
        by_folder[e["folder"]] = by_folder.get(e["folder"], 0) + 1
    print(f"[REDEPLOY] '{ref}' = {sha[:12]}: {len(manifest)} object(s) "
          f"({', '.join(f'{n} {f}' for f, n in by_folder.items())}) from {count} file(s).")


if __name__ == "__main__":
    main()
