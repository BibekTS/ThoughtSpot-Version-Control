#!/usr/bin/env python3
"""
Stage 4c: Export Spotter model-level NL instructions for all model-type objects.

Calls /api/rest/2.0/ai/instructions/get (getNLInstructions) for every model in
the manifest and saves the result to
orgs/<org_key>/instructions/instructions__<timestamp>.json, which Stage 6
commits to ts-dev alongside TML and memory.

Models with no instructions are omitted. Skips silently (exit 0, non-blocking) if:
  - No model-type objects exist in the manifest
  - Spotter is not enabled / insufficient permissions (HTTP 403)
  - No model has any instructions configured

A failure on one model is recorded and does not stop the others.

Beta endpoint — requires ThoughtSpot 10.15.0.cl or later with Spotter enabled
and the CAN_USE_SPOTTER privilege.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.spotter_results import ENDPOINT_UNAVAILABLE_STATUSES
from lib.secrets import resolve_secret
from lib.ts_client import TSClient

MODEL_FOLDERS = {"models"}


def export_instructions(
    source_client: TSClient,
    manifest: list[dict],
    org_key: str,
    repo_root: Path,
    timestamp: str = "",
) -> dict:
    models = [e for e in manifest if e.get("folder") in MODEL_FOLDERS]
    if not models:
        print("[STAGE 4c] No model-type objects in manifest — skipping instructions export.")
        return {"skipped": True, "reason": "no_models", "model_count": 0}

    print(f"[STAGE 4c] Exporting NL instructions for {len(models)} model(s).")

    exported: list[dict] = []
    failed: list[dict] = []

    for entry in models:
        obj_id, name = entry["obj_id"], entry.get("name", "")
        try:
            resp = source_client.get_nl_instructions(entry["source_guid"])
        except Exception as exc:
            failed.append({"obj_id": obj_id, "name": name, "error": str(exc)})
            print(f"[STAGE 4c][WARN] '{name}' ({obj_id}): request failed: {exc}", file=sys.stderr)
            continue

        if resp.status_code == 403:
            print("[STAGE 4c] HTTP 403 — Spotter not enabled or missing CAN_USE_SPOTTER / "
                  "view access. Skipping instructions export (non-blocking).", file=sys.stderr)
            return {"skipped": True, "reason": "spotter_not_enabled", "http_status": 403}

        if resp.status_code in ENDPOINT_UNAVAILABLE_STATUSES:
            print(f"[STAGE 4c] HTTP {resp.status_code} — the NL instructions API is not available "
                  "on this cluster (needs 10.15.0.cl+). Skipping instructions export "
                  "(non-blocking).", file=sys.stderr)
            return {"skipped": True, "reason": "endpoint_unavailable",
                    "http_status": resp.status_code, "failed": failed}

        if resp.status_code != 200:
            failed.append({"obj_id": obj_id, "name": name, "error": f"HTTP {resp.status_code}"})
            print(f"[STAGE 4c][WARN] '{name}' ({obj_id}): HTTP {resp.status_code}", file=sys.stderr)
            continue

        try:
            info = resp.json().get("nl_instructions_info") or []
        except Exception:
            failed.append({"obj_id": obj_id, "name": name, "error": "unparseable response"})
            continue

        # Drop scopes with no instruction text; nothing to migrate for them.
        info = [i for i in info if any((t or "").strip() for t in i.get("instructions") or [])]
        if not info:
            print(f"[STAGE 4c] '{name}' ({obj_id}): no instructions configured.")
            continue

        exported.append({"obj_id": obj_id, "name": name, "nl_instructions_info": info})
        count = sum(len(i.get("instructions") or []) for i in info)
        print(f"[STAGE 4c] '{name}' ({obj_id}): {count} instruction(s).")

    if not exported:
        return {"skipped": True, "reason": "no_instructions",
                "model_count": len(models), "failed": failed}

    ts_suffix = f"__{timestamp}" if timestamp else ""
    out_path = repo_root / "orgs" / org_key / "instructions" / f"instructions{ts_suffix}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps({"models": exported}, indent=2, ensure_ascii=False) + "\n",
                        encoding="utf-8")
    print(f"[STAGE 4c] Instructions saved -> {out_path}")

    return {
        "skipped": False,
        "model_count": len(models),
        "exported_count": len(exported),
        "failed": failed,
        "instructions_path": str(out_path.relative_to(repo_root)),
    }


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

    result = export_instructions(source_client, manifest, org_key, repo_root, timestamp)

    out_path = os.environ.get("INSTRUCTIONS_EXPORT_OUT",
                              f"/tmp/ts_migration_{org_key}_instructions_export.json")
    Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
