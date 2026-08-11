from datetime import date, datetime
import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase, main
from unittest.mock import patch

from scripts.read_time import (
    SHANGHAI,
    collect_daily_read_times,
    day_timestamp,
    include_today,
    normalize_daily_read_times,
    palette_from_env,
    raw_heatmap_url,
    remove_stale_heatmaps,
    sync_statistics,
)


def notion_number(value):
    return {"type": "number", "number": value}


def notion_title(value):
    return {
        "type": "title",
        "title": [{"plain_text": value, "type": "text", "text": {"content": value}}],
    }


class FakeNotion:
    day_database_id = "day-db"
    week_database_id = "week-db"
    month_database_id = "month-db"
    year_database_id = "year-db"

    def __init__(self, existing=None):
        self.rows = {
            "day-db": [],
            "week-db": [],
            "month-db": [],
            "year-db": [],
            **(existing or {}),
        }
        self.created = []
        self.updated = []

    def query_all(self, database_id):
        return list(self.rows[database_id])

    def create_page_in_database(self, database_id, properties, icon, cover=None):
        page_id = f"{database_id}-{len(self.created) + 1}"
        row = {"id": page_id, "properties": properties}
        self.created.append((database_id, row, icon))
        self.rows[database_id].append(row)
        return row

    def update_book_page(self, page_id, properties):
        self.updated.append((page_id, properties))
        return {"id": page_id, "properties": properties}


class WritableAggregateNotion(FakeNotion):
    def retrieve_data_source(self, database_id):
        return {
            "properties": {
                "时长": {"type": "number", "number": {}},
            }
        }


class ReadTimeTests(TestCase):
    def test_normalize_accepts_gateway_shapes_and_millisecond_timestamps(self):
        day = date(2025, 2, 3)
        timestamp = day_timestamp(day)
        payload = {
            "dailyReadTimes": {
                str(timestamp): "60",
                str(timestamp * 1000): 30,
                str(day_timestamp(date(2024, 12, 31))): 500,
                "invalid": 10,
            }
        }

        self.assertEqual(normalize_daily_read_times(payload, 2025), {timestamp: 90})

    def test_collect_prefers_modern_method_and_supports_legacy_noarg(self):
        timestamp = day_timestamp(date(2025, 4, 5))

        class Modern:
            def __init__(self):
                self.year = None

            def get_daily_read_times(self, year=None):
                self.year = year
                return {timestamp: 120}

            def get_api_data(self):
                raise AssertionError("legacy method should not be called")

        modern = Modern()
        self.assertEqual(collect_daily_read_times(modern, 2025), {timestamp: 120})
        self.assertEqual(modern.year, 2025)

        class Legacy:
            def get_api_data(self):
                return {"readTimes": {str(timestamp): 45}}

        self.assertEqual(collect_daily_read_times(Legacy(), 2025), {timestamp: 45})

    def test_include_today_only_changes_the_selected_current_year(self):
        now = datetime(2025, 6, 7, 12, tzinfo=SHANGHAI)
        self.assertEqual(
            include_today({}, 2025, now), {day_timestamp(date(2025, 6, 7)): 0}
        )
        self.assertEqual(include_today({}, 2024, now), {})

    def test_sync_writes_day_week_month_year_and_merges_new_year_week(self):
        old_timestamp = day_timestamp(date(2024, 12, 31))
        new_timestamp = day_timestamp(date(2025, 1, 1))
        existing_day = {
            "id": "old-day",
            "properties": {
                "标题": notion_title("2024年12月31日"),
                "时间戳": notion_number(old_timestamp),
                "时长": notion_number(300),
            },
        }
        notion = FakeNotion({"day-db": [existing_day]})

        counts = sync_statistics(notion, {new_timestamp: 600})

        self.assertEqual(counts, {"日": 2, "周": 1, "月": 2, "年": 2})
        created_by_database = {}
        for database_id, row, _ in notion.created:
            created_by_database.setdefault(database_id, []).append(row)

        week = created_by_database["week-db"][0]
        self.assertEqual(week["properties"]["标题"]["title"][0]["text"]["content"], "2025年第1周")
        self.assertNotIn("时长", week["properties"])

        new_day = created_by_database["day-db"][0]
        self.assertEqual(new_day["properties"]["时长"]["number"], 600)
        self.assertEqual(len(new_day["properties"]["年"]["relation"]), 1)
        self.assertEqual(len(new_day["properties"]["月"]["relation"]), 1)
        self.assertEqual(len(new_day["properties"]["周"]["relation"]), 1)
        old_day_update = next(
            properties
            for page_id, properties in notion.updated
            if page_id == "old-day"
        )
        self.assertEqual(len(old_day_update["年"]["relation"]), 1)
        self.assertEqual(len(old_day_update["月"]["relation"]), 1)
        self.assertEqual(len(old_day_update["周"]["relation"]), 1)

    def test_self_created_template_writes_period_aggregate_duration(self):
        notion = WritableAggregateNotion()
        timestamp = day_timestamp(date(2025, 1, 2))

        sync_statistics(notion, {timestamp: 600})

        rows = {
            database: row
            for database, row, _ in notion.created
            if database in {"year-db", "month-db", "week-db"}
        }
        self.assertEqual(rows["year-db"]["properties"]["时长"]["number"], 600)
        self.assertEqual(rows["month-db"]["properties"]["时长"]["number"], 600)
        self.assertEqual(rows["week-db"]["properties"]["时长"]["number"], 600)

    def test_raw_url_keeps_slashes_in_branch_and_has_cache_buster(self):
        environment = {
            "GITHUB_REPOSITORY": "reader/example",
            "GITHUB_REF_NAME": "codex/modernize-pro",
        }
        with patch.dict(os.environ, environment, clear=True):
            url = raw_heatmap_url("OUT_FOLDER/weread-2026.svg", version=42)

        self.assertEqual(
            url,
            "https://raw.githubusercontent.com/reader/example/codex/modernize-pro/"
            "OUT_FOLDER/weread-2026.svg?v=42",
        )

    def test_palette_prefers_new_names_and_keeps_legacy_fallback(self):
        with patch.dict(
            os.environ,
            {
                "HEATMAP_BACKGROUND": "#111111",
                "BACKGROUND_COLOR": "#ffffff",
                "DOM_COLOR": "#222222",
                "HEATMAP_HIGH": "#444444",
                "SPECIAL_COLOR2": "#333333",
            },
            clear=True,
        ):
            palette = palette_from_env()

        self.assertEqual(palette.background, "#111111")
        self.assertEqual(palette.empty, "#222222")
        self.assertEqual(palette.high, "#444444")

    def test_stale_cleanup_is_limited_to_sibling_svgs_in_out_folder(self):
        with TemporaryDirectory() as directory:
            root = Path(directory)
            output_folder = root / "OUT_FOLDER"
            nested = output_folder / "nested"
            output_folder.mkdir()
            nested.mkdir()
            target = output_folder / "weread-2026.svg"
            old = output_folder / "old.svg"
            keep_image = output_folder / "cover.png"
            keep_nested = nested / "nested.svg"
            keep_outside = root / "outside.svg"
            for path in (target, old, keep_image, keep_nested, keep_outside):
                path.write_text("x", encoding="utf-8")

            removed = remove_stale_heatmaps(target)

            self.assertEqual(removed, [old])
            self.assertTrue(target.exists())
            self.assertFalse(old.exists())
            self.assertTrue(keep_image.exists())
            self.assertTrue(keep_nested.exists())
            self.assertTrue(keep_outside.exists())

            other_folder = root / "other"
            other_folder.mkdir()
            untouched = other_folder / "old.svg"
            untouched.write_text("x", encoding="utf-8")
            self.assertEqual(remove_stale_heatmaps(other_folder / "new.svg"), [])
            self.assertTrue(untouched.exists())

    def test_existing_noncanonical_day_timestamp_is_not_double_counted(self):
        canonical = day_timestamp(date(2025, 1, 1))
        old_timestamp = canonical + 8 * 3600
        existing_day = {
            "id": "old-day",
            "properties": {
                "标题": notion_title("2025年01月01日"),
                "时间戳": notion_number(old_timestamp),
                "时长": notion_number(600),
            },
        }
        notion = FakeNotion({"day-db": [existing_day]})

        sync_statistics(notion, {canonical: 600})

        week = next(row for database, row, _ in notion.created if database == "week-db")
        # Week/month/year are rollup parents, so no aggregate value is written;
        # the existing row is repaired in place rather than duplicated.
        self.assertNotIn("时长", week["properties"])
        self.assertFalse(any(database == "day-db" for database, _, _ in notion.created))
        repaired = next(
            properties
            for page_id, properties in notion.updated
            if page_id == "old-day"
        )
        self.assertEqual(repaired["时间戳"]["number"], canonical)
        self.assertEqual(len(repaired["年"]["relation"]), 1)


if __name__ == "__main__":
    main()
