#!/usr/bin/env python3
"""
Stage 8b: Snapshot the target before the import overwrites it.

For every object about to be imported, export what the target holds now (matched by obj_id)
to snapshot/<org_key>/<folder>/<obj_id>.tml, and write stage8b_snapshot.json with one result
per object:

  captured  the target had the object; its current TML is saved
  new       the target does not have it yet, so there is nothing to save (and the import will
            create it; nothing is ever deleted, so rolling back means leaving it alone)
  skipped   the type is not exported separately (coaching feedback travels inside its model)
  failed    the object exists but could not be read back

The snapshot is the "before" copy: the artifact to inspect or re-import by hand if a run
overwrote something it should not have, and the baseline for noticing manual edits in the
target. It is read-only against the target.

A failed capture stops the run (exit 1) before Stage 9, because an import that cannot be
undone is the thing this stage exists to prevent. Set SNAPSHOT_REQUIRED=false to continue
anyway.

Not covered: Spotter memory and NL instructions (they have their own export APIs and are
replaced wholesale on import), sharing, and anything TML does not carry.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.secrets import resolve_secret
from lib.ts_client import TSClient
from lib.tml_utils import parse_tml, to_tml_string

# Types the target can be searched and exported by obj_id.
SNAPSHOT_TYPES = {"LOGICAL_TABLE", "ANSWER", "LIVEBOARD", "COLLECTION"}


def snapshot_objects(target_client: TSClient, manifest: list[dict], org_key: str,
                     out_root: Path) -> list[dict]:
    results = []
    for entry in manifest:
        base = {"obj_id": entry["obj_id"], "name": entry["name"], "type": entry["type"],
                "folder": entry.get("folder", "")}
        if entry["type"] not in SNAPSHOT_TYPES:
            results.append({**base, "status": "skipped",
                            "reason": f"{entry['type']} is not exported on its own"})
            continue
        try:
            guid = target_client.find_guid_by_obj_id(entry["type"], entry["obj_id"])
            if not guid:
                results.append({**base, "status": "new"})
                print(f"[STAGE 8b] '{entry['name']}' is not in the target yet — nothing to save.")
                continue
            exported = target_client.export_tml(
                [{"identifier": guid}],
                include_obj_id=True, include_obj_id_ref=True, include_guid=False,
                export_with_feedbacks=True,
            )
            edocs = exported if isinstance(exported, list) else exported.get("object", [])
            if not edocs:
                raise ValueError("export returned nothing")
            doc = edocs[0].get("edoc") or edocs[0].get("content")
            tml = parse_tml(doc) if isinstance(doc, str) else edocs[0]
            path = out_root / org_key / (entry.get("folder") or "misc") / f"{entry['obj_id']}.tml"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(to_tml_string(tml), encoding="utf-8")
            results.append({**base, "status": "captured", "path": str(path)})
            print(f"[STAGE 8b] Saved '{entry['name']}' -> {path}")
        except Exception as exc:
            results.append({**base, "status": "failed", "error": str(exc)})
            print(f"[STAGE 8b][FAIL] '{entry['name']}' ({entry['obj_id']}): {exc}",
                  file=sys.stderr)
    return results


def main():
    org_key = os.environ["ORG_KEY"]
    cfg_path = os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{org_key}_config.json")
    manifest_path = os.environ.get("MANIFEST_OUT", f"/tmp/ts_migration_{org_key}_manifest.json")
    export_path = os.environ.get("EXPORT_OUT", f"/tmp/ts_migration_{org_key}_export_results.json")
    out_root = Path(os.environ.get("SNAPSHOT_DIR", "snapshot"))
    out_path = Path(os.environ.get("SNAPSHOT_OUT", f"/tmp/ts_migration_{org_key}_snapshot.json"))
    required = os.environ.get("SNAPSHOT_REQUIRED", "true").lower() != "false"

    cfg = json.loads(Path(cfg_path).read_text())
    manifest = json.loads(Path(manifest_path).read_text())
    # Only objects that will actually be imported: the ones whose export succeeded.
    exported = {r["obj_id"] for r in json.loads(Path(export_path).read_text())
                if r.get("status") == "success"}
    manifest = [e for e in manifest if e["obj_id"] in exported]

    tgt = cfg["target"]
    client = TSClient(tgt["base_url"], tgt["org_id"],
                      resolve_secret("TS_TARGET_USERNAME"), resolve_secret("TS_TARGET_SECRET_KEY"))
    client.authenticate()

    results = snapshot_objects(client, manifest, org_key, out_root)
    out_path.write_text(json.dumps(results, indent=2), encoding="utf-8")

    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in ("captured", "new", "skipped", "failed")}
    print(f"[STAGE 8b] Snapshot: {counts['captured']} saved, {counts['new']} new, "
          f"{counts['skipped']} skipped, {counts['failed']} failed.")
    if counts["failed"] and required:
        print("[STAGE 8b][ERROR] Could not save the current version of "
              f"{counts['failed']} object(s); stopping before the import so nothing is "
              "overwritten without a copy. Set SNAPSHOT_REQUIRED=false to import anyway.",
              file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
