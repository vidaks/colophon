"""heal_book convergence — a write only counts as a heal if the locked target ISBN
actually landed. wait_for_change also returns on TIMEOUT, so its return alone must
never be recorded as success. Run: python -m unittest -v tests.test_heal"""
import os
import tempfile
import unittest
from unittest import mock

from colophon import heal
from colophon.store import Store

TARGET = "9780000000000"
BEFORE = {"title": "Old", "isbn_13": "bad", "page_count": 1, "cover_updated_on": None}


class FakeG:
    def __init__(self):
        self.puts, self.refreshes = [], []

    def put_identity(self, bid, isbn, hcid, slug):
        self.puts.append((bid, isbn, hcid, slug))

    def refresh(self, ids):
        self.refreshes.append(list(ids))


class Convergence(unittest.TestCase):
    def setUp(self):
        self.store = Store(path=os.path.join(tempfile.mkdtemp(), "t.db"))
        self.g = FakeG()

    def _heal(self, after):
        with mock.patch.object(heal, "snapshot", return_value=BEFORE), \
             mock.patch.object(heal, "wait_for_change", return_value=after):
            return heal.heal_book(self.g, self.store, "run-t", 5, TARGET, "1", "s",
                                  dry_run=False)

    def test_converged_records_ok(self):
        res = self._heal({**BEFORE, "title": "New", "isbn_13": "978-0-000-00000-0"})
        self.assertTrue(res["ok"])
        row = self.store.recent(1)[0]
        self.assertTrue(row["ok"])
        self.assertIsNone(row["error"])

    def test_timeout_without_landing_raises_and_records_failure(self):
        with self.assertRaises(heal.ConvergenceError):
            self._heal(dict(BEFORE))  # snapshot unchanged — the refresh never landed
        row = self.store.recent(1)[0]
        self.assertFalse(row["ok"])
        self.assertIn("did not land", row["error"])

    def test_dry_run_writes_nothing(self):
        with mock.patch.object(heal, "snapshot", return_value=BEFORE):
            res = heal.heal_book(self.g, self.store, "run-t", 5, TARGET, dry_run=True)
        self.assertTrue(res["ok"])
        self.assertEqual(self.g.puts, [])


class RevertCoversFailedWrites(unittest.TestCase):
    """A heal that raised AFTER put_identity has already locked the target identity;
    revert must replay its before-state even though the row is ok=0."""

    def test_failed_heal_row_is_reverted(self):
        store = Store(path=os.path.join(tempfile.mkdtemp(), "t.db"))
        g = FakeG()
        g.put_metadata = lambda bid, meta: g.puts.append((bid, meta))
        with mock.patch.object(heal, "snapshot", return_value=BEFORE), \
             mock.patch.object(heal, "wait_for_change", return_value=dict(BEFORE)):
            with self.assertRaises(heal.ConvergenceError):
                heal.heal_book(g, store, "run-t", 5, TARGET, "1", "s", dry_run=False)
            g.puts.clear()
            _, results = heal.revert_run(g, store, "run-t", dry_run=False)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["ok"])
        self.assertEqual(g.puts[0][0], 5)              # before-state re-PUT
        self.assertEqual(g.puts[0][1]["isbn13"], "bad")

    def test_dry_rows_still_excluded(self):
        store = Store(path=os.path.join(tempfile.mkdtemp(), "t.db"))
        with mock.patch.object(heal, "snapshot", return_value=BEFORE):
            heal.heal_book(FakeG(), store, "run-t", 5, TARGET, dry_run=True)
        _, results = heal.revert_run(FakeG(), store, "run-t", dry_run=True)
        self.assertEqual(results, [])


if __name__ == "__main__":
    unittest.main()
