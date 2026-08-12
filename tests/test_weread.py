import sys
import unittest
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import weread


class WeReadNoteTests(unittest.TestCase):
    def test_personal_rating_uses_newest_whole_book_review_star(self):
        reviews = [
            {
                "reviewId": "highlight-note",
                "star": 5,
                "range": "1-2",
                "createTime": 30,
            },
            {
                "reviewId": "old-book-review",
                "type": 4,
                "star": 5,
                "createTime": 10,
            },
            {
                "reviewId": "new-book-review",
                "chapterUid": 1_000_000,
                "star": "3",
                "createTime": 20,
            },
        ]

        self.assertEqual(weread.personal_rating_from_reviews(reviews), "⭐️⭐️⭐️")

    def test_personal_rating_ignores_unrated_and_chapter_content(self):
        self.assertIsNone(
            weread.personal_rating_from_reviews(
                [
                    {"type": 4, "star": -1},
                    {"chapterUid": 7, "star": 4, "chapterName": "第一章"},
                ]
            )
        )


if __name__ == "__main__":
    unittest.main()
