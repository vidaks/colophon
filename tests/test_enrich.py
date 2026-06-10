"""Enrich memory + digest — pure / SQLite only, no network. Run: python -m unittest -v

Covers the loop-killer: a bare (no-hcid) import that never matches must be counted,
marked stuck after N failed sweeps, dropped from the sweep, and surfaced exactly
once. No live grimmory — `unseeded_ids` is stubbed and `g` is a fake.
"""
import os
import tempfile
import unittest

from colophon import enrich, grimmory, maintain
from colophon.heal import PreconditionError
from colophon.store import Store


class FakeG:
    """Stand-in for Grimmory: records refreshes, gates on preconditions."""
    def __init__(self, ok=True):
        self._ok = ok
        self.refreshed = []

    def preconditions(self):
        det = {"moveFilesToLibraryPattern": False, "saveToOriginalFile.anyFormatEnabled": False}
        return (True, det) if self._ok else (False, {"moveFilesToLibraryPattern": True})

    def refresh(self, book_ids, refresh_covers=True, replace_mode="REPLACE_ALL"):
        self.refreshed.append((list(book_ids), replace_mode))
        return "ok"


class Observe(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(path=os.path.join(self.dir, "t.db"))

    def test_increments_and_marks_stuck_at_threshold(self):
        self.assertEqual(self.store.enrich_observe([1, 2], stuck_after=3), [])  # fail=1
        self.assertEqual(self.store.enrich_observe([1, 2], stuck_after=3), [])  # fail=2
        self.assertEqual(set(self.store.enrich_observe([1, 2], stuck_after=3)), {1, 2})  # fail=3
        self.assertEqual(set(self.store.enrich_stuck_ids()), {1, 2})

    def test_prunes_books_that_seeded_or_were_deleted(self):
        self.store.enrich_observe([1, 2, 3], stuck_after=2)   # all fail=1
        self.store.enrich_observe([1, 3], stuck_after=2)      # 2 dropped out → pruned
        self.assertEqual(set(self.store.enrich_stuck_ids()), {1, 3})
        self.assertNotIn(2, {r["book_id"] for r in self.store.enrich_stuck_unreported()})

    def test_report_once_then_silent_but_still_stuck(self):
        self.store.enrich_observe([5], stuck_after=1)         # stuck immediately
        self.assertEqual([r["book_id"] for r in self.store.enrich_stuck_unreported()], [5])
        self.assertEqual(self.store.enrich_mark_reported([5]), 1)
        self.assertEqual(self.store.enrich_stuck_unreported(), [])   # not re-surfaced
        self.assertEqual(self.store.enrich_stuck_ids(), [5])         # still excluded from sweeps


class RunEnrich(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(path=os.path.join(self.dir, "t.db"))
        self._orig = enrich.unseeded_ids

    def tearDown(self):
        enrich.unseeded_ids = self._orig

    def test_dry_run_never_refreshes(self):
        enrich.unseeded_ids = lambda: [1, 2, 3]
        g = FakeG()
        res = enrich.run_enrich(g, self.store, apply=False, stuck_after=2)
        self.assertEqual(res["active"], [1, 2, 3])
        self.assertFalse(res["submitted"])
        self.assertEqual(g.refreshed, [])

    def test_apply_refreshes_active_and_excludes_stuck(self):
        self.store.enrich_observe([1, 2], stuck_after=2)
        self.store.enrich_observe([1, 2], stuck_after=2)     # 1,2 now stuck
        enrich.unseeded_ids = lambda: [1, 2, 3]              # 3 is new
        g = FakeG()
        res = enrich.run_enrich(g, self.store, apply=True, stuck_after=2)
        self.assertEqual(set(res["stuck"]), {1, 2})
        self.assertEqual(res["active"], [3])
        self.assertTrue(res["submitted"])
        self.assertEqual(g.refreshed, [([3], "REPLACE_MISSING")])

    def test_apply_with_all_stuck_submits_nothing(self):
        self.store.enrich_observe([1], stuck_after=1)        # stuck
        enrich.unseeded_ids = lambda: [1]
        g = FakeG()
        res = enrich.run_enrich(g, self.store, apply=True, stuck_after=1)
        self.assertEqual(res["active"], [])
        self.assertFalse(res["submitted"])
        self.assertEqual(g.refreshed, [])

    def test_apply_aborts_when_preconditions_fail(self):
        enrich.unseeded_ids = lambda: [1, 2]
        g = FakeG(ok=False)
        with self.assertRaises(PreconditionError):
            enrich.run_enrich(g, self.store, apply=True, stuck_after=5)
        self.assertEqual(g.refreshed, [])


class BookUrl(unittest.TestCase):
    def test_none_when_unset(self):
        old = grimmory.BOOKSTORE_URL
        grimmory.BOOKSTORE_URL = None
        try:
            self.assertIsNone(grimmory.book_url(5))
        finally:
            grimmory.BOOKSTORE_URL = old

    def test_builds_route_and_strips_trailing_slash(self):
        old = grimmory.BOOKSTORE_URL
        grimmory.BOOKSTORE_URL = "https://books.example.com/"
        try:
            self.assertEqual(grimmory.book_url(530), "https://books.example.com/book/530")
        finally:
            grimmory.BOOKSTORE_URL = old


class DigestRender(unittest.TestCase):
    def _res(self, stuck):
        return {"ts": "2026-06-10 04:30", "apply": True, "ok": True, "aborted": False,
                "backfill": None, "resolve": None, "errors": [], "stuck": stuck}

    def test_section_with_link(self):
        out = maintain.render_summary(self._res([
            {"book_id": 530, "title": "Journal of the Plague Year",
             "authors": "Adrian Tchaikovsky", "isbn": "9781849976824",
             "fail_count": 7, "url": "https://books.x/book/530"}]))
        self.assertIn("Unresolvable — manual review (1)", out)
        self.assertIn("book 530", out)
        self.assertIn("https://books.x/book/530", out)

    def test_no_section_when_empty(self):
        self.assertNotIn("Unresolvable", maintain.render_summary(self._res([])))

    def test_handles_missing_url_and_isbn(self):
        out = maintain.render_summary(self._res([
            {"book_id": 7, "title": "X", "authors": "", "isbn": "", "fail_count": 6, "url": None}]))
        self.assertIn("book 7", out)
        self.assertIn("no isbn", out)


if __name__ == "__main__":
    unittest.main()
