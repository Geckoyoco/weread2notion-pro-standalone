"""Synchronize WeRead daily reading statistics and render a local SVG heatmap."""

from __future__ import annotations

import argparse
import calendar
import inspect
import os
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping
from urllib.parse import quote

try:  # ``python -m scripts.read_time`` and test imports
    from .heatmap_svg import HeatmapPalette, write_heatmap
except ImportError:  # ``python scripts/read_time.py`` used by the workflow
    from heatmap_svg import HeatmapPalette, write_heatmap


SHANGHAI = timezone(timedelta(hours=8))
TARGET_ICON_URL = "https://www.notion.so/icons/target_red.svg"


@dataclass(frozen=True)
class PeriodRow:
    key: str
    title: str
    start: date
    end: date | None
    duration: int
    timestamp: int | None = None
    year_key: str | None = None
    month_key: str | None = None
    week_key: str | None = None


def _call_with_optional_year(method, year: int):
    """Call a bound API method without hiding TypeErrors raised inside it."""

    try:
        parameters = inspect.signature(method).parameters
    except (TypeError, ValueError):
        return method(year)
    if not parameters:
        return method()
    if "year" in parameters or any(
        item.kind == inspect.Parameter.VAR_KEYWORD for item in parameters.values()
    ):
        return method(year=year)
    return method(year)


def _payload_read_times(payload: Any) -> Mapping[object, object]:
    if isinstance(payload, tuple) and payload:
        payload = payload[0]
    if isinstance(payload, list):
        return {
            item.get("timestamp"): item.get("duration", 0)
            for item in payload
            if isinstance(item, dict) and item.get("timestamp") is not None
        }
    if not isinstance(payload, Mapping):
        return {}
    for key in ("dailyReadTimes", "readTimes"):
        value = payload.get(key)
        if isinstance(value, Mapping):
            return value
    return payload


def _local_day(raw_day: object) -> date:
    if isinstance(raw_day, datetime):
        return (
            raw_day.astimezone(SHANGHAI).date()
            if raw_day.tzinfo
            else raw_day.date()
        )
    if isinstance(raw_day, date):
        return raw_day
    if isinstance(raw_day, str) and len(raw_day) == 10 and raw_day[4] == "-":
        return date.fromisoformat(raw_day)
    timestamp = int(float(raw_day))
    if abs(timestamp) >= 10**12:
        timestamp //= 1000
    return datetime.fromtimestamp(timestamp, SHANGHAI).date()


def day_timestamp(day: date) -> int:
    return int(datetime.combine(day, time.min, SHANGHAI).timestamp())


def normalize_daily_read_times(payload: Any, year: int | None = None) -> dict[int, int]:
    """Normalize Gateway daily buckets to Shanghai-midnight timestamps."""

    result: dict[int, int] = defaultdict(int)
    for raw_day, raw_duration in _payload_read_times(payload).items():
        try:
            day = _local_day(raw_day)
            duration = max(int(float(raw_duration or 0)), 0)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
        if year is not None and day.year != year:
            continue
        result[day_timestamp(day)] += duration
    return dict(sorted(result.items()))


def collect_daily_read_times(weread_api, year: int) -> dict[int, int]:
    """Use the modern API method when present, with a legacy-compatible fallback."""

    method = getattr(weread_api, "get_daily_read_times", None)
    if callable(method):
        payload = _call_with_optional_year(method, year)
    else:
        method = getattr(weread_api, "get_api_data")
        payload = _call_with_optional_year(method, year)
    return normalize_daily_read_times(payload, year)


def include_today(
    read_times: Mapping[int, int], year: int, now: datetime | None = None
) -> dict[int, int]:
    result = dict(read_times)
    current = now or datetime.now(SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    else:
        current = current.astimezone(SHANGHAI)
    if current.year == year:
        result.setdefault(day_timestamp(current.date()), 0)
    return dict(sorted(result.items()))


def _period_rows(read_times: Mapping[int, int]) -> dict[str, list[PeriodRow]]:
    durations: dict[tuple[str, str], int] = defaultdict(int)
    day_rows: list[PeriodRow] = []

    for timestamp, duration in sorted(read_times.items()):
        day = datetime.fromtimestamp(int(timestamp), SHANGHAI).date()
        monday = day - timedelta(days=day.weekday())
        iso_year, iso_week, _ = day.isocalendar()
        year_key = str(day.year)
        month_key = f"{day.year:04d}-{day.month:02d}"
        week_key = monday.isoformat()
        durations[("year", year_key)] += int(duration)
        durations[("month", month_key)] += int(duration)
        durations[("week", week_key)] += int(duration)
        day_rows.append(
            PeriodRow(
                key=day.isoformat(),
                title=day.strftime("%Y年%m月%d日"),
                start=day,
                end=None,
                duration=int(duration),
                timestamp=int(timestamp),
                year_key=year_key,
                month_key=month_key,
                week_key=week_key,
            )
        )

    years = [
        PeriodRow(
            key=key,
            title=key,
            start=date(int(key), 1, 1),
            end=date(int(key), 12, 31),
            duration=duration,
        )
        for (kind, key), duration in sorted(durations.items())
        if kind == "year"
    ]
    months = []
    for (kind, key), duration in sorted(durations.items()):
        if kind != "month":
            continue
        year, month = (int(item) for item in key.split("-"))
        months.append(
            PeriodRow(
                key=key,
                title=f"{year}年{month}月",
                start=date(year, month, 1),
                end=date(year, month, calendar.monthrange(year, month)[1]),
                duration=duration,
            )
        )
    weeks = []
    for (kind, key), duration in sorted(durations.items()):
        if kind != "week":
            continue
        start = date.fromisoformat(key)
        iso_year, iso_week, _ = start.isocalendar()
        weeks.append(
            PeriodRow(
                key=key,
                title=f"{iso_year}年第{iso_week}周",
                start=start,
                end=start + timedelta(days=6),
                duration=duration,
            )
        )
    return {"year": years, "month": months, "week": weeks, "day": day_rows}


def _property_value(property_value: Any) -> Any:
    if not isinstance(property_value, Mapping):
        return None
    kind = property_value.get("type")
    content = property_value.get(kind) if kind else None
    if content is None:
        for candidate in ("number", "title", "rich_text", "date"):
            if candidate in property_value:
                kind, content = candidate, property_value[candidate]
                break
    if kind == "number":
        return content
    if kind in {"title", "rich_text"}:
        if not content:
            return None
        return content[0].get("plain_text") or (content[0].get("text") or {}).get(
            "content"
        )
    if kind == "date":
        return (content or {}).get("start")
    return content


def _existing_index(rows: list[dict[str, Any]]) -> tuple[dict[str, dict], dict[int, dict]]:
    by_title: dict[str, dict] = {}
    by_timestamp: dict[int, dict] = {}
    for row in rows:
        properties = row.get("properties") or {}
        title = _property_value(properties.get("标题"))
        if title:
            by_title[str(title)] = row
        timestamp = _property_value(properties.get("时间戳"))
        if timestamp is not None:
            try:
                by_timestamp[int(timestamp)] = row
            except (TypeError, ValueError):
                pass
    return by_title, by_timestamp


def _title(value: str) -> dict[str, Any]:
    return {"title": [{"type": "text", "text": {"content": value[:2000]}}]}


def _number(value: int | float) -> dict[str, Any]:
    return {"number": value}


def _date(start: date, end: date | None = None) -> dict[str, Any]:
    return {
        "date": {
            "start": start.isoformat(),
            "end": end.isoformat() if end and end != start else None,
        }
    }


def _relation(page_ids: list[str]) -> dict[str, Any]:
    return {"relation": [{"id": page_id} for page_id in page_ids]}


def _relation_ids(property_value: Any) -> set[str]:
    if not isinstance(property_value, Mapping):
        return set()
    values = property_value.get("relation") or []
    return {
        str(item.get("id"))
        for item in values
        if isinstance(item, Mapping) and item.get("id")
    }


def _icon() -> dict[str, Any]:
    return {"type": "external", "external": {"url": TARGET_ICON_URL}}


def _create_page(notion_helper, database_id: str, properties: dict[str, Any]) -> dict:
    modern = getattr(notion_helper, "create_page_in_database", None)
    if callable(modern):
        return modern(database_id, properties, _icon())
    parent = {"type": "database_id", "database_id": database_id}
    return notion_helper.create_page(parent, properties, _icon())


def _upsert_periods(
    notion_helper,
    database_id: str,
    rows: list[PeriodRow],
    existing_rows: list[dict[str, Any]],
    *,
    write_duration: bool = False,
) -> tuple[dict[str, str], int]:
    by_title, _ = _existing_index(existing_rows)
    page_ids: dict[str, str] = {}
    changed = 0
    for row in rows:
        properties = {
            "标题": _title(row.title),
            "日期": _date(row.start, row.end),
        }
        if write_duration:
            properties["时长"] = _number(row.duration)
        existing = by_title.get(row.title)
        if existing:
            page_id = existing["id"]
            if write_duration and _property_value(
                (existing.get("properties") or {}).get("时长")
            ) != row.duration:
                notion_helper.update_book_page(page_id, properties)
                changed += 1
        else:
            created = _create_page(notion_helper, database_id, properties)
            page_id = created["id"]
            changed += 1
        page_ids[row.key] = page_id
    return page_ids, changed


def _has_writable_duration(notion_helper, database_id: str) -> bool:
    """New self-created templates use a number; legacy templates use rollups."""

    retrieve = getattr(notion_helper, "retrieve_data_source", None)
    if not callable(retrieve):
        return False
    try:
        schema = retrieve(database_id)
    except Exception as error:
        print(f"无法读取统计数据库结构，保留旧模板写入方式: {error}")
        return False
    prop = (schema.get("properties") or {}).get("时长") or {}
    return prop.get("type") == "number" or "number" in prop


def _existing_daily_values(rows: list[dict[str, Any]]) -> dict[int, int]:
    result: dict[int, int] = {}
    for row in rows:
        properties = row.get("properties") or {}
        timestamp = _property_value(properties.get("时间戳"))
        duration = _property_value(properties.get("时长"))
        try:
            canonical_timestamp = day_timestamp(_local_day(timestamp))
            result[canonical_timestamp] = max(int(duration or 0), 0)
        except (TypeError, ValueError, OverflowError, OSError):
            continue
    return result


def sync_statistics(notion_helper, read_times: Mapping[int, int]) -> dict[str, int]:
    """Upsert daily rows and aggregate their week/month/year statistics."""

    database_ids = {
        "day": notion_helper.day_database_id,
        "week": notion_helper.week_database_id,
        "month": notion_helper.month_database_id,
        "year": notion_helper.year_database_id,
    }
    existing = {
        kind: notion_helper.query_all(database_id)
        for kind, database_id in database_ids.items()
    }

    # Preserve days from earlier runs/years and overlay the authoritative
    # buckets fetched in this run.  Aggregating this merged set keeps ISO weeks
    # that straddle New Year complete.
    all_daily = _existing_daily_values(existing["day"])
    all_daily.update({int(key): int(value) for key, value in read_times.items()})
    periods = _period_rows(all_daily)

    writable_duration = {
        kind: _has_writable_duration(notion_helper, database_ids[kind])
        for kind in ("year", "month", "week")
    }
    year_ids, year_changes = _upsert_periods(
        notion_helper,
        database_ids["year"],
        periods["year"],
        existing["year"],
        write_duration=writable_duration["year"],
    )
    month_ids, month_changes = _upsert_periods(
        notion_helper,
        database_ids["month"],
        periods["month"],
        existing["month"],
        write_duration=writable_duration["month"],
    )
    week_ids, week_changes = _upsert_periods(
        notion_helper,
        database_ids["week"],
        periods["week"],
        existing["week"],
        write_duration=writable_duration["week"],
    )

    by_title, by_timestamp = _existing_index(existing["day"])
    day_changes = 0
    for row in periods["day"]:
        properties = {
            "标题": _title(row.title),
            "日期": _date(row.start),
            "时长": _number(row.duration),
            "时间戳": _number(row.timestamp or 0),
            "年": _relation([year_ids[row.year_key]]),
            "月": _relation([month_ids[row.month_key]]),
            "周": _relation([week_ids[row.week_key]]),
        }
        current = by_timestamp.get(row.timestamp or 0) or by_title.get(row.title)
        if current:
            current_properties = current.get("properties") or {}
            old_duration = _property_value(current_properties.get("时长"))
            old_timestamp = _property_value(current_properties.get("时间戳"))
            expected_relations = {
                "年": {year_ids[row.year_key]},
                "月": {month_ids[row.month_key]},
                "周": {week_ids[row.week_key]},
            }
            relation_changed = any(
                _relation_ids(current_properties.get(name)) != expected_ids
                for name, expected_ids in expected_relations.items()
            )
            if (
                old_duration != row.duration
                or old_timestamp != row.timestamp
                or relation_changed
            ):
                notion_helper.update_book_page(current["id"], properties)
                day_changes += 1
        else:
            _create_page(notion_helper, database_ids["day"], properties)
            day_changes += 1

    return {
        "日": day_changes,
        "周": week_changes,
        "月": month_changes,
        "年": year_changes,
    }


def _env_color(primary: str, legacy: str, default: str) -> str:
    return os.getenv(primary) or os.getenv(legacy) or default


def palette_from_env() -> HeatmapPalette:
    return HeatmapPalette(
        background=_env_color("HEATMAP_BACKGROUND", "BACKGROUND_COLOR", "#ffffff"),
        empty=_env_color("HEATMAP_EMPTY", "DOM_COLOR", "#ebedf0"),
        low=_env_color("HEATMAP_LOW", "TRACK_COLOR", "#ace7ae"),
        medium=_env_color("HEATMAP_MEDIUM", "SPECIAL_COLOR1", "#69c16e"),
        high=_env_color("HEATMAP_HIGH", "SPECIAL_COLOR2", "#549f57"),
        text=_env_color("HEATMAP_TEXT", "TEXT_COLOR", "#24292f"),
    )


def remove_stale_heatmaps(output: str | Path) -> list[Path]:
    """Keep yearly SVG siblings so the dashboard can switch between years."""

    return []


def raw_heatmap_url(output: str | Path, version: str | int | None = None) -> str | None:
    repository = (os.getenv("GITHUB_REPOSITORY") or os.getenv("REPOSITORY") or "").strip()
    ref_name = (os.getenv("GITHUB_REF_NAME") or "").strip()
    if not ref_name:
        ref = (os.getenv("REF") or os.getenv("GITHUB_REF") or "").strip()
        ref_name = ref.removeprefix("refs/heads/")
    if not repository or not ref_name:
        return None
    try:
        relative = Path(output).resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return None
    cache_key = version or os.getenv("GITHUB_RUN_ID")
    if not cache_key:
        cache_key = int(datetime.now(timezone.utc).timestamp())
    return (
        f"https://raw.githubusercontent.com/{repository}/{ref_name}/{relative}"
        f"?v={quote(str(cache_key), safe='')}"
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="同步微信读书阅读统计并生成独立 SVG 热力图"
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--skip-heatmap-update",
        action="store_true",
        help="生成 SVG 并同步统计，但暂不更新 Notion 热力图链接",
    )
    mode.add_argument(
        "--heatmap-only",
        action="store_true",
        help="只用已生成的 SVG 更新 Notion 链接，不请求微信读书或写统计",
    )
    mode.add_argument(
        "--render-only",
        action="store_true",
        help="只读取微信读书并生成 SVG，不写入 Notion 统计",
    )
    return parser


def update_heatmap_link(notion_helper, output: str | Path) -> bool:
    dashboard_url = (os.getenv("HEATMAP_DASHBOARD_URL") or "").strip()
    image_url = dashboard_url or raw_heatmap_url(output)
    if notion_helper.heatmap_block_id and image_url:
        notion_helper.update_heatmap(notion_helper.heatmap_block_id, image_url)
        print(f"Notion 热力图已更新为公开 SVG: {image_url}")
        return True
    create_heatmap = getattr(notion_helper, "create_heatmap", None)
    if image_url and callable(create_heatmap):
        create_heatmap(image_url)
        print(f"Notion 热力图图片块已创建: {image_url}")
        return True
    if image_url:
        print(f"未找到热力图占位块；SVG 可直接使用: {image_url}")
    else:
        print("未检测到仓库与分支环境变量，已跳过 Notion 热力图链接更新。")
    return False


def main(argv=None) -> None:
    args = build_parser().parse_args(argv)
    try:
        from .notion_helper import NotionHelper
    except ImportError:
        from notion_helper import NotionHelper

    current_year = datetime.now(SHANGHAI).year
    year = int(os.getenv("YEAR") or current_year)
    output = Path(os.getenv("HEATMAP_OUTPUT", f"OUT_FOLDER/weread-{year}.svg"))

    if args.heatmap_only:
        if not output.is_file():
            raise SystemExit(f"没有找到已生成的热力图: {output}")
        update_heatmap_link(NotionHelper(), output)
        return

    try:
        from .weread_api import WeReadApi
    except ImportError:
        from weread_api import WeReadApi

    weread_api = WeReadApi()
    read_times = include_today(collect_daily_read_times(weread_api, year), year)

    title = os.getenv("HEATMAP_TITLE") or os.getenv("NAME") or f"微信读书 · {year}"
    removed = remove_stale_heatmaps(output)
    if removed:
        print(f"已清理 {len(removed)} 个旧 SVG 热力图。")
    write_heatmap(
        output,
        read_times,
        year,
        title=title,
        palette=palette_from_env(),
    )
    print(f"已生成阅读热力图: {output}")

    if args.render_only:
        return

    notion_helper = NotionHelper()
    counts = sync_statistics(notion_helper, read_times)
    print(
        "阅读统计同步完成: "
        + ", ".join(f"{name} {count} 条" for name, count in counts.items())
    )
    if args.skip_heatmap_update:
        print("已按要求跳过 Notion 热力图链接更新；请在 SVG 推送后运行 --heatmap-only。")
    else:
        update_heatmap_link(notion_helper, output)


if __name__ == "__main__":
    main()
