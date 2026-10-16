#!/usr/bin/env python3
"""
Stage 9c: Import Spotter model-level NL instructions into the target org.

Reads instructions__<TIMESTAMP>.json produced by Stage 4c (same run). For each
model, resolves the target GUID by obj_id, then calls
/api/rest/2.0/ai/instructions/set (setNLInstructions).

Instructions are NEVER cleared: a model with no instructions in the export is left
untouched on the target. Setting REPLACES the model's existing instructions, so the current target value
is read first and the set is skipped when it is already identical (idempotent).

Dry-run mode (DRY_RUN=true): reads target state and logs what would change;
nothing is written.

Skips non-blocking (exit 0) if the export file is absent, Spotter is not enabled
or permissions are insufficient (HTTP 403), or a model does not exist on the
target yet (e.g. dry run before the first real import). Per-model failures are
recorded in the result JSON and do not stop other models.

Beta endpoint — requires ThoughtSpot 10.15.0.cl or later with Spotter enabled,
CAN_USE_SPOTTER, and edit access or SPOTTER_COACHING_PRIVILEGE on the model.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.artifacts import resolve_export_file
from lib.spotter_results import ENDPOINT_UNAVAILABLE_STATUSES
from lib.secrets import resolve_secret
from lib.ts_client import TSClient


def _normalize(info: list[dict]) -> list[tuple[str, tuple[str, ...]]]:
    """Order-insensitive-by-scope, whitespace-trimmed form for equality checks."""
    return sorted(
        (i.get("scope", "GLOBAL"), tuple((t or "").strip() for t in i.get("instructions") or []))
        for i in info
    )


def import_instructions(target_client: TSClient, payload: dict, dry_run: bool) -> dict:
    results: list[dict] = []

    for model in payload.get("models", []):
        obj_id, name = model["obj_id"], model.get("name", "")
        desired = model["nl_instructions_info"]
        rec = {"obj_id": obj_id, "name": name}

        # Never clear target instructions: nothing in dev means leave the target alone.
        if not any((t or "").strip() for i in desired for t in i.get("instructions") or []):
            print(f"[STAGE 9c] '{name}': no instructions in export — target left untouched.")
            results.append({**rec, "status": "skipped", "reason": "no_source_instructions"})
            continue

        guid = target_client.find_model_guid(obj_id)
        if not guid:
            print(f"[STAGE 9c][WARN] '{name}' ({obj_id}) not found in target — skipping.",
                  file=sys.stderr)
            results.append({**rec, "status": "skipped", "reason": "model_not_in_target"})
            continue

        cur = target_client.get_nl_instructions(guid)
        if cur.status_code == 403:
            print("[STAGE 9c] HTTP 403 — Spotter not enabled or insufficient permissions. "
                  "Skipping instructions import (non-blocking).", file=sys.stderr)
            return {"skipped": True, "reason": "spotter_not_enabled", "http_status": 403,
                    "dry_run": dry_run}
        if cur.status_code in ENDPOINT_UNAVAILABLE_STATUSES:
            print(f"[STAGE 9c] HTTP {cur.status_code} — the NL instructions API is not available "
                  "on this cluster. Skipping instructions import (non-blocking).", file=sys.stderr)
            return {"skipped": True, "reason": "endpoint_unavailable",
                    "http_status": cur.status_code, "dry_run": dry_run}
        existing: list[dict] = []
        if cur.status_code == 200:
            try:
                existing = cur.json().get("nl_instructions_info") or []
            except Exception:
                print(f"[STAGE 9c][WARN] '{name}': could not parse target instructions; "
                      "treating as none.", file=sys.stderr)
        else:
            # Without this, a failed read is indistinguishable from "target has 0".
            print(f"[STAGE 9c][WARN] '{name}': reading target instructions returned HTTP "
                  f"{cur.status_code}; treating as none.", file=sys.stderr)

        n_new = sum(len(i.get("instructions") or []) for i in desired)
        n_old = sum(len(i.get("instructions") or []) for i in existing)

        if _normalize(existing) == _normalize(desired):
            print(f"[STAGE 9c] '{name}': target already has identical instructions — no change.")
            results.append({**rec, "status": "unchanged", "instruction_count": n_new})
            continue

        print(f"[STAGE 9c] '{name}': target has {n_old} instruction(s), source has {n_new} — "
              f"{'would replace' if dry_run else 'replacing'}.")
        if dry_run:
            results.append({**rec, "status": "dry_run", "existing_count": n_old,
                            "instruction_count": n_new})
            continue

        resp = target_client.set_nl_instructions(guid, desired)
        ok = False
        if resp.status_code == 200:
            try:
                ok = bool(resp.json().get("success"))
            except Exception:
                ok = False
        if ok:
            print(f"[STAGE 9c] '{name}': APPLIED — {n_new} instruction(s) set on target "
                  f"(HTTP {resp.status_code}, success=true).")
            results.append({**rec, "status": "applied", "existing_count": n_old,
                            "instruction_count": n_new})
        else:
            print(f"[STAGE 9c][WARN] '{name}': set failed (HTTP {resp.status_code}): "
                  f"{resp.text[:200]}", file=sys.stderr)
            results.append({**rec, "status": "failed", "http_status": resp.status_code,
                            "error": resp.text[:200]})

    counts = {s: sum(1 for r in results if r["status"] == s)
              for s in ("applied", "dry_run", "unchanged", "skipped", "failed")}
    print(f"[STAGE 9c] Summary ({'DRY RUN' if dry_run else 'LIVE'}): "
          f"{len(results)} model(s) — {counts['applied']} applied, "
          f"{counts['dry_run']} would change, {counts['unchanged']} unchanged, "
          f"{counts['skipped']} skipped, {counts['failed']} failed.")

    return {
        "skipped": False,
        "dry_run": dry_run,
        "applied": counts["applied"],
        "failed": counts["failed"],
        "results": results,
    }


def main():
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    org_key = os.environ["ORG_KEY"]
    cfg_path = os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{org_key}_config.json")
    repo_root = Path(os.environ.get("REPO_ROOT", "."))
    timestamp = os.environ.get("TIMESTAMP", "")
    out_path = os.environ.get("INSTRUCTIONS_IMPORT_OUT",
                              f"/tmp/ts_migration_{org_key}_instructions_import.json")

    path = resolve_export_file(
        repo_root / "orgs" / org_key / "instructions", "instructions", ".json", timestamp
    )
    if path is None:
        print("[STAGE 9c] No instructions export for this run — Stage 4c was skipped, "
              "nothing to import.")
        Path(out_path).write_text(
            json.dumps({"skipped": True, "reason": "no_instructions_file"}, indent=2),
            encoding="utf-8")
        return

    print(f"[STAGE 9c] Using instructions export: {path}")
    payload = json.loads(path.read_text(encoding="utf-8"))
    cfg = json.loads(Path(cfg_path).read_text())

    tgt = cfg["target"]
    target_client = TSClient(
        tgt["base_url"], tgt["org_id"],
        resolve_secret("TS_TARGET_USERNAME"),
        resolve_secret("TS_TARGET_SECRET_KEY"),
    )
    target_client.authenticate()

    result = import_instructions(target_client, payload, dry_run)
    Path(out_path).write_text(json.dumps(result, indent=2), encoding="utf-8")
    if result.get("failed"):
        print(f"[STAGE 9c][WARN] {result['failed']} model(s) failed — see {out_path}.",
              file=sys.stderr)


if __name__ == "__main__":
    main()
