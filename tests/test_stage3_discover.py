"""Tests for Stage 3's small helpers: response unwrapping, folder choice and tag extraction.

Dependency handling and the objects-without-obj_id report are covered by
test_stage3_dependencies.py and test_discovery_failures_reach_report.py.
"""
import pytest

from scripts.stage3_discover import _extract_tags, _folder_for_object, _unwrap_response


class TestUnwrapResponse:
    def test_list_is_returned_as_is(self):
        assert _unwrap_response([{"a": 1}]) == [{"a": 1}]

    @pytest.mark.parametrize("key", ["data", "results", "objects", "metadata"])
    def test_known_wrapper_keys(self, key):
        assert _unwrap_response({key: [{"a": 1}]}) == [{"a": 1}]

    def test_unknown_shape_is_empty(self):
        assert _unwrap_response({"something": [1]}) == []
        assert _unwrap_response(None) == []


class TestFolderForObject:
    @pytest.mark.parametrize("meta_type,folder", [
        ("ANSWER", "answers"), ("LIVEBOARD", "liveboards"),
        ("COLLECTION", "collections"), ("SOMETHING_ELSE", "misc"),
    ])
    def test_non_table_types(self, meta_type, folder):
        assert _folder_for_object(meta_type, {}) == folder

    def test_table_without_subtype_defaults_to_tables(self):
        assert _folder_for_object("LOGICAL_TABLE", {}) == "tables"


class TestExtractTags:
    def test_top_level_tag_objects(self):
        obj = {"tags": [{"name": "a"}, {"name": "b"}]}
        assert _extract_tags(obj) == ["a", "b"]

    def test_tags_nested_under_header_and_plain_strings(self):
        assert _extract_tags({"metadata_header": {"tags": ["x", {"name": "y"}]}}) == ["x", "y"]

    def test_no_tags(self):
        assert _extract_tags({}) == []
