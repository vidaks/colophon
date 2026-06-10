"""series_audit categorization + the maintain series phase. No network, no writes.
Run: python -m unittest -v

audit_one is pure once the Hardcover lookup is stubbed, so most coverage targets it
directly. The grouping (series-name-missing) path is the new surface (Symptom 1).
"""
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

    def test_ungrouped_already_canonical_not_reheal(self):
        # Same ISBN as Hardcover's canonical → re-heal would not change identity;
        # leave it (a grimmory derivation gap, not ours to churn on).
        _stub_lookup({"100": {"series": "Dune", "position": 1, "isbn": "9780441013593"}})
        cat, reason, fix = series_audit.audit_one(_row(isbn="978-0-441-01359-3"), {})
        self.assertEqual(cat, "series-name-missing")
        self.assertIsNone(fix)
        self.assertIn("already on canonical", reason)

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
