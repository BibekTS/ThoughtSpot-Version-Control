#!/usr/bin/env python3
"""
Stage 5: Verify parameterization and sanitize TML before commit.

- Checks table TML: db/schema must not be literal source values (Path B warning)
- Strips guid/fqn fields and migration tag from all TML
- Scans for ts_var() references; adds found variables to the variable sync set
- Overwrites TML files in-place with sanitized content
- Writes sanitize_results.json
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.variables import variable_config
from lib.tml_utils import (
    parse_tml, to_tml_string, sanitize,
    get_table_db_schema, is_variable_bound,
    scan_ts_var_references, extract_connection_name,
)


def sanitize_all(
    export_results: list[dict],
    manifest: list[dict],
    cfg: dict,
    repo_root: Path,
) -> dict:
    migration_tag = cfg["migration_tag"]
    src_db = variable_config(cfg, "database")["source_value"]
    src_schema = variable_config(cfg, "schema")["source_value"]

    warnings = []
    var_sync_set: set[str] = set()
    connection_names: set[str] = set()
    sanitize_results = []

    # build obj_id -> manifest entry for quick lookup
    manifest_map = {e["obj_id"]: e for e in manifest}

    for res in export_results:
        if res["status"] != "success" or not res["tml_path"]:
            sanitize_results.append({**res, "sanitize_status": "skipped_no_tml"})
            continue

        tml_path = repo_root / res["tml_path"]
        if not tml_path.exists():
            sanitize_results.append({**res, "sanitize_status": "skipped_file_missing"})
            continue

        try:
            tml_dict = parse_tml(tml_path.read_text(encoding="utf-8"))
        except Exception as e:
            sanitize_results.append({**res, "sanitize_status": "failed", "error": str(e)})
            continue

        # Scan ts_var() references for the variable sync set
        ts_vars = scan_ts_var_references(tml_dict)
        var_sync_set.update(ts_vars)

        # For tables: check parameterization
        if res["type"] == "LOGICAL_TABLE" and "tables" in res["folder"]:
            db, schema = get_table_db_schema(tml_dict)
            db_bound = is_variable_bound(db)
            schema_bound = is_variable_bound(schema)

            if db and db == src_db and not db_bound:
                warnings.append({
                    "obj_id": res["obj_id"],
                    "name": res["name"],
                    "field": "db",
                    "literal_value": db,
                    "message": f"Literal source db value '{db}' found in TML (Path B). "
                               "Will substitute at import time.",
                })
            if schema and schema == src_schema and not schema_bound:
                warnings.append({
                    "obj_id": res["obj_id"],
                    "name": res["name"],
                    "field": "schema",
                    "literal_value": schema,
                    "message": f"Literal source schema value '{schema}' found in TML (Path B). "
                               "Will substitute at import time.",
                })

            conn_name = extract_connection_name(tml_dict)
            if conn_name:
                connection_names.add(conn_name)

        # Sanitize: strip guid/fqn, remove migration tag
        clean = sanitize(tml_dict, migration_tag)
        tml_path.write_text(to_tml_string(clean), encoding="utf-8")

        sanitize_results.append({**res, "sanitize_status": "ok"})

    return {
        "results": sanitize_results,
        "warnings": warnings,
        "var_sync_set": sorted(var_sync_set),
        "connection_names": sorted(connection_names),
    }


def main():
    org_key = os.environ["ORG_KEY"]
    cfg_path = os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{org_key}_config.json")
    manifest_path = os.environ.get("MANIFEST_OUT", f"/tmp/ts_migration_{org_key}_manifest.json")
    export_path = os.environ.get("EXPORT_OUT", f"/tmp/ts_migration_{org_key}_export_results.json")
    repo_root = Path(os.environ.get("REPO_ROOT", "."))

    cfg = json.loads(Path(cfg_path).read_text())
    manifest = json.loads(Path(manifest_path).read_text())
    export_results = json.loads(Path(export_path).read_text())

    result = sanitize_all(export_results, manifest, cfg, repo_root)

    out_path = os.environ.get("SANITIZE_OUT", f"/tmp/ts_migration_{org_key}_sanitize_results.json")
    Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")

    warn_count = len(result["warnings"])
    print(f"[STAGE 5] Sanitization complete. {warn_count} Path-B warnings. "
          f"Variable sync set: {result['var_sync_set']}")
    if warn_count:
        for w in result["warnings"]:
            print(f"  [WARN] {w['message']}")


if __name__ == "__main__":
    main()
