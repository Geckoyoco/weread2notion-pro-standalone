"""Initialize or validate the self-owned Notion Pro workspace."""

from notion_helper import NotionHelper


def main() -> None:
    helper = NotionHelper()
    names = "、".join(sorted(helper.database_id_dict))
    print(f"Notion Pro 初始化成功。已识别数据库：{names}")


if __name__ == "__main__":
    main()
