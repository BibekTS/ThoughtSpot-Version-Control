#!/usr/bin/env python3
"""
Stage 4b: Export Spotter Model Memory for all model-type objects.

Calls /api/rest/2.0/ai/memory/export for every model in the manifest and
saves the YAML payload to orgs/<org_key>/memory/memory__<timestamp>.yaml,
which Stage 6 commits to ts-dev alongside TML.

Skips silently (exit 0, non-blocking) if:
  - No model-type objects exist in the manifest
  - Spotter is not enabled (HTTP 403)
  - Memory content is empty

Beta endpoint — requires ThoughtSpot v26.8.0.cl or later with Spotter enabled.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.artifacts import memory_is_empty
from lib.spotter_results import ENDPOINT_UNAVAILABLE_STATUSES
from lib.ts_client import TSClient

MODEL_FOLDERS = {"models"}


def export_memory(
    source_client: TSClient,
    manifest: list[dict],
    org_key: str,
    repo_root: Path,
    timestamp: str = "",
) -> dict:
    model_ids = [
        entry["obj_id"]
        for entry in manifest
        if entry.get("folder") in MODEL_FOLDERS
    ]

    if not model_ids:
        print("[STAGE 4b] No model-type objects in manifest — skipping memory export.")
        return {"skipped": True, "reason": "no_models", "model_count": 0}

    print(f"[STAGE 4b] Exporting memory for {len(model_ids)} model(s): {model_ids}")

    try:
        resp = source_client.post("/ai/memory/export", {
            "sources": [{"type": "DATA_MODEL", "identifiers": model_ids}]
        })
    except Exception as exc:
        print(f"[STAGE 4b] Memory export request failed: {exc} — skipping (non-blocking).",
              file=sys.stderr)
        return {"skipped": True, "reason": "api_error", "error": str(exc)}

    if resp.status_code == 403:
        print("[STAGE 4b] HTTP 403 — Spotter not enabled or insufficient permissions. "
              "Skipping memory export (non-blocking).", file=sys.stderr)
        return {"skipped": True, "reason": "spotter_not_enabled", "http_status": 403}

    if resp.status_code in ENDPOINT_UNAVAILABLE_STATUSES:
        print(f"[STAGE 4b] HTTP {resp.status_code} — the memory API is not available on this "
              "cluster (needs 26.8.0.cl+). Skipping memory export (non-blocking).", file=sys.stderr)
        return {"skipped": True, "reason": "endpoint_unavailable", "http_status": resp.status_code}

    if resp.status_code != 200:
        print(f"[STAGE 4b] Memory export returned HTTP {resp.status_code} — "
              "skipping (non-blocking).", file=sys.stderr)
        return {"skipped": True, "reason": "http_error", "http_status": resp.status_code}

    try:
        data = resp.json()
        memory_yaml = data.get("content") or ""
    except Exception:
        memory_yaml = resp.text or ""

    if memory_is_empty(memory_yaml):
        print("[STAGE 4b] Memory export returned empty content — no memory to migrate.")
        return {"skipped": True, "reason": "empty_memory", "model_count": len(model_ids)}

    ts_suffix = f"__{timestamp}" if timestamp else ""
    filename = f"memory{ts_suffix}.yaml"
    out_path = repo_root / "orgs" / org_key / "memory" / filename
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(memory_yaml, encoding="utf-8")
    print(f"[STAGE 4b] Memory YAML saved -> {out_path} ({len(memory_yaml.encode())} bytes)")

    return {
        "skipped": False,
        "model_count": len(model_ids),
        "model_ids": model_ids,
        "memory_path": str(out_path.relative_to(repo_root)),
        "memory_size_bytes": len(memory_yaml.encode()),
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
        os.environ["TS_SOURCE_USERNAME"],
        os.environ["TS_SOURCE_SECRET_KEY"],
    )
    source_client.authenticate()

    result = export_memory(source_client, manifest, org_key, repo_root, timestamp)

    out_path = os.environ.get("MEMORY_EXPORT_OUT",
                              f"/tmp/ts_migration_{org_key}_memory_export.json")
    Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
