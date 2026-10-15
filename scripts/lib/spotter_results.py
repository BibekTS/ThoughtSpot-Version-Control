"""
Turn the Spotter memory / NL instructions stage results into one outcome.

Stages 4b, 4c, 9b and 9c are non-blocking: on a failure they log and exit 0. Without this
their results were invisible to the run: Stage 11 never read them and Stage 10 removed the
migration tag even when a model's memory or instructions had not migrated, so a re-run
would not retry them.

Inputs are the four stage result dicts (any may be {} when the stage did not run).

Returns:
  failures            [{"kind", "obj_id", "name", "stage", "error"}]  -> failed in the report
  notes               [{"kind", "reason"}]                            -> informational only
  blocked_all_models  True when a failure affects every model (memory is one payload)
  blocked_obj_ids     obj_ids of models whose instructions failed

What is NOT a failure: Spotter not enabled (HTTP 403), a model with nothing to migrate
(empty memory / no instructions), or a model that is not in the target yet.
"""
from __future__ import annotations

import json
from pathlib import Path

MEMORY_EXPORT_FAILURES = {"api_error", "http_error"}

# The memory / instructions APIs are beta and absent on older clusters. These statuses mean
# "this cluster does not have the endpoint": nothing the user can fix per run, so the Spotter
# stages treat them like "Spotter not enabled" (a note), not a failure.
ENDPOINT_UNAVAILABLE_STATUSES = {404, 405, 501}
MEMORY_IMPORT_BAD_STATUSES = {"VALIDATION_FAILED", "FAILED"}


def load_json(path: str, default):
    p = Path(path)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except (OSError, ValueError):
            return default
    return default


def _spotter_off(reason: str) -> bool:
    """True when the stage was skipped because Spotter / its API is not available."""
    low = str(reason).lower()
    return ("spotter not enabled" in low or "not available on this cluster" in low
            or reason in ("spotter_not_enabled", "endpoint_unavailable"))


def spotter_outcome(
    memory_export: dict | None,
    memory_import: dict | None,
    instructions_export: dict | None,
    instructions_import: dict | None,
) -> dict:
    memory_export = memory_export or {}
    memory_import = memory_import or {}
    instructions_export = instructions_export or {}
    instructions_import = instructions_import or {}

    failures: list[dict] = []
    notes: list[dict] = []
    blocked_obj_ids: set[str] = set()
    blocked_all_models = False

    def fail(kind, stage, name, error, obj_id=""):
        failures.append({"kind": kind, "obj_id": obj_id, "name": name,
                         "stage": stage, "error": error})

    # ---- memory (one payload for all models) ------------------------------------------
    if memory_export.get("skipped") and memory_export.get("reason") in MEMORY_EXPORT_FAILURES:
        detail = memory_export.get("error") or memory_export.get("http_status") or ""
        fail("memory", "spotter_memory_export", "Spotter memory (all models)",
             f"Memory export failed ({memory_export['reason']}: {detail}).")
        blocked_all_models = True
    elif memory_export.get("skipped") and _spotter_off(memory_export.get("reason", "")):
        notes.append({"kind": "memory",
                      "reason": "Spotter not enabled or its API is unavailable; memory not exported."})

    if memory_import.get("skipped"):
        if _spotter_off(memory_import.get("reason", "")):
            notes.append({"kind": "memory", "reason": "Spotter not enabled or its API is unavailable "
                                                       "on the target; memory not imported."})
    else:
        problems = memory_import.get("validation_failures") or []
        status = memory_import.get("validation_status", "")
        applied_ok = memory_import.get("applied") is True
        live = memory_import.get("dry_run") is False
        if problems or status in MEMORY_IMPORT_BAD_STATUSES:
            first = problems[0].get("message") if problems and isinstance(problems[0], dict) else ""
            fail("memory", "spotter_memory_import", "Spotter memory (all models)",
                 f"Memory payload failed validation ({len(problems)} issue(s)"
                 f"{': ' + first if first else ''}).")
            blocked_all_models = True
        elif live and not applied_ok and memory_import:
            fail("memory", "spotter_memory_import", "Spotter memory (all models)",
                 f"Memory import was not applied (status {memory_import.get('apply_status')}).")
            blocked_all_models = True

    # ---- NL instructions (per model) --------------------------------------------------
    for item in instructions_export.get("failed") or []:
        oid = item.get("obj_id", "")
        fail("instructions", "spotter_instructions_export", item.get("name") or oid,
             f"Instructions export failed: {item.get('error', 'unknown error')}.", oid)
        if oid:
            blocked_obj_ids.add(oid)
    if instructions_export.get("skipped") and _spotter_off(instructions_export.get("reason", "")):
        notes.append({"kind": "instructions", "reason": "Spotter not enabled or its API is "
                                                        "unavailable; instructions not exported."})

    if instructions_import.get("skipped"):
        if _spotter_off(instructions_import.get("reason", "")):
            notes.append({"kind": "instructions", "reason": "Spotter not enabled or its API is "
                                                             "unavailable on the target; "
                                                             "instructions not imported."})
    for item in instructions_import.get("results") or []:
        if item.get("status") == "failed":
            oid = item.get("obj_id", "")
            fail("instructions", "spotter_instructions_import", item.get("name") or oid,
                 f"Setting instructions failed (HTTP {item.get('http_status', '?')}): "
                 f"{item.get('error', '')}".strip(), oid)
            if oid:
                blocked_obj_ids.add(oid)

    return {"failures": failures, "notes": notes,
            "blocked_all_models": blocked_all_models, "blocked_obj_ids": blocked_obj_ids}
