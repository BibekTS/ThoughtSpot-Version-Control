#!/usr/bin/env python3
"""
Stage 11: Build and write final reports.

Runs with if: always() in the workflow so reports are produced
even when earlier stages fail.

Writes to reports/<org_key>/<UTC-timestamp>_<run_id>/:
  migration_summary.txt
  failures_report.txt
  failures.json
  manifest.json

Also commits the reports to ts-dev (with [skip ci]).
Writes the GitHub job summary.
"""
from __future__ import annotations

import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
from lib.report import RunReport, ObjectRecord


def load_json(path: str, default):
    p = Path(path)
    if p.exists():
        return json.loads(p.read_text())
    return default


def build_report(
    cfg: dict,
    run_id: str,
    dry_run: bool,
    manifest: list[dict],
    export_results: list[dict],
    import_results: list[dict],
    postcheck_data: dict,
    prod_prep_data: list[dict],
) -> RunReport:
    src = cfg["source"]
    tgt = cfg["target"]
    report = RunReport(
        org_key=cfg["org_key"],
        run_id=run_id,
        mode="DRY-RUN" if dry_run else "LIVE",
        source_url=src["base_url"],
        source_org_id=src["org_id"],
        target_url=tgt["base_url"],
        target_org_id=tgt["org_id"],
        migration_tag=cfg["migration_tag"],
    )

    export_map = {r["obj_id"]: r for r in export_results}
    import_map = {r["obj_id"]: r for r in import_results}

    for entry in manifest:
        obj_id = entry["obj_id"]
        name = entry["name"]
        obj_type = entry["type"]
        reason = entry.get("reason", "tagged")
        other_tags = entry.get("other_tags", [])

        exp = export_map.get(obj_id, {})
        imp = import_map.get(obj_id, {})
        tml_path = exp.get("tml_path", "")

        rec = ObjectRecord(
            obj_type=obj_type, name=name, obj_id=obj_id,
            source_guid=entry.get("source_guid", ""),
            reason=reason, other_tags=other_tags,
            tml_path=tml_path,
        )
        report.add_pulled(rec)

        if exp.get("status") == "failed":
            rec.stage = "export"
            rec.error = exp.get("error", "Export failed")
            rec.suggested_action = "Re-tag after fixing the export issue."
            report.mark_failed(rec)
        elif imp.get("status") == "failed":
            rec.stage = "import"
            rec.error = imp.get("error", "Import failed")
            rec.suggested_action = "Re-tag after resolving the import error."
            report.mark_failed(rec)
        elif imp.get("status") == "skipped":
            rec.reason = imp.get("reason", "skipped")
            report.mark_skipped(rec)
        elif imp.get("status") == "success":
            report.mark_success(rec)
        else:
            rec.stage = "unknown"
            rec.reason = "No import result found"
            report.mark_skipped(rec)

    # Collect discover failures (objects without obj_id)
    for item in prod_prep_data:
        if item.get("severity") == "ERROR":
            conn = item.get("connection", "")
            if conn:
                rec = ObjectRecord(
                    obj_type="CONNECTION", name=conn, obj_id="",
                    stage="prod_prep", status="failed",
                    error=item.get("message", ""),
                    suggested_action=f'Create connection "{conn}" in target org.',
                )
                report.mark_failed(rec)

    # Post-check and tag cleanup
    report.post_check = postcheck_data.get("post_check", {})
    report.tag_cleanup = postcheck_data.get("tag_cleanup", {})

    # Unsupported
    report.add_unsupported(
        "Spotter conversation history",
        "Per-user session data, not migratable metadata. Model-level memory (Stage 4b/9b) "
        "and NL instructions (Stage 4c/9c) are migrated.",
    )

    report.finish()
    return report


def write_job_summary(report: RunReport, summary_path: str) -> None:
    lines = [
        f"## ThoughtSpot Migration — {report.org_key} ({report.mode})",
        "",
        f"Run: `{report.run_id}`",
        "",
        "| Category | Count |",
        "|---|---|",
        f"| Pulled | {len(report.pulled)} |",
        f"| Succeeded | {len(report.succeeded)} |",
        f"| Failed | {len(report.failed)} |",
        f"| Skipped | {len(report.skipped)} |",
    ]
    if summary_path:
        with open(summary_path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")


def main():
    org_key = os.environ["ORG_KEY"]
    run_id = os.environ.get("RUN_ID", "local")
    dry_run = os.environ.get("DRY_RUN", "false").lower() == "true"
    repo_root = Path(os.environ.get("REPO_ROOT", "."))

    cfg = load_json(
        os.environ.get("CONFIG_OUT", f"/tmp/ts_migration_{org_key}_config.json"), {}
    )
    manifest = load_json(
        os.environ.get("MANIFEST_OUT", f"/tmp/ts_migration_{org_key}_manifest.json"), []
    )
    export_results = load_json(
        os.environ.get("EXPORT_OUT", f"/tmp/ts_migration_{org_key}_export_results.json"), []
    )
    import_results = load_json(
        os.environ.get("IMPORT_OUT", f"/tmp/ts_migration_{org_key}_import_results.json"), []
    )
    postcheck_data = load_json(
        os.environ.get("POSTCHECK_OUT", f"/tmp/ts_migration_{org_key}_postcheck.json"), {}
    )
    prod_prep_data = load_json(
        os.environ.get("PROD_PREP_OUT", f"/tmp/ts_migration_{org_key}_prod_prep.json"), []
    )

    if not cfg:
        print("[STAGE 11] No config found — cannot produce full report.", file=sys.stderr)
        sys.exit(0)

    report = build_report(
        cfg, run_id, dry_run,
        manifest, export_results, import_results,
        postcheck_data, prod_prep_data,
    )

    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = repo_root / "reports" / org_key / f"{ts}_{run_id}"
    report.write_all(out_dir)
    print(f"[STAGE 11] Reports written to {out_dir}")

    # GitHub Actions job summary
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY", "")
    write_job_summary(report, summary_path)

    # Exit red if any object failed or was skipped for an error reason
    error_skipped = [
        r for r in report.skipped
        if "failed" in r.reason.lower() or "missing" in r.reason.lower()
    ]
    if report.failed or error_skipped:
        print(f"[STAGE 11] Run ended with {len(report.failed)} failures and "
              f"{len(error_skipped)} error-skips.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()
