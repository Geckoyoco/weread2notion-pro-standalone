import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import book


class BookNormalizationTests(unittest.TestCase):
    def test_normalize_progress_accepts_percent_and_fraction(self):
        self.assertEqual(book.normalize_progress(42), 0.42)
        self.assertEqual(book.normalize_progress(0.42), 0.42)
        self.assertEqual(book.normalize_progress(120), 1)

    def test_gateway_progress_one_means_one_percent(self):
        self.assertEqual(book.normalize_gateway_progress(1), 0.01)
        self.assertEqual(book.normalize_gateway_progress(100), 1)

    def test_gateway_rating_uses_the_documented_zero_to_hundred_scale(self):
        self.assertEqual(book.normalize_gateway_rating(10), 1)
        self.assertEqual(book.normalize_gateway_rating(83), 8.3)
        self.assertEqual(book.normalize_gateway_rating(100), 10)

    def test_reading_status_requires_the_gateway_finished_state(self):
        self.assertEqual(book.reading_status(1, 0, 0), "想读")
        self.assertEqual(book.reading_status(2, 0.01, 0), "在读")
        self.assertEqual(book.reading_status(2, 1, 3600), "已读")
        self.assertEqual(book.reading_status(4, 1, 3600), "已读")

    def test_completed_book_is_not_downgraded_by_empty_gateway_progress(self):
        progress = book.preserve_completed_progress(
            {"status": "已读", "progress": 1, "readingTime": 600},
            {
                "readingProgress": 0,
                "readingTime": 0,
                "markedStatus": 1,
                "lastReadingDate": 1_783_965_981,
            },
        )
        self.assertEqual(progress["readingProgress"], 100)
        self.assertEqual(progress["markedStatus"], 4)
        self.assertEqual(progress["readingTime"], 600)

    def test_active_gateway_progress_can_update_completed_book(self):
        progress = book.preserve_completed_progress(
            {"status": "已读", "progress": 1},
            {"readingProgress": 80, "readingTime": 3600, "markedStatus": 2},
        )
        self.assertEqual(progress["readingProgress"], 80)

    def test_normalize_timestamp_accepts_milliseconds(self):
        self.assertEqual(book.normalize_timestamp(1_700_000_000_000), 1_700_000_000)
        self.assertEqual(book.normalize_timestamp(1_700_000_000), 1_700_000_000)
        self.assertEqual(
            book.normalize_timestamp("2024-03-09T16:00:00.000Z"),
            1_710_000_000,
        )

    def test_metadata_update_time_is_not_treated_as_a_reading_date(self):
        self.assertIsNone(book.book_date({"updateTime": 1_700_000_000}))
        self.assertEqual(
            book.book_date({"updateTime": 1_700_000_000, "readUpdateTime": 123}),
            123,
        )

    def test_normalize_cover_accepts_notion_cover_objects(self):
        self.assertEqual(
            book.normalize_cover(
                {"type": "external", "external": {"url": "https://img.test/a.jpg"}}
            ),
            "https://img.test/a.jpg",
        )

    def test_collect_books_merges_shelf_and_notebook(self):
        shelf = {
            "books": [
                {"bookId": "1", "title": "Book", "readUpdateTime": 100}
            ]
        }
        notebooks = [
            {"bookId": "1", "sort": 200, "book": {"bookId": "1", "author": "A"}}
        ]
        result = book.collect_books(shelf, notebooks)
        self.assertEqual(result["1"]["title"], "Book")
        self.assertEqual(result["1"]["author"], "A")
        self.assertEqual(result["1"]["sort"], 200)

    def test_archive_and_book_ids_are_normalized_to_strings(self):
        self.assertEqual(
            book.build_archive_map(
                {"archive": [{"name": "技术", "bookIds": [1]}]}
            ),
            {"1": "技术"},
        )
        result = book.collect_books({"books": [{"bookId": 1}]}, [])
        self.assertEqual(result, {"1": {"bookId": "1"}})

    def test_needs_sync_detects_progress_change_but_leaves_note_cursor_to_notes_job(self):
        existing = {
            "Sort": 10,
            "category": "默认",
            "cover": "https://example.com/cover.jpg",
            "lastReadingDate": 100,
            "progress": 0.1,
            "status": "在读",
        }
        archive = {"1": "默认"}
        self.assertFalse(
            book.needs_sync(
                "1",
                {"sort": 10, "readUpdateTime": 100, "progress": 10},
                existing,
                archive,
            )
        )
        self.assertFalse(
            book.needs_sync(
                "1",
                {"sort": 11, "readUpdateTime": 100, "progress": 10},
                existing,
                archive,
            )
        )
        self.assertTrue(
            book.needs_sync(
                "1",
                {"sort": 10, "readUpdateTime": 101, "progress": 10},
                existing,
                archive,
            )
        )

    def test_needs_sync_detects_progress_and_explicit_completion_changes(self):
        existing = {
            "category": None,
            "cover": "https://example.com/cover.jpg",
            "lastReadingDate": 100,
            "progress": 0.45,
            "status": "在读",
        }

        self.assertTrue(
            book.needs_sync("1", {"progress": 46}, existing, {})
        )
        self.assertTrue(
            book.needs_sync(
                "1", {"progress": 45, "finishTime": 101}, existing, {}
            )
        )

    def test_needs_sync_does_not_compare_metadata_time_to_last_reading_time(self):
        existing = {
            "category": None,
            "cover": "https://example.com/cover.jpg",
            "lastReadingDate": None,
        }
        self.assertFalse(
            book.needs_sync(
                "1", {"updateTime": 1_700_000_000}, existing, {}
            )
        )

    def test_sync_book_writes_gateway_duration_and_one_percent_progress(self):
        class FakeApi:
            def get_bookinfo(self, book_id):
                return {
                    "bookId": book_id,
                    "title": "Example",
                    "cover": "",
                    "isbn": "",
                }

            def get_read_info(self, book_id):
                return {
                    "bookId": book_id,
                    "readingProgress": 1,
                    "readingTime": 3661,
                    "markedStatus": 2,
                    "lastReadingDate": 1710000000,
                }

        class FakeNotion:
            author_database_id = None
            category_database_id = None
            legacy_book_database = False

            def __init__(self):
                self.properties = None

            def get_date_relation(self, properties, value):
                return None

            def create_book_page(self, *, properties, icon):
                self.properties = properties
                return {"id": "page-1"}

        notion = FakeNotion()
        book.sync_book(
            notion,
            FakeApi(),
            {},
            {},
            {
                "b1": {
                    "bookId": "b1",
                    "title": "Example",
                    "cover": "https://img.test/shelf-cover.jpg",
                }
            },
            0,
            "b1",
        )

        self.assertEqual(notion.properties["阅读时长"]["number"], 3661)
        self.assertEqual(notion.properties["阅读进度"]["number"], 0.01)
        self.assertEqual(notion.properties["阅读状态"]["status"]["name"], "在读")
        self.assertEqual(
            notion.properties["封面"]["files"][0]["external"]["url"],
            "https://img.test/shelf-cover.jpg",
        )

    def test_sync_book_preserves_remote_notebook_sort_for_new_books(self):
        class FakeApi:
            def get_bookinfo(self, book_id):
                return {"bookId": book_id, "title": "Imported"}

            def get_read_info(self, book_id):
                return {}

        class FakeNotion:
            author_database_id = None
            category_database_id = None
            legacy_book_database = False

            def get_date_relation(self, properties, value):
                return None

            def create_book_page(self, *, properties, icon):
                self.properties = properties
                return {"id": "page-1"}

        notion = FakeNotion()
        book.sync_book(
            notion,
            FakeApi(),
            {},
            {},
            {"b1": {"bookId": "b1", "title": "Imported", "sort": 42}},
            0,
            "b1",
        )

        self.assertEqual(notion.properties["Sort"]["number"], 42)


if __name__ == "__main__":
    unittest.main()
