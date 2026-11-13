"""Run with: pytest tests/test_diff.py -v"""

from __future__ import annotations

import os
import tempfile
import unittest

from db import Database
from diff_engine import compute_diff, persist_current_state
from models import Assignment, Grade


class TestDiffEngine(unittest.TestCase):
    def setUp(self) -> None:
        fd, self.db_path = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        self.db = Database(self.db_path)
        self.user = self.db.get_or_create_user("test-chat-1", "tester")

    def tearDown(self) -> None:
        self.db.close()
        try:
            os.remove(self.db_path)
        except OSError:
            pass

    def test_first_poll_marks_everything_new(self) -> None:
        assignments = [Assignment(course="CS 101", title="HW1", key="c1:a1", due_date="2026-01-01", default_tz="UTC")]
        diffs = compute_diff(self.db, self.user.id, assignments, [], [])
        self.assertEqual(len(diffs), 1)
        self.assertTrue(diffs[0].is_new)

    def test_unchanged_item_produces_no_diff(self) -> None:
        assignments = [Assignment(course="CS 101", title="HW1", key="c1:a1", due_date="2026-01-01", default_tz="UTC")]
        compute_diff(self.db, self.user.id, assignments, [], [])
        persist_current_state(self.db, self.user.id, assignments, [], [])

        # second poll, identical data
        diffs = compute_diff(self.db, self.user.id, assignments, [], [])
        self.assertEqual(len(diffs), 0)

    def test_grade_posted_is_detected_as_change(self) -> None:
        assignments = [Assignment(course="CS 101", title="HW1", key="c1:a1", due_date="2026-01-01", default_tz="UTC")]
        compute_diff(self.db, self.user.id, assignments, [], [])
        persist_current_state(self.db, self.user.id, assignments, [], [])

        grades = [Grade(course="CS 101", title="HW1", key="c1:a1", score="95", max_score="100")]
        diffs = compute_diff(self.db, self.user.id, assignments, grades, [])
        grade_diffs = [d for d in diffs if d.item.item_type.value == "grade"]
        self.assertEqual(len(grade_diffs), 1)
        self.assertTrue(grade_diffs[0].is_new)

    def test_due_date_change_is_detected(self) -> None:
        v1 = [Assignment(course="CS 101", title="HW1", key="c1:a1", due_date="2026-01-01", default_tz="UTC")]
        compute_diff(self.db, self.user.id, v1, [], [])
        persist_current_state(self.db, self.user.id, v1, [], [])

        v2 = [Assignment(course="CS 101", title="HW1", key="c1:a1", due_date="2026-01-08", default_tz="UTC")]
        diffs = compute_diff(self.db, self.user.id, v2, [], [])
        self.assertEqual(len(diffs), 1)
        self.assertFalse(diffs[0].is_new)
        self.assertIsNotNone(diffs[0].previous_fields)
        if diffs[0].previous_fields:
            self.assertIn("2026-01-01", diffs[0].previous_fields["due_date"])

    def test_removed_item_is_detected_when_enabled(self) -> None:
        assignments = [Assignment(course="CS 101", title="HW1", key="c1:a1", due_date="2026-01-01", default_tz="UTC")]
        compute_diff(self.db, self.user.id, assignments, [], [])
        persist_current_state(self.db, self.user.id, assignments, [], [])

        diffs = compute_diff(self.db, self.user.id, [], [], [], detect_removed=True)
        self.assertEqual(len(diffs), 1)
        self.assertTrue(diffs[0].is_removed)
        self.assertEqual(diffs[0].item.key, "c1:a1")

    def test_two_users_are_isolated(self) -> None:
        user2 = self.db.get_or_create_user("test-chat-2", "tester2")
        assignments = [Assignment(course="CS 101", title="HW1", key="c1:a1", due_date="2026-01-01", default_tz="UTC")]

        compute_diff(self.db, self.user.id, assignments, [], [])
        persist_current_state(self.db, self.user.id, assignments, [], [])

        diffs_user2 = compute_diff(self.db, user2.id, assignments, [], [])
        self.assertEqual(len(diffs_user2), 1)
        self.assertTrue(diffs_user2[0].is_new)


if __name__ == "__main__":
    unittest.main()
