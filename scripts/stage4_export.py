#!/usr/bin/env python3
"""
Stage 4: Export TML for all manifest objects.

Uses export_options: include_obj_id=True, include_obj_id_ref=True,
                     include_guid=False, export_with_associated_feedbacks=True

Saves TML as JSON files: orgs/<org_key>/<folder>/<slug>__<obj_id>__<timestamp>.<type>.tml
Writes export_results.json with per-object success/failure.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.secrets import resolve_secret
from lib.ts_client import TSClient
from lib.tml_utils import parse_tml, to_tml_string, slug_from_name

TYPE_EXT = {
    "LOGICAL_TABLE": "table",
    "ANSWER": "answer",
    "LIVEBOARD": "liveboard",
    "COLLECTION": "collection",
    "FEEDBACK": "feedback",
}


def export_objects(
    source_client: TSClient,
    manifest: list[dict],
    org_key: str,
    repo_root: Path,
    timestamp: str = "",
) -> list[dict]:
    results = []

    for entry in manifest:
        obj_type = entry["type"]
        folder = entry["folder"]
        name = entry["name"]
        obj_id = entry["obj_id"]
        source_guid = entry["source_guid"]
        ext = TYPE_EXT.get(obj_type, obj_type.lower())

        slug = slug_from_name(name)
        ts_suffix = f"__{timestamp}" if timestamp else ""
        filename = f"{slug}__{obj_id}{ts_suffix}.{ext}.tml"
        out_path = repo_root / "orgs" / org_key / folder / filename

        try:
            resp_data = source_client.export_tml(
                metadata=[{"identifier": source_guid}],
                include_obj_id=True,
                include_obj_id_ref=True,
                include_guid=False,
                export_with_feedbacks=True,
            )
            # export returns a list of edoc objects
            edocs = resp_data if isinstance(resp_data, list) else resp_data.get("object", [resp_data])
            if not edocs:
                raise ValueError("Empty export response")
            edoc = edocs[0]
            tml_str = edoc.get("edoc") or edoc.get("content") or json.dumps(edoc)
            tml_dict = parse_tml(tml_str) if isinstance(tml_str, str) else tml_str

            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(to_tml_string(tml_dict), encoding="utf-8")

            results.append({
                "obj_id": obj_id,
                "name": name,
                "type": obj_type,
                "folder": folder,
                "tml_path": str(out_path.relative_to(repo_root)),
                "status": "success",
            })
            print(f"[EXPORT] {obj_type} '{name}' -> {out_path.name}")

        except Exception as exc:
            results.append({
                "obj_id": obj_id,
                "name": name,
                "type": obj_type,
                "folder": folder,
                "tml_path": "",
                "status": "failed",
                "error": str(exc),
            })
            print(f"[FAIL] Export {obj_type} '{name}' ({obj_id}): {exc}", file=sys.stderr)

    return results


def main():
    org_key = os.environ["ORG_KEY"]
    cfg_path = os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{org_key}_config.json")
    manifest_path = os.environ.get("MANIFEST_OUT", f"/tmp/ts_migration_{org_key}_manifest.json")
    repo_root = Path(os.environ.get("REPO_ROOT", "."))
    timestamp = os.environ.get("TIMESTAMP", "")
    cfg = json.loads(Path(cfg_path).read_text())
    manifest = json.loads(Path(manifest_path).read_text())

    src = cfg["source"]
    source_client = TSClient(
        src["base_url"], src["org_id"],
        resolve_secret("TS_SOURCE_USERNAME"),
        resolve_secret("TS_SOURCE_SECRET_KEY"),
    )
    source_client.authenticate()

    results = export_objects(source_client, manifest, org_key, repo_root, timestamp)

    out_path = os.environ.get("EXPORT_OUT", f"/tmp/ts_migration_{org_key}_export_results.json")
    Path(out_path).write_text(json.dumps(results, indent=2), encoding="utf-8")
    failures = [r for r in results if r["status"] == "failed"]
    print(f"[STAGE 4] Exported {len(results) - len(failures)}/{len(results)} objects. "
          f"{len(failures)} failed.")


if __name__ == "__main__":
    main()
