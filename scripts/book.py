"""Synchronize WeRead bookshelf metadata and progress to the Pro template."""

import os
import re

import pendulum
import requests
from retrying import retry

from config import book_properties_type_dict, tz
from notion_helper import NotionHelper
from weread_api import WeReadApi
import utils


TAG_ICON_URL = "https://www.notion.so/icons/tag_gray.svg"
USER_ICON_URL = "https://www.notion.so/icons/user-circle-filled_gray.svg"
BOOK_ICON_URL = "https://www.notion.so/icons/book_gray.svg"

RATING = {"poor": "⭐️", "fair": "⭐️⭐️⭐️", "good": "⭐️⭐️⭐️⭐️⭐️"}


def to_number(value, default=0):
    if value is None or value == "":
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return int(number) if number.is_integer() else number


def format_duration(seconds):
    seconds = int(to_number(seconds, 0))
    hours, remainder = divmod(seconds, 3600)
    minutes = remainder // 60
    if hours:
        return f"{hours}时{minutes}分"
    return f"{minutes}分"


def normalize_timestamp(value):
    value = to_number(value, 0)
    if value > 100_000_000_000:
        value /= 1000
    return int(value) if value else None


def normalize_progress(value):
    """Normalize an already-fractional value, or a percentage above 1."""
    value = to_number(value, 0)
    if value > 1:
        value /= 100
    return round(min(max(value, 0), 1), 4)


def normalize_gateway_progress(value):
    """Convert the Gateway's documented 0..100 integer to Notion 0..1.

    The boundary matters: Gateway ``progress=1`` means one percent, while a
    generic percent helper commonly interprets the numeric value 1 as 100%.
    """

    value = to_number(value, 0) / 100
    return round(min(max(value, 0), 1), 4)


def normalize_gateway_rating(value):
    """Convert Gateway ``newRating`` (0..100) to the Pro template's 0..10."""

    value = to_number(value, 0) / 10
    return round(min(max(value, 0), 10), 2)


def reading_status(marked_status, progress, reading_time):
    """Map the compatibility status to the Pro template's status names."""

    # WeRead does not always return ``finishTime`` for books that are already
    # at 100%.  Progress is therefore the reliable completion signal.
    if marked_status == 4 or to_number(progress, 0) >= 1:
        return "已读"
    if to_number(reading_time, 0) > 0 or to_number(progress, 0) > 0:
        return "在读"
    return "想读"


def preserve_completed_progress(existing, remote_progress):
    """Keep a confirmed completion when Gateway returns an empty progress row.

    Imported/local books occasionally have a valid Notion status but Gateway
    reports zero progress and zero reading time.  That response must not
    downgrade a book the user has already marked as completed.
    """

    if not existing:
        return remote_progress
    completed = existing.get("status") in {"已读", "读完"} or to_number(
        existing.get("progress"), 0
    ) >= 1
    if not completed:
        return remote_progress

    progress = dict(remote_progress or {})
    has_remote_reading = any(
        (
            to_number(progress.get("readingProgress"), 0) > 0,
            to_number(progress.get("readingTime"), 0) > 0,
            to_number(progress.get("finishedDate"), 0) > 0,
            to_number(progress.get("finishTime"), 0) > 0,
            str(progress.get("isStartReading") or "").strip().lower()
            in {"1", "true", "yes"},
        )
    )
    if has_remote_reading:
        return progress

    progress["readingProgress"] = 100
    progress["markedStatus"] = 4
    if existing.get("readingTime") is not None:
        progress["readingTime"] = existing.get("readingTime")
    return progress


def normalize_categories(value):
    if not value:
        return []
    if isinstance(value, str):
        value = [value]
    result = []
    for item in value:
        if isinstance(item, dict):
            name = item.get("title") or item.get("name") or item.get("categoryTitle")
        else:
            name = str(item)
        if name:
            name = name.strip()
            if name and name not in result:
                result.append(name)
    return result


def normalize_cover(value):
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        cover_type = value.get("type")
        nested = value.get(cover_type) if cover_type else None
        if isinstance(nested, dict):
            return nested.get("url") or ""
        return value.get("url") or ""
    return ""


def split_authors(value):
    if not value:
        return []
    if isinstance(value, (list, tuple)):
        values = value
    else:
        values = re.split(r"\s*(?:/|、|;|；|,|，)\s*", str(value))
    return [str(item).strip() for item in values if str(item).strip()]


def unwrap_book(value):
    if not isinstance(value, dict):
        return {}
    nested = value.get("book")
    return dict(nested) if isinstance(nested, dict) else dict(value)


@retry(stop_max_attempt_number=3, wait_fixed=3000)
def get_douban_url(isbn):
    """Best-effort ISBN lookup; synchronization must not depend on NeoDB."""
    if not isbn:
        return None
    params = {"query": isbn, "page": "1", "category": "book"}
    response = requests.get(
        "https://neodb.social/api/catalog/search", params=params, timeout=15
    )
    response.raise_for_status()
    books = response.json().get("data") or []
    for result in books:
        if result.get("isbn") != isbn:
            continue
        for resource in result.get("external_resources") or []:
            url = resource.get("url") or ""
            if url.startswith("https://book.douban.com"):
                return url
    return None


def build_archive_map(shelf):
    result = {}
    for archive in shelf.get("archive") or []:
        name = archive.get("name")
        if not name:
            continue
        result.update(
            {str(book_id): name for book_id in archive.get("bookIds") or []}
        )
    return result


def collect_books(shelf, notebooks):
    """Merge shelf and notebook records while retaining notebook sort cursors."""
    records = {}
    for item in shelf.get("books") or []:
        book = unwrap_book(item)
        book_id = book.get("bookId") or item.get("bookId")
        if not book_id:
            continue
        book_id = str(book_id)
        records[book_id] = {**book, "bookId": book_id}

    for item in notebooks or []:
        book = unwrap_book(item)
        book_id = item.get("bookId") or book.get("bookId")
        if not book_id:
            continue
        book_id = str(book_id)
        previous = records.get(book_id, {})
        records[book_id] = {
            **previous,
            **book,
            "bookId": book_id,
            "sort": item.get("sort") or previous.get("sort") or 0,
        }
    return records


def needs_sync(book_id, book, existing, archive_map, full_sync=False):
    if full_sync or not existing:
        return True
    if archive_map.get(book_id) != existing.get("category"):
        return True
    if not existing.get("cover"):
        return True

    # ``updateTime`` is the book metadata timestamp, not a reading cursor.
    # Comparing it with Notion's last-reading field would resync every unread
    # book on every run. Metadata can still be refreshed with FULL_SYNC.
    remote_update = normalize_timestamp(book.get("readUpdateTime"))
    local_update = normalize_timestamp(existing.get("lastReadingDate"))
    return bool(remote_update and (not local_update or remote_update > local_update))


def book_date(book):
    return normalize_timestamp(
        book.get("finishedDate")
        or book.get("activityDate")
        or book.get("lastReadingDate")
        or book.get("readUpdateTime")
        or book.get("readingBookDate")
    )


def insert_read_data(notion_helper, page_id, read_times):
    """Compatibility hook for legacy per-book daily details when available."""
    if not read_times or not notion_helper.read_database_id:
        return
    read_times = {int(key): value for key, value in sorted(read_times.items())}
    relation_filter = {"property": "书架", "relation": {"contains": page_id}}
    results = notion_helper.query_all_by_book(
        notion_helper.read_database_id, relation_filter
    )
    for result in results:
        timestamp = result.get("properties", {}).get("时间戳", {}).get("number")
        duration = result.get("properties", {}).get("时长", {}).get("number")
        if timestamp in read_times:
            value = read_times.pop(timestamp)
            if value != duration:
                insert_read_record(
                    notion_helper, result.get("id"), timestamp, value, page_id
                )
    for timestamp, duration in read_times.items():
        insert_read_record(notion_helper, None, timestamp, duration, page_id)


def insert_read_record(notion_helper, page_id, timestamp, duration, book_page_id):
    properties = {
        "标题": utils.get_title(
            pendulum.from_timestamp(timestamp, tz=tz).to_date_string()
        ),
        "日期": utils.get_date(
            start=pendulum.from_timestamp(timestamp, tz=tz).format(
                "YYYY-MM-DD HH:mm:ss"
            )
        ),
        "时长": utils.get_number(duration),
        "时间戳": utils.get_number(timestamp),
        "书架": utils.get_relation([book_page_id]),
    }
    if page_id:
        notion_helper.client.pages.update(page_id=page_id, properties=properties)
    else:
        notion_helper.create_page_in_database(
            notion_helper.read_database_id,
            properties,
            utils.get_icon("https://www.notion.so/icons/target_red.svg"),
        )


def legacy_book_properties(notion_helper, seed):
    """Map Pro's normalized book data to the original Reading Notes schema."""
    authors = split_authors(seed.get("author"))
    categories = normalize_categories(seed.get("categories") or seed.get("category"))
    values = {
        "BookName": seed.get("书名"),
        "BookId": seed.get("BookId"),
        "ISBN": seed.get("ISBN"),
        "链接": seed.get("链接"),
        "作者": ", ".join(authors),
        "Sort": seed.get("Sort"),
        "评分": seed.get("评分"),
        "ReadingTime": format_duration(seed.get("阅读时长")),
        "Progress": seed.get("阅读进度"),
        "Date": seed.get("时间"),
        "阅读分钟": round(to_number(seed.get("阅读时长"), 0) / 60, 2),
        # The moved legacy database has exactly these two options.
        "Status": "读完" if seed.get("阅读状态") == "已读" else "在读",
    }
    properties = utils.get_properties(
        {key: value for key, value in values.items() if key in notion_helper.property_dict},
        {
            "BookName": "title",
            "BookId": "rich_text",
            "ISBN": "rich_text",
            "链接": "url",
            "作者": "rich_text",
            "Sort": "number",
            "评分": "number",
            "ReadingTime": "rich_text",
            "Progress": "number",
            "Date": "date",
            "阅读分钟": "number",
            "Status": "select",
        },
    )
    cover = seed.get("封面")
    if "Cover" in notion_helper.property_dict and cover:
        properties["Cover"] = utils.get_properties(
            {"Cover": cover}, {"Cover": "files"}
        )["Cover"]
    if "Categories" in notion_helper.property_dict and categories:
        properties["Categories"] = {
            "multi_select": [{"name": category} for category in categories]
        }
    return properties


def sync_book(notion_helper, weread_api, notion_books, archive_map, books, index, book_id):
    existing = notion_books.get(book_id)
    seed = {
        key: value
        for key, value in (existing or {}).items()
        if value is not None
    }
    seed.update(books[book_id])
    if existing:
        if existing.get("myRating"):
            seed["我的评分"] = existing.get("myRating")
        if existing.get("douban_url"):
            seed["豆瓣链接"] = existing.get("douban_url")

    detail = unwrap_book(weread_api.get_bookinfo(book_id))
    # An incomplete metadata response must not erase richer shelf or existing
    # Notion values. Numeric zero remains meaningful (for example newRating).
    seed.update(
        {
            key: value
            for key, value in detail.items()
            if value is not None and value != "" and value != [] and value != {}
        }
    )
    progress = preserve_completed_progress(
        existing, weread_api.get_read_info(book_id) or {}
    )
    seed.update({key: value for key, value in progress.items() if value is not None})

    seed["书架分类"] = archive_map.get(book_id)
    seed["阅读进度"] = normalize_gateway_progress(seed.get("readingProgress"))
    marked_status = seed.get("markedStatus")
    status = reading_status(
        marked_status, seed["阅读进度"], seed.get("readingTime")
    )
    seed["阅读状态"] = status
    seed["阅读时长"] = to_number(seed.get("readingTime"), 0)
    if seed.get("totalReadDay") is not None:
        seed["阅读天数"] = to_number(seed.get("totalReadDay"), 0)
    seed["评分"] = normalize_gateway_rating(seed.get("newRating"))

    rating_detail = seed.get("newRatingDetail") or {}
    if rating_detail.get("myRating"):
        seed["我的评分"] = RATING.get(rating_detail.get("myRating"))
    elif status == "已读" and not seed.get("我的评分"):
        seed["我的评分"] = "未评分"

    seed["时间"] = book_date(seed)
    seed["开始阅读时间"] = normalize_timestamp(seed.get("beginReadingDate"))
    seed["最后阅读时间"] = normalize_timestamp(
        seed.get("lastReadingDate")
        or seed.get("activityDate")
        or seed.get("readUpdateTime")
    )

    cover = normalize_cover(seed.get("cover")).replace("/s_", "/t7_")
    if not cover.startswith("http"):
        cover = BOOK_ICON_URL

    title = seed.get("title") or seed.get("书名") or f"微信读书 {book_id}"
    seed["书名"] = title
    seed["BookId"] = book_id
    seed["ISBN"] = seed.get("isbn") or ""
    seed["链接"] = seed.get("deepLink") or utils.get_weread_url(book_id)
    seed["简介"] = seed.get("intro") or ""
    # Sort is the notes cursor. Keep the remote notebook cursor for newly
    # imported books; weread.py advances it only after the page blocks and
    # auxiliary note databases have been updated successfully.
    remote_sort = to_number(books[book_id].get("sort"), 0)
    existing_sort = to_number((existing or {}).get("Sort"), 0)
    seed["Sort"] = existing_sort or remote_sort
    seed["封面"] = cover

    authors = split_authors(seed.get("author"))
    if authors and notion_helper.author_database_id:
        seed["作者"] = [
            notion_helper.get_relation_id(
                author, notion_helper.author_database_id, USER_ICON_URL
            )
            for author in authors
        ]
    categories = normalize_categories(seed.get("categories") or seed.get("category"))
    if categories and notion_helper.category_database_id:
        seed["分类"] = [
            notion_helper.get_relation_id(
                category, notion_helper.category_database_id, TAG_ICON_URL
            )
            for category in categories
        ]

    if not existing and seed["ISBN"]:
        try:
            seed["豆瓣链接"] = get_douban_url(seed["ISBN"])
        except Exception as error:
            print(f"NeoDB 查询失败，继续同步《{title}》: {error}")

    if notion_helper.legacy_book_database:
        properties = legacy_book_properties(notion_helper, seed)
    else:
        properties = utils.get_properties(seed, book_properties_type_dict)
    if existing and existing.get("category") and book_id not in archive_map:
        properties["书架分类"] = {"select": None}
    if seed.get("时间"):
        notion_helper.get_date_relation(
            properties, pendulum.from_timestamp(seed["时间"], tz=tz)
        )

    print(f"正在同步《{title}》，共 {len(books)} 本，当前第 {index + 1} 本。")
    icon = utils.get_icon(cover)
    if existing:
        result = notion_helper.update_page(
            page_id=existing.get("pageId"), properties=properties, cover=icon
        )
    else:
        result = notion_helper.create_book_page(properties=properties, icon=icon)

    read_detail = seed.get("readDetail") or {}
    data = read_detail.get("data") or []
    if data:
        insert_read_data(
            notion_helper,
            result.get("id"),
            {item.get("readDate"): item.get("readTime") for item in data},
        )


def main():
    weread_api = WeReadApi()
    notion_helper = NotionHelper()
    notion_books = notion_helper.get_all_book()
    shelf = weread_api.get_bookshelf()
    notebooks = weread_api.get_notebooklist()
    archive_map = build_archive_map(shelf)
    books = collect_books(shelf, notebooks)
    full_sync = os.getenv("FULL_SYNC", "").lower() in {"1", "true", "yes"}

    selected = [
        book_id
        for book_id, book in books.items()
        if needs_sync(
            book_id,
            book,
            notion_books.get(book_id),
            archive_map,
            full_sync=full_sync,
        )
    ]
    print(f"书架共 {len(books)} 本，本次需更新 {len(selected)} 本。")
    selected_books = {book_id: books[book_id] for book_id in selected}
    for index, book_id in enumerate(selected):
        try:
            sync_book(
                notion_helper,
                weread_api,
                notion_books,
                archive_map,
                selected_books,
                index,
                book_id,
            )
        except Exception as error:
            title = books[book_id].get("title") or book_id
            print(f"同步《{title}》失败，跳过本次，继续后续书籍：{error}")


if __name__ == "__main__":
    main()
