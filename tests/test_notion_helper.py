import os
import sys
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, call, patch

import pendulum


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from notion_helper import NotionHelper


class FakeNotionError(Exception):
    def __init__(self, code, status):
        super().__init__(code)
        self.code = code
        self.status = status


def make_helper():
    helper = NotionHelper.__new__(NotionHelper)
    helper.client = Mock()
    helper._NotionHelper__cache = {}
    helper._data_source_id_cache = {}
    helper._data_source_schema_cache = {}
    helper._searched_block_ids = set()
    helper._heatmap_block_type = None
    helper.database_id_dict = {}
    helper.property_dict = {}
    helper.heatmap_block_id = None
    return helper


class NotionDataSourceTests(unittest.TestCase):
    def test_missing_token_setting_has_clear_error(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(Exception, "NOTION_TOKEN"):
                NotionHelper()

    def test_missing_page_setting_has_clear_error(self):
        helper = make_helper()
        with self.assertRaisesRegex(Exception, "NOTION_PAGE"):
            helper.extract_page_id(None)

    def test_resolves_legacy_database_id_once_and_caches_it(self):
        helper = make_helper()
        helper.client.request.side_effect = [
            FakeNotionError("object_not_found", 404),
            {"id": "database", "data_sources": [{"id": "source"}]},
        ]

        self.assertEqual(helper.resolve_data_source_id("database"), "source")
        self.assertEqual(helper.resolve_data_source_id("database"), "source")
        self.assertEqual(helper.resolve_data_source_id("source"), "source")
        self.assertEqual(helper.client.request.call_count, 2)

    def test_query_uses_data_source_endpoint(self):
        helper = make_helper()
        helper._data_source_id_cache["database"] = "source"
        helper.client.request.return_value = {"results": []}
        filter_value = {"property": "BookId", "rich_text": {"equals": "1"}}

        helper.query(database_id="database", filter=filter_value, page_size=100)

        helper.client.request.assert_called_once_with(
            path="data_sources/source/query",
            method="POST",
            body={"filter": filter_value, "page_size": 100},
        )

    def test_create_book_page_defaults_to_book_data_source(self):
        helper = make_helper()
        helper.book_database_id = "source"
        helper._data_source_id_cache["source"] = "source"
        helper.client.pages.create.return_value = {"id": "page"}
        icon = {"type": "emoji", "emoji": "📚"}

        helper.create_book_page(properties={"Name": {}}, icon=icon)

        helper.client.pages.create.assert_called_once_with(
            parent={"type": "data_source_id", "data_source_id": "source"},
            properties={"Name": {}},
            icon=icon,
            cover=icon,
        )

    def test_block_children_are_paginated(self):
        helper = make_helper()
        helper.client.blocks.children.list.side_effect = [
            {"results": [{"id": "one"}], "has_more": True, "next_cursor": "c"},
            {"results": [{"id": "two"}], "has_more": False, "next_cursor": None},
        ]

        self.assertEqual(
            helper.get_block_children("root"), [{"id": "one"}, {"id": "two"}]
        )
        self.assertEqual(
            helper.client.blocks.children.list.call_args_list,
            [
                call(block_id="root", page_size=100),
                call(block_id="root", page_size=100, start_cursor="c"),
            ],
        )

    def test_recursive_scan_finds_database_and_raw_heatmap_image(self):
        helper = make_helper()
        helper.get_block_children = Mock(
            side_effect=lambda block_id: {
                "root": [
                    {
                        "id": "nested",
                        "type": "paragraph",
                        "has_children": True,
                    }
                ],
                "nested": [
                    {
                        "id": "database",
                        "type": "child_database",
                        "child_database": {"title": "书架"},
                        "has_children": False,
                    },
                    {
                        "id": "heatmap",
                        "type": "image",
                        "image": {
                            "type": "external",
                            "external": {
                                "url": "https://raw.githubusercontent.com/u/r/main/OUT_FOLDER/weread.svg"
                            },
                        },
                        "has_children": False,
                    },
                ],
            }[block_id]
        )

        helper.search_database("root")

        self.assertEqual(helper.database_id_dict["书架"], "database")
        self.assertEqual(helper.heatmap_block_id, "heatmap")
        self.assertEqual(helper._heatmap_block_type, "image")

    def test_heatmap_image_update_preserves_block_type(self):
        helper = make_helper()
        helper.heatmap_block_id = "heatmap"
        helper._heatmap_block_type = "image"

        helper.update_heatmap("heatmap", "https://example.com/OUT_FOLDER/map.svg")

        helper.client.blocks.update.assert_called_once_with(
            block_id="heatmap",
            image={
                "type": "external",
                "external": {"url": "https://example.com/OUT_FOLDER/map.svg"},
            },
        )

    def test_creates_missing_heatmap_image_on_parent_page(self):
        helper = make_helper()
        helper.page_id = "root"
        helper.append_blocks = Mock(
            return_value={"results": [{"id": "new-heatmap"}]}
        )

        block_id = helper.create_heatmap(
            "https://raw.githubusercontent.com/u/r/main/OUT_FOLDER/weread.svg"
        )

        self.assertEqual(block_id, "new-heatmap")
        self.assertEqual(helper.heatmap_block_id, "new-heatmap")
        children = helper.append_blocks.call_args.args[1]
        self.assertEqual(children[0]["type"], "image")

    def test_append_after_uses_2026_position_object(self):
        helper = make_helper()

        helper.append_blocks_after("page", [{"type": "paragraph"}], "previous")

        helper.client.blocks.children.append.assert_called_once_with(
            block_id="page",
            children=[{"type": "paragraph"}],
            position={
                "type": "after_block",
                "after_block": {"id": "previous"},
            },
        )

    def test_day_timestamp_is_shanghai_start_of_day(self):
        helper = make_helper()
        helper.get_year_relation_id = Mock(return_value="year")
        helper.get_month_relation_id = Mock(return_value="month")
        helper.get_week_relation_id = Mock(return_value="week")
        helper.get_relation_id = Mock(return_value="day")
        helper.day_database_id = "days"

        helper.get_day_relation_id(datetime(2026, 8, 11, 18, 30))

        properties = helper.get_relation_id.call_args.args[3]
        expected = pendulum.datetime(2026, 8, 11, tz="Asia/Shanghai").int_timestamp
        self.assertEqual(properties["时间戳"]["number"], expected)

    def test_get_all_book_tolerates_optional_missing_properties(self):
        helper = make_helper()
        helper.book_database_id = "source"
        helper.query_all = Mock(
            return_value=[
                {
                    "id": "page",
                    "cover": None,
                    "properties": {
                        "BookId": {
                            "type": "rich_text",
                            "rich_text": [{"plain_text": "book-1"}],
                        },
                        "阅读进度": {"type": "number", "number": 0.5},
                        "阅读状态": {
                            "type": "status",
                            "status": {"name": "在读"},
                        },
                    },
                }
            ]
        )

        book = helper.get_all_book()["book-1"]

        self.assertEqual(book["progress"], 0.5)
        self.assertEqual(book["status"], "在读")
        self.assertIsNone(book["lastReadingDate"])
        self.assertIsNone(book["readingTime"])


if __name__ == "__main__":
    unittest.main()
