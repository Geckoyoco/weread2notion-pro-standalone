import os
from pathlib import Path
import sys
import types
import unittest
from datetime import datetime, timezone
from unittest.mock import patch


try:
    import requests  # noqa: F401
except ModuleNotFoundError:
    # The tests inject a session and never access the network. This lightweight
    # fallback lets the normalization suite run even before requirements.txt is
    # installed (for example, in a minimal checkout-validation environment).
    requests_stub = types.ModuleType("requests")

    class RequestException(Exception):
        pass

    class HTTPError(RequestException):
        def __init__(self, *args, response=None):
            super().__init__(*args)
            self.response = response

    class Session:
        def __init__(self):
            self.headers = {}

    requests_stub.RequestException = RequestException
    requests_stub.HTTPError = HTTPError
    requests_stub.Session = Session
    requests_stub.Response = object
    sys.modules["requests"] = requests_stub


SCRIPTS_DIR = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS_DIR))

import weread_api as weread_api_module  # noqa: E402
from weread_api import WeReadApi, WeReadGatewayError  # noqa: E402


class FakeResponse:
    def __init__(self, payload):
        self.payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self.payload


class FakeSession:
    def __init__(self, responses):
        self.responses = {
            name: list(payloads) if isinstance(payloads, list) else [payloads]
            for name, payloads in responses.items()
        }
        self.headers = {}
        self.calls = []

    def post(self, url, json, timeout):
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        api_name = json["api_name"]
        if api_name not in self.responses or not self.responses[api_name]:
            raise AssertionError(f"Unexpected Gateway call: {api_name} {json}")
        return FakeResponse(self.responses[api_name].pop(0))


class WeReadApiTest(unittest.TestCase):
    def make_api(self, responses):
        session = FakeSession(responses)
        return WeReadApi(api_key="  test-api-key\n", session=session), session

    def test_requires_api_key_and_sets_gateway_headers(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(WeReadGatewayError, "WEREAD_API_KEY"):
                WeReadApi(session=FakeSession({}))

        api, session = self.make_api({"/shelf/sync": {"books": []}})
        api.get_bookshelf()
        self.assertEqual(session.headers["Authorization"], "Bearer test-api-key")
        self.assertEqual(session.headers["Content-Type"], "application/json")
        self.assertEqual(
            session.calls[0]["json"],
            {"api_name": "/shelf/sync", "skill_version": "1.0.4"},
        )

    def test_bookshelf_does_not_mislabel_update_timestamp_as_reading_time(self):
        api, _ = self.make_api(
            {
                "/shelf/sync": {
                    "books": [
                        {
                            "bookId": "b1",
                            "title": "Book",
                            "readUpdateTime": 1700000000,
                        }
                    ],
                    "archive": [{"name": "技术", "bookIds": ["b1"]}],
                    "albums": [],
                }
            }
        )

        shelf = api.get_bookshelf()
        self.assertEqual(shelf["books"][0]["bookId"], "b1")
        self.assertEqual(shelf["archive"][0]["name"], "技术")
        self.assertEqual(shelf["bookProgress"], [])

    def test_bookshelf_normalizes_real_legacy_progress_when_present(self):
        api, _ = self.make_api(
            {
                "/shelf/sync": {
                    "books": [{"bookId": "b1"}],
                    "bookProgress": [
                        {
                            "bookId": "b1",
                            "readingTime": 0,
                            "recordReadingTime": "3661",
                        }
                    ],
                }
            }
        )

        shelf = api.get_bookshelf()

        self.assertEqual(shelf["bookProgress"][0]["readingTime"], 3661)

    def test_notebooks_use_last_sort_pagination_and_normalize_items(self):
        api, session = self.make_api(
            {
                "/user/notebooks": [
                    {
                        "books": [
                            {"bookId": "b2", "sort": 20, "book": {"title": "B"}}
                        ],
                        "hasMore": 1,
                    },
                    {
                        "books": [
                            {"sort": "10", "book": {"bookId": "b1", "title": "A"}}
                        ],
                        "hasMore": 0,
                    },
                ]
            }
        )

        books = api.get_notebooklist()
        self.assertEqual([item["bookId"] for item in books], ["b1", "b2"])
        self.assertEqual(books[0]["book"]["title"], "A")
        self.assertEqual(session.calls[1]["json"]["lastSort"], 20)
        self.assertNotIn("params", session.calls[1]["json"])

    def test_book_info_and_progress_match_legacy_units(self):
        api, _ = self.make_api(
            {
                "/book/info": {
                    "bookId": "b1",
                    "title": "Example",
                    "author": "Author",
                    "cover": "https://cover",
                    "category": "文学",
                    "newRating": 83,
                },
                "/book/getprogress": {
                    "bookId": "b1",
                    "book": {
                        "progress": 45,
                        "recordReadingTime": 3661,
                        "readingTime": 4000,
                        "updateTime": 1710000000,
                        "isStartReading": 1,
                    },
                },
            }
        )

        info = api.get_bookinfo("b1")
        progress = api.get_read_info("b1")
        self.assertEqual(info["categories"], [{"title": "文学"}])
        self.assertEqual(info["newRating"], 83)
        self.assertEqual(progress["readingProgress"], 45)
        self.assertEqual(progress["readingTime"], 4000)
        self.assertEqual(progress["markedStatus"], 2)
        self.assertEqual(progress["lastReadingDate"], 1710000000)
        self.assertEqual(progress["activityDate"], 1710000000)

    def test_progress_falls_back_to_current_record_reading_time_name(self):
        api, _ = self.make_api(
            {
                "/book/getprogress": {
                    "book": {
                        "progress": 12,
                        "readingTime": 0,
                        "recordReadingTime": "3661",
                        "updateTime": 1710000000,
                    }
                }
            }
        )

        progress = api.get_read_info("b1")

        self.assertEqual(progress["readingTime"], 3661)
        self.assertEqual(progress["readingProgress"], 12)

    def test_progress_status_follows_documented_boundaries(self):
        api, _ = self.make_api(
            {
                "/book/getprogress": [
                    {"book": {"progress": 0}},
                    {"book": {"progress": 0, "isStartReading": 1}},
                    {"book": {"progress": 100, "updateTime": 10}},
                    {
                        "book": {
                            "progress": 100,
                            "updateTime": 10,
                            "finishTime": 20,
                        }
                    },
                ]
            }
        )

        statuses = [api.get_read_info("b1")["markedStatus"] for _ in range(4)]

        self.assertEqual(statuses, [1, 2, 2, 4])

    def test_bookmarks_reviews_and_chapters_keep_bare_legacy_shapes(self):
        api, session = self.make_api(
            {
                "/book/bookmarklist": {
                    "updated": [
                        {
                            "bookmarkId": "m1",
                            "chapterUid": "7",
                            "markText": "highlight",
                        }
                    ]
                },
                "/review/list/mine": [
                    {
                        "reviews": [
                            {
                                "review": {
                                    "reviewId": "r1",
                                    "content": "note",
                                    "type": 1,
                                    "chapterUid": "7",
                                }
                            }
                        ],
                        "hasMore": 1,
                        "synckey": 99,
                    },
                    {
                        "reviews": [
                            {
                                "review": {
                                    "reviewId": "r2",
                                    "content": "summary",
                                    "type": 4,
                                }
                            }
                        ],
                        "hasMore": 0,
                        "synckey": 100,
                    },
                ],
                "/book/chapterinfo": {
                    "chapters": [
                        {"chapterUid": "7", "chapterIdx": 2, "title": "Chapter"}
                    ]
                },
            }
        )

        bookmarks = api.get_bookmark_list("b1")
        reviews = api.get_review_list("b1")
        chapters = api.get_chapter_info("b1")

        self.assertEqual(bookmarks[0]["bookId"], "b1")
        self.assertEqual(bookmarks[0]["chapterUid"], 7)
        self.assertEqual(reviews[0]["content"], "note")
        self.assertEqual(reviews[1]["chapterUid"], 1000000)
        self.assertEqual(chapters[7]["title"], "Chapter")
        self.assertEqual(chapters[1000000]["title"], "点评")
        review_calls = [
            call for call in session.calls if call["json"]["api_name"] == "/review/list/mine"
        ]
        self.assertEqual(review_calls[1]["json"]["synckey"], 99)

    def test_annual_daily_stats_prefer_daily_read_times(self):
        api, session = self.make_api(
            {
                "/readdata/detail": {
                    "readTimes": {"1704038400": 9999},
                    "dailyReadTimes": {
                        "2024-01-02": "120",
                        "1704211200000": {"readTime": 60},
                    },
                    "totalReadTime": 180,
                }
            }
        )

        result = api.get_api_data(year=2024)
        expected_date_timestamp = 1704124800  # 2024-01-02 00:00 Asia/Shanghai
        self.assertEqual(result["readTimes"][expected_date_timestamp], 120)
        self.assertEqual(result["readTimes"][1704211200], 60)
        self.assertNotIn(1704038400, result["readTimes"])
        self.assertEqual(result["totalReadTime"], 180)
        self.assertEqual(session.calls[0]["json"]["mode"], "annually")
        self.assertEqual(session.calls[0]["json"]["baseTime"], 1704038400)
        self.assertEqual(api.get_daily_read_times, api.get_daily_read_times)

    def test_annual_stats_fall_back_to_twelve_monthly_daily_buckets(self):
        monthly_payloads = []
        for month in range(1, 13):
            if month == 1:
                monthly_payloads.append(
                    {"readTimes": {"1704211200000": 30}}
                )
            else:
                monthly_payloads.append({"readTimes": {}})
        api, session = self.make_api(
            {
                "/readdata/detail": [
                    {"readTimes": {"1704038400": 9999}},
                    *monthly_payloads,
                ]
            }
        )

        result = api.get_api_data(year=2024)
        self.assertEqual(result["readTimes"], {1704211200: 30})
        self.assertEqual(len(session.calls), 13)
        self.assertEqual(session.calls[1]["json"]["mode"], "monthly")
        self.assertEqual(session.calls[-1]["json"]["baseTime"], 1732982400)

    def test_current_year_monthly_fallback_stops_at_current_month(self):
        now = datetime.now(timezone.utc).astimezone(
            weread_api_module.SHANGHAI_TIMEZONE
        )
        api, session = self.make_api(
            {
                "/readdata/detail": [
                    {"readTimes": {}},
                    *({"readTimes": {}} for _ in range(now.month)),
                ]
            }
        )

        result = api.get_api_data(year=now.year)
        self.assertEqual(result["readTimes"], {})
        self.assertEqual(len(session.calls), now.month + 1)
        expected_last_month = int(
            datetime(
                now.year,
                now.month,
                1,
                tzinfo=weread_api_module.SHANGHAI_TIMEZONE,
            ).timestamp()
        )
        self.assertEqual(
            session.calls[-1]["json"]["baseTime"], expected_last_month
        )

    def test_future_year_stats_return_empty_without_gateway_request(self):
        now = datetime.now(timezone.utc).astimezone(
            weread_api_module.SHANGHAI_TIMEZONE
        )
        api, session = self.make_api({})

        result = api.get_api_data(year=now.year + 1)

        self.assertEqual(
            result,
            {
                "year": now.year + 1,
                "readTimes": {},
                "dailyReadTimes": {},
            },
        )
        self.assertEqual(session.calls, [])

    def test_gateway_business_error_does_not_leak_api_key(self):
        api, _ = self.make_api(
            {"/shelf/sync": {"errcode": 42, "errmsg": "denied"}}
        )
        with self.assertRaises(WeReadGatewayError) as context:
            api.get_bookshelf()
        self.assertIn("errcode=42", str(context.exception))
        self.assertNotIn("test-api-key", str(context.exception))

    def test_transient_gateway_failures_are_retried_three_times(self):
        class FlakySession(FakeSession):
            def __init__(self):
                super().__init__({"/shelf/sync": {"books": []}})
                self.attempts = 0

            def post(self, url, json, timeout):
                self.attempts += 1
                if self.attempts < 3:
                    raise weread_api_module.requests.RequestException("temporary")
                return super().post(url, json, timeout)

        session = FlakySession()
        api = WeReadApi(api_key="test-api-key", session=session)
        with patch.object(weread_api_module.time, "sleep") as sleep:
            self.assertEqual(api.get_bookshelf()["books"], [])

        self.assertEqual(session.attempts, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])

    def test_transient_gateway_403_is_retried(self):
        class ForbiddenResponse(FakeResponse):
            status_code = 403

            def raise_for_status(self):
                raise weread_api_module.requests.HTTPError(response=self)

        class FlakyForbiddenSession(FakeSession):
            def __init__(self):
                super().__init__({"/shelf/sync": {"books": []}})
                self.attempts = 0

            def post(self, url, json, timeout):
                self.attempts += 1
                if self.attempts < 3:
                    return ForbiddenResponse({})
                return super().post(url, json, timeout)

        session = FlakyForbiddenSession()
        api = WeReadApi(api_key="test-api-key", session=session)
        with patch.object(weread_api_module.time, "sleep") as sleep:
            self.assertEqual(api.get_bookshelf()["books"], [])

        self.assertEqual(session.attempts, 3)
        self.assertEqual([call.args[0] for call in sleep.call_args_list], [1, 2])


if __name__ == "__main__":
    unittest.main()
