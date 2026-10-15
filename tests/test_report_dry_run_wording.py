"""A dry run imports nothing; its report must not read like objects went missing."""
from scripts.stage11_report import build_report

CFG = {"org_key": "o", "display_name": "O", "migration_tag": "t",
       "source": {"base_url": "s", "org_id": 1}, "target": {"base_url": "t", "org_id": 2}}
MANIFEST = [{"obj_id": "A-1", "name": "A", "type": "LOGICAL_TABLE", "reason": "tagged"}]


def _build(dry_run):
    return build_report(CFG, "run", dry_run, MANIFEST, [], [], {}, [])


def test_dry_run_says_not_imported(tmp_path):
    r = _build(True)
    assert [x.reason for x in r.skipped] == ["Dry run: not imported"]
    r.write_all(tmp_path)
    text = (tmp_path / "migration_summary.txt").read_text()
    assert "Dry run: not imported" in text and "No import result found" not in text


def test_live_run_without_a_result_still_warns():
    r = _build(False)
    assert [x.reason for x in r.skipped] == ["No import result found"]
