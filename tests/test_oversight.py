"""Oversight verdict + render — no network, no DB. Run: python -m unittest -v

Covers the active-vs-settled split: oscillation is only DRIFT while it is still
happening. A book that oscillated earlier in the window but has gone quiet is
surfaced as resolved (REVIEW), not flagged as active drift.
"""
import unittest

from colophon import oversight

NOW = 1_750_000_000.0   # fixed epoch (mid-2026 summer; no DST edge within the window)


def _ago(days=0, hours=0):
    """Changelog `ts` string for a point `days`+`hours` before NOW."""
    return oversight._ts(NOW - days * 86400 - hours * 3600)


def _row(book_id, run_id, ts, action="heal", ok=True, dry_run=0):
    return {"id": 0, "run_id": run_id, "ts": ts, "book_id": book_id,
            "action": action, "dry_run": dry_run, "ok": ok,
            "error": None, "before_json": None, "after_json": None, "target_json": None}


class FakeStore:
    def __init__(self, rows):
        self._rows = rows

    def recent(self, n=20):
        return list(self._rows[:n])


def _osc(book_id, run_tag, *hour_offsets):
    """Two-plus heals of one book in distinct runs at the given hour offsets."""
    return [_row(book_id, f"run-{run_tag}-{h}-seriesnum", _ago(hours=h)) for h in hour_offsets]


class SettledOscillation(unittest.TestCase):
    def test_settled_only_is_review_not_drift(self):
        # Two books, each oscillated ~4 days ago then quiet — the user's scenario.
        rows = _osc(546, "a", 4 * 24 + 5, 4 * 24 + 1) + _osc(679, "b", 4 * 24 + 6, 4 * 24 + 2)
        res = oversight.review(FakeStore(rows), now=NOW)
        self.assertEqual(res["verdict"], "REVIEW")
        self.assertEqual(res["oscillating_active"], {})
        self.assertEqual(set(res["oscillating_settled"]), {546, 679})
        report = oversight.render(res)
        self.assertIn("Settled", report)
        self.assertIn("resolved itself", report)
        self.assertIn("not active drift", report)
        self.assertNotIn("pausing the daily timer", report)   # no DRIFT guidance

    def test_active_oscillation_is_drift(self):
        rows = _osc(546, "a", 3, 2, 1)   # three heals within the last day
        res = oversight.review(FakeStore(rows), now=NOW)
        self.assertEqual(res["verdict"], "DRIFT")
        self.assertEqual(set(res["oscillating_active"]), {546})
        self.assertEqual(res["oscillating_settled"], {})
        report = oversight.render(res)
        self.assertIn("Actively oscillating", report)
        self.assertIn("pausing the daily timer", report)

    def test_mixed_is_drift_and_lists_both(self):
        rows = _osc(546, "a", 2, 1) + _osc(679, "b", 3 * 24 + 2, 3 * 24 + 1)
        res = oversight.review(FakeStore(rows), now=NOW)
        self.assertEqual(res["verdict"], "DRIFT")
        self.assertEqual(set(res["oscillating_active"]), {546})
        self.assertEqual(set(res["oscillating_settled"]), {679})
        report = oversight.render(res)
        self.assertIn("Actively oscillating", report)
        self.assertIn("Settled", report)
        self.assertIn("other book(s) oscillated earlier", report)


class CleanAndErrors(unittest.TestCase):
    def test_single_run_heals_are_ok(self):
        # Two books healed once each, in the same run — no book in >1 run.
        rows = [_row(1, "run-x", _ago(hours=2)), _row(2, "run-x", _ago(hours=2))]
        res = oversight.review(FakeStore(rows), now=NOW)
        self.assertEqual(res["verdict"], "OK")
        self.assertEqual(res["oscillating"], {})
        self.assertIn("set-once holding", oversight.render(res))

    def test_errors_below_threshold_are_review(self):
        rows = [_row(1, "run-x", _ago(hours=2)), _row(2, "run-x", _ago(hours=2), ok=False)]
        res = oversight.review(FakeStore(rows), now=NOW)
        self.assertEqual(res["verdict"], "REVIEW")
        self.assertIn("write errors", oversight.render(res))

    def test_sustained_error_rate_is_drift(self):
        rows = [_row(i, "run-x", _ago(hours=2), ok=False) for i in range(1, 7)]
        res = oversight.review(FakeStore(rows), now=NOW)
        self.assertEqual(res["verdict"], "DRIFT")

    def test_out_of_window_heals_are_ignored(self):
        # An oscillation entirely older than the window must not count at all.
        rows = _osc(546, "old", 9 * 24, 8 * 24)
        res = oversight.review(FakeStore(rows), now=NOW)
        self.assertEqual(res["verdict"], "OK")
        self.assertEqual(res["oscillating"], {})


if __name__ == "__main__":
    unittest.main()
