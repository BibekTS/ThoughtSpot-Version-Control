#!/usr/bin/env python3
"""
Stage 9: Import TML into the production ThoughtSpot cluster.

Import order (dependency tiers):
  tables -> views -> models/worksheets -> sets -> collections ->
  answers -> liveboards -> feedback

Path B: substitute source db/schema literals -> target values in memory
         before import, then call parameterize-fields to rebind.

Safety gate: after tables import, read them back and verify no source
db/schema values remain.

Skipped entirely if DRY_RUN=true.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.secrets import resolve_secret
from lib.ts_client import TSClient
from lib.variables import variable_config
from lib.tml_utils import (
    parse_tml, to_tml_string,
    path_b_substitute, get_table_db_schema, is_variable_bound, extract_refs,
)

IMPORT_TIERS = [
    ("tables",      ["LOGICAL_TABLE"],  ["TABLE"]),
    ("views",       ["LOGICAL_TABLE"],  ["SQL_VIEW"]),
    ("models",      ["LOGICAL_TABLE"],  ["WORKSHEET", "AGGR_WORKSHEET"]),
    ("collections", ["COLLECTION"],     []),
    ("answers",     ["ANSWER"],         []),
    ("liveboards",  ["LIVEBOARD"],      []),
    ("feedback",    ["FEEDBACK"],       []),
]

ASYNC_THRESHOLD = 20  # use async import for batches larger than this


def order_by_dependencies(entries: list[dict], repo_root: Path) -> list[dict]:
    """
    Order one tier so an object comes after the objects in the same tier that it references.

    Tiers already put tables before models before liveboards; this covers objects of the
    same kind that depend on each other (a model built on another model, a table joined to
    another table). Entries keep manifest order otherwise. Unreadable TML counts as no
    dependencies, and a cycle falls back to manifest order for the objects in it.
    """
    in_tier = {e["obj_id"] for e in entries}
    deps: dict[str, list[str]] = {}
    for e in entries:
        refs: list[str] = []
        path = e.get("tml_path")
        if path and (repo_root / path).exists():
            try:
                tml = parse_tml((repo_root / path).read_text(encoding="utf-8"))
                refs = [r["obj_id"] for r in extract_refs(tml)
                        if r["obj_id"] in in_tier and r["obj_id"] != e["obj_id"]]
            except Exception:
                refs = []
        deps[e["obj_id"]] = refs

    by_id = {e["obj_id"]: e for e in entries}
    ordered: list[dict] = []
    state: dict[str, int] = {}          # 1 = in progress, 2 = done

    def visit(obj_id: str) -> None:
        if state.get(obj_id):
            return                       # done, or a cycle: keep going in manifest order
        state[obj_id] = 1
        for dep in deps[obj_id]:
            visit(dep)
        state[obj_id] = 2
        ordered.append(by_id[obj_id])

    for e in entries:
        visit(e["obj_id"])
    return ordered


def import_tier(
    target_client: TSClient,
    tier_entries: list[dict],
    cfg: dict,
    repo_root: Path,
    failed_obj_ids: set[str],
) -> list[dict]:
    results = []
    db_cfg, schema_cfg = variable_config(cfg, "database"), variable_config(cfg, "schema")
    src_db, tgt_db = db_cfg["source_value"], db_cfg["target_value"]
    src_schema, tgt_schema = schema_cfg["source_value"], schema_cfg["target_value"]
    db_var, schema_var = db_cfg["ts_variable_name"], schema_cfg["ts_variable_name"]

    for entry in tier_entries:
        obj_id = entry["obj_id"]
        name = entry["name"]
        obj_type = entry["type"]
        tml_path_rel = entry.get("tml_path", "")
        other_tags = entry.get("other_tags", [])

        if not tml_path_rel:
            results.append({"obj_id": obj_id, "name": name, "type": obj_type,
                            "status": "skipped", "reason": "no tml_path"})
            continue

        tml_path = repo_root / tml_path_rel
        if not tml_path.exists():
            results.append({"obj_id": obj_id, "name": name, "type": obj_type,
                            "status": "skipped", "reason": "tml file missing"})
            continue

        try:
            tml_dict = parse_tml(tml_path.read_text(encoding="utf-8"))

            # Don't import an object whose dependencies failed: it would be missing what
            # it is built on. Tiers run dependencies first, so their failures are known.
            broken = [r["obj_id"] for r in extract_refs(tml_dict)
                      if r["obj_id"] in failed_obj_ids]
            if broken:
                failed_obj_ids.add(obj_id)      # so whatever depends on this one is skipped too
                results.append({
                    "obj_id": obj_id, "name": name, "type": obj_type,
                    "status": "skipped",
                    "reason": f"dependency {', '.join(broken)} failed",
                })
                print(f"[STAGE 9] Skipping '{name}': dependency {', '.join(broken)} failed.",
                      file=sys.stderr)
                continue

            # Path B substitution (in-memory only, never persisted)
            if obj_type == "LOGICAL_TABLE" and entry["folder"] == "tables":
                tml_for_import = path_b_substitute(
                    tml_dict, src_db, tgt_db, src_schema, tgt_schema
                )
            else:
                tml_for_import = tml_dict

            resp = target_client.import_tml(
                [to_tml_string(tml_for_import)],
                import_policy="PARTIAL_OBJECT",
                create_new=False,
            )

            # Per-object status: response.status.status_code (OK / WARNING / ERROR). The REST
            # reference only says the response is an array of objects, so this shape is the one
            # seen from a live cluster, not a documented one. Anything else fails closed.
            status_ok = _parse_import_status(resp, obj_id)

            if status_ok:
                results.append({"obj_id": obj_id, "name": name, "type": obj_type,
                                "status": "success"})

                # Reapply non-migration tags in target
                if other_tags:
                    _type_for_tag = _ts_type_for_tag(obj_type)
                    if _type_for_tag:
                        try:
                            # The tags API takes a GUID or name; look the target GUID up by obj_id.
                            tgt_guid = target_client.find_guid_by_obj_id(_type_for_tag, obj_id)
                            if not tgt_guid:
                                raise RuntimeError("object not found in target by obj_id")
                            target_client.assign_tags(
                                [{"type": _type_for_tag, "identifier": tgt_guid}],
                                other_tags,
                            )
                        except Exception as tag_err:
                            print(f"[WARN] Failed to reapply tags for '{name}': {tag_err}",
                                  file=sys.stderr)

                # Path B: bind variables after table import
                if obj_type == "LOGICAL_TABLE" and entry["folder"] == "tables":
                    warnings = _parameterize_table(target_client, obj_id, db_var, schema_var)
                    if warnings:
                        # The table imported (with the target db/schema literals) but is not
                        # bound to its variables; keep it visible in stage9_import.json.
                        results[-1]["warnings"] = warnings

            else:
                failed_obj_ids.add(obj_id)
                err = _extract_error(resp)
                results.append({"obj_id": obj_id, "name": name, "type": obj_type,
                                "status": "failed", "error": err})

        except Exception as exc:
            failed_obj_ids.add(obj_id)
            results.append({"obj_id": obj_id, "name": name, "type": obj_type,
                            "status": "failed", "error": str(exc)})
            print(f"[FAIL] Import {obj_type} '{name}' ({obj_id}): {exc}", file=sys.stderr)

    return results


def _parse_import_status(resp: dict | list, obj_id: str) -> bool:
    """True only if the API reported at least one object and every one of them OK or WARNING.

    One TML is sent per call, so one object comes back; if the response ever holds more,
    a single OK must not hide another object's ERROR. Anything unreadable counts as a failure.
    """
    objects = resp if isinstance(resp, list) else resp.get("object", [resp])
    if not objects:
        return False
    for obj in objects:
        status = obj.get("response", {}).get("status", {}).get("status_code", "ERROR")
        if status not in ("OK", "WARNING"):
            return False
    return True


def _extract_error(resp: dict | list) -> str:
    objects = resp if isinstance(resp, list) else resp.get("object", [resp])
    for obj in objects:
        msg = obj.get("response", {}).get("status", {}).get("error_message", "")
        if msg:
            return msg
    return str(resp)[:200] if objects else "Import returned no objects."


def _ts_type_for_tag(meta_type: str) -> str | None:
    return {"LOGICAL_TABLE": "LOGICAL_TABLE", "ANSWER": "ANSWER",
            "LIVEBOARD": "LIVEBOARD", "COLLECTION": "COLLECTION"}.get(meta_type)


def _parameterize_table(
    client: TSClient, obj_id: str, db_var: str, schema_var: str,
) -> list[str]:
    """
    Bind a just-imported table's databaseName and schemaName to their variables.

    One parameterize call binds every field in it to ONE variable, so the database and the
    schema need separate calls (binding both to the database variable would be wrong). The
    API also wants the table's GUID or name, not its obj_id, so look the GUID up first.
    Returns warnings; an empty list means everything configured was bound.
    """
    bindings = [(f, v) for f, v in (("databaseName", db_var), ("schemaName", schema_var)) if v]
    if not bindings:
        return []
    guid = client.find_guid_by_obj_id("LOGICAL_TABLE", obj_id)
    if not guid:
        msg = f"table {obj_id} not found in the target by obj_id; not parameterized"
        print(f"[WARN] {msg}", file=sys.stderr)
        return [msg]
    warnings: list[str] = []
    for field, var in bindings:
        try:
            client.parameterize_fields(
                table_identifier=guid, field_names=[field], variable_identifier=var)
            print(f"[PARAMETERIZE] Bound {field} on table {obj_id} to variable '{var}'.")
        except Exception as e:
            warnings.append(f"{field} -> '{var}': {e}")
            print(f"[WARN] Failed to bind {field} on table {obj_id} to '{var}': {e}",
                  file=sys.stderr)
    return warnings


def safety_gate(
    target_client: TSClient,
    table_results: list[dict],
    src_db: str, src_schema: str,
    failed_obj_ids: set[str],
) -> None:
    """After tables import: verify no source db/schema values remain."""
    def fail(res: dict, message: str) -> None:
        res["status"] = "failed"
        res["error"] = message
        failed_obj_ids.add(res["obj_id"])
        print(f"[SAFETY GATE FAIL] Table {res['obj_id']}: {message}", file=sys.stderr)

    for res in table_results:
        if res["status"] != "success":
            continue
        obj_id = res["obj_id"]
        # Fail closed: a table that cannot be read back is not known to be free of source
        # values, so it is failed (it keeps its tag and a re-run retries it) rather than trusted.
        try:
            exported = target_client.export_tml(
                [{"identifier": obj_id}],
                include_obj_id=True, include_obj_id_ref=True, include_guid=False,
            )
            edocs = exported if isinstance(exported, list) else exported.get("object", [])
            if not edocs:
                fail(res, "Safety gate: the table could not be read back from the target "
                          "(export returned nothing), so it was not verified.")
                continue
            doc = edocs[0].get("edoc") or edocs[0].get("content")
            tml_dict = parse_tml(doc if isinstance(doc, str) else json.dumps(edocs[0]))
            db, schema = get_table_db_schema(tml_dict)
        except Exception as e:
            fail(res, f"Safety gate: the table could not be read back from the target "
                      f"({e}), so it was not verified.")
            continue
        if (db and db == src_db) or (schema and schema == src_schema):
            fail(res, f"Safety gate: table still has source db/schema "
                      f"(db={db}, schema={schema}) after import.")


def untiered_results(manifest: list[dict], results: list[dict]) -> list[dict]:
    """A failed result for every manifest object that no import tier handled.

    An object whose type/folder matches no tier would otherwise never be attempted and show up
    only as a quiet "no import result". Failing it keeps its tag and turns the run red.
    """
    done = {r["obj_id"] for r in results}
    out = []
    for e in manifest:
        if e["obj_id"] in done:
            continue
        msg = (f"No import step handles type {e['type']} / folder {e.get('folder', '')!r}; "
               "the object was not imported.")
        print(f"[STAGE 9][FAIL] '{e['name']}' ({e['obj_id']}): {msg}", file=sys.stderr)
        out.append({"obj_id": e["obj_id"], "name": e["name"], "type": e["type"],
                    "status": "failed", "error": msg})
    return out


def main():
    if os.environ.get("DRY_RUN", "false").lower() == "true":
        print("[STAGE 9] DRY_RUN=true — skipping import entirely.")
        sys.exit(0)

    org_key = os.environ["ORG_KEY"]
    cfg_path = os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{org_key}_config.json")
    export_path = os.environ.get("EXPORT_OUT", f"/tmp/ts_migration_{org_key}_export_results.json")
    manifest_path = os.environ.get("MANIFEST_OUT", f"/tmp/ts_migration_{org_key}_manifest.json")
    repo_root = Path(os.environ.get("REPO_ROOT", "."))

    cfg = json.loads(Path(cfg_path).read_text())
    export_results = json.loads(Path(export_path).read_text())
    manifest = json.loads(Path(manifest_path).read_text())

    # Build tml_path lookup from export results
    tml_paths = {r["obj_id"]: r["tml_path"] for r in export_results if r["status"] == "success"}
    for entry in manifest:
        entry["tml_path"] = tml_paths.get(entry["obj_id"], "")

    tgt = cfg["target"]
    target_client = TSClient(
        tgt["base_url"], tgt["org_id"],
        resolve_secret("TS_TARGET_USERNAME"),
        resolve_secret("TS_TARGET_SECRET_KEY"),
    )
    target_client.authenticate()

    failed_obj_ids: set[str] = set()
    all_results = []

    for folder, types, _subtypes in IMPORT_TIERS:
        # Manifest entries use `folder` (set by stage3) not `subtypes`, so filter LOGICAL_TABLE
        # objects by folder name which matches the tier name (tables/views/models).
        # Other types have a single tier each, so no folder filter is needed.
        tier_entries = [
            e for e in manifest
            if e["type"] in types and
               ("LOGICAL_TABLE" not in types or e.get("folder") == folder)
        ]
        if not tier_entries:
            continue
        tier_entries = order_by_dependencies(tier_entries, repo_root)
        print(f"[STAGE 9] Importing tier '{folder}': {len(tier_entries)} objects...")
        tier_results = import_tier(
            target_client, tier_entries, cfg, repo_root, failed_obj_ids
        )
        all_results.extend(tier_results)

        # Safety gate after table tier
        if folder == "tables":
            safety_gate(
                target_client,
                [r for r in tier_results if r["status"] == "success"],
                variable_config(cfg, "database")["source_value"],
                variable_config(cfg, "schema")["source_value"],
                failed_obj_ids,
            )

    all_results.extend(untiered_results(manifest, all_results))

    out_path = os.environ.get("IMPORT_OUT", f"/tmp/ts_migration_{org_key}_import_results.json")
    Path(out_path).write_text(json.dumps(all_results, indent=2), encoding="utf-8")

    successes = sum(1 for r in all_results if r["status"] == "success")
    failures = sum(1 for r in all_results if r["status"] == "failed")
    skipped = sum(1 for r in all_results if r["status"] == "skipped")
    print(f"[STAGE 9] Import complete: {successes} success, {failures} failed, {skipped} skipped.")


if __name__ == "__main__":
    main()
