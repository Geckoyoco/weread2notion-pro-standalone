import sys
import unittest
from pathlib import Path
from unittest.mock import patch


SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

from notion_template import ProTemplateBuilder


class FakeClient:
    def __init__(self):
        self.created = []
        self.patched = []
        self.databases = {}

    def request(self, *, path, method, body=None):
        if path == "databases" and method == "POST":
            name = body["title"][0]["text"]["content"]
            database_id = f"db-{len(self.databases) + 1}"
            source_id = f"ds-{len(self.databases) + 1}"
            self.databases[database_id] = source_id
            self.created.append((name, body))
            return {
                "id": database_id,
                "data_sources": [{"id": source_id}],
            }
        if path.startswith("databases/") and method == "GET":
            database_id = path.split("/", 1)[1]
            return {
                "id": database_id,
                "data_sources": [{"id": self.databases[database_id]}],
            }
        if path.startswith("data_sources/") and method == "PATCH":
            self.patched.append((path.split("/", 1)[1], body["properties"]))
            return {"properties": body["properties"]}
        raise AssertionError((path, method, body))


class ProTemplateBuilderTests(unittest.TestCase):
    def test_empty_page_creates_complete_relational_schema(self):
        client = FakeClient()
        builder = ProTemplateBuilder(client, "page", system_page_id="system-page")

        with patch("notion_template.time.sleep"):
            refs = builder.ensure()

        self.assertEqual(
            set(refs),
            {
                "书架",
                "读书笔记",
                "划线",
                "章节",
                "作者",
                "分类",
                "阅读记录",
                "日",
                "周",
                "月",
                "年",
            },
        )
        self.assertEqual(client.created[-1][0], "书架")
        self.assertEqual(
            client.created[-1][1]["parent"],
            {"type": "page_id", "page_id": "system-page"},
        )
        book_properties = client.created[-1][1]["initial_data_source"]["properties"]
        self.assertEqual(
            book_properties["读书笔记"]["relation"]["data_source_id"],
            refs["读书笔记"].data_source_id,
        )
        self.assertEqual(
            book_properties["阅读进度"]["number"]["format"], "percent"
        )
        patched_names = {
            name
            for _, properties in client.patched
            for name in properties
        }
        self.assertIn("书籍", patched_names)
        self.assertIn("书架", patched_names)

    def test_partial_run_reuses_existing_databases(self):
        client = FakeClient()
        with patch("notion_template.time.sleep"):
            first = ProTemplateBuilder(
                client, "page", system_page_id="system-page"
            ).ensure()
        existing = {name: ref.database_id for name, ref in first.items()}
        created_before = len(client.created)

        with patch("notion_template.time.sleep"):
            second = ProTemplateBuilder(
                client, "page", existing, system_page_id="system-page"
            ).ensure()

    def test_refuses_to_create_backend_databases_on_dashboard(self):
        with self.assertRaisesRegex(Exception, "NOTION_SYSTEM_PAGE"):
            ProTemplateBuilder(FakeClient(), "dashboard").ensure()

        self.assertEqual(len(client.created), created_before)
        self.assertEqual(second["书架"].database_id, first["书架"].database_id)


if __name__ == "__main__":
    unittest.main()
