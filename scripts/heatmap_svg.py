"""Generate a self-contained SVG heatmap from WeRead daily reading times.

This module intentionally uses only the Python standard library.  The generated
file can be served directly from the repository's ``OUT_FOLDER`` branch/path;
no third-party rendering or proxy service is required.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path
from typing import Mapping


SHANGHAI = timezone(timedelta(hours=8))


@dataclass(frozen=True)
class HeatmapPalette:
    background: str = "#ffffff"
    empty: str = "#ebedf0"
    low: str = "#ace7ae"
    medium: str = "#69c16e"
    high: str = "#549f57"
    text: str = "#24292f"


def _coerce_date(value: object) -> date:
    if isinstance(value, datetime):
        return value.astimezone(SHANGHAI).date() if value.tzinfo else value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and len(value) == 10 and value[4] == "-":
        return date.fromisoformat(value)

    timestamp = int(float(value))
    if abs(timestamp) >= 10**12:
        timestamp //= 1000
    return datetime.fromtimestamp(timestamp, SHANGHAI).date()


def _daily_values(read_times: Mapping[object, object], year: int) -> dict[date, int]:
    daily: dict[date, int] = {}
    for raw_day, raw_seconds in read_times.items():
        try:
            day = _coerce_date(raw_day)
            seconds = max(int(float(raw_seconds or 0)), 0)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if day.year == year:
            daily[day] = daily.get(day, 0) + seconds
    return daily


def _color(seconds: int, palette: HeatmapPalette) -> str:
    if seconds <= 0:
        return palette.empty
    if seconds < 10 * 60:
        return palette.low
    if seconds < 30 * 60:
        return palette.medium
    return palette.high


def _duration_text(seconds: int) -> str:
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours} 小时 {minutes} 分钟"
    if minutes:
        return f"{minutes} 分钟"
    return f"{seconds} 秒"


def render_heatmap(
    read_times: Mapping[object, object],
    year: int,
    *,
    title: str | None = None,
    palette: HeatmapPalette | None = None,
) -> str:
    """Return a GitHub-style yearly reading heatmap as standalone SVG text."""

    palette = palette or HeatmapPalette()
    daily = _daily_values(read_times, year)
    first_day = date(year, 1, 1)
    last_day = date(year, 12, 31)

    # The grid starts on Sunday, matching the familiar contribution-calendar
    # layout.  Cells outside the requested year are left blank.
    grid_start = first_day - timedelta(days=(first_day.weekday() + 1) % 7)
    last_row = (last_day.weekday() + 1) % 7
    grid_end = last_day + timedelta(days=6 - last_row)
    week_count = ((grid_end - grid_start).days // 7) + 1

    cell = 12
    gap = 3
    step = cell + gap
    left = 38
    top = 48
    right = 22
    bottom = 42
    width = left + week_count * step + right
    height = top + 7 * step + bottom
    heading = title or f"微信读书 · {year}"

    background = escape(palette.background, quote=True)
    text_color = escape(palette.text, quote=True)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
            f'height="{height}" viewBox="0 0 {width} {height}" role="img" '
            f'aria-label="{escape(heading, quote=True)}">'
        ),
        f'<rect width="100%" height="100%" rx="8" fill="{background}"/>',
        "<style>",
        (
            ".title{font:600 15px -apple-system,BlinkMacSystemFont,"
            "\"Segoe UI\",sans-serif}"
        ),
        (
            ".label{font:10px -apple-system,BlinkMacSystemFont,"
            "\"Segoe UI\",sans-serif}"
        ),
        "</style>",
        f'<text class="title" x="12" y="22" fill="{text_color}">{escape(heading)}</text>',
    ]

    month_names = ("1月", "2月", "3月", "4月", "5月", "6月", "7月", "8月", "9月", "10月", "11月", "12月")
    for month, month_name in enumerate(month_names, 1):
        month_start = date(year, month, 1)
        week_index = (month_start - grid_start).days // 7
        x = left + week_index * step
        lines.append(
            f'<text class="label" x="{x}" y="{top - 10}" fill="{text_color}">{month_name}</text>'
        )

    for row, label in ((1, "一"), (3, "三"), (5, "五")):
        y = top + row * step + cell - 2
        lines.append(
            f'<text class="label" x="18" y="{y}" fill="{text_color}">{label}</text>'
        )

    current = first_day
    while current <= last_day:
        offset = (current - grid_start).days
        week_index, row = divmod(offset, 7)
        seconds = daily.get(current, 0)
        x = left + week_index * step
        y = top + row * step
        fill = escape(_color(seconds, palette), quote=True)
        description = escape(
            f"{current.isoformat()}：{_duration_text(seconds)}", quote=False
        )
        lines.extend(
            [
                (
                    f'<rect x="{x}" y="{y}" width="{cell}" height="{cell}" '
                    f'rx="2" fill="{fill}" data-date="{current.isoformat()}" '
                    f'data-seconds="{seconds}">'
                ),
                f"<title>{description}</title>",
                "</rect>",
            ]
        )
        current += timedelta(days=1)

    legend_y = top + 7 * step + 24
    legend_x = width - right - (4 * step + 42)
    lines.append(
        f'<text class="label" x="{legend_x - 20}" y="{legend_y + 10}" fill="{text_color}">少</text>'
    )
    for index, color in enumerate(
        (palette.empty, palette.low, palette.medium, palette.high)
    ):
        x = legend_x + index * step
        lines.append(
            f'<rect x="{x}" y="{legend_y}" width="{cell}" height="{cell}" rx="2" '
            f'fill="{escape(color, quote=True)}"/>'
        )
    lines.append(
        f'<text class="label" x="{legend_x + 4 * step + 2}" y="{legend_y + 10}" fill="{text_color}">多</text>'
    )
    lines.append("</svg>")
    return "\n".join(lines) + "\n"


def write_heatmap(
    output: str | Path,
    read_times: Mapping[object, object],
    year: int,
    *,
    title: str | None = None,
    palette: HeatmapPalette | None = None,
) -> Path:
    """Write a heatmap SVG and return its path."""

    path = Path(output)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        render_heatmap(read_times, year, title=title, palette=palette),
        encoding="utf-8",
    )
    return path
