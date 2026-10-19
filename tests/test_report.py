"""Tests for RunReport: all report files are generated, no secrets leak."""
import json
import os
import tempfile
from pathlib import Path
import pytest

from scripts.lib.report import RunReport, ObjectRecord


@pytest.fixture
def sample_run_report():
    report = RunReport(
        org_key="sales",
        run_id="12345-1",
        mode="LIVE",
        source_url="https://dev.example.com",
        source_org_id=12,
        target_url="https://prod.example.com",
        target_org_id=3,
        migration_tag="sales_readyformigration",
    )
    report.add_pulled(ObjectRecord(
        obj_type="tables", name="Sales Fact", obj_id="obj_001",
        source_guid="guid-1", reason="tagged", other_tags=["finance"],
    ))
    report.mark_success(ObjectRecord(
        obj_type="tables", name="Sales Fact", obj_id="obj_001",
        source_guid="guid-1", reason="tagged", other_tags=[],
        stage="import",
    ))
    report.mark_failed(ObjectRecord(
        obj_type="liveboards", name="Broken LB", obj_id="obj_002",
        source_guid="guid-2", reason="tagged", other_tags=[],
        stage="import", status="failed",
        error="Connection not found", http_status=404,
    ))
    return report


class TestRunReport:
    def test_writes_all_report_files(self, sample_run_report):
        with tempfile.TemporaryDirectory() as tmp:
            sample_run_report.write_all(Path(tmp))
            files = os.listdir(tmp)
        assert "migration_summary.txt" in files
        assert "failures_report.txt" in files
        assert "failures.json" in files
        assert "manifest.json" in files

    def test_failures_json_is_valid(self, sample_run_report):
        with tempfile.TemporaryDirectory() as tmp:
            sample_run_report.write_all(Path(tmp))
            with open(os.path.join(tmp, "failures.json")) as f:
                data = json.load(f)
        assert len(data) == 1
        assert data[0]["obj_id"] == "obj_002"

    def test_no_secrets_in_summary(self, sample_run_report):
        with tempfile.TemporaryDirectory() as tmp:
            sample_run_report.write_all(Path(tmp))
            with open(os.path.join(tmp, "migration_summary.txt")) as f:
                content = f.read()
        # No token-like strings should appear
        assert "secret_key" not in content.lower()
        assert "Bearer" not in content

    def test_summary_has_required_sections(self, sample_run_report):
        with tempfile.TemporaryDirectory() as tmp:
            sample_run_report.write_all(Path(tmp))
            with open(os.path.join(tmp, "migration_summary.txt")) as f:
                content = f.read()
        for section in ["PULLED", "SUCCEEDED", "FAILED", "SKIPPED",
                        "POST-MIGRATION CHECK", "TAG CLEANUP", "UNSUPPORTED"]:
            assert section in content
