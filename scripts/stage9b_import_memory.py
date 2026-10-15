#!/usr/bin/env python3
"""
Stage 9b: Import Spotter Model Memory into the target ThoughtSpot org.

Reads memory__<TIMESTAMP>.yaml produced by Stage 4b (same run) and imports it via
/api/rest/2.0/ai/memory/import.

Dry-run mode (DRY_RUN=true):
  Calls importMemory with dry_run=true — validates payload and logs preview
  counts (inserted/deleted per model) WITHOUT writing anything to the target.

Live mode:
  Validates first with dry_run=true, then applies with dry_run=false.
  The import REPLACES existing memory on target models — running twice
  overwrites whatever was added manually in the target between runs.

Skips silently (exit 0, non-blocking) if:
  - this run's memory export does not exist (Stage 4b was skipped); an older
    run's file is never used when TIMESTAMP is set
  - Spotter is not enabled on target (HTTP 403)
  - Payload fails validation (logged as warning, pipeline continues)

Beta endpoint — requires ThoughtSpot v26.8.0.cl or later with Spotter enabled.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.artifacts import memory_is_empty, resolve_export_file
from lib.ts_client import TSClient


def import_memory(target_client: TSClient, memory_yaml: str, dry_run: bool) -> dict:
    def call_import(is_dry_run: bool) -> dict:
        resp = target_client.post("/ai/memory/import", {
            "content": memory_yaml,
            "dry_run": is_dry_run,
        })
        if resp.status_code == 403:
            return {"_http_status": 403, "status": "SKIPPED",
                    "_reason": "Spotter not enabled or insufficient permissions"}
        if resp.status_code != 200:
            return {"_http_status": resp.status_code, "status": "FAILED",
                    "_reason": f"HTTP {resp.status_code}"}
        try:
            return resp.json()
        except Exception:
            return {"status": "FAILED", "_raw": resp.text[:500]}

    # Always validate first
    print("[STAGE 9b] Validating memory payload (dry_run=true)...")
    validation = call_import(True)
    val_status = validation.get("status", "UNKNOWN")
    failures = validation.get("validation_failures") or []

    if val_status == "SKIPPED":
        reason = validation.get("_reason", "unknown")
        print(f"[STAGE 9b] Memory import skipped: {reason} (non-blocking).", file=sys.stderr)
        return {"skipped": True, "reason": reason, "dry_run": dry_run}

    if val_status == "VALIDATION_FAILED" or failures:
        print(f"[STAGE 9b][WARN] Memory payload has {len(failures)} validation issue(s) — "
              "skipping import (non-blocking).", file=sys.stderr)
        for f in failures[:10]:
            print(f"  line {f.get('line_number')}: [{f.get('reason')}] "
                  f"{f.get('field_name')} — {f.get('message')}", file=sys.stderr)
        return {
            "skipped": False,
            "dry_run": dry_run,
            "validation_status": val_status,
            "validation_failures": failures,
            "applied": False,
        }

    # Log preview counts
    summary = validation.get("summary") or []
    for row in summary:
        src = (row.get("source") or {}).get("identifier", "?")
        mtype = row.get("memory_type", "?")
        ins = row.get("inserted_record_count", 0)
        dlt = row.get("deleted_record_count", 0)
        existing = row.get("existing_record_count", 0)
        print(f"[STAGE 9b] Preview — model {src}: {mtype}: "
              f"existing={existing}, delete={dlt}, insert={ins}")

    if dry_run:
        print("[STAGE 9b] DRY_RUN=true — memory validated OK, no writes applied to target.")
        return {
            "skipped": False,
            "dry_run": True,
            "validation_status": val_status,
            "preview_summary": summary,
            "applied": False,
        }

    # Apply
    print("[STAGE 9b] Applying memory import (dry_run=false)...")
    apply_result = call_import(False)
    apply_status = apply_result.get("status", "UNKNOWN")
    print(f"[STAGE 9b] Memory import status: {apply_status}")

    if apply_status != "SUCCESS":
        diagnostics = apply_result.get("diagnostics") or []
        for d in diagnostics:
            sub = d.get("sub_status", "?")
            for msg in (d.get("messages") or []):
                print(f"[STAGE 9b][{sub}] {msg}", file=sys.stderr)

    return {
        "skipped": False,
        "dry_run": False,
        "validation_status": val_status,
        "apply_status": apply_status,
        "applied": apply_status == "SUCCESS",
        "summary": apply_result.get("summary"),
    }


def main():
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    org_key = os.environ["ORG_KEY"]
    cfg_path = os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{org_key}_config.json")
    repo_root = Path(os.environ.get("REPO_ROOT", "."))

    timestamp = os.environ.get("TIMESTAMP", "")
    out_path = os.environ.get("MEMORY_IMPORT_OUT",
                              f"/tmp/ts_migration_{org_key}_memory_import.json")
    memory_path = resolve_export_file(
        repo_root / "orgs" / org_key / "memory", "memory", ".yaml", timestamp
    )
    if memory_path is None:
        print("[STAGE 9b] No memory export for this run — Stage 4b was skipped, "
              "nothing to import.")
        result = {"skipped": True, "reason": "no_memory_file"}
        Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
        return

    print(f"[STAGE 9b] Using memory export: {memory_path}")
    memory_yaml = memory_path.read_text(encoding="utf-8")
    if memory_is_empty(memory_yaml):
        # Import replaces target memory; an empty payload would wipe it.
        print("[STAGE 9b] Memory export contains no memories — nothing to import.")
        Path(out_path).write_text(
            json.dumps({"skipped": True, "reason": "empty_memory"}, indent=2), encoding="utf-8")
        return
    cfg = json.loads(Path(cfg_path).read_text())

    tgt = cfg["target"]
    target_client = TSClient(
        tgt["base_url"], tgt["org_id"],
        os.environ["TS_TARGET_USERNAME"],
        os.environ["TS_TARGET_SECRET_KEY"],
    )
    target_client.authenticate()

    result = import_memory(target_client, memory_yaml, dry_run)

    Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
