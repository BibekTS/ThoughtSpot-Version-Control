#!/usr/bin/env python3
"""
Stage 3: Discover tagged objects in the source org.

Finds all objects tagged with <migration_tag> across all in-scope types,
resolves dependencies per dependency_mode, and writes manifest.json.

Dependencies are what a tagged object is built on (a liveboard's model, a model's tables,
a table's joined tables). They are found from the object's own TML, which references them
by obj_id, so they are NOT taken from the search API's `dependent_objects`: that field
lists the opposite direction (objects that depend ON the one searched).

dependency_mode:
  include         pull every dependency that is not already in the manifest
  skip_if_exists  as include, but leave out dependencies that already exist in the target
  require_tagged  pull nothing; a tagged object with an untagged dependency is excluded
                  (and reported), as is anything that depends on an excluded object

dependency_exclude (optional list of obj_ids): never treated as a dependency in any mode.
Use it for objects that exist everywhere and must not be migrated, such as ThoughtSpot's
built-in "TS: BI Server" worksheet or sample data. A dry run lists every dependency pulled.

Objects without obj_id are recorded as failures and excluded.
"""
from __future__ import annotations

import json
import os
import sys
from collections import deque
from dataclasses import asdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.ts_client import TSClient
from lib.report import RunReport, ObjectRecord
from lib.tml_utils import parse_tml, extract_refs

# Types to search — one API call per main metadata_type.
# Subtype classification happens client-side from metadata_header.type.
SEARCH_TYPES = [
    "LOGICAL_TABLE",
    "ANSWER",
    "LIVEBOARD",
    "COLLECTION",
]

# Maps metadata_header.type → folder name for LOGICAL_TABLE objects
_TABLE_SUBTYPE_FOLDER = {
    "TABLE":           "tables",
    "SQL_VIEW":        "views",
    "WORKSHEET":       "models",
    "AGGR_WORKSHEET":  "models",
    "MODEL":           "models",
}


def _unwrap_response(raw) -> list:
    if isinstance(raw, list):
        return raw
    if isinstance(raw, dict):
        for key in ("data", "results", "objects", "metadata"):
            if key in raw and isinstance(raw[key], list):
                return raw[key]
    return []


def _folder_for_object(meta_type: str, obj: dict) -> str:
    if meta_type == "LOGICAL_TABLE":
        header = obj.get("metadata_header") or {}
        subtype = header.get("type", "")
        return _TABLE_SUBTYPE_FOLDER.get(subtype, "tables")
    return {"ANSWER": "answers", "LIVEBOARD": "liveboards", "COLLECTION": "collections"}.get(meta_type, "misc")


def _resolve_tag_id(source_client: TSClient, tag_name: str, cfg: dict) -> str:
    """
    Return the tag identifier to use in tag_identifiers filter.
    tag_identifiers accepts both the tag name and its GUID; the tag name is
    simpler and confirmed to work. We use it directly.
    """
    print(f"[STAGE 3] Using tag name as identifier: {tag_name}")
    return tag_name


def discover(source_client: TSClient, cfg: dict, report: RunReport,
             target_client: TSClient | None = None) -> list[dict]:
    tag = cfg["migration_tag"]
    dep_mode = cfg.get("dependency_mode", "include")
    manifest: list[dict] = []
    seen_guids: set[str] = set()

    # Resolve the tag identifier to use in tag_identifiers filter (server-side only).
    # Priority: config-provided GUID > /tags/search GUID > tag name as fallback.
    tag_id_for_filter = _resolve_tag_id(source_client, tag, cfg)

    for meta_type in SEARCH_TYPES:
        objects = _search_by_tag(source_client, meta_type, tag_id_for_filter)
        for obj in objects:
            guid = obj.get("metadata_id", "")
            if guid in seen_guids:
                continue
            seen_guids.add(guid)

            obj_id = obj.get("metadata_obj_id") or ""
            name = obj.get("metadata_name", "")
            tags_on_obj = _extract_tags(obj)
            other_tags = [t for t in tags_on_obj if t != tag]

            if not obj_id:
                rec = ObjectRecord(
                    obj_type=meta_type,
                    name=name,
                    obj_id="",
                    source_guid=guid,
                    reason="missing_obj_id",
                    stage="discover",
                    status="failed",
                    error="Object has no obj_id. Enable Object ID feature and assign one.",
                    suggested_action="Assign a custom obj_id via ThoughtSpot UI or API, then re-tag.",
                )
                report.mark_failed(rec)
                print(f"[SKIP] {meta_type} '{name}' (GUID {guid}) has no obj_id — excluded.",
                      file=sys.stderr)
                continue

            folder = _folder_for_object(meta_type, obj)
            entry = {
                "type": meta_type,
                "folder": folder,
                "name": name,
                "obj_id": obj_id,
                "source_guid": guid,
                "reason": "tagged",
                "other_tags": other_tags,
            }
            manifest.append(entry)
            pulled_rec = ObjectRecord(
                obj_type=meta_type, name=name, obj_id=obj_id,
                source_guid=guid, reason="tagged", other_tags=other_tags,
            )
            report.add_pulled(pulled_rec)

    exclude = {str(x) for x in (cfg.get("dependency_exclude") or [])}
    return apply_dependency_mode(source_client, manifest, dep_mode, report, target_client,
                                 exclude)


def _export_refs(client: TSClient, guid: str) -> list[dict]:
    """Export an object's TML (same flags as Stage 4) and return the objects it references."""
    resp = client.export_tml(
        metadata=[{"identifier": guid}],
        include_obj_id=True,
        include_obj_id_ref=True,
        include_guid=False,
        export_with_feedbacks=False,
    )
    edocs = resp if isinstance(resp, list) else resp.get("object", [resp])
    if not edocs:
        return []
    raw = edocs[0].get("edoc") or edocs[0].get("content")
    tml = parse_tml(raw) if isinstance(raw, str) else raw
    return extract_refs(tml) if isinstance(tml, dict) else []


def _lookup_logical_table(client: TSClient, obj_id: str) -> dict | None:
    """Find a table / view / model in the source by its obj_id; None if it is not there."""
    resp = client.post("/metadata/search", {
        "metadata": [{"type": "LOGICAL_TABLE", "obj_identifier": obj_id}],
        "include_headers": True,
        "record_size": 5,
        "record_offset": 0,
    })
    if resp.status_code != 200:
        return None
    for item in _unwrap_response(resp.json()):
        if item.get("metadata_obj_id") == obj_id and item.get("metadata_id"):
            return item
    return None


def apply_dependency_mode(
    source_client: TSClient,
    manifest: list[dict],
    dep_mode: str,
    report: RunReport,
    target_client: TSClient | None = None,
    exclude: set[str] | None = None,
) -> list[dict]:
    """Return the final manifest for dep_mode (see the module docstring)."""
    exclude = exclude or set()
    if dep_mode == "require_tagged":
        return _require_tagged(source_client, manifest, report, exclude)
    if dep_mode in ("include", "skip_if_exists"):
        if dep_mode == "skip_if_exists" and target_client is None:
            print("[WARN] dependency_mode=skip_if_exists but no target client; "
                  "treating as 'include'.", file=sys.stderr)
        return manifest + _collect_dependencies(
            source_client, manifest, dep_mode, report, target_client, exclude)
    return manifest


def _collect_dependencies(
    source_client: TSClient,
    manifest: list[dict],
    dep_mode: str,
    report: RunReport,
    target_client: TSClient | None,
    exclude: set[str],
) -> list[dict]:
    """Walk the dependency graph from the tagged objects; return the entries to add."""
    known = {e["obj_id"] for e in manifest}
    left_out: set[str] = set()      # already looked at and not added (missing / in target)
    added: list[dict] = []
    queue = deque(manifest)

    while queue:
        parent = queue.popleft()
        try:
            refs = _export_refs(source_client, parent["source_guid"])
        except Exception as exc:
            print(f"[WARN] Could not read dependencies of {parent['type']} "
                  f"'{parent['name']}' ({parent['obj_id']}): {exc}", file=sys.stderr)
            report.mark_failed(ObjectRecord(
                obj_type=parent["type"], name=parent["name"], obj_id=parent["obj_id"],
                source_guid=parent["source_guid"], reason="dependencies_unreadable",
                stage="discover",
                error=f"Could not read this object's dependencies: {exc}",
                suggested_action="Its dependencies may be missing from the migration; re-run.",
            ))
            continue

        for ref in refs:
            oid = ref["obj_id"]
            if oid in known or oid in left_out:
                continue
            if oid in exclude:
                left_out.add(oid)
                print(f"[STAGE 3] Dependency '{ref['name']}' ({oid}) is in "
                      "dependency_exclude; skipping.")
                continue
            item = _lookup_logical_table(source_client, oid)
            if item is None:
                left_out.add(oid)
                print(f"[WARN] '{parent['name']}' ({parent['obj_id']}) depends on obj_id "
                      f"'{oid}' ({ref['name']}), which was not found in the source; "
                      "it will not be migrated.", file=sys.stderr)
                report.mark_failed(ObjectRecord(
                    obj_type="LOGICAL_TABLE", name=ref["name"] or oid, obj_id=oid,
                    reason=f"dependency_missing: needed by {parent['obj_id']}",
                    stage="discover",
                    error=(f"'{parent['name']}' ({parent['obj_id']}) depends on obj_id '{oid}', "
                           "which was not found in the source org."),
                    suggested_action="If it is a built-in object, add its obj_id to "
                                     "dependency_exclude; otherwise check that it exists.",
                ))
                continue
            if dep_mode == "skip_if_exists" and target_client is not None \
                    and target_client.find_model_guid(oid):
                left_out.add(oid)
                print(f"[STAGE 3] Dependency '{ref['name']}' ({oid}) already exists in the "
                      "target; skipping (skip_if_exists).")
                continue

            name = item.get("metadata_name") or ref["name"]
            entry = {
                "type": "LOGICAL_TABLE",
                "folder": _folder_for_object("LOGICAL_TABLE", item),
                "name": name,
                "obj_id": oid,
                "source_guid": item["metadata_id"],
                "reason": f"dependency_of:{parent['obj_id']}",
                "other_tags": [],
            }
            known.add(oid)
            added.append(entry)
            queue.append(entry)     # its own dependencies (e.g. a model's tables) come next
            report.add_pulled(ObjectRecord(
                obj_type="LOGICAL_TABLE", name=name, obj_id=oid,
                source_guid=entry["source_guid"], reason=entry["reason"],
            ))
            print(f"[STAGE 3] Dependency: {entry['folder']} '{name}' ({oid}) "
                  f"<- {parent['obj_id']}")

    return added


def _require_tagged(source_client: TSClient, manifest: list[dict],
                    report: RunReport, exclude: set[str]) -> list[dict]:
    """
    Exclude tagged objects that depend on something that is not tagged, and anything that
    depends on an excluded object. Objects whose dependencies cannot be read are kept.
    """
    tagged_ids = {e["obj_id"] for e in manifest}
    deps_of: dict[str, set[str]] = {}
    untagged_of: dict[str, list[dict]] = {}

    for entry in manifest:
        try:
            refs = [r for r in _export_refs(source_client, entry["source_guid"])
                    if r["obj_id"] not in exclude]
        except Exception as exc:
            print(f"[WARN] Could not read dependencies of {entry['type']} "
                  f"'{entry['name']}' ({entry['obj_id']}): {exc}; keeping it.",
                  file=sys.stderr)
            continue
        deps_of[entry["obj_id"]] = {r["obj_id"] for r in refs}
        untagged_of[entry["obj_id"]] = [r for r in refs if r["obj_id"] not in tagged_ids]

    excluded = {oid for oid, bad in untagged_of.items() if bad}
    while True:   # an object that depends on an excluded one cannot be migrated either
        more = {oid for oid, deps in deps_of.items() if oid not in excluded and deps & excluded}
        if not more:
            break
        excluded |= more

    kept = []
    for entry in manifest:
        oid = entry["obj_id"]
        if oid not in excluded:
            kept.append(entry)
            continue
        untagged = [f"'{r['name']}' ({r['obj_id']})" for r in untagged_of.get(oid, [])]
        why = (f"depends on untagged object(s): {', '.join(untagged)}" if untagged
               else "depends on an object that was excluded")
        report.mark_failed(ObjectRecord(
            obj_type=entry["type"], name=entry["name"], obj_id=oid,
            source_guid=entry["source_guid"], reason="dependency_not_tagged",
            stage="discover", status="failed",
            error=f"dependency_mode=require_tagged: {why}.",
            suggested_action="Tag the dependency too, or use dependency_mode 'include'.",
        ))
        print(f"[SKIP] {entry['type']} '{entry['name']}' ({oid}) excluded "
              f"(require_tagged): {why}.", file=sys.stderr)
    return kept


def _search_by_tag(
    source_client: TSClient,
    meta_type: str,
    tag_id_for_filter: str,
) -> list[dict]:
    """Return objects of meta_type that match the tag_identifiers server-side filter."""
    all_objects: list[dict] = []
    offset = 0
    page_size = 500  # avoid record_size:-1 which crashes TS when combined with tag_identifiers

    while True:
        body: dict = {
            "metadata": [{"type": meta_type}],
            "tag_identifiers": [tag_id_for_filter],
            "include_headers": True,
            "record_size": page_size,
            "record_offset": offset,
        }
        resp = source_client.post("/metadata/search", body)
        if resp.status_code != 200:
            print(
                f"[WARN] metadata/search type={meta_type} HTTP {resp.status_code}: {resp.text[:200]}",
                file=sys.stderr,
            )
            return []
        page = _unwrap_response(resp.json())
        all_objects.extend(page)
        if len(page) < page_size:
            break
        offset += page_size

    print(f"[STAGE 3] type={meta_type}: tag filter returned {len(all_objects)} objects")
    return all_objects


def _extract_tags(obj: dict) -> list[str]:
    # Tags may sit at top level OR nested under metadata_header depending on TS version
    tag_list = obj.get("tags") or []
    if not tag_list:
        header = obj.get("metadata_header") or {}
        tag_list = header.get("tags") or []
    return [t if isinstance(t, str) else t.get("name", "") for t in tag_list]


def main():
    from lib.ts_client import TSClient
    org_key = os.environ["ORG_KEY"]
    run_id = os.environ.get("RUN_ID", "local")
    cfg_path = os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{org_key}_config.json")
    cfg = json.loads(Path(cfg_path).read_text())

    src = cfg["source"]
    username = os.environ["TS_SOURCE_USERNAME"]
    secret_key = os.environ["TS_SOURCE_SECRET_KEY"]
    source_client = TSClient(src["base_url"], src["org_id"], username, secret_key)
    source_client.authenticate()

    report = RunReport(
        org_key=org_key, run_id=run_id,
        mode="DRY-RUN" if os.environ.get("DRY_RUN", "false").lower() == "true" else "LIVE",
        source_url=src["base_url"], source_org_id=src["org_id"],
        target_url=cfg["target"]["base_url"], target_org_id=cfg["target"]["org_id"],
        migration_tag=cfg["migration_tag"],
    )

    target_client = None
    if cfg.get("dependency_mode") == "skip_if_exists":
        tgt = cfg["target"]
        target_client = TSClient(
            tgt["base_url"], tgt["org_id"],
            os.environ["TS_TARGET_USERNAME"],
            os.environ["TS_TARGET_SECRET_KEY"],
        )
        target_client.authenticate()

    manifest = discover(source_client, cfg, report, target_client)

    # Stage 3's report object is otherwise discarded, which hid every discovery failure
    # (objects without an obj_id, missing dependencies, require_tagged exclusions) from the
    # run report. Stage 11 reads this file.
    failures_path = os.environ.get("DISCOVER_OUT",
                                   f"/tmp/ts_migration_{org_key}_discover_failures.json")
    Path(failures_path).write_text(
        json.dumps([asdict(r) for r in report.failed], indent=2), encoding="utf-8")

    out_path = os.environ.get("MANIFEST_OUT", f"/tmp/ts_migration_{org_key}_manifest.json")
    Path(out_path).write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    n_deps = sum(1 for e in manifest if str(e.get("reason", "")).startswith("dependency_of:"))
    print(f"[STAGE 3] Discovered {len(manifest)} objects "
          f"({len(manifest) - n_deps} tagged, {n_deps} dependencies) -> {out_path}")
    if report.failed:
        print(f"[WARN] {len(report.failed)} objects excluded (no obj_id). See failures.",
              file=sys.stderr)

    tagged_count = sum(1 for e in manifest if e.get("reason") == "tagged")
    if tagged_count == 0:
        print(
            f"[INFO] 0 objects with tag '{cfg['migration_tag']}' found in source org "
            f"{src['org_id']}. Nothing to migrate — pipeline will exit cleanly.",
            file=sys.stderr,
        )


if __name__ == "__main__":
    main()
