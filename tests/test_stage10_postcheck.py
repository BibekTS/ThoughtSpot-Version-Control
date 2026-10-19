"""Tests for Stage 10: the post-check against the target and the tag cleanup.

Tags kept for Spotter failures are covered in test_spotter_in_run_outcome.py.
"""
from unittest.mock import MagicMock

from scripts import stage10_postcheck as s10


def _client(by_type):
    """A target whose /metadata/search returns by_type[<type>] as one page."""
    client = MagicMock()

    def post(path, body):
        resp = MagicMock(status_code=200)
        resp.json.return_value = by_type.get(body["metadata"][0]["type"], [])
        return resp

    client.post.side_effect = post
    return client


def _entry(obj_id, typ="LIVEBOARD", reason="tagged", guid=None):
    return {"obj_id": obj_id, "name": obj_id.title(), "type": typ, "reason": reason,
            "source_guid": guid or f"g-{obj_id}"}


def _ok(*ids):
    return [{"obj_id": i, "status": "success"} for i in ids]


class TestPostCheck:
    def test_object_found_by_obj_id_is_confirmed(self):
        client = _client({"LIVEBOARD": [{"metadata_obj_id": "a", "metadata_name": "A"}]})
        out = s10.post_check(client, [_entry("a")], _ok("a"))
        assert out["mismatches"] == []
        assert out["by_type"]["LIVEBOARD"] == {"manifest": 1, "confirmed": 1}

    def test_object_found_only_by_name_is_still_confirmed(self):
        client = _client({"LIVEBOARD": [{"metadata_obj_id": "", "metadata_name": "A"}]})
        out = s10.post_check(client, [_entry("a")], _ok("a"))
        assert out["mismatches"] == []

    def test_object_absent_from_the_target_is_a_mismatch(self):
        client = _client({"LIVEBOARD": []})
        out = s10.post_check(client, [_entry("a")], _ok("a"))
        assert [m["obj_id"] for m in out["mismatches"]] == ["a"]
        assert out["by_type"]["LIVEBOARD"]["confirmed"] == 0

    def test_objects_that_did_not_import_are_not_checked(self):
        client = _client({})
        out = s10.post_check(client, [_entry("a")], [{"obj_id": "a", "status": "failed"}])
        assert out["mismatches"] == [] and client.post.call_count == 0

    def test_the_target_is_scanned_once_per_type(self):
        client = _client({"LIVEBOARD": [{"metadata_obj_id": "a", "metadata_name": "A"},
                                        {"metadata_obj_id": "b", "metadata_name": "B"}]})
        s10.post_check(client, [_entry("a"), _entry("b")], _ok("a", "b"))
        assert client.post.call_count == 1

    def test_a_search_error_is_a_mismatch_not_a_crash(self):
        client = MagicMock()
        client.post.return_value = MagicMock(status_code=500)
        out = s10.post_check(client, [_entry("a")], _ok("a"))
        assert "Error during post-check" in out["mismatches"][0]["issue"]


class TestCleanupTags:
    def _removed(self, src):
        return sorted(m["identifier"] for m in src.unassign_tags.call_args.args[0])

    def test_removes_the_tag_only_from_tagged_successes(self):
        src = MagicMock()
        manifest = [_entry("a"), _entry("dep", reason="dependency_of:a"), _entry("bad")]
        imported = _ok("a", "dep") + [{"obj_id": "bad", "status": "failed"}]
        out = s10.cleanup_tags(src, manifest, imported, [], "tag")
        assert self._removed(src) == ["g-a"]
        assert out["removed"] == 1 and out["retained"] == 1

    def test_post_check_mismatches_keep_their_tag(self):
        src = MagicMock()
        out = s10.cleanup_tags(src, [_entry("a"), _entry("b")], _ok("a", "b"),
                               [{"obj_id": "b"}], "tag")
        assert self._removed(src) == ["g-a"] and out["retained"] == 1

    def test_nothing_to_remove_makes_no_api_call(self):
        src = MagicMock()
        out = s10.cleanup_tags(src, [_entry("a")], [{"obj_id": "a", "status": "failed"}], [], "tag")
        src.unassign_tags.assert_not_called()
        assert out["removed"] == 0

    def test_a_failed_untag_call_is_reported_not_raised(self):
        src = MagicMock()
        src.unassign_tags.side_effect = RuntimeError("boom")
        out = s10.cleanup_tags(src, [_entry("a")], _ok("a"), [], "tag")
        assert out["removed"] == 0
