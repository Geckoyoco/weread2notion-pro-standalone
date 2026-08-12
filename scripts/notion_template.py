"""Create a self-owned WeRead2Notion Pro workspace below a system-data page.

The original public Pro template is no longer available.  This module keeps
the repository self-contained by creating the databases and relations that
the sync code needs.  Creation is name-based and idempotent, so a run that was
interrupted by a Notion rate limit can safely be started again.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from retrying import retry

DATABASE_ICONS = {
    "书架": "📚",
    "读书笔记": "💭",
    "划线": "🔖",
    "章节": "📑",
    "作者": "👤",
    "分类": "🏷️",
    "阅读记录": "⏱️",
    "日": "📅",
    "周": "🗓️",
    "月": "📆",
    "年": "🎯",
}

def _relation(data_source_id: str) -> dict[str, Any]:
    return {
        "relation": {
            "data_source_id": data_source_id,
            "single_property": {},
        }
    }


def _text(content: str) -> list[dict[str, Any]]:
    return [{"type": "text", "text": {"content": content}}]


def _status() -> dict[str, Any]:
    return {
        "status": {
            "options": [
                {"name": "想读", "color": "gray", "group": "To-do"},
                {"name": "在读", "color": "blue", "group": "In progress"},
                {"name": "已读", "color": "green", "group": "Complete"},
            ]
        }
    }


@dataclass(frozen=True)
class DatabaseRef:
    database_id: str
    data_source_id: str


class ProTemplateBuilder:
    """Build the functional Pro schema below one Notion parent page."""

    def __init__(
        self,
        client,
        page_id: str,
        existing_databases: dict[str, str] | None = None,
        *,
        book_name: str = "书架",
        chapter_name: str = "章节",
        read_name: str = "阅读记录",
        system_page_id: str | None = None,
    ):
        self.client = client
        self.page_id = page_id
        self.existing_databases = dict(existing_databases or {})
        self.book_name = book_name
        self.chapter_name = chapter_name
        self.read_name = read_name
        self.system_page_id = system_page_id
        self.refs: dict[str, DatabaseRef] = {}

    def _source_for_database(self, database_id: str) -> str:
        response = self.client.request(
            path=f"databases/{database_id}", method="GET"
        )
        sources = response.get("data_sources") or []
        if not sources or not sources[0].get("id"):
            raise Exception(f"Notion 数据库 {database_id} 没有可用的数据源")
        return sources[0]["id"]

    def _create_database(
        self, name: str, properties: dict[str, Any]
    ) -> DatabaseRef:
        print(f"正在创建 Notion 数据库：{name}")
        response = self.client.request(
            path="databases",
            method="POST",
            body={
                "parent": {
                    "type": "page_id",
                    "page_id": self.system_page_id or self.page_id,
                },
                "title": _text(name),
                "is_inline": True,
                "icon": {
                    "type": "emoji",
                    "emoji": DATABASE_ICONS.get(name, "📚"),
                },
                "initial_data_source": {"properties": properties},
            },
        )
        database_id = response.get("id")
        sources = response.get("data_sources") or []
        source_id = sources[0].get("id") if sources else None
        if not database_id or not source_id:
            raise Exception(f"Notion 创建数据库 {name} 后没有返回完整 ID")
        time.sleep(0.35)
        return DatabaseRef(database_id, source_id)

    def _ensure(self, name: str, properties: dict[str, Any]) -> DatabaseRef:
        if name in self.refs:
            return self.refs[name]
        database_id = self.existing_databases.get(name)
        if database_id:
            ref = DatabaseRef(database_id, self._source_for_database(database_id))
        else:
            ref = self._create_database(name, properties)
            self.existing_databases[name] = ref.database_id
        self.refs[name] = ref
        return ref

    @retry(stop_max_attempt_number=3, wait_fixed=1200)
    def _add_properties(self, ref: DatabaseRef, properties: dict[str, Any]) -> None:
        self.client.request(
            path=f"data_sources/{ref.data_source_id}",
            method="PATCH",
            body={"properties": properties},
        )

    @staticmethod
    def _period_properties() -> dict[str, Any]:
        # ``时长`` is a writable aggregate for pages created without the old
        # template rollups. read_time.py detects this number field and updates
        # it, while continuing to leave legacy rollup fields untouched.
        return {"标题": {"title": {}}, "日期": {"date": {}}, "时长": {"number": {}}}

    @staticmethod
    def _dated_note_properties() -> dict[str, Any]:
        return {
            "Name": {"title": {}},
            "bookId": {"rich_text": {}},
            "blockId": {"rich_text": {}},
            "chapterUid": {"number": {}},
            "bookVersion": {"number": {}},
            "type": {"number": {}},
            "Date": {"date": {}},
        }

    def ensure(self) -> dict[str, DatabaseRef]:
        # Backend databases must never be created inline on the user dashboard.
        # Existing databases are reused wherever they currently live; only new
        # databases are placed below the dedicated system-data page.
        if not self.system_page_id:
            raise Exception(
                "缺少 NOTION_SYSTEM_PAGE；为避免污染主页面，后台数据库不会创建。"
            )
        year = self._ensure("年", self._period_properties())
        month = self._ensure("月", self._period_properties())
        week = self._ensure("周", self._period_properties())
        day = self._ensure(
            "日",
            {
                "标题": {"title": {}},
                "日期": {"date": {}},
                "时长": {"number": {}},
                "时间戳": {"number": {}},
                "年": _relation(year.data_source_id),
                "月": _relation(month.data_source_id),
                "周": _relation(week.data_source_id),
            },
        )
        author = self._ensure("作者", {"标题": {"title": {}}})
        category = self._ensure("分类", {"标题": {"title": {}}})

        review_properties = self._dated_note_properties()
        review_properties.update(
            {
                "reviewId": {"rich_text": {}},
                "range": {"rich_text": {}},
                "star": {"number": {}},
                "abstract": {"rich_text": {}},
            }
        )
        review = self._ensure("读书笔记", review_properties)

        bookmark_properties = self._dated_note_properties()
        bookmark_properties.update(
            {
                "range": {"rich_text": {}},
                "bookmarkId": {"rich_text": {}},
                "colorStyle": {"number": {}},
                "style": {"number": {}},
            }
        )
        bookmark = self._ensure("划线", bookmark_properties)
        chapter = self._ensure(
            self.chapter_name,
            {
                "Name": {"title": {}},
                "blockId": {"rich_text": {}},
                "chapterUid": {"number": {}},
                "chapterIdx": {"number": {}},
                "readAhead": {"number": {}},
                "updateTime": {"number": {}},
                "level": {"number": {}},
            },
        )
        read = self._ensure(
            self.read_name,
            {
                "标题": {"title": {}},
                "时长": {"number": {}},
                "时间戳": {"number": {}},
                "日期": {"date": {}},
            },
        )

        book = self._ensure(
            self.book_name,
            {
                "书名": {"title": {}},
                "BookId": {"rich_text": {}},
                "ISBN": {"rich_text": {}},
                "链接": {"url": {}},
                "作者": _relation(author.data_source_id),
                "Sort": {"number": {}},
                "评分": {"number": {}},
                "封面": {"files": {}},
                "分类": _relation(category.data_source_id),
                "阅读状态": _status(),
                "阅读时长": {"number": {}},
                "阅读进度": {"number": {"format": "percent"}},
                "阅读天数": {"number": {}},
                "时间": {"date": {}},
                "开始阅读时间": {"date": {}},
                "最后阅读时间": {"date": {}},
                "简介": {"rich_text": {}},
                "书架分类": {"select": {}},
                "我的评分": {
                    "select": {
                        "options": [
                            {"name": "未评分", "color": "gray"},
                            {"name": "⭐️", "color": "red"},
                            {"name": "⭐️⭐️", "color": "orange"},
                            {"name": "⭐️⭐️⭐️", "color": "yellow"},
                            {"name": "⭐️⭐️⭐️⭐️", "color": "blue"},
                            {"name": "⭐️⭐️⭐️⭐️⭐️", "color": "green"},
                        ]
                    }
                },
                "豆瓣链接": {"url": {}},
                "豆瓣短评": {"rich_text": {}},
                "读书笔记": _relation(review.data_source_id),
                "划线": _relation(bookmark.data_source_id),
                "章节": _relation(chapter.data_source_id),
                "阅读记录": _relation(read.data_source_id),
                "日": _relation(day.data_source_id),
                "周": _relation(week.data_source_id),
                "月": _relation(month.data_source_id),
                "年": _relation(year.data_source_id),
            },
        )

        date_relations = {
            "日": _relation(day.data_source_id),
            "周": _relation(week.data_source_id),
            "月": _relation(month.data_source_id),
            "年": _relation(year.data_source_id),
        }
        self._add_properties(
            review, {"书籍": _relation(book.data_source_id), **date_relations}
        )
        self._add_properties(
            bookmark, {"书籍": _relation(book.data_source_id), **date_relations}
        )
        self._add_properties(chapter, {"书籍": _relation(book.data_source_id)})
        self._add_properties(read, {"书架": _relation(book.data_source_id)})

        print("Notion Pro 数据库结构已准备完成。")
        return dict(self.refs)


def ensure_pro_template(
    client,
    page_id: str,
    existing_databases: dict[str, str] | None = None,
    **names,
) -> dict[str, DatabaseRef]:
    return ProTemplateBuilder(
        client, page_id, existing_databases, **names
    ).ensure()
