#!/usr/bin/env python3
"""
Stage 10: Post-check and tag cleanup.

1. Post-check: confirm each manifest object exists in target by obj_id.
2. Tag cleanup: remove migration tag from source objects that:
   - imported successfully AND
   - passed the post-check
   - are tagged objects (not dependencies)
   Dependencies that were not tagged are left alone.
   Failed/skipped objects keep the tag so a rerun retries them.

Never runs in DRY_RUN mode.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.ts_client import TSClient
from lib.spotter_results import load_json, spotter_outcome


def _scan_by_type(
    client: TSClient, obj_type: str
) -> tuple[dict[str, str], dict[str, str]]:
    """
    Paginate all objects of obj_type in target.
    Returns two dicts:
      obj_id_map: {metadata_obj_id -> metadata_name}
      name_map:   {metadata_name -> metadata_obj_id or ""}  (first hit wins)
    """
    page_size = 500
    offset = 0
    obj_id_map: dict[str, str] = {}
    name_map: dict[str, str] = {}
    while True:
        resp = client.post("/metadata/search", {
            "metadata": [{"type": obj_type}],
            "include_headers": True,
            "record_size": page_size,
            "record_offset": offset,
        })
        if resp.status_code != 200:
            raise RuntimeError(f"metadata/search returned HTTP {resp.status_code}")
        raw = resp.json()
        page = raw if isinstance(raw, list) else (
            raw.get("data") or raw.get("metadata") or
            raw.get("results") or raw.get("objects") or []
        )
        for item in page:
            oid = item.get("metadata_obj_id") or ""
            nm = item.get("metadata_name") or item.get("name") or ""
            if oid:
                obj_id_map[oid] = nm
            if nm and nm not in name_map:
                name_map[nm] = oid
        if len(page) < page_size:
            break
        offset += page_size
    return obj_id_map, name_map


def _find_by_obj_id(
    obj_id_map: dict[str, str],
    name_map: dict[str, str],
    obj_id: str,
    name: str,
) -> str:
    """
    Check whether an object exists in pre-fetched target maps.

    Returns:
      "obj_id"   — found by metadata_obj_id (definitive)
      "name"     — found by metadata_name only (metadata_obj_id not set in target;
                   ThoughtSpot may not persist obj_id on TML update of an existing object)
      "not_found"— object absent from target
    """
    if obj_id in obj_id_map:
        return "obj_id"

    # Fallback: object may exist without its custom obj_id (e.g. pre-existing object
    # updated via TML import — ThoughtSpot does not always apply obj_id on update).
    if name in name_map:
        print(
            f"[WARN] '{name}' ({obj_id}): found in target by name but metadata_obj_id "
            f"is not set (stored as {name_map[name]!r}). "
            f"Migration confirmed by name match."
        )
        return "name"

    print(f"[DEBUG] Not found in target: obj_id={obj_id!r} name={name!r}")
    return "not_found"


def post_check(
    target_client: TSClient,
    manifest: list[dict],
    import_results: list[dict],
) -> dict:
    """Compare manifest vs target. Returns post_check dict."""
    import_map = {r["obj_id"]: r for r in import_results}

    # Scan target once per type — avoids redundant API calls when multiple
    # objects of the same type appear in the manifest.
    type_cache: dict[str, tuple[dict, dict]] = {}

    by_type: dict[str, dict] = {}
    mismatches = []

    for entry in manifest:
        obj_id = entry["obj_id"]
        obj_type = entry["type"]
        name = entry["name"]

        if obj_type not in by_type:
            by_type[obj_type] = {"manifest": 0, "confirmed": 0}
        by_type[obj_type]["manifest"] += 1

        result = import_map.get(obj_id, {})
        if result.get("status") == "success":
            # ThoughtSpot's metadata/search `identifier` field resolves GUIDs and
            # names, not custom obj_ids. Paginate all objects of this type and
            # match client-side on metadata_obj_id — same approach as Stage 3.
            # Fallback to name match when metadata_obj_id is absent on the target
            # object (ThoughtSpot does not always set obj_id when updating existing
            # objects via TML import).
            try:
                if obj_type not in type_cache:
                    type_cache[obj_type] = _scan_by_type(target_client, obj_type)
                obj_id_map, name_map = type_cache[obj_type]
                result_kind = _find_by_obj_id(obj_id_map, name_map, obj_id, name)
                if result_kind in ("obj_id", "name"):
                    by_type[obj_type]["confirmed"] += 1
                else:
                    mismatches.append({
                        "obj_id": obj_id, "name": name, "type": obj_type,
                        "issue": "Not found in target by obj_id or name after import.",
                    })
            except Exception as e:
                mismatches.append({
                    "obj_id": obj_id, "name": name, "type": obj_type,
                    "issue": f"Error during post-check: {e}",
                })

    return {"by_type": by_type, "mismatches": mismatches}


def cleanup_tags(
    source_client: TSClient,
    manifest: list[dict],
    import_results: list[dict],
    post_check_mismatches: list[dict],
    migration_tag: str,
    blocked_ids: set[str] | None = None,
) -> dict:
    """
    blocked_ids: obj_ids whose tag must stay because something besides the TML did not
    migrate (a model's Spotter memory or NL instructions). They are retried on the next run.
    """
    import_map = {r["obj_id"]: r for r in import_results}
    mismatch_ids = {m["obj_id"] for m in post_check_mismatches}
    blocked_ids = blocked_ids or set()
    kept_for_spotter = 0

    to_remove = []
    to_retain = []

    for entry in manifest:
        obj_id = entry["obj_id"]
        reason = entry.get("reason", "")

        # Only untag objects that were originally tagged (not dependency_of:...)
        if reason != "tagged":
            continue

        result = import_map.get(obj_id, {})
        if obj_id in blocked_ids and result.get("status") == "success":
            kept_for_spotter += 1
            to_retain.append(entry)
        elif result.get("status") == "success" and obj_id not in mismatch_ids:
            to_remove.append(entry)
        else:
            to_retain.append(entry)

    # Batch unassign
    removed = 0
    if to_remove:
        metadata = [
            {"type": e["type"], "identifier": e["source_guid"]}
            for e in to_remove
        ]
        try:
            source_client.unassign_tags(metadata, [migration_tag])
            removed = len(to_remove)
            print(f"[STAGE 10] Removed migration tag from {removed} objects.")
        except Exception as e:
            # The tags stay on the source objects, so a re-run will pick them up again.
            print(f"[ERROR] Tag removal failed; tags were NOT removed: {e}", file=sys.stderr)

    if kept_for_spotter:
        print(f"[STAGE 10] Kept the migration tag on {kept_for_spotter} object(s) whose Spotter "
              "memory or NL instructions did not migrate, so a re-run retries them.")
    return {"removed": removed, "retained": len(to_retain),
            "kept_for_spotter": kept_for_spotter}


def main():
    if os.environ.get("DRY_RUN", "false").lower() == "true":
        print("[STAGE 10] DRY_RUN=true — skipping post-check and tag cleanup.")
        sys.exit(0)

    org_key = os.environ["ORG_KEY"]
    cfg_path = os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{org_key}_config.json")
    manifest_path = os.environ.get("MANIFEST_OUT", f"/tmp/ts_migration_{org_key}_manifest.json")
    import_path = os.environ.get("IMPORT_OUT", f"/tmp/ts_migration_{org_key}_import_results.json")

    cfg = json.loads(Path(cfg_path).read_text())
    manifest = json.loads(Path(manifest_path).read_text())
    import_results = json.loads(Path(import_path).read_text())

    tgt = cfg["target"]
    target_client = TSClient(
        tgt["base_url"], tgt["org_id"],
        os.environ["TS_TARGET_USERNAME"],
        os.environ["TS_TARGET_SECRET_KEY"],
    )
    target_client.authenticate()

    check_result = post_check(target_client, manifest, import_results)
    print(f"[STAGE 10] Post-check: {check_result['by_type']}")
    if check_result["mismatches"]:
        print(f"[WARN] {len(check_result['mismatches'])} post-check mismatches:",
              file=sys.stderr)
        for m in check_result["mismatches"]:
            print(f"  {m['type']} '{m['name']}' ({m['obj_id']}): {m['issue']}",
                  file=sys.stderr)

    src = cfg["source"]
    source_client = TSClient(
        src["base_url"], src["org_id"],
        os.environ["TS_SOURCE_USERNAME"],
        os.environ["TS_SOURCE_SECRET_KEY"],
    )
    source_client.authenticate()

    spotter = spotter_outcome(
        load_json(os.environ.get("MEMORY_EXPORT_OUT",
                                 f"/tmp/ts_migration_{org_key}_memory_export.json"), {}),
        load_json(os.environ.get("MEMORY_IMPORT_OUT",
                                 f"/tmp/ts_migration_{org_key}_memory_import.json"), {}),
        load_json(os.environ.get("INSTRUCTIONS_EXPORT_OUT",
                                 f"/tmp/ts_migration_{org_key}_instructions_export.json"), {}),
        load_json(os.environ.get("INSTRUCTIONS_IMPORT_OUT",
                                 f"/tmp/ts_migration_{org_key}_instructions_import.json"), {}),
    )
    blocked = set(spotter["blocked_obj_ids"])
    if spotter["blocked_all_models"]:      # memory is one payload: every model is affected
        blocked |= {e["obj_id"] for e in manifest if e.get("folder") == "models"}
    cleanup_result = cleanup_tags(
        source_client, manifest, import_results,
        check_result["mismatches"], cfg["migration_tag"], blocked,
    )

    out = {"post_check": check_result, "tag_cleanup": cleanup_result}
    out_path = os.environ.get("POSTCHECK_OUT",
                              f"/tmp/ts_migration_{org_key}_postcheck.json")
    Path(out_path).write_text(json.dumps(out, indent=2), encoding="utf-8")
    print(f"[STAGE 10] Tag cleanup: removed={cleanup_result['removed']}, "
          f"retained={cleanup_result['retained']}")


if __name__ == "__main__":
    main()
