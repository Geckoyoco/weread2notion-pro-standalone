import os

from notion_helper import NotionHelper
from weread_api import WeReadApi

from utils import (
    get_callout,
    get_heading,
    get_number,
    get_number_from_result,
    get_quote,
    get_rich_text_from_result,
    get_table_of_contents,
)


# These clients are initialized by ``main`` and shared by the small sync
# helpers below. Keeping the names explicit also makes the helpers easy to
# replace with fakes in offline tests.
weread_api = None
notion_helper = None


def allow_remote_deletions():
    return os.getenv("ALLOW_REMOTE_DELETIONS", "").lower() in {"1", "true", "yes"}


def personal_rating_from_reviews(reviews):
    """Return the newest whole-book personal rating as a Notion select name."""

    candidates = []
    for review in reviews or []:
        whole_book_review = (
            review.get("type") == 4
            or review.get("chapterUid") == 1_000_000
            or (
                "isFinish" in review
                and not review.get("range")
                and not review.get("chapterName")
            )
        )
        if not whole_book_review:
            continue
        try:
            star = int(float(review.get("star")))
        except (TypeError, ValueError):
            continue
        if 1 <= star <= 5:
            try:
                created_at = float(review.get("createTime") or 0)
            except (TypeError, ValueError):
                created_at = 0
            candidates.append((created_at, star))
    if not candidates:
        return None
    _, star = max(candidates, key=lambda item: item[0])
    return "⭐️" * star


def get_bookmark_list(page_id, bookId):
    """获取我的划线"""
    filter = {
        "and": [
            {"property": "书籍", "relation": {"contains": page_id}},
            {"property": "blockId", "rich_text": {"is_not_empty": True}},
        ]
    }
    results = notion_helper.query_all_by_book(
        notion_helper.bookmark_database_id, filter
    )
    dict1 = {}
    dict2 = {}
    for result in results:
        bookmark_id = get_rich_text_from_result(result, "bookmarkId")
        block_id = get_rich_text_from_result(result, "blockId")
        if bookmark_id and block_id:
            dict1[bookmark_id] = block_id
            dict2[block_id] = result.get("id")
    bookmarks = weread_api.get_bookmark_list(bookId)
    for i in bookmarks:
        if i.get("bookmarkId") in dict1:
            i["blockId"] = dict1.pop(i.get("bookmarkId"))
    if dict1 and allow_remote_deletions():
        for blockId in dict1.values():
            notion_helper.delete_block(blockId)
            notion_helper.delete_block(dict2.get(blockId))
    elif dict1:
        print(f"检测到 {len(dict1)} 条远端已缺失划线；默认保留，避免误删。")
    return bookmarks


def get_review_list(page_id, bookId):
    """获取笔记"""
    filter = {
        "and": [
            {"property": "书籍", "relation": {"contains": page_id}},
            {"property": "blockId", "rich_text": {"is_not_empty": True}},
        ]
    }
    results = notion_helper.query_all_by_book(notion_helper.review_database_id, filter)
    dict1 = {}
    dict2 = {}
    for result in results:
        review_id = get_rich_text_from_result(result, "reviewId")
        block_id = get_rich_text_from_result(result, "blockId")
        if review_id and block_id:
            dict1[review_id] = block_id
            dict2[block_id] = result.get("id")
    reviews = weread_api.get_review_list(bookId)
    for i in reviews:
        if i.get("reviewId") in dict1:
            i["blockId"] = dict1.pop(i.get("reviewId"))
    if dict1 and allow_remote_deletions():
        for blockId in dict1.values():
            notion_helper.delete_block(blockId)
            notion_helper.delete_block(dict2.get(blockId))
    elif dict1:
        print(f"检测到 {len(dict1)} 条远端已缺失想法；默认保留，避免误删。")
    return reviews


def check(bookId):
    """检查是否已经插入过"""
    filter = {"property": "BookId", "rich_text": {"equals": bookId}}
    response = notion_helper.query(
        database_id=notion_helper.book_database_id, filter=filter
    )
    if len(response["results"]) > 0:
        return response["results"][0]["id"]
    return None


def get_sort():
    """获取database中的最新时间"""
    filter = {"property": "Sort", "number": {"is_not_empty": True}}
    sorts = [
        {
            "property": "Sort",
            "direction": "descending",
        }
    ]
    response = notion_helper.query(
        database_id=notion_helper.book_database_id,
        filter=filter,
        sorts=sorts,
        page_size=1,
    )
    if len(response.get("results")) == 1:
        return response.get("results")[0].get("properties").get("Sort").get("number")
    return 0


def sort_notes(page_id, chapter, bookmark_list):
    """对笔记进行排序"""

    def note_sort_key(item):
        chapter_uid = item.get("chapterUid", 1)
        chapter_info = (chapter or {}).get(chapter_uid) or (chapter or {}).get(
            str(chapter_uid)
        )
        chapter_index = (chapter_info or {}).get("chapterIdx", chapter_uid)
        try:
            chapter_index = int(chapter_index)
        except (TypeError, ValueError):
            chapter_index = 1_000_000
        try:
            range_start = int(str(item.get("range") or "0-0").split("-")[0] or 0)
        except (TypeError, ValueError):
            range_start = 0
        return chapter_index, range_start

    bookmark_list = sorted(
        bookmark_list,
        key=note_sort_key,
    )

    notes = []
    if chapter != None:
        filter = {"property": "书籍", "relation": {"contains": page_id}}
        results = notion_helper.query_all_by_book(
            notion_helper.chapter_database_id, filter
        )
        dict1 = {}
        dict2 = {}
        for result in results:
            chapter_uid = get_number_from_result(result, "chapterUid")
            block_id = get_rich_text_from_result(result, "blockId")
            if chapter_uid is not None and block_id:
                dict1[chapter_uid] = block_id
                dict2[block_id] = result.get("id")
        d = {}
        for data in bookmark_list:
            chapterUid = data.get("chapterUid", 1)
            if chapterUid not in d:
                d[chapterUid] = []
            d[chapterUid].append(data)
        for key, value in d.items():
            if key in chapter:
                if key in dict1:
                    chapter.get(key)["blockId"] = dict1.pop(key)
                notes.append(chapter.get(key))
            notes.extend(value)
        if dict1 and allow_remote_deletions():
            for blockId in dict1.values():
                notion_helper.delete_block(blockId)
                notion_helper.delete_block(dict2.get(blockId))
        elif dict1:
            print(f"检测到 {len(dict1)} 个远端已缺失章节；默认保留，避免误删。")
    else:
        notes.extend(bookmark_list)
    return notes


def append_blocks(id, contents):
    print(f"笔记数{len(contents)}")
    before_block_id = ""
    block_children = notion_helper.get_block_children(id)
    if len(block_children) > 0 and block_children[0].get("type") == "table_of_contents":
        before_block_id = block_children[0].get("id")
    else:
        response = notion_helper.append_blocks(
            block_id=id, children=[get_table_of_contents()]
        )
        before_block_id = response.get("results")[0].get("id")
    blocks = []
    sub_contents = []
    l = []
    for content in contents:
        if len(blocks) == 100:
            results = append_blocks_to_notion(id, blocks, before_block_id, sub_contents)
            before_block_id = results[-1].get("blockId")
            l.extend(results)
            blocks.clear()
            sub_contents.clear()
            blocks.append(content_to_block(content))
            sub_contents.append(content)
        elif "blockId" in content:
            if len(blocks) > 0:
                l.extend(
                    append_blocks_to_notion(id, blocks, before_block_id, sub_contents)
                )
                blocks.clear()
                sub_contents.clear()
            before_block_id = content["blockId"]
        else:
            blocks.append(content_to_block(content))
            sub_contents.append(content)

    if len(blocks) > 0:
        l.extend(append_blocks_to_notion(id, blocks, before_block_id, sub_contents))
    for index, value in enumerate(l):
        print(f"正在插入第{index+1}条笔记，共{len(l)}条")
        if "bookmarkId" in value:
            notion_helper.insert_bookmark(id, value)
        elif "reviewId" in value:
            notion_helper.insert_review(id, value)
        else:
            notion_helper.insert_chapter(id, value)


def content_to_block(content):
    if "bookmarkId" in content:
        return get_callout(
            content.get("markText",""),
            content.get("style"),
            content.get("colorStyle"),
            content.get("reviewId"),
        )
    elif "reviewId" in content:
        return get_callout(
            content.get("content",""),
            content.get("style"),
            content.get("colorStyle"),
            content.get("reviewId"),
        )
    else:
        return get_heading(content.get("level"), content.get("title"))


def append_blocks_to_notion(id, blocks, after, contents):
    response = notion_helper.append_blocks_after(
        block_id=id, children=blocks, after=after
    )
    results = response.get("results")
    l = []
    for index, content in enumerate(contents):
        result = results[index]
        if content.get("abstract") != None and content.get("abstract") != "":
            notion_helper.append_blocks(
                block_id=result.get("id"), children=[get_quote(content.get("abstract"))]
            )
        content["blockId"] = result.get("id")
        l.append(content)
    return l


def main():
    global weread_api, notion_helper
    weread_api = WeReadApi()
    notion_helper = NotionHelper()
    notion_books = notion_helper.get_all_book()
    books = weread_api.get_notebooklist()
    full_sync = os.getenv("FULL_SYNC_NOTES", "").lower() in {"1", "true", "yes"}
    print(f"找到 {len(books)} 本含笔记的书。")
    for index, item in enumerate(books):
        book = item.get("book") or item
        bookId = item.get("bookId") or book.get("bookId")
        title = book.get("title") or f"微信读书 {bookId}"
        sort = item.get("sort") or 0
        if not bookId or bookId not in notion_books:
            continue
        if not full_sync and sort == (notion_books.get(bookId).get("Sort") or 0):
            continue
        pageId = notion_books.get(bookId).get("pageId")
        print(f"正在同步《{title}》，共 {len(books)} 本，当前第 {index + 1} 本。")
        try:
            chapter = weread_api.get_chapter_info(bookId)
            bookmark_list = get_bookmark_list(pageId, bookId)
            reviews = get_review_list(pageId, bookId)
            personal_rating = personal_rating_from_reviews(reviews)
            bookmark_list.extend(reviews)
            content = sort_notes(pageId, chapter, bookmark_list)
            append_blocks(pageId, content)
            properties = {"Sort": get_number(sort)}
            if personal_rating:
                properties["我的评分"] = {"select": {"name": personal_rating}}
            notion_helper.update_book_page(
                page_id=pageId, properties=properties
            )
        except Exception as error:
            print(f"同步《{title}》笔记失败，跳过本次，继续后续书籍：{error}")


if __name__ == "__main__":
    main()
