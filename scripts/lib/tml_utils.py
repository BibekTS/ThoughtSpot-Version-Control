"""
TML parsing, sanitization, and Path B substitution utilities.

TML files are exported as JSON strings. This module:
  - Parses TML JSON
  - Removes guid / fqn fields before commit
  - Removes the migration tag from the tags list
  - Detects literal db/schema values (Path B)
  - Substitutes source -> target db/schema values in memory (Path B, never persisted)
  - Scans for ts_var() references in RLS rules
  - Produces deterministic JSON for meaningful git diffs
"""
from __future__ import annotations

import json
import re
from typing import Any

# ThoughtSpot writes a variable reference without quotes, e.g. ts_var(country_var) or
# ts_var (ts_user_timezone); quotes are accepted too. Names are word characters.
_TS_VAR_RE = re.compile(r"\bts_var\s*\(\s*['\"]?(\w+)['\"]?\s*\)")

# Built-in variables that exist everywhere and must not be created by the pipeline.
_SYSTEM_VARIABLES = {"ts_user_timezone"}
_GUID_RE = re.compile(
    r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
    re.IGNORECASE,
)

# Fields to strip from TML before committing to git
_STRIP_KEYS = {"guid", "fqn"}


def parse_tml(tml_string: str) -> dict:
    return json.loads(tml_string)


def to_tml_string(tml_dict: dict) -> str:
    """Deterministic JSON serialization for stable git diffs."""
    return json.dumps(tml_dict, indent=2, sort_keys=True, ensure_ascii=False)


def sanitize(tml_dict: dict, migration_tag: str) -> dict:
    """
    Return a sanitized copy of tml_dict:
      - GUIDs / FQNs stripped
      - migration_tag removed from tags list
    """
    result = _deep_strip_keys(tml_dict, _STRIP_KEYS)
    result = _remove_tag(result, migration_tag)
    return result


def _deep_strip_keys(obj: Any, keys: set) -> Any:
    if isinstance(obj, dict):
        return {k: _deep_strip_keys(v, keys) for k, v in obj.items() if k not in keys}
    if isinstance(obj, list):
        return [_deep_strip_keys(item, keys) for item in obj]
    return obj


def _remove_tag(tml_dict: dict, tag_name: str) -> dict:
    """Return a copy of tml_dict with `tag_name` removed from every `tags` list, at any depth."""
    import copy
    result = copy.deepcopy(tml_dict)
    _patch_tags(result, tag_name)
    return result


def _patch_tags(obj: Any, tag_name: str) -> None:
    if isinstance(obj, dict):
        if "tags" in obj and isinstance(obj["tags"], list):
            obj["tags"] = [t for t in obj["tags"] if t != tag_name]
        for v in obj.values():
            _patch_tags(v, tag_name)
    elif isinstance(obj, list):
        for item in obj:
            _patch_tags(item, tag_name)


def get_table_db_schema(tml_dict: dict) -> tuple[str | None, str | None]:
    """
    Extract (db, schema) from a LOGICAL_TABLE TML.
    Returns (None, None) if not present.
    """
    table = tml_dict.get("table", {})
    return table.get("db"), table.get("schema")


def is_variable_bound(value: str | None) -> bool:
    """
    Heuristic: a value is variable-bound if it looks like a variable reference.
    ThoughtSpot may use ${varname} or another pattern depending on Path A.
    This is a best-effort check; the canonical check is the parameterize API.
    TODO(verify): confirm variable reference format once Path A/B is resolved.
    """
    if value is None:
        return False
    return value.startswith("${") and value.endswith("}")


def path_b_substitute(tml_dict: dict, source_db: str, target_db: str,
                      source_schema: str, target_schema: str) -> dict:
    """
    Path B only: replace literal source db/schema with target values in memory.
    The result is passed directly to the import API and is NEVER committed.
    """
    import copy
    result = copy.deepcopy(tml_dict)
    _substitute_in_table(result, source_db, target_db, source_schema, target_schema)
    return result


def _substitute_in_table(obj: Any, src_db: str, tgt_db: str,
                         src_schema: str, tgt_schema: str) -> None:
    if isinstance(obj, dict):
        if "db" in obj and obj["db"] == src_db:
            obj["db"] = tgt_db
        if "schema" in obj and obj["schema"] == src_schema:
            obj["schema"] = tgt_schema
        for v in obj.values():
            _substitute_in_table(v, src_db, tgt_db, src_schema, tgt_schema)
    elif isinstance(obj, list):
        for item in obj:
            _substitute_in_table(item, src_db, tgt_db, src_schema, tgt_schema)


def scan_ts_var_references(tml_dict: dict) -> set[str]:
    """
    Find all ts_var(name) references in the TML (RLS rules, formulas).
    Returns the set of variable names, without built-in system variables.

    Scans the string values directly: serializing the whole TML to JSON first escapes
    double quotes and would hide references written with them.
    """
    found: set[str] = set()

    def walk(node: Any) -> None:
        if isinstance(node, str):
            found.update(_TS_VAR_RE.findall(node))
        elif isinstance(node, dict):
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(tml_dict)
    return found - _SYSTEM_VARIABLES


# Keys whose nested obj_ids are not dependencies.
_NON_DEPENDENCY_KEYS = {"connection", "views"}


def extract_refs(tml_dict: dict) -> list[dict]:
    """
    Objects this TML depends on, found by their obj_id.

    Requires the TML to be exported with include_obj_id_ref=True. In real exports a
    reference is a nested dict that carries an `obj_id`, for example:
      answer.tables[]                              (what an answer is built on)
      liveboard.visualizations[].answer.tables[]   (what a liveboard is built on)
      model.model_tables[]                         (the tables of a model)
      table.joins_with[].destination               (a joined table)

    Not returned: the object's own obj_id, connections (`connection`), which are not
    migrated (Stage 8 checks that they exist in the target), and a liveboard's saved views
    (`liveboard.views[]`), which carry their own obj_id but are part of the liveboard.
    Returns [{"obj_id", "name"}], de-duplicated, in first-seen order.
    """
    own = tml_dict.get("obj_id")
    refs: list[dict] = []
    seen: set[str] = set()

    def walk(node: Any, parent_key: str | None) -> None:
        if isinstance(node, dict):
            if parent_key in _NON_DEPENDENCY_KEYS:
                return
            oid = node.get("obj_id")
            if parent_key is not None and isinstance(oid, str) and oid and oid != own \
                    and oid not in seen:
                seen.add(oid)
                refs.append({"obj_id": oid, "name": node.get("name", "") or ""})
            for key, value in node.items():
                walk(value, key)
        elif isinstance(node, list):
            for item in node:
                walk(item, parent_key)

    walk(tml_dict, None)
    return refs


def extract_connection_name(tml_dict: dict) -> str | None:
    """
    Extract the connection name from a LOGICAL_TABLE TML.
    Tables reference their connection by name in the TML.
    """
    table = tml_dict.get("table", {})
    db_table = table.get("db_table", {})
    if isinstance(db_table, dict):
        connection = db_table.get("connection", {})
        return connection.get("name")
    # Fallback: check top-level
    connection = table.get("connection", {})
    if isinstance(connection, dict):
        return connection.get("name")
    return None


def slug_from_name(name: str) -> str:
    """Convert an object name to a filesystem-safe slug."""
    import unicodedata
    normalized = unicodedata.normalize("NFKD", name)
    ascii_str = normalized.encode("ascii", "ignore").decode()
    slug = re.sub(r"[^\w\-]", "_", ascii_str).strip("_").lower()
    return slug[:80] or "unnamed"
