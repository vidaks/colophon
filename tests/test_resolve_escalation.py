"""run_resolve EPUB-escalation glue — pure logic, mocked I/O. Run: python -m unittest -v"""
import unittest
from unittest import mock

from colophon import audit, resolver

_BOOK = {"book_id": 1, "title": "X", "authors": "Y"}


def _propose(conf, source=None, cid="a"):
    return {"book_id": 1, "title": "X", "action": "propose", "confidence": conf,
            "chosen_id": cid, "chosen_title": "X", "isbn": "9780000000001",
            "slug": "x", "source": source}


class AdjudicationBreaker(unittest.TestCase):
    def test_adjudication_errors_trip_the_breaker(self):
        # An Anthropic/Hardcover outage returns action=error for every book; the run
        # must stop at ABORT_ERRORS instead of walking (and paying for) the whole list.
        books = [{"book_id": i, "title": f"B{i}", "authors": ""} for i in range(1, 7)]
        err = lambda b, file_signals=None: {  # noqa: E731
            "book_id": b["book_id"], "title": b["title"], "action": "error", "reason": "api down"}
        with mock.patch.object(audit, "all_books", return_value=books), \
             mock.patch.object(resolver, "_epub_signals", return_value=None), \
             mock.patch.object(resolver, "resolve", side_effect=err), \
             mock.patch.object(resolver.time, "sleep", new=lambda s: None):
            out = resolver.run_resolve(book_ids=[1, 2, 3, 4, 5, 6], apply=False, store=None)
        self.assertEqual(len(out["proposals"]), resolver.ABORT_ERRORS)
        self.assertTrue(out["proposals"][-1].get("aborted"))


class Escalation(unittest.TestCase):
    def test_below_threshold_inspects_and_swaps_in_better(self):
        seq = [_propose(0.6), _propose(0.97, source="epub-opf", cid="b")]  # first pass, then escalated
        with mock.patch.object(audit, "all_books", return_value=[_BOOK]), \
             mock.patch.object(resolver, "_epub_signals", return_value={"opf_isbns": ["9780000000001"]}) as sig, \
             mock.patch.object(resolver, "resolve", side_effect=seq) as res:
            out = resolver.run_resolve(book_ids=[1], apply=False, store=None, min_conf=0.9)
        sig.assert_called_once_with(1)
        self.assertEqual(res.call_count, 2)
        self.assertIsNotNone(res.call_args.kwargs.get("file_signals"))   # 2nd call carried the signals
        self.assertEqual(out["proposals"][0]["source"], "epub-opf")
        self.assertEqual(out["proposals"][0]["confidence"], 0.97)

    def test_above_threshold_never_inspects(self):
        with mock.patch.object(audit, "all_books", return_value=[_BOOK]), \
             mock.patch.object(resolver, "_epub_signals") as sig, \
             mock.patch.object(resolver, "resolve", side_effect=[_propose(0.95)]):
            out = resolver.run_resolve(book_ids=[1], apply=False, store=None, min_conf=0.9)
        sig.assert_not_called()
        self.assertEqual(out["proposals"][0]["confidence"], 0.95)

    def test_escalation_kept_only_when_strictly_better(self):
        seq = [_propose(0.6, cid="a"), _propose(0.5, source="epub-colophon", cid="b")]  # worse → discard
        with mock.patch.object(audit, "all_books", return_value=[_BOOK]), \
             mock.patch.object(resolver, "_epub_signals", return_value={"colophon_text": "x"}), \
             mock.patch.object(resolver, "resolve", side_effect=seq):
            out = resolver.run_resolve(book_ids=[1], apply=False, store=None, min_conf=0.9)
        self.assertEqual(out["proposals"][0]["chosen_id"], "a")        # kept the original
        self.assertIsNone(out["proposals"][0]["source"])


if __name__ == "__main__":
    unittest.main()
