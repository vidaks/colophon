"""backfill run-loop — circuit breaker + changelog discipline. No network.
Run: python -m unittest -v tests.test_backfill"""
import os
import tempfile
import unittest
from unittest import mock

from colophon import backfill
from colophon.store import Store


def _heal(bid):
    return {"book_id": bid, "action": "heal", "reason": "r", "isbn": "9780000000000",
            "hcid": "1", "slug": "s", "confidence": 0.95, "hc_title": "T"}


class Breaker(unittest.TestCase):
    def setUp(self):
        self.store = Store(path=os.path.join(tempfile.mkdtemp(), "t.db"))

    def _run(self, n, heal_effects):
        with mock.patch.object(backfill, "survey", return_value=list(range(1, n + 1))), \
             mock.patch.object(backfill, "snapshot", return_value={"title": "T"}), \
             mock.patch.object(backfill, "propose", side_effect=[_heal(i) for i in range(1, n + 1)]), \
             mock.patch.object(backfill, "assert_preconditions", new=lambda g: None), \
             mock.patch.object(backfill, "heal_book", side_effect=heal_effects):
            return backfill.run(None, self.store, limit=n, apply=True)

    def test_sustained_errors_abort(self):
        res = self._run(8, [RuntimeError("boom")] * 8)
        self.assertTrue(res["aborted"])
        self.assertEqual(res["healed"], 0)
        self.assertEqual(res["errors"], backfill.ABORT_MIN_ATTEMPTS)  # stopped at 4, not 8
        self.assertTrue(any(r["action"] == "ABORT" for r in self.store.recent(20)))

    def test_scattered_errors_do_not_abort(self):
        res = self._run(4, [{"ok": True}, RuntimeError("x"), {"ok": True}, {"ok": True}])
        self.assertFalse(res["aborted"])
        self.assertEqual((res["healed"], res["errors"]), (3, 1))


class DryRunRecords(unittest.TestCase):
    def test_reason_travels_in_the_proposal_not_the_error_column(self):
        store = Store(path=os.path.join(tempfile.mkdtemp(), "t.db"))
        with mock.patch.object(backfill, "survey", return_value=[1]), \
             mock.patch.object(backfill, "snapshot", return_value={"title": "T"}), \
             mock.patch.object(backfill, "propose", return_value=_heal(1)):
            backfill.run(None, store, limit=1, apply=False)
        row = store.recent(1)[0]
        self.assertEqual(row["action"], "backfill-heal")
        self.assertIsNone(row["error"])
        self.assertIn("9780000000000", row["target_json"])


if __name__ == "__main__":
    unittest.main()
