"""Helpers for reading Stage 8's prod_prep.json (the list of issues it found)."""
from __future__ import annotations

ALLOWED_VALIDATION_GATES = ("block", "warn")
DEFAULT_VALIDATION_GATE = "block"


def blocked_objects(issues: list[dict]) -> dict[str, str]:
    """obj_id -> message for every object Stage 8's validate-only import rejected.

    Only failures attributed to an object count. A validate-only call that failed as a whole,
    or a variable/connection error, names no object, so it blocks nothing here (Stage 11
    still reports it).
    """
    return {
        i["obj_id"]: str(i.get("message", ""))
        for i in issues
        if i.get("severity") == "VALIDATION_FAILED" and i.get("obj_id")
    }
