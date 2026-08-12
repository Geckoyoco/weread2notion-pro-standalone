import logging
import os
import re
import time
from urllib.parse import urlparse

import pendulum
from notion_client import Client
from retrying import retry
from dotenv import load_dotenv
try:
    from .notion_template import ensure_pro_template
except ImportError:
    from notion_template import ensure_pro_template
from utils import (
    format_date,
    get_date,
    get_first_and_last_day_of_month,
    get_first_and_last_day_of_week,
    get_first_and_last_day_of_year,
    get_icon,
    get_number,
    get_relation,
    get_rich_text,
    get_title,
    timestamp_to_date,
    get_property_value,
)

load_dotenv()
TAG_ICON_URL = "https://www.notion.so/icons/tag_gray.svg"
USER_ICON_URL = "https://www.notion.so/icons/user-circle-filled_gray.svg"
TARGET_ICON_URL = "https://www.notion.so/icons/target_red.svg"
BOOKMARK_ICON_URL = "https://www.notion.so/icons/bookmark_gray.svg"
NOTION_VERSION = "2026-03-11"


class NotionHelper:
    database_name_dict = {
        "BOOK_DATABASE_NAME": "书架",
        "CHAPTER_DATABASE_NAME": "章节",
        "READ_DATABASE_NAME": "阅读记录",
    }
    database_id_dict = {}
    heatmap_block_id = None
    property_dict = {}

    def __init__(self):
        notion_token = (os.getenv("NOTION_TOKEN") or "").strip()
        if not notion_token:
            raise Exception(
                "没有找到 NOTION_TOKEN，请在 GitHub Secret 中配置 Notion Integration Token"
            )
        self.client = Client(
            auth=notion_token,
            log_level=logging.ERROR,
            notion_version=NOTION_VERSION,
        )
        self.__cache = {}
        self._data_source_id_cache = {}
        self._data_source_schema_cache = {}
        self._searched_block_ids = set()
        self._heatmap_block_type = None
        # These used to be class-level mutable dictionaries.  Keep the public
        # attributes, but isolate every helper instance so tests and multiple
        # syncs in one process cannot leak state into each other.
        self.database_name_dict = dict(type(self).database_name_dict)
        self.database_id_dict = {}
        self.property_dict = {}
        self.heatmap_block_id = (os.getenv("HEATMAP_BLOCK_ID") or "").strip() or None
        self.page_id = self.extract_page_id(os.getenv("NOTION_PAGE"))
        system_page = (os.getenv("NOTION_SYSTEM_PAGE") or "").strip()
        self.system_page_id = self.extract_page_id(system_page) if system_page else None
        for key in self.database_name_dict.keys():
            value = os.getenv(key)
            if value:
                self.database_name_dict[key] = value
        self.search_database(self.page_id)
        if self.system_page_id and self.system_page_id != self.page_id:
            self.search_database(self.system_page_id)
        book_name = self.database_name_dict.get("BOOK_DATABASE_NAME")
        if not self.database_id_dict.get(book_name):
            normalized_book_name = (book_name or "").replace("📖", "").strip()
            for discovered_name, discovered_id in self.database_id_dict.items():
                if discovered_name.replace("📖", "").strip() == normalized_book_name:
                    self.database_id_dict[book_name] = discovered_id
                    break
        initialize_requested = os.getenv(
            "INITIALIZE_NOTION_TEMPLATE", ""
        ).lower() in {"1", "true", "yes"}
        if not self.database_id_dict.get(book_name):
            if not self.database_id_dict.get(book_name):
                print(
                    "没有检测到旧 Pro 模板，开始在指定页面自动创建自有 Pro 数据库。"
                )
            created = ensure_pro_template(
                self.client,
                self.page_id,
                self.database_id_dict,
                book_name=book_name,
                chapter_name=self.database_name_dict.get("CHAPTER_DATABASE_NAME"),
                read_name=self.database_name_dict.get("READ_DATABASE_NAME"),
                system_page_id=self.system_page_id,
            )
            self.database_id_dict.update(
                {name: ref.database_id for name, ref in created.items()}
            )
        elif initialize_requested:
            print("已找到书籍主库，仅验证现有结构，不再创建后台数据库。")
        self.book_database_id = self.resolve_data_source_id(
            self.database_id_dict.get(
                self.database_name_dict.get("BOOK_DATABASE_NAME")
            )
        )
        if not self.book_database_id:
            raise Exception(
                f"没有找到 Notion 数据库：{self.database_name_dict.get('BOOK_DATABASE_NAME')}"
            )
        self.property_dict.update(
            self.retrieve_data_source(self.book_database_id).get("properties") or {}
        )
        # The original Reading Notes database is intentionally supported as
        # the single source of truth. It has book fields but no Pro relation
        # fields because highlights are stored in each book page instead of a
        # separate database.
        self.legacy_book_database = (
            "BookId" in self.property_dict
            and "读书笔记" not in self.property_dict
        )
        if self.legacy_book_database:
            self.review_database_id = None
            self.bookmark_database_id = None
            self.day_database_id = self.resolve_data_source_id(
                self.database_id_dict.get("日")
            )
            self.week_database_id = self.resolve_data_source_id(
                self.database_id_dict.get("周")
            )
            self.month_database_id = self.resolve_data_source_id(
                self.database_id_dict.get("月")
            )
            self.year_database_id = self.resolve_data_source_id(
                self.database_id_dict.get("年")
            )
            self.category_database_id = None
            self.author_database_id = None
            self.chapter_database_id = None
            self.read_database_id = None
            print("检测到旧版 Reading Notes，使用它作为唯一书库，跳过独立划线同步。")
            return
        self.review_database_id = self.get_relation_database_id(
            self.property_dict.get("读书笔记")
        )
        self.bookmark_database_id = self.get_relation_database_id(
            self.property_dict.get("划线")
        )
        self.day_database_id = self.get_relation_database_id(
            self.property_dict.get("日")
        )
        self.week_database_id = self.get_relation_database_id(
            self.property_dict.get("周")
        )
        self.month_database_id = self.get_relation_database_id(
            self.property_dict.get("月")
        )
        self.year_database_id = self.get_relation_database_id(
            self.property_dict.get("年")
        )
        self.category_database_id = self.get_relation_database_id(
            self.property_dict.get("分类")
        )
        self.author_database_id = self.get_relation_database_id(
            self.property_dict.get("作者")
        )
        if "章节" in self.property_dict:
            self.chapter_database_id = self.get_relation_database_id(
                self.property_dict.get("章节")
            )
        else:
            self.chapter_database_id = self.resolve_data_source_id(
                self.database_id_dict.get(
                    self.database_name_dict.get("CHAPTER_DATABASE_NAME")
                )
            )
        if "阅读记录" in self.property_dict:
            self.read_database_id = self.get_relation_database_id(
                self.property_dict.get("阅读记录")
            )
        else:
            self.read_database_id = self.resolve_data_source_id(
                self.database_id_dict.get(
                    self.database_name_dict.get("READ_DATABASE_NAME")
                )
            )
        required_relations = {
            "读书笔记": self.review_database_id,
            "划线": self.bookmark_database_id,
            "日": self.day_database_id,
            "周": self.week_database_id,
            "月": self.month_database_id,
            "年": self.year_database_id,
            "分类": self.category_database_id,
            "作者": self.author_database_id,
            "章节": self.chapter_database_id,
        }
        missing_relations = [
            name for name, data_source_id in required_relations.items() if not data_source_id
        ]
        if missing_relations:
            raise Exception(
                "Notion 页面不是兼容的 Pro 模板，缺少数据库或关系属性："
                + "、".join(missing_relations)
            )
        self.update_book_database()
        if self.read_database_id is None:
            self.create_database()

    @staticmethod
    def _exception_code(error):
        code = getattr(error, "code", None)
        return getattr(code, "value", code)

    @classmethod
    def _can_retry_as_database_id(cls, error):
        """Return whether a failed data-source lookup may have received a DB id."""
        code = cls._exception_code(error)
        status = getattr(error, "status", None)
        return code in {"object_not_found", "validation_error"} or status in {
            400,
            404,
        }

    def resolve_data_source_id(self, database_id):
        """Resolve either a legacy database ID or a data-source ID.

        Public attribute names intentionally retain their historical
        ``*_database_id`` spelling.  Their values are data-source IDs after
        initialization, which keeps old callers working while all row queries
        and page creates use the post-2025 API model.
        """
        if not database_id:
            return None
        if database_id in self._data_source_id_cache:
            return self._data_source_id_cache[database_id]

        try:
            response = self.client.request(
                path=f"data_sources/{database_id}", method="GET"
            )
        except Exception as error:
            if not self._can_retry_as_database_id(error):
                raise
        else:
            data_source_id = response.get("id") or database_id
            self._data_source_id_cache[database_id] = data_source_id
            self._data_source_id_cache[data_source_id] = data_source_id
            self._data_source_schema_cache[data_source_id] = response
            return data_source_id

        database = self.client.request(
            path=f"databases/{database_id}", method="GET"
        )
        data_sources = database.get("data_sources") or []
        if not data_sources:
            raise Exception(f"Notion 数据库 {database_id} 下没有可用的 data source")
        data_source_id = data_sources[0].get("id")
        if not data_source_id:
            raise Exception(f"Notion 数据库 {database_id} 返回了无效的 data source")
        if len(data_sources) > 1:
            print(
                f"Notion 数据库 {database_id} 包含 {len(data_sources)} 个 data sources，"
                f"默认使用第一个：{data_source_id}"
            )
        self._data_source_id_cache[database_id] = data_source_id
        self._data_source_id_cache[data_source_id] = data_source_id
        return data_source_id

    def retrieve_data_source(self, database_id, refresh=False):
        data_source_id = self.resolve_data_source_id(database_id)
        if not data_source_id:
            raise Exception("Notion data source ID 不能为空")
        if not refresh and data_source_id in self._data_source_schema_cache:
            return self._data_source_schema_cache[data_source_id]
        response = self.client.request(
            path=f"data_sources/{data_source_id}", method="GET"
        )
        self._data_source_schema_cache[data_source_id] = response
        return response

    def get_relation_database_id(self, property):
        if not isinstance(property, dict):
            return None
        relation = property.get("relation") or {}
        related_id = relation.get("data_source_id") or relation.get("database_id")
        return self.resolve_data_source_id(related_id)

    def extract_page_id(self, notion_url):
        # 正则表达式匹配 32 个字符的 Notion page_id
        if not isinstance(notion_url, str) or not notion_url.strip():
            raise Exception("没有找到 NOTION_PAGE，请在 GitHub Secret 中配置 Notion 页面 URL")
        match = re.search(
            r"([a-f0-9]{32}|[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12})",
            notion_url,
        )
        if match:
            return match.group(0)
        else:
            raise Exception(f"获取NotionID失败，请检查输入的Url是否正确")

    @staticmethod
    def _block_url(block):
        block_type = block.get("type")
        value = block.get(block_type) or {}
        if block_type == "embed":
            return value.get("url")
        if block_type == "image":
            image_type = value.get("type")
            image_value = value.get(image_type) or {}
            return image_value.get("url")
        return None

    @staticmethod
    def _is_heatmap_url(url):
        if not isinstance(url, str):
            return False
        parsed = urlparse(url)
        if parsed.scheme not in {"http", "https"}:
            return False
        normalized = url.lower()
        return (
            "heatmap" in normalized
            or "/out_folder/" in normalized
            or "/title-inline/" in normalized
        )

    def search_database(self, block_id):
        """Recursively discover child databases and the optional heatmap block."""
        if not block_id or block_id in self._searched_block_ids:
            return
        self._searched_block_ids.add(block_id)
        for child in self.get_block_children(block_id):
            child_type = child.get("type")
            if child_type == "child_database":
                title = (child.get("child_database") or {}).get("title")
                if title and child.get("id"):
                    self.database_id_dict[title] = child.get("id")
            elif self.heatmap_block_id is None and child_type in {"embed", "image"}:
                if self._is_heatmap_url(self._block_url(child)):
                    self.heatmap_block_id = child.get("id")
                    self._heatmap_block_type = child_type
            if child.get("has_children") and child.get("id"):
                self.search_database(child.get("id"))

    def update_book_database(self):
        """更新数据库"""
        response = self.retrieve_data_source(self.book_database_id, refresh=True)
        data_source_id = response.get("id") or self.book_database_id
        properties = response.get("properties") or {}
        update_properties = {}
        if (
            properties.get("阅读时长") is None
            or properties.get("阅读时长").get("type") != "number"
        ):
            update_properties["阅读时长"] = {"number": {}}
        if (
            properties.get("书架分类") is None
            or properties.get("书架分类").get("type") != "select"
        ):
            update_properties["书架分类"] = {"select": {}}
        if (
            properties.get("豆瓣链接") is None
            or properties.get("豆瓣链接").get("type") != "url"
        ):
            update_properties["豆瓣链接"] = {"url": {}}
        if (
            properties.get("我的评分") is None
            or properties.get("我的评分").get("type") != "select"
        ):
            update_properties["我的评分"] = {"select": {}}
        if (
            properties.get("豆瓣短评") is None
            or properties.get("豆瓣短评").get("type") != "rich_text"
        ):
            update_properties["豆瓣短评"] = {"rich_text": {}}
        """NeoDB先不添加了，现在受众还不广，可能有的小伙伴不知道是干什么的"""
        # if properties.get("NeoDB链接") is None or properties.get("NeoDB链接").get("type") != "url":
        #     update_properties["NeoDB链接"] = {"url": {}}
        if len(update_properties) > 0:
            response = self.client.request(
                path=f"data_sources/{data_source_id}",
                method="PATCH",
                body={"properties": update_properties},
            )
            merged_properties = dict(properties)
            merged_properties.update(response.get("properties") or update_properties)
            cached_response = dict(response)
            cached_response["properties"] = merged_properties
            self._data_source_schema_cache[data_source_id] = cached_response
            self.property_dict.update(merged_properties)

    def create_database(self):
        title = [
            {
                "type": "text",
                "text": {
                    "content": self.database_name_dict.get("READ_DATABASE_NAME"),
                },
            },
        ]
        properties = {
            "标题": {"title": {}},
            "时长": {"number": {}},
            "时间戳": {"number": {}},
            "日期": {"date": {}},
            "书架": {
                "relation": {
                    "data_source_id": self.book_database_id,
                    "single_property": {},
                }
            },
        }
        if not self.system_page_id:
            raise Exception(
                "缺少 NOTION_SYSTEM_PAGE；为避免污染主页面，阅读记录数据库不会创建。"
            )
        parent = {"page_id": self.system_page_id, "type": "page_id"}
        response = self.client.request(
            path="databases",
            method="POST",
            body={
                "parent": parent,
                "title": title,
                "icon": get_icon("https://www.notion.so/icons/target_gray.svg"),
                "initial_data_source": {"properties": properties},
            },
        )
        database_id = response.get("id")
        data_sources = response.get("data_sources") or []
        if data_sources and data_sources[0].get("id"):
            self.read_database_id = data_sources[0].get("id")
            self._data_source_id_cache[database_id] = self.read_database_id
            self._data_source_id_cache[self.read_database_id] = self.read_database_id
        else:
            self.read_database_id = self.resolve_data_source_id(database_id)
        self.database_id_dict[
            self.database_name_dict.get("READ_DATABASE_NAME")
        ] = database_id

    def update_heatmap(self, block_id, url):
        block_type = self._heatmap_block_type
        if block_id != self.heatmap_block_id or block_type not in {"embed", "image"}:
            block = self.client.blocks.retrieve(block_id=block_id)
            block_type = block.get("type")
        if block_type == "image":
            return self.client.blocks.update(
                block_id=block_id,
                image={"type": "external", "external": {"url": url}},
            )
        if block_type == "embed":
            return self.client.blocks.update(block_id=block_id, embed={"url": url})
        raise Exception(
            f"热力图 block {block_id} 的类型是 {block_type!r}，仅支持 embed 或 image"
        )

    def create_heatmap(self, url):
        """Append the heatmap using the block type required by its URL."""
        is_dashboard = "/title-inline/" in url.lower()
        block = (
            {"object": "block", "type": "embed", "embed": {"url": url}}
            if is_dashboard
            else {
                "object": "block",
                "type": "image",
                "image": {
                    "type": "external",
                    "external": {"url": url},
                },
            }
        )
        response = self.append_blocks(
            self.page_id,
            [block],
        )
        results = response.get("results") or []
        if not results or not results[0].get("id"):
            raise Exception("Notion 创建热力图图片块后没有返回 block ID")
        self.heatmap_block_id = results[0]["id"]
        self._heatmap_block_type = block["type"]
        return self.heatmap_block_id

    def get_week_relation_id(self, date):
        year = date.isocalendar().year
        week = date.isocalendar().week
        week = f"{year}年第{week}周"
        start, end = get_first_and_last_day_of_week(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            week, self.week_database_id, TARGET_ICON_URL, properties
        )

    def get_month_relation_id(self, date):
        month = date.strftime("%Y年%-m月")
        start, end = get_first_and_last_day_of_month(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            month, self.month_database_id, TARGET_ICON_URL, properties
        )

    def get_year_relation_id(self, date):
        year = date.strftime("%Y")
        start, end = get_first_and_last_day_of_year(date)
        properties = {"日期": get_date(format_date(start), format_date(end))}
        return self.get_relation_id(
            year, self.year_database_id, TARGET_ICON_URL, properties
        )

    def get_day_relation_id(self, date):
        if getattr(date, "tzinfo", None) is None:
            local_date = pendulum.instance(date, tz="Asia/Shanghai")
        else:
            local_date = pendulum.instance(date).in_timezone("Asia/Shanghai")
        new_date = local_date.start_of("day")
        timestamp = new_date.int_timestamp
        day = new_date.strftime("%Y年%m月%d日")
        properties = {
            "日期": get_date(format_date(date)),
            "时间戳": get_number(timestamp),
        }
        properties["年"] = get_relation(
            [
                self.get_year_relation_id(new_date),
            ]
        )
        properties["月"] = get_relation(
            [
                self.get_month_relation_id(new_date),
            ]
        )
        properties["周"] = get_relation(
            [
                self.get_week_relation_id(new_date),
            ]
        )
        return self.get_relation_id(
            day, self.day_database_id, TARGET_ICON_URL, properties
        )

    def get_relation_id(self, name, id, icon, properties=None):
        key = f"{id}{name}"
        if key in self.__cache:
            return self.__cache.get(key)
        filter = {"property": "标题", "title": {"equals": name}}
        response = self.query(database_id=id, filter=filter)
        results = response.get("results") or []
        if len(results) == 0:
            properties = dict(properties or {})
            properties["标题"] = get_title(name)
            page_id = self.create_page_in_database(
                id, properties, get_icon(icon)
            ).get("id")
        else:
            page_id = results[0].get("id")
        self.__cache[key] = page_id
        return page_id

    def insert_bookmark(self, id, bookmark):
        icon = get_icon(BOOKMARK_ICON_URL)
        properties = {
            "Name": get_title(bookmark.get("markText", "")),
            "bookId": get_rich_text(bookmark.get("bookId")),
            "range": get_rich_text(bookmark.get("range")),
            "bookmarkId": get_rich_text(bookmark.get("bookmarkId")),
            "blockId": get_rich_text(bookmark.get("blockId")),
            "chapterUid": get_number(bookmark.get("chapterUid")),
            "bookVersion": get_number(bookmark.get("bookVersion")),
            "colorStyle": get_number(bookmark.get("colorStyle")),
            "type": get_number(bookmark.get("type")),
            "style": get_number(bookmark.get("style")),
            "书籍": get_relation([id]),
        }
        if "createTime" in bookmark:
            create_time = timestamp_to_date(int(bookmark.get("createTime")))
            properties["Date"] = get_date(create_time.strftime("%Y-%m-%d %H:%M:%S"))
            self.get_date_relation(properties, create_time)
        self.create_page_in_database(self.bookmark_database_id, properties, icon)

    def insert_review(self, id, review):
        time.sleep(0.1)
        icon = get_icon(TAG_ICON_URL)
        properties = {
            "Name": get_title(review.get("content", "")),
            "bookId": get_rich_text(review.get("bookId")),
            "reviewId": get_rich_text(review.get("reviewId")),
            "blockId": get_rich_text(review.get("blockId")),
            "chapterUid": get_number(review.get("chapterUid")),
            "bookVersion": get_number(review.get("bookVersion")),
            "type": get_number(review.get("type")),
            "书籍": get_relation([id]),
        }
        if "range" in review:
            properties["range"] = get_rich_text(review.get("range"))
        if "star" in review:
            properties["star"] = get_number(review.get("star"))
        if "abstract" in review:
            properties["abstract"] = get_rich_text(review.get("abstract"))
        if "createTime" in review:
            create_time = timestamp_to_date(int(review.get("createTime")))
            properties["Date"] = get_date(create_time.strftime("%Y-%m-%d %H:%M:%S"))
            self.get_date_relation(properties, create_time)
        self.create_page_in_database(self.review_database_id, properties, icon)

    def insert_chapter(self, id, chapter):
        time.sleep(0.1)
        icon = {"type": "external", "external": {"url": TAG_ICON_URL}}
        properties = {
            "Name": get_title(chapter.get("title")),
            "blockId": get_rich_text(chapter.get("blockId")),
            "chapterUid": {"number": chapter.get("chapterUid")},
            "chapterIdx": {"number": chapter.get("chapterIdx")},
            "readAhead": {"number": chapter.get("readAhead")},
            "updateTime": {"number": chapter.get("updateTime")},
            "level": {"number": chapter.get("level")},
            "书籍": {"relation": [{"id": id}]},
        }
        self.create_page_in_database(self.chapter_database_id, properties, icon)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_book_page(self, page_id, properties):
        return self.client.pages.update(page_id=page_id, properties=properties)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def update_page(self, page_id, properties, cover):
        return self.client.pages.update(
            page_id=page_id, properties=properties, cover=cover
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def create_page(self, parent, properties, icon):
        parent = self._normalize_page_parent(parent)
        return self.client.pages.create(parent=parent, properties=properties, icon=icon)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def create_page_in_database(
        self, database_id, properties, icon=None, cover=None
    ):
        parent = self._data_source_parent(database_id)
        kwargs = {"parent": parent, "properties": properties}
        if icon is not None:
            kwargs["icon"] = icon
        if cover is not None:
            kwargs["cover"] = cover
        return self.client.pages.create(**kwargs)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def create_book_page(self, parent=None, properties=None, icon=None):
        if properties is None:
            raise TypeError("properties is required")
        parent = self._normalize_page_parent(
            parent or self._data_source_parent(self.book_database_id)
        )
        return self.client.pages.create(
            parent=parent, properties=properties, icon=icon, cover=icon
        )

    def _data_source_parent(self, database_id):
        data_source_id = self.resolve_data_source_id(database_id)
        if not data_source_id:
            raise Exception("Notion data source ID 不能为空")
        return {"type": "data_source_id", "data_source_id": data_source_id}

    def _normalize_page_parent(self, parent):
        if isinstance(parent, str):
            return self._data_source_parent(parent)
        if not isinstance(parent, dict):
            raise TypeError("parent 必须是 Notion parent 对象或数据库 ID")
        if parent.get("type") == "page_id" or "page_id" in parent:
            return parent
        related_id = parent.get("data_source_id") or parent.get("database_id")
        return self._data_source_parent(related_id)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query(self, **kwargs):
        database_id = kwargs.pop("data_source_id", None) or kwargs.pop(
            "database_id", None
        )
        data_source_id = self.resolve_data_source_id(database_id)
        if not data_source_id:
            raise Exception("Notion data source ID 不能为空")
        body = {key: value for key, value in kwargs.items() if value is not None}
        return self.client.request(
            path=f"data_sources/{data_source_id}/query",
            method="POST",
            body=body,
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_block_children(self, id):
        results = []
        start_cursor = None
        while True:
            kwargs = {"block_id": id, "page_size": 100}
            if start_cursor:
                kwargs["start_cursor"] = start_cursor
            response = self.client.blocks.children.list(**kwargs)
            results.extend(response.get("results") or [])
            if not response.get("has_more"):
                return results
            start_cursor = response.get("next_cursor")
            if not start_cursor:
                return results

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def append_blocks(self, block_id, children):
        return self.client.blocks.children.append(block_id=block_id, children=children)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def append_blocks_after(self, block_id, children, after):
        return self.client.blocks.children.append(
            block_id=block_id,
            children=children,
            position={
                "type": "after_block",
                "after_block": {"id": after},
            },
        )

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def delete_block(self, block_id):
        return self.client.blocks.delete(block_id=block_id)

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def get_all_book(self):
        """从Notion中获取所有的书籍"""
        results = self.query_all(self.book_database_id)
        books_dict = {}
        for result in results:
            properties = result.get("properties") or {}
            bookId = self._safe_property_value(properties, "BookId")
            if not bookId:
                continue
            if getattr(self, "legacy_book_database", False):
                books_dict[bookId] = {
                    "pageId": result.get("id"),
                    "readingTime": self._safe_property_value(properties, "ReadingTime"),
                    "category": self._safe_property_value(properties, "Categories"),
                    "Sort": self._safe_property_value(properties, "Sort"),
                    "cover": result.get("cover"),
                    "myRating": self._safe_property_value(properties, "评分"),
                    "lastReadingDate": self._safe_property_value(properties, "Date"),
                    "progress": self._safe_property_value(properties, "Progress"),
                    "status": self._safe_property_value(properties, "Status"),
                }
                continue
            books_dict[bookId] = {
                "pageId": result.get("id"),
                "readingTime": self._safe_property_value(properties, "阅读时长"),
                "category": self._safe_property_value(properties, "书架分类"),
                "Sort": self._safe_property_value(properties, "Sort"),
                "douban_url": self._safe_property_value(properties, "豆瓣链接"),
                "cover": result.get("cover"),
                "myRating": self._safe_property_value(properties, "我的评分"),
                "comment": self._safe_property_value(properties, "豆瓣短评"),
                "lastReadingDate": self._safe_property_value(
                    properties, "最后阅读时间"
                ),
                "progress": self._safe_property_value(properties, "阅读进度"),
                "status": self._safe_property_value(properties, "阅读状态"),
            }
        return books_dict

    @staticmethod
    def _safe_property_value(properties, name):
        """Read an optional page property without letting schema drift abort sync."""
        if not isinstance(properties, dict):
            return None
        property_value = properties.get(name)
        if not isinstance(property_value, dict):
            return None
        try:
            return get_property_value(property_value)
        except (AttributeError, IndexError, KeyError, TypeError, ValueError):
            return None

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query_all_by_book(self, database_id, filter):
        results = []
        has_more = True
        start_cursor = None
        while has_more:
            response = self.query(
                database_id=database_id,
                filter=filter,
                start_cursor=start_cursor,
                page_size=100,
            )
            start_cursor = response.get("next_cursor")
            has_more = bool(response.get("has_more") and start_cursor)
            results.extend(response.get("results") or [])
        return results

    @retry(stop_max_attempt_number=3, wait_fixed=5000)
    def query_all(self, database_id):
        """获取database中所有的数据"""
        results = []
        has_more = True
        start_cursor = None
        while has_more:
            response = self.query(
                database_id=database_id,
                start_cursor=start_cursor,
                page_size=100,
            )
            start_cursor = response.get("next_cursor")
            has_more = bool(response.get("has_more") and start_cursor)
            results.extend(response.get("results") or [])
        return results

    def get_date_relation(self, properties, date):
        properties["年"] = get_relation(
            [
                self.get_year_relation_id(date),
            ]
        )
        properties["月"] = get_relation(
            [
                self.get_month_relation_id(date),
            ]
        )
        properties["周"] = get_relation(
            [
                self.get_week_relation_id(date),
            ]
        )
        properties["日"] = get_relation(
            [
                self.get_day_relation_id(date),
            ]
        )
