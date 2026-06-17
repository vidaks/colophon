"""series_audit categorization + the maintain series phase. No network, no writes.
Run: python -m unittest -v

audit_one is pure once the Hardcover lookup is stubbed, so most coverage targets it
directly. The grouping (series-name-missing) path is the new surface (Symptom 1).
"""
import time
import unittest

from colophon import maintain, series_audit


def _stub_lookup(mapping):
    """Replace series_audit's Hardcover lookup with a dict keyed by hcid."""
    series_audit.audit._book_by_id = lambda hcid: mapping.get(str(hcid))


def _row(**kw):
    d = {"book_id": 1, "title": "T", "hcid": "100", "series_name": "", "series_number": "",
         "isbn": "", "num_locked": False, "name_locked": False}
    d.update(kw)
    return d


class AuditOne(unittest.TestCase):
    def setUp(self):
        self._orig = series_audit.audit._book_by_id

    def tearDown(self):
        series_audit.audit._book_by_id = self._orig

    def test_ungrouped_heals_to_canonical(self):
        # null series_name + non-canonical 13-char ISBN + Hardcover series → heal.
        _stub_lookup({"100": {"series": "Foundation", "position": 2,
                              "isbn": "9780553293357"}})
        b = _row(series_name="", isbn="9788424117788")  # foreign edition
        cat, reason, fix = series_audit.audit_one(b, {})
        self.assertEqual(cat, "series-name-missing")
        self.assertEqual(fix, ("9780553293357", "100"))
        self.assertIn("Foundation", reason)

    def test_ungrouped_standalone_is_left(self):
        _stub_lookup({"100": {"series": None, "position": None, "isbn": "9780553293357"}})
        cat, _, fix = series_audit.audit_one(_row(isbn="9780553293357"), {})
        self.assertEqual(cat, "no-series")
        self.assertIsNone(fix)

    def test_ungrouped_already_canonical_refreshes_to_rederive(self):
        # Same ISBN as Hardcover's canonical → no ISBN swap to make, but Hardcover
        # DOES place it in a series, so a plain REPLACE_MISSING refresh re-derives it.
        _stub_lookup({"100": {"series": "Dune", "position": 1, "isbn": "9780441013593"}})
        cat, reason, fix = series_audit.audit_one(_row(isbn="978-0-441-01359-3"), {})
        self.assertEqual(cat, "series-name-missing")
        self.assertEqual(fix, (series_audit.REFRESH_MISSING, "100"))
        self.assertIn("refresh to re-derive", reason)

    def test_ungrouped_no_canonical_isbn_refreshes_to_rederive(self):
        # Hardcover has the series but exposes no canonical ISBN to swap to → the
        # book's existing ISBN still re-derives the series on a REPLACE_MISSING refresh.
        _stub_lookup({"100": {"series": "Dune", "position": 1, "isbn": None}})
        cat, reason, fix = series_audit.audit_one(_row(isbn="9788424117788"), {})
        self.assertEqual(cat, "series-name-missing")
        self.assertEqual(fix, (series_audit.REFRESH_MISSING, "100"))

    def test_ungrouped_name_locked_not_healed(self):
        _stub_lookup({"100": {"series": "Dune", "position": 1, "isbn": "9780441013593"}})
        cat, reason, fix = series_audit.audit_one(_row(isbn="9788424117788", name_locked=True), {})
        self.assertEqual(cat, "series-name-missing")
        self.assertIsNone(fix)
        self.assertIn("LOCKED", reason)

    def test_no_hcid_left(self):
        cat, _, fix = series_audit.audit_one(_row(hcid="", series_name="Dune"), {})
        self.assertEqual(cat, "no-hcid")
        self.assertIsNone(fix)

    def test_number_mismatch_still_heals(self):
        _stub_lookup({"100": {"series": "Dune", "position": 5, "isbn": "9780441013593"}})
        b = _row(series_name="Dune", series_number="2", isbn="9788424117788")
        cat, _, fix = series_audit.audit_one(b, {})
        self.assertEqual(cat, "number-mismatch")
        self.assertEqual(fix, ("9780441013593", "100"))

    def test_number_ok(self):
        _stub_lookup({"100": {"series": "Dune", "position": 5, "isbn": "9780441013593"}})
        b = _row(series_name="Dune", series_number="5")
        cat, _, fix = series_audit.audit_one(b, {})
        self.assertEqual(cat, "number-ok")
        self.assertIsNone(fix)

    def test_series_name_variant_heals_when_title_matches(self):
        # Name differs but the book TITLE corroborates the hcid → it's a stale
        # variant name; heal to canonical so grimmory re-derives the right one.
        _stub_lookup({"100": {"title": "The Way of Kings", "series": "The Stormlight Archive",
                              "position": 1, "isbn": "9780765326355"}})
        b = _row(title="The Way of Kings", series_name="Cosmere Saga",
                 series_number="1", isbn="9788401021234")  # foreign edition
        cat, _, fix = series_audit.audit_one(b, {})
        self.assertEqual(cat, "series-name-variant")
        self.assertEqual(fix, ("9780765326355", "100"))

    def test_series_name_variant_already_canonical_left(self):
        _stub_lookup({"100": {"title": "The Way of Kings", "series": "The Stormlight Archive",
                              "position": 1, "isbn": "9780765326355"}})
        b = _row(title="The Way of Kings", series_name="Cosmere Saga", isbn="9780765326355")
        cat, _, fix = series_audit.audit_one(b, {})
        self.assertEqual(cat, "series-name-variant")
        self.assertIsNone(fix)

    def test_series_mismatch_deferred_when_title_also_differs(self):
        # Name AND title disagree → the hcid itself is suspect (a real mis-seed):
        # leave it for the resolver, do NOT lock a wrong identity.
        _stub_lookup({"100": {"title": "Dune", "series": "Dune", "position": 1,
                              "isbn": "9780441013593"}})
        b = _row(title="The Hobbit", series_name="Middle-earth", series_number="1")
        cat, _, fix = series_audit.audit_one(b, {})
        self.assertEqual(cat, "series-mismatch")
        self.assertIsNone(fix)


class RunSurvey(unittest.TestCase):
    """run() must fold the ungrouped survey in and categorize it (apply=False)."""

    def setUp(self):
        self._db, self._lookup = series_audit.grimmory._db, series_audit.audit._book_by_id

        def fake_db(sql):
            if "series_name IS NULL OR" in sql:  # ungrouped survey
                return "9\tThe Ungrouped One\t200\t9788424117788\t0"
            return ""  # no books already carrying a series_name

        series_audit.grimmory._db = fake_db
        _stub_lookup({"200": {"series": "Foundation", "position": 3, "isbn": "9780553293357"}})

    def tearDown(self):
        series_audit.grimmory._db, series_audit.audit._book_by_id = self._db, self._lookup

    def test_ungrouped_book_is_categorized(self):
        res = series_audit.run(apply=False)
        self.assertEqual(res["total"], 1)
        recs = res["categories"]["series-name-missing"]
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["fix"], ("9780553293357", "200"))


class RunRegroup(unittest.TestCase):
    """An ungrouped book that needs no ISBN swap is re-derived via one batched
    REPLACE_MISSING refresh — never the ISBN-swap heal path."""

    def setUp(self):
        self._db = series_audit.grimmory._db
        self._lookup = series_audit.audit._book_by_id
        self._precond = series_audit.assert_preconditions
        series_audit.assert_preconditions = lambda g: None

        def fake_db(sql):
            if "series_name IS NULL OR" in sql:  # ungrouped book 9, already on canonical ISBN
                return "9\tThe Ungrouped One\t200\t9780553293357\t0"
            return ""

        series_audit.grimmory._db = fake_db
        _stub_lookup({"200": {"series": "Foundation", "position": 3, "isbn": "9780553293357"}})

    def tearDown(self):
        series_audit.grimmory._db = self._db
        series_audit.audit._book_by_id = self._lookup
        series_audit.assert_preconditions = self._precond

    def test_dry_run_flags_refresh_missing_but_calls_nothing(self):
        res = series_audit.run(apply=False)
        rec = res["categories"]["series-name-missing"][0]
        self.assertEqual(rec["fix"], (series_audit.REFRESH_MISSING, "200"))
        self.assertFalse(rec["applied"])
        self.assertEqual(res["regrouped"], 0)

    def test_apply_batches_one_replace_missing_refresh(self):
        class FakeGrimmory:
            def __init__(self):
                self.calls = []

            def refresh(self, ids, refresh_covers=True, replace_mode="REPLACE_ALL"):
                self.calls.append((list(ids), replace_mode))
                return "ok"

        g = FakeGrimmory()
        res = series_audit.run(apply=True, g=g, store=None)
        self.assertEqual(res["regrouped"], 1)
        self.assertEqual(g.calls, [([9], "REPLACE_MISSING")])
        self.assertTrue(res["categories"]["series-name-missing"][0]["applied"])


class _FakeStore:
    """Minimal store exposing just the verdict-cache surface run() touches."""

    def __init__(self, cache=None):
        self.cache = dict(cache or {})  # book_id -> (fingerprint, verdict, expires_at)
        self.puts, self.deletes = [], []

    def series_verdict_map(self):
        return dict(self.cache)

    def series_verdict_put(self, book_id, fingerprint, verdict, expires_at):
        self.puts.append((book_id, fingerprint, verdict))
        self.cache[book_id] = (fingerprint, verdict, expires_at)

    def series_verdict_delete(self, book_id):
        self.deletes.append(book_id)
        self.cache.pop(book_id, None)

    def series_verdict_prune(self, before=None):
        return 0

    def new_run_id(self):
        return "run-test"


class VerdictCache(unittest.TestCase):
    """A settled verdict is cached and then re-used without re-querying Hardcover,
    unless the identity changed, the entry expired, --force, or the book is a dup."""

    def setUp(self):
        self._db, self._lookup = series_audit.grimmory._db, series_audit.audit._book_by_id
        self.lookups = []

        def fake_db(sql):
            if "series_name IS NULL OR" in sql:  # no ungrouped books
                return ""
            # one in-series book that is correct → number-ok (cacheable)
            return "9\tDune 5\t100\tDune\t5\t9780441013593\t0\t0"

        def counting_lookup(hcid):
            self.lookups.append(hcid)
            return {"series": "Dune", "position": 5, "isbn": "9780441013593", "title": "Dune"}

        series_audit.grimmory._db = fake_db
        series_audit.audit._book_by_id = counting_lookup

    def tearDown(self):
        series_audit.grimmory._db, series_audit.audit._book_by_id = self._db, self._lookup

    def _fp(self):
        return series_audit._fingerprint(
            {"hcid": "100", "isbn": "9780441013593", "series_name": "Dune", "series_number": "5"})

    def test_miss_caches_then_hit_skips_the_lookup(self):
        store = _FakeStore()
        r1 = series_audit.run(store=store)
        self.assertEqual(len(self.lookups), 1)
        self.assertEqual(r1["cached"], 0)
        self.assertEqual([p[2] for p in store.puts], ["number-ok"])

        self.lookups.clear()
        r2 = series_audit.run(store=store)
        self.assertEqual(self.lookups, [])        # cache hit — no Hardcover query
        self.assertEqual(r2["cached"], 1)
        self.assertEqual(len(r2["categories"]["number-ok"]), 1)

    def test_force_bypasses_the_cache(self):
        store = _FakeStore({9: (self._fp(), "number-ok", time.time() + 1e6)})
        series_audit.run(store=store, force=True)
        self.assertEqual(len(self.lookups), 1)    # re-queried despite a fresh entry

    def test_expired_entry_is_requeried(self):
        store = _FakeStore({9: (self._fp(), "number-ok", time.time() - 1)})
        series_audit.run(store=store)
        self.assertEqual(len(self.lookups), 1)

    def test_changed_identity_is_requeried(self):
        store = _FakeStore({9: ("stale-fingerprint", "number-ok", time.time() + 1e6)})
        series_audit.run(store=store)
        self.assertEqual(len(self.lookups), 1)
        self.assertEqual(store.deletes, [])       # overwritten by a fresh put, not deleted


class VerdictCacheDuplicates(unittest.TestCase):
    """A shared-hcid book is never cache-skipped: dup-overlap is cross-book."""

    def setUp(self):
        self._db, self._lookup = series_audit.grimmory._db, series_audit.audit._book_by_id
        self.lookups = []

        def fake_db(sql):
            if "series_name IS NULL OR" in sql:
                return ""
            return ("9\tDune 5\t100\tDune\t5\t9780441013593\t0\t0\n"
                    "10\tDune 5 dup\t100\tDune\t5\t9780441013593\t0\t0")  # same hcid 100

        def counting_lookup(hcid):
            self.lookups.append(hcid)
            return {"series": "Dune", "position": 5, "isbn": "9780441013593", "title": "Dune"}

        series_audit.grimmory._db = fake_db
        series_audit.audit._book_by_id = counting_lookup

    def tearDown(self):
        series_audit.grimmory._db, series_audit.audit._book_by_id = self._db, self._lookup

    def test_duplicate_hcid_is_dup_overlap_and_not_cached(self):
        store = _FakeStore()
        res = series_audit.run(store=store)
        self.assertEqual(len(res["categories"]["dup-overlap"]), 2)
        self.assertEqual(store.puts, [])          # dup-overlap is never cached
        self.assertEqual(self.lookups, [])        # dup is decided before any lookup


class MaintainSeriesPhase(unittest.TestCase):
    def setUp(self):
        self._bf, self._rs, self._sa, self._stuck = (
            maintain.backfill.run, maintain.run_resolve, maintain.series_audit.run,
            maintain._gather_stuck)
        maintain.backfill.run = lambda *a, **k: {"errors": 0, "aborted": False, "proposals": [], "healed": 0}
        maintain.run_resolve = lambda *a, **k: {"proposals": []}
        maintain._gather_stuck = lambda store: []

    def tearDown(self):
        (maintain.backfill.run, maintain.run_resolve, maintain.series_audit.run,
         maintain._gather_stuck) = self._bf, self._rs, self._sa, self._stuck

    def test_series_phase_runs(self):
        maintain.series_audit.run = lambda **k: {
            "total": 3, "healed": 1, "errors": 0, "run_id": None, "apply": False,
            "categories": {"series-name-missing": [
                {"applied": True, "book": {"book_id": 9, "title": "X"}, "reason": "ungrouped → series 'Y'"}]}}
        res = maintain.run_maintain(None, None, apply=False)
        self.assertTrue(res["ok"])
        self.assertEqual(res["series"]["healed"], 1)
        self.assertIn("Series (numbering + grouping)", maintain.render_summary(res))

    def test_series_failure_isolated(self):
        def _boom(**k):
            raise RuntimeError("hardcover down")

        maintain.series_audit.run = _boom
        res = maintain.run_maintain(None, None, apply=False)
        self.assertFalse(res["ok"])
        self.assertTrue(any(e.startswith("series:") for e in res["errors"]))
        # The earlier phases still ran — one failure must not skip the rest.
        self.assertIsNotNone(res["backfill"])
        self.assertIsNotNone(res["resolve"])


if __name__ == "__main__":
    unittest.main()
