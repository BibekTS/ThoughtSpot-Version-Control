"""
Locate timestamped export artifacts (memory, NL instructions) written by the
extract job so the import job reads the file from the SAME run.

Exports are written as <stem>__<TIMESTAMP>.<suffix> (e.g. memory__20260924T190200Z.yaml).
Timestamps are UTC and sortable, so lexicographic order == chronological order.
"""
from __future__ import annotations

from pathlib import Path

import yaml


def resolve_export_file(
    directory: Path,
    stem: str,
    suffix: str,
    timestamp: str = "",
) -> Path | None:
    """
    Return the export file to import, or None if there is nothing to import.

    - TIMESTAMP set (pipeline run): only <stem>__<timestamp><suffix> qualifies. If
      this run's export was skipped (empty/403/error) we must NOT fall back to an
      older run's file, or stale content would overwrite the target.
    - TIMESTAMP empty (local/manual use): newest <stem>__*<suffix>, then the legacy
      un-timestamped <stem><suffix>.
    """
    if timestamp:
        exact = directory / f"{stem}__{timestamp}{suffix}"
        return exact if exact.exists() else None

    candidates = sorted(directory.glob(f"{stem}__*{suffix}"))
    if candidates:
        return candidates[-1]
    legacy = directory / f"{stem}{suffix}"
    return legacy if legacy.exists() else None


def memory_is_empty(memory_yaml: str) -> bool:
    """
    True when a memory export holds no memories, e.g. the API's "memories: []\\n".

    Importing such a payload would REPLACE (wipe) the target model's memory, so
    callers must treat it as "nothing to migrate". Unparseable content is NOT
    treated as empty; the import API's own validation decides what to do with it.
    """
    if not memory_yaml or not memory_yaml.strip():
        return True
    try:
        data = yaml.safe_load(memory_yaml)
    except yaml.YAMLError:
        return False
    if data is None:
        return True
    if isinstance(data, dict):
        return not data.get("memories")
    return False
