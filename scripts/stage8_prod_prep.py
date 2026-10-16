#!/usr/bin/env python3
"""
Stage 8: Production preparation.

1. Sync variables to target org (create missing definitions; set org values only when
   target_value is provided in config — if blank, the variable is assumed to be already
   configured in ThoughtSpot).
2. Connection preflight: verify each connection name found in table TML exists in target.
3. Validate-only import to catch per-object issues before the real import.

Never reads source variable values. Only writes target_value from config when provided.
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
from lib.tml_utils import parse_tml


def _http_status(exc: Exception) -> int | None:
    """HTTP status carried by an ApiError or a requests HTTPError, if any."""
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    return status


def sync_variables(target_client: TSClient, cfg: dict, var_sync_set: set[str]) -> list[dict]:
    """
    Ensure variable definitions exist in the target; set org-scoped values when provided.
    If target_value is blank, the variable must already exist in ThoughtSpot.
    Returns list of issues.
    """
    issues = []
    variables_cfg = cfg.get("variables") or {}

    # Build map from ts_variable_name -> config entry
    var_map: dict[str, dict] = {}
    for field in ("database", "schema"):
        v = variables_cfg.get(field) or {}
        name = v.get("ts_variable_name")
        if name:
            var_map[name] = {"type": "TABLE_MAPPING", **v}
    for extra in variables_cfg.get("extra") or []:
        name = extra.get("ts_variable_name")
        if name:
            var_map[name] = extra

    # Add ts_var() references found in TML
    for name in var_sync_set:
        if name not in var_map:
            var_map[name] = {"ts_variable_name": name, "type": "FORMULA_VARIABLE"}

    # Check variable API availability. Whatever goes wrong here is recorded as an issue and
    # variable sync is skipped: the connection check and validation must still run.
    try:
        target_client.search_variables()
    except Exception as e:
        status = _http_status(e)
        if status == 403 or status in ENDPOINT_UNAVAILABLE_STATUSES or "not enabled" in str(e).lower():
            issues.append({
                "severity": "WARNING",
                "message": "Variable API not available on target cluster (or not permitted). "
                           "It needs 26.4.0.cl+ and the variable-management permission. "
                           "Variable sync skipped.",
            })
            print("[WARN] Variable API not available — skipping variable sync.", file=sys.stderr)
        else:
            issues.append({
                "severity": "ERROR",
                "message": f"Could not read variables from the target (HTTP {status or 'n/a'}): "
                           f"{e}. Variable sync skipped.",
            })
            print(f"[WARN] Variable lookup failed — skipping variable sync: {e}", file=sys.stderr)
        return issues

    for name, v in var_map.items():
        var_type = v.get("type", "TABLE_MAPPING")
        target_value = v.get("target_value")
        is_sensitive = v.get("is_sensitive", False)

        # Check if variable exists
        try:
            existing = target_client.search_variables(name_pattern=name)
        except Exception as e:
            issues.append({"severity": "ERROR", "variable": name,
                           "message": f"Could not look up variable '{name}' in the target: {e}"})
            continue
        found = next((x for x in existing if x.get("name") == name), None)

        if found:
            found_type = found.get("variable_type") or found.get("type", "")
            if found_type and found_type != var_type:
                issues.append({
                    "severity": "ERROR",
                    "variable": name,
                    "message": f"Variable '{name}' exists in target with type '{found_type}', "
                               f"config expects '{var_type}'. Cannot proceed with this variable.",
                })
                continue
            if not target_value and var_type == "TABLE_MAPPING":
                # Variable exists and target_value not in config — assume ThoughtSpot manages it.
                print(f"[VARS] '{name}' exists in ThoughtSpot with values assigned — skipping set.")
        else:
            if not target_value and var_type == "TABLE_MAPPING":
                # Variable doesn't exist and no target_value to set — can't create it safely.
                issues.append({
                    "severity": "ERROR",
                    "variable": name,
                    "message": f"TABLE_MAPPING variable '{name}' not found in target org and "
                               "no target_value is configured. Either create the variable in "
                               "ThoughtSpot or add target_value to config.",
                })
                continue
            try:
                target_client.create_variable(name, var_type, is_sensitive=is_sensitive)
                print(f"[VARS] Created variable '{name}' ({var_type}) in target.")
            except Exception as e:
                issues.append({"severity": "ERROR", "variable": name,
                                "message": f"Failed to create variable '{name}': {e}"})
                continue

        # Set org-scoped value only when target_value is explicitly provided in config.
        if target_value and var_type in ("TABLE_MAPPING", "CONNECTION_PROPERTY"):
            try:
                target_client.set_variable_org_value(name, target_value)
                print(f"[VARS] Set org value for variable '{name}'.")
            except Exception as e:
                issues.append({"severity": "ERROR", "variable": name,
                                "message": f"Failed to set value for '{name}': {e}"})
        elif var_type == "FORMULA_VARIABLE" and not target_value:
            issues.append({
                "severity": "INFO",
                "variable": name,
                "message": f"FORMULA_VARIABLE '{name}' has no target_value — "
                           "values are assigned per-user at login (ABAC). Definition created only.",
            })

    return issues


def connection_preflight(target_client: TSClient, connection_names: set[str]) -> list[dict]:
    issues = []
    for name in connection_names:
        try:
            conns = target_client.search_connections(name=name)
            exact = [c for c in conns if c.get("name") == name]
            if not exact:
                issues.append({
                    "severity": "ERROR",
                    "connection": name,
                    "message": f'Connection "{name}" missing in target org.',
                })
                print(f"[PREFLIGHT] FAIL: connection '{name}' not found in target.",
                      file=sys.stderr)
            else:
                print(f"[PREFLIGHT] OK: connection '{name}' found in target.")
        except Exception as e:
            issues.append({"severity": "ERROR", "connection": name,
                           "message": f"Error checking connection '{name}': {e}"})
    return issues


def validate_import(target_client: TSClient, tml_strings: list[str]) -> list[dict]:
    """Run VALIDATE_ONLY import; return per-object validation issues."""
    try:
        result = target_client.import_tml(tml_strings, import_policy="VALIDATE_ONLY")
        objects = result if isinstance(result, list) else result.get("object", [])
        issues = []
        for obj in objects:
            status = obj.get("response", {}).get("status", {}).get("status_code", "")
            if status not in ("OK", "WARNING"):
                issues.append({
                    "severity": "VALIDATION_FAILED",
                    "obj_id": obj.get("response", {}).get("header", {}).get("obj_id_ref", ""),
                    "message": str(obj.get("response", {}).get("status", "")),
                })
        return issues
    except Exception as e:
        return [{"severity": "ERROR", "message": f"Validate-only import failed: {e}"}]


def format_issue(issue: dict, max_len: int = 300) -> str:
    """One log line per issue: severity, what it is about, and the message."""
    subject = (issue.get("variable") or issue.get("connection")
               or issue.get("obj_id") or "")
    msg = str(issue.get("message", "")).replace("\n", " ")
    if len(msg) > max_len:
        msg = msg[:max_len] + "..."
    prefix = f"[STAGE 8] {issue.get('severity', 'UNKNOWN')}"
    return f"{prefix} [{subject}]: {msg}" if subject else f"{prefix}: {msg}"


def main():
    org_key = os.environ["ORG_KEY"]
    cfg_path = os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{org_key}_config.json")
    sanitize_path = os.environ.get("SANITIZE_OUT",
                                   f"/tmp/ts_migration_{org_key}_sanitize_results.json")
    export_path = os.environ.get("EXPORT_OUT", f"/tmp/ts_migration_{org_key}_export_results.json")
    repo_root = Path(os.environ.get("REPO_ROOT", "."))

    cfg = json.loads(Path(cfg_path).read_text())
    sanitize_data = json.loads(Path(sanitize_path).read_text())
    export_results = json.loads(Path(export_path).read_text())

    var_sync_set = set(sanitize_data.get("var_sync_set", []))
    connection_names = set(sanitize_data.get("connection_names", []))

    tgt = cfg["target"]
    target_client = TSClient(
        tgt["base_url"], tgt["org_id"],
        resolve_secret("TS_TARGET_USERNAME"),
        resolve_secret("TS_TARGET_SECRET_KEY"),
    )
    target_client.authenticate()

    var_issues = sync_variables(target_client, cfg, var_sync_set)
    conn_issues = connection_preflight(target_client, connection_names)

    # Collect TML strings for validate-only
    tml_strings = []
    for res in export_results:
        if res.get("status") == "success" and res.get("tml_path"):
            tml_path = repo_root / res["tml_path"]
            if tml_path.exists():
                tml_strings.append(tml_path.read_text(encoding="utf-8"))

    validation_issues = []
    if tml_strings:
        validation_issues = validate_import(target_client, tml_strings)

    all_issues = var_issues + conn_issues + validation_issues
    out_path = os.environ.get("PROD_PREP_OUT",
                              f"/tmp/ts_migration_{org_key}_prod_prep.json")
    Path(out_path).write_text(json.dumps(all_issues, indent=2), encoding="utf-8")

    fatal = [i for i in all_issues if i.get("severity") in ("FATAL", "ERROR")]
    print(f"[STAGE 8] Prod prep complete. {len(fatal)} errors, "
          f"{len(all_issues) - len(fatal)} warnings/info.")
    for issue in all_issues:
        loud = issue.get("severity") != "INFO"
        print(format_issue(issue), file=sys.stderr if loud else sys.stdout)
    if fatal:
        # Stage 8 only records issues in prod_prep.json. Stage 9 does not read it, so
        # nothing is excluded here; Stage 11 reports only connection-related errors.
        print(f"[WARN] {len(fatal)} error(s) recorded in {Path(out_path).name}. Stage 8 does "
              "not exclude objects or block the import; Stage 11 reports only errors that "
              "name a connection as failures.", file=sys.stderr)


if __name__ == "__main__":
    main()
