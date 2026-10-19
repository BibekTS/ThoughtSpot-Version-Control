"""Tests for Stage 11: report generation and exit code."""
import json
import os
import tempfile
from pathlib import Path
import pytest

from scripts.lib.report import RunReport, ObjectRecord


def make_report(failures=0, succeeded=1):
    report = RunReport(
        org_key="sales", run_id="999",
        mode="LIVE", source_url="https://dev.ex.com", source_org_id=12,
        target_url="https://prod.ex.com", target_org_id=3,
        migration_tag="sales_readyformigration",
    )
    for i in range(succeeded):
        report.mark_success(ObjectRecord(
            obj_type="tables", name=f"T{i}", obj_id=f"obj_{i:03d}",
            source_guid=f"g{i}", reason="tagged", other_tags=[],
            stage="import",
        ))
    for i in range(failures):
        report.mark_failed(ObjectRecord(
            obj_type="liveboards", name=f"LB_fail_{i}", obj_id=f"obj_fail_{i:03d}",
            source_guid=f"gf{i}", reason="tagged", other_tags=[],
            stage="import", status="failed", error="Timeout",
        ))
    return report


class TestReportExitCodes:
    def test_no_failures_produces_no_failure_entries(self):
        report = make_report(failures=0, succeeded=2)
        with tempfile.TemporaryDirectory() as tmp:
            report.write_all(Path(tmp))
            with open(os.path.join(tmp, "failures.json")) as f:
                data = json.load(f)
        assert data == []

    def test_failures_present_in_json(self):
        report = make_report(failures=2, succeeded=1)
        with tempfile.TemporaryDirectory() as tmp:
            report.write_all(Path(tmp))
            with open(os.path.join(tmp, "failures.json")) as f:
                data = json.load(f)
        assert len(data) == 2

    def test_dry_run_labeled_in_summary(self):
        report = RunReport(
            org_key="sales", run_id="1",
            mode="DRY-RUN", source_url="u", source_org_id=1,
            target_url="u2", target_org_id=2, migration_tag="t",
        )
        with tempfile.TemporaryDirectory() as tmp:
            report.write_all(Path(tmp))
            with open(os.path.join(tmp, "migration_summary.txt")) as f:
                content = f.read()
        assert "DRY-RUN" in content
