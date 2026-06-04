"""Skip-list logic — pure / SQLite only, no network. Run: python -m unittest -v"""
import os
import tempfile
import unittest

from colophon import resolver
from colophon.store import Store


def _book(book_id=1, title="The Wool Trilogy", authors="Hugh Howey"):
    return {"book_id": book_id, "title": title, "authors": authors}


class Fingerprint(unittest.TestCase):
    def test_stable_and_normalized(self):
        a = resolver._fingerprint(_book(title="The Wool Trilogy", authors="Hugh Howey"))
        b = resolver._fingerprint(_book(title="  the   WOOL  trilogy ", authors="hugh howey"))
        self.assertEqual(a, b)                       # case/whitespace/punctuation-insensitive

    def test_changes_with_title_or_author(self):
        base = resolver._fingerprint(_book())
        self.assertNotEqual(base, resolver._fingerprint(_book(title="Wool Omnibus")))
        self.assertNotEqual(base, resolver._fingerprint(_book(authors="Someone Else")))

    def test_no_builtin_hash(self):
        # A normalized string, never the per-process-salted builtin hash().
        fp = resolver._fingerprint(_book())
        self.assertIsInstance(fp, str)
        self.assertIn("wool", fp)


class Expiry(unittest.TestCase):
    def test_zero_days_never_expires(self):
        self.assertFalse(resolver._expired("1970-01-01T00:00:00", days=0))

    def test_old_entry_expires_with_ttl(self):
        self.assertTrue(resolver._expired("1970-01-01T00:00:00", days=30))
        now = __import__("time").strftime("%Y-%m-%dT%H:%M:%S")
        self.assertFalse(resolver._expired(now, days=30))


class StoreRoundtrip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(path=os.path.join(self.dir, "t.db"))

    def test_put_get_clear(self):
        self.store.skip_put(7, "Dune Saga", "fp-1", "none", reason="no candidates")
        m = self.store.skip_map()
        self.assertIn(7, m)
        self.assertEqual(m[7]["action"], "none")
        self.assertEqual(m[7]["attempts"], 1)
        self.assertEqual(self.store.skip_clear(7), 1)
        self.assertEqual(self.store.skip_map(), {})

    def test_upsert_increments_attempts_keeps_first_seen(self):
        self.store.skip_put(7, "Dune Saga", "fp-1", "none")
        first = self.store.skip_map()[7]["first_seen"]
        self.store.skip_put(7, "Dune Saga", "fp-1", "none")
        row = self.store.skip_map()[7]
        self.assertEqual(row["attempts"], 2)
        self.assertEqual(row["first_seen"], first)

    def test_clear_all(self):
        self.store.skip_put(1, "a", "f1", "none")
        self.store.skip_put(2, "b", "f2", "none")
        self.assertEqual(self.store.skip_clear(), 2)


class CachedSkipFilter(unittest.TestCase):
    def test_matches_only_on_same_fingerprint(self):
        b = _book(book_id=5)
        skips = {5: {"fingerprint": resolver._fingerprint(b),
                     "last_seen": "2999-01-01T00:00:00"}}
        self.assertIsNotNone(resolver._cached_skip(b, skips, days=0))

    def test_no_skip_when_title_changed(self):
        b = _book(book_id=5, title="Original")
        skips = {5: {"fingerprint": resolver._fingerprint(b),
                     "last_seen": "2999-01-01T00:00:00"}}
        moved = _book(book_id=5, title="Corrected Title")
        self.assertIsNone(resolver._cached_skip(moved, skips, days=0))

    def test_no_skip_when_absent(self):
        self.assertIsNone(resolver._cached_skip(_book(book_id=9), {}, days=0))


class RecordSkip(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.store = Store(path=os.path.join(self.dir, "t.db"))

    def test_none_is_recorded(self):
        b = _book(book_id=10)
        resolver._record_skip(self.store, b, {"action": "none", "reason": "no candidates"})
        row = self.store.skip_map()[10]
        self.assertEqual(row["action"], "none")
        self.assertEqual(row["fingerprint"], resolver._fingerprint(b))

    def test_below_threshold_propose_keeps_match_detail(self):
        b = _book(book_id=11)
        resolver._record_skip(self.store, b, {
            "action": "propose", "confidence": 0.85, "chosen_id": "430000",
            "chosen_title": "The Phoenix Project", "isbn": "9780988262577"})
        row = self.store.skip_map()[11]
        self.assertEqual(row["action"], "propose-below")
        self.assertEqual(row["chosen_id"], "430000")
        self.assertEqual(row["isbn"], "9780988262577")

    def test_applied_clears_existing_skip(self):
        b = _book(book_id=12)
        resolver._record_skip(self.store, b, {"action": "none"})
        self.assertIn(12, self.store.skip_map())
        resolver._record_skip(self.store, b, {"action": "propose", "applied": True})
        self.assertNotIn(12, self.store.skip_map())

    def test_transient_errors_not_recorded(self):
        b = _book(book_id=13)
        resolver._record_skip(self.store, b, {"action": "error", "reason": "Haiku timeout"})
        resolver._record_skip(self.store, _book(book_id=14),
                              {"action": "propose", "apply_error": "grimmory 500"})
        self.assertEqual(self.store.skip_map(), {})


if __name__ == "__main__":
    unittest.main()
