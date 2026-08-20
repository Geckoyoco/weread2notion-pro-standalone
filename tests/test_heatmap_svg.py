import unittest
import xml.etree.ElementTree as ET
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory

from scripts.heatmap_svg import SHANGHAI, HeatmapPalette, render_heatmap, write_heatmap


class HeatmapSvgTests(unittest.TestCase):
    def test_render_is_standalone_escaped_and_has_one_cell_per_day(self):
        timestamp = int(datetime(2024, 1, 1, tzinfo=SHANGHAI).timestamp())
        svg = render_heatmap(
            {timestamp: 300, "2024-01-02": 1800},
            2024,
            title="我 < 读书",
        )

        ET.fromstring(svg)
        self.assertTrue(svg.startswith('<?xml version="1.0"'))
        self.assertEqual(svg.count("data-date="), 366)
        self.assertIn('aria-label="我 &lt; 读书"', svg)
        self.assertNotIn('class="title"', svg)
        self.assertIn("2024：0小时35分钟", svg)
        self.assertIn("阅读 2 天", svg)
        self.assertIn("最长连续 2 天", svg)
        self.assertIn('width="100%"', svg)
        self.assertIn('preserveAspectRatio="xMinYMin meet"', svg)
        self.assertIn('data-summary="2024：0小时35分钟', svg)
        self.assertIn('<text class="summary" x="0" y="14"', svg)
        self.assertIn('.summary{font:600 10px', svg)
        self.assertIn('"PingFang SC","Microsoft YaHei"', svg)
        self.assertIn(
            'fill="#ace7ae" data-date="2024-01-01" data-seconds="300"', svg
        )
        self.assertIn(
            'fill="#549f57" data-date="2024-01-02" data-seconds="1800"', svg
        )

    def test_palette_and_invalid_entries_are_handled_without_dependencies(self):
        palette = HeatmapPalette(empty="#000000", high="#ffffff")
        svg = render_heatmap(
            {date(2025, 3, 4): 3600, "not-a-date": 99, 0: -5},
            2025,
            palette=palette,
        )

        self.assertIn(
            'fill="#ffffff" data-date="2025-03-04" data-seconds="3600"', svg
        )
        self.assertNotIn("not-a-date", svg)

    def test_summary_uses_weread_one_minute_active_day_threshold(self):
        svg = render_heatmap(
            {
                "2026-08-13": 60,
                "2026-08-14": 30,
                "2026-08-15": 120,
                "2026-08-16": 180,
            },
            2026,
        )

        self.assertIn("阅读 3 天", svg)
        self.assertIn("最长连续 2 天", svg)

    def test_write_heatmap_creates_the_target_directory(self):
        with TemporaryDirectory() as directory:
            output = Path(directory) / "nested" / "weread-2025.svg"
            result = write_heatmap(output, {"2025-01-01": 60}, 2025)

            self.assertEqual(result, output)
            self.assertTrue(output.is_file())
            self.assertIn("2025-01-01", output.read_text(encoding="utf-8"))

    def test_empty_year_is_explicit_and_has_no_duplicate_year_badge(self):
        svg = render_heatmap({}, 2020)

        self.assertIn("2020：暂无数据", svg)
        self.assertIn(">暂无数据</text>", svg)
        self.assertNotIn('width="56" height="24"', svg)


if __name__ == "__main__":
    unittest.main()
