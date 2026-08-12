"""WeRead Gateway adapter with the legacy WeRead2Notion-Pro API shape.

The original project called undocumented WeRead endpoints with a browser cookie.
This module uses Tencent's API-key authenticated Agent Gateway instead, while
keeping the method names and return values consumed by the existing scripts.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import os
import re
import time
from typing import Any, Iterable, Mapping

import requests


WEREAD_GATEWAY_URL = "https://i.weread.qq.com/api/agent/gateway"
WEREAD_SKILL_VERSION = "1.0.4"
SHANGHAI_TIMEZONE = timezone(timedelta(hours=8))


class WeReadGatewayError(RuntimeError):
    """Raised when the Tencent WeRead Gateway rejects a request."""


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _to_number(value: Any, default: int | float = 0) -> int | float:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int, float)):
        return value
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return int(number) if number.is_integer() else number


def _truthy(value: Any) -> bool:
    if isinstance(value, str):
        return value.strip().lower() not in {"", "0", "false", "no", "none"}
    return bool(value)


def _first(mapping: Mapping[str, Any], names: Iterable[str], default: Any = None) -> Any:
    for name in names:
        value = mapping.get(name)
        if value is not None and value != "":
            return value
    return default


class WeReadApi:
    """Client for the official WeRead Agent Gateway.

    ``session`` is injectable so all behaviour can be tested without network
    access. Existing callers can continue to instantiate ``WeReadApi()``.
    """

    def __init__(
        self,
        api_key: str | None = None,
        session: requests.Session | None = None,
        timeout: int = 30,
    ) -> None:
        raw_api_key = api_key if api_key is not None else os.getenv("WEREAD_API_KEY")
        self.api_key = re.sub(r"\s+", "", raw_api_key or "")
        if not self.api_key:
            raise WeReadGatewayError(
                "没有找到 WEREAD_API_KEY，请在 GitHub Actions Secrets 中配置"
            )

        self.gateway_url = os.getenv("WEREAD_GATEWAY_URL", WEREAD_GATEWAY_URL)
        self.skill_version = os.getenv(
            "WEREAD_SKILL_VERSION", WEREAD_SKILL_VERSION
        )
        self.timeout = timeout
        self.session = session or requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            }
        )

    def request(self, api_name: str, **params: Any) -> dict[str, Any]:
        """Call a Gateway API with business parameters at the JSON top level."""

        payload = {
            "api_name": api_name,
            "skill_version": self.skill_version,
            **params,
        }
        response = self._post_with_retry(payload)
        result = response.json()
        if not isinstance(result, Mapping):
            raise WeReadGatewayError(f"微信读书 Gateway 返回了无效数据: {api_name}")

        upgrade_info = result.get("upgrade_info")
        if upgrade_info:
            raise WeReadGatewayError(f"微信读书 skill 需要升级: {upgrade_info}")
        errcode = _to_number(result.get("errcode"), 0)
        if errcode != 0:
            message = result.get("errmsg") or result.get("message") or "未知错误"
            raise WeReadGatewayError(
                f"微信读书 Gateway 请求失败: {api_name}, "
                f"errcode={errcode}, message={message}"
            )

        # Some Gateway deployments wrap business fields in ``data`` while the
        # current endpoint returns them at the top level. Accept both forms.
        nested = result.get("data")
        if isinstance(nested, Mapping):
            return dict(nested)
        return dict(result)

    def _post_with_retry(self, payload: dict[str, Any]) -> requests.Response:
        """Retry transient network and server failures up to three times."""

        for attempt in range(5):
            try:
                response = self.session.post(
                    self.gateway_url,
                    json=payload,
                    timeout=self.timeout,
                )
                response.raise_for_status()
                return response
            except requests.HTTPError as error:
                status_code = getattr(error.response, "status_code", None)
                # 499 is returned by the WeRead gateway for a transient
                # upstream/client disconnect. Treat it like a network error
                # so a single slow book gets a few chances.
                if (
                    status_code is not None
                    and 400 <= status_code < 500
                    and status_code != 499
                ):
                    raise
                if attempt == 4:
                    raise
            except requests.RequestException:
                if attempt == 4:
                    raise
            time.sleep(3 * (attempt + 1))
        raise AssertionError("unreachable")

    def get_bookshelf(self) -> dict[str, Any]:
        """Return shelf data in the shape expected by ``scripts/book.py``."""

        data = self.request("/shelf/sync")
        books = [self._normalize_shelf_book(item) for item in _as_list(data.get("books"))]
        books = [item for item in books if item.get("bookId")]

        progress_by_book: dict[str, dict[str, Any]] = {}
        for item in _as_list(data.get("bookProgress")):
            progress = _as_mapping(item)
            book_id = str(progress.get("bookId") or "")
            if not book_id:
                continue
            progress["bookId"] = book_id
            # The working non-Pro project already accounts for both names.
            # ``readingTime`` can be present as 0 while the current Gateway
            # exposes the real cumulative value as ``recordReadingTime``.
            progress["readingTime"] = _to_number(
                progress.get("readingTime"), 0
            ) or _to_number(progress.get("recordReadingTime"), 0)
            progress_by_book[book_id] = progress

        result = dict(data)
        result["books"] = books
        result["archive"] = [
            _as_mapping(item) for item in _as_list(data.get("archive"))
        ]
        result["bookProgress"] = list(progress_by_book.values())
        result["albums"] = _as_list(data.get("albums"))
        return result

    @staticmethod
    def _normalize_shelf_book(item: Any) -> dict[str, Any]:
        wrapper = _as_mapping(item)
        nested_book = _as_mapping(wrapper.get("book"))
        book = {**nested_book, **{k: v for k, v in wrapper.items() if k != "book"}}
        book_id = book.get("bookId") or nested_book.get("bookId")
        if book_id is not None:
            book["bookId"] = str(book_id)
        return book

    def get_notebooklist(self) -> list[dict[str, Any]]:
        """Fetch all notebook pages using the documented ``lastSort`` cursor."""

        books: list[dict[str, Any]] = []
        seen_book_ids: set[str] = set()
        last_sort: int | float | None = None
        seen_cursors: set[int | float] = set()

        while True:
            params: dict[str, Any] = {"count": 100}
            if last_sort is not None:
                params["lastSort"] = last_sort
            data = self.request("/user/notebooks", **params)
            batch = _as_list(data.get("books"))

            for raw_item in batch:
                item = self._normalize_notebook(raw_item)
                book_id = item.get("bookId")
                if not book_id or book_id in seen_book_ids:
                    continue
                seen_book_ids.add(book_id)
                books.append(item)

            if not _truthy(data.get("hasMore")) or not batch:
                break

            next_cursor = _to_number(
                _as_mapping(batch[-1]).get("sort"),
                _to_number(data.get("lastSort"), 0),
            )
            if next_cursor in seen_cursors or next_cursor == last_sort:
                raise WeReadGatewayError("微信读书笔记本分页游标没有前进")
            seen_cursors.add(next_cursor)
            last_sort = next_cursor

        books.sort(key=lambda item: _to_number(item.get("sort"), 0))
        return books

    @staticmethod
    def _normalize_notebook(raw_item: Any) -> dict[str, Any]:
        item = _as_mapping(raw_item)
        book = _as_mapping(item.get("book"))
        if not book:
            # Some responses expose book metadata directly on the notebook.
            book = {
                key: value
                for key, value in item.items()
                if key not in {"sort", "reviewCount", "noteCount", "bookmarkCount"}
            }
        book_id = item.get("bookId") or book.get("bookId")
        book_id = str(book_id) if book_id is not None else ""
        book.setdefault("bookId", book_id)

        result = dict(item)
        result["bookId"] = book_id
        result["sort"] = _to_number(
            item.get("sort"),
            _to_number(_first(book, ("readUpdateTime", "updateTime")), 0),
        )
        result["book"] = book
        return result

    def get_bookinfo(self, bookId: str) -> dict[str, Any]:
        """Return full book metadata using legacy field names."""

        data = self.request("/book/info", bookId=str(bookId))
        nested_book = _as_mapping(data.get("book"))
        book = {**data, **nested_book}
        book.pop("data", None)
        book.pop("book", None)
        book["bookId"] = str(book.get("bookId") or bookId)
        book["title"] = book.get("title") or ""
        book["author"] = book.get("author") or ""
        book["cover"] = book.get("cover") or ""
        book["intro"] = book.get("intro") or ""
        book["isbn"] = book.get("isbn") or ""
        book["newRating"] = _to_number(book.get("newRating"), 0)
        book["categories"] = self._normalize_categories(
            book.get("categories") or book.get("category")
        )
        return book

    @staticmethod
    def _normalize_categories(value: Any) -> list[dict[str, str]]:
        if not value:
            return []
        values = [value] if isinstance(value, (str, Mapping)) else _as_list(value)
        categories: list[dict[str, str]] = []
        seen: set[str] = set()
        for item in values:
            if isinstance(item, Mapping):
                title = item.get("title") or item.get("name") or ""
            else:
                title = str(item)
            title = str(title).strip()
            if title and title not in seen:
                categories.append({"title": title})
                seen.add(title)
        return categories

    def get_read_info(self, bookId: str) -> dict[str, Any]:
        """Return progress fields in the units expected by the old Pro code.

        ``readingProgress`` remains a 0-100 percentage because ``book.py``
        divides it by 100. ``readingTime`` is always seconds.
        """

        data = self.request("/book/getprogress", bookId=str(bookId))
        book = _as_mapping(data.get("book")) or dict(data)
        result = dict(book)

        progress = _to_number(
            _first(book, ("progress", "readingProgress")), 0
        )
        progress = min(max(progress, 0), 100)
        finish_time = _to_number(book.get("finishTime"), 0) or _to_number(
            book.get("finishedDate"), 0
        )
        update_time = (
            _to_number(book.get("updateTime"), 0)
            or _to_number(book.get("lastReadingDate"), 0)
            or _to_number(book.get("activityDate"), 0)
        )
        begin_time = (
            _to_number(book.get("beginReadingDate"), 0)
            or _to_number(book.get("beginReadingTime"), 0)
            or _to_number(book.get("startReadingTime"), 0)
            or _to_number(book.get("firstReadingTime"), 0)
        )
        reading_time = _to_number(book.get("readingTime"), 0) or _to_number(
            book.get("recordReadingTime"), 0
        )

        if finish_time and progress >= 100:
            marked_status = 4
        elif update_time or _truthy(book.get("isStartReading")) or progress > 0:
            marked_status = 2
        else:
            marked_status = 1

        result.update(
            {
                "bookId": str(data.get("bookId") or book.get("bookId") or bookId),
                "readingProgress": progress,
                "readingTime": reading_time,
                "markedStatus": marked_status,
                "finishedDate": finish_time,
                "beginReadingDate": begin_time,
                "lastReadingDate": update_time,
                "activityDate": finish_time or update_time,
                "readingBookDate": update_time,
            }
        )
        return result

    def get_bookmark_list(self, bookId: str) -> list[dict[str, Any]]:
        """Return the bare legacy ``updated`` highlight list."""

        data = self.request("/book/bookmarklist", bookId=str(bookId))
        bookmarks: list[dict[str, Any]] = []
        for raw_item in _as_list(data.get("updated")):
            item = _as_mapping(raw_item)
            item["bookId"] = str(item.get("bookId") or bookId)
            if "chapterUid" in item:
                item["chapterUid"] = self._normalize_uid(item.get("chapterUid"))
            bookmarks.append(item)
        return bookmarks

    def get_review_list(self, bookId: str) -> list[dict[str, Any]]:
        """Return all personal reviews/notes as bare review dictionaries."""

        reviews: list[dict[str, Any]] = []
        seen_ids: set[str] = set()
        synckey: int | float = 0
        seen_cursors: set[int | float] = set()

        while True:
            data = self.request(
                "/review/list/mine",
                bookid=str(bookId),
                synckey=synckey,
                count=100,
            )
            batch = _as_list(data.get("reviews"))
            for raw_item in batch:
                review = self._normalize_review(raw_item, str(bookId))
                review_id = str(review.get("reviewId") or "")
                if review_id and review_id in seen_ids:
                    continue
                if review_id:
                    seen_ids.add(review_id)
                reviews.append(review)

            if not _truthy(data.get("hasMore")) or not batch:
                break
            next_cursor = _to_number(data.get("synckey"), 0)
            if next_cursor == synckey or next_cursor in seen_cursors:
                raise WeReadGatewayError("微信读书笔记分页游标没有前进")
            seen_cursors.add(next_cursor)
            synckey = next_cursor

        return reviews

    @classmethod
    def _normalize_review(cls, raw_item: Any, book_id: str) -> dict[str, Any]:
        wrapper = _as_mapping(raw_item)
        review = _as_mapping(wrapper.get("review"))
        nested_review = _as_mapping(review.get("review"))
        if nested_review:
            review = {**review, **nested_review}
            review.pop("review", None)
        if not review:
            review = dict(wrapper)

        for name in ("reviewId", "type", "chapterUid", "chapterIdx"):
            if review.get(name) is None and wrapper.get(name) is not None:
                review[name] = wrapper.get(name)
        review["bookId"] = str(review.get("bookId") or book_id)

        review_type = review.get("type")
        if review_type is None and "isFinish" in review and not review.get("range"):
            review_type = 4
            review["type"] = review_type
        if _to_number(review_type, 0) == 4:
            review["chapterUid"] = 1000000
        elif "chapterUid" in review:
            review["chapterUid"] = cls._normalize_uid(review.get("chapterUid"))
        return review

    def get_chapter_info(self, bookId: str) -> dict[int | str, dict[str, Any]]:
        """Return chapters keyed by ``chapterUid``, plus the legacy review node."""

        data = self.request("/book/chapterinfo", bookId=str(bookId))
        chapters: dict[int | str, dict[str, Any]] = {}
        for index, raw_item in enumerate(_as_list(data.get("chapters"))):
            chapter = _as_mapping(raw_item)
            if chapter.get("chapterUid") is None:
                continue
            uid = self._normalize_uid(chapter.get("chapterUid"))
            chapter["chapterUid"] = uid
            chapter["chapterIdx"] = _to_number(chapter.get("chapterIdx"), index)
            chapter["readAhead"] = _to_number(chapter.get("readAhead"), 0)
            chapter["updateTime"] = _to_number(chapter.get("updateTime"), 0)
            chapter["level"] = _to_number(chapter.get("level"), 1)
            chapters[uid] = chapter

        chapters[1000000] = {
            "chapterUid": 1000000,
            "chapterIdx": 1000000,
            "updateTime": 0,
            "readAhead": 0,
            "title": "点评",
            "level": 1,
        }
        return chapters

    @staticmethod
    def _normalize_uid(value: Any) -> int | str:
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.strip().lstrip("-").isdigit():
            return int(value)
        return str(value)

    def get_read_data(
        self, mode: str = "monthly", base_time: int | None = None
    ) -> dict[str, Any]:
        """Expose the official fixed-period reading statistics endpoint."""

        if mode not in {"weekly", "monthly", "annually", "overall"}:
            raise ValueError(f"不支持的阅读统计周期: {mode}")
        params: dict[str, Any] = {"mode": mode}
        if base_time is not None:
            params["baseTime"] = int(base_time)
        return self.request("/readdata/detail", **params)

    def get_api_data(self, year: int | str | None = None) -> dict[str, Any]:
        """Return annual stats with daily seconds under legacy ``readTimes``.

        The official annual response may contain both monthly ``readTimes`` and
        daily ``dailyReadTimes``. The daily field is deliberately preferred so
        the old heatmap and Day database do not mistake monthly buckets for days.
        """

        normalized_year = self._normalize_year(year)
        now = datetime.now(SHANGHAI_TIMEZONE)
        if normalized_year > now.year:
            return {
                "year": normalized_year,
                "readTimes": {},
                "dailyReadTimes": {},
            }
        base_time = int(
            datetime(
                normalized_year,
                1,
                1,
                tzinfo=SHANGHAI_TIMEZONE,
            ).timestamp()
        )
        annual = self.get_read_data(mode="annually", base_time=base_time)
        daily_source = annual.get("dailyReadTimes")
        if not daily_source:
            daily = self._get_monthly_daily_read_times(normalized_year)
        else:
            daily = self._normalize_daily_read_times(daily_source)

        result = dict(annual)
        result["year"] = normalized_year
        result["readTimes"] = daily
        result["dailyReadTimes"] = daily
        return result

    def _get_monthly_daily_read_times(
        self, year: int
    ) -> dict[int, int | float]:
        """Build daily data from the twelve fixed monthly periods.

        Annual ``readTimes`` is monthly according to Tencent's contract, so it
        must never be fed directly to the daily heatmap.
        """

        now = datetime.now(SHANGHAI_TIMEZONE)
        if year > now.year:
            return {}
        final_month = now.month if year == now.year else 12

        daily: dict[int, int | float] = {}
        for month in range(1, final_month + 1):
            base_time = int(
                datetime(year, month, 1, tzinfo=SHANGHAI_TIMEZONE).timestamp()
            )
            monthly = self.get_read_data(mode="monthly", base_time=base_time)
            source = monthly.get("dailyReadTimes") or monthly.get("readTimes")
            daily.update(self._normalize_daily_read_times(source))
        return dict(sorted(daily.items()))

    def get_daily_read_times(
        self, year: int | str | None = None
    ) -> dict[int, int | float]:
        """Convenience method for heatmap/day-database consumers."""

        return dict(self.get_api_data(year=year).get("readTimes") or {})

    @staticmethod
    def _normalize_year(year: int | str | None) -> int:
        candidate = year if year is not None else os.getenv("YEAR")
        if candidate in (None, ""):
            return datetime.now(SHANGHAI_TIMEZONE).year
        try:
            normalized = int(candidate)
        except (TypeError, ValueError) as error:
            raise ValueError(f"无效年份: {candidate}") from error
        if normalized < 1970 or normalized > 9999:
            raise ValueError(f"无效年份: {candidate}")
        return normalized

    @classmethod
    def _normalize_daily_read_times(
        cls, value: Any
    ) -> dict[int, int | float]:
        items: Iterable[tuple[Any, Any]]
        if isinstance(value, Mapping):
            items = value.items()
        elif isinstance(value, (list, tuple)):
            rows: list[tuple[Any, Any]] = []
            for raw_item in value:
                item = _as_mapping(raw_item)
                rows.append(
                    (
                        _first(item, ("readDate", "date", "timestamp")),
                        _first(item, ("readTime", "duration", "seconds", "value")),
                    )
                )
            items = rows
        else:
            return {}

        result: dict[int, int | float] = {}
        for raw_key, raw_value in items:
            timestamp = cls._normalize_day_timestamp(raw_key)
            if timestamp is None:
                continue
            if isinstance(raw_value, Mapping):
                raw_value = _first(
                    raw_value,
                    ("readTime", "duration", "seconds", "value"),
                )
            seconds = _to_number(raw_value, 0)
            if seconds < 0:
                continue
            result[timestamp] = seconds
        return dict(sorted(result.items()))

    @staticmethod
    def _normalize_day_timestamp(value: Any) -> int | None:
        if value is None or value == "":
            return None
        if isinstance(value, (int, float)):
            timestamp = int(value)
        else:
            text = str(value).strip()
            if text.lstrip("-").isdigit():
                timestamp = int(text)
            else:
                try:
                    parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
                except ValueError:
                    return None
                if parsed.tzinfo is None:
                    parsed = parsed.replace(tzinfo=SHANGHAI_TIMEZONE)
                timestamp = int(parsed.timestamp())
        if abs(timestamp) >= 1_000_000_000_000:
            timestamp //= 1000
        local_day = datetime.fromtimestamp(timestamp, SHANGHAI_TIMEZONE).date()
        return int(
            datetime(
                local_day.year,
                local_day.month,
                local_day.day,
                tzinfo=SHANGHAI_TIMEZONE,
            ).timestamp()
        )

    def transform_id(self, book_id: str) -> tuple[str, list[str]]:
        book_id = str(book_id)
        id_length = len(book_id)
        if re.match(r"^\d*$", book_id):
            transformed = []
            for index in range(0, id_length, 9):
                transformed.append(
                    format(int(book_id[index : min(index + 9, id_length)]), "x")
                )
            return "3", transformed

        result = "".join(format(ord(character), "x") for character in book_id)
        return "4", [result]

    def calculate_book_str_id(self, book_id: str) -> str:
        book_id = str(book_id)
        digest = hashlib.md5(book_id.encode("utf-8")).hexdigest()
        result = digest[0:3]
        code, transformed_ids = self.transform_id(book_id)
        result += code + "2" + digest[-2:]

        for index, transformed_id in enumerate(transformed_ids):
            hex_length = format(len(transformed_id), "x").zfill(2)
            result += hex_length + transformed_id
            if index < len(transformed_ids) - 1:
                result += "g"

        if len(result) < 20:
            result += digest[0 : 20 - len(result)]
        checksum = hashlib.md5(result.encode("utf-8")).hexdigest()[0:3]
        return result + checksum

    def get_url(self, book_id: str) -> str:
        return f"https://weread.qq.com/web/reader/{self.calculate_book_str_id(book_id)}"
