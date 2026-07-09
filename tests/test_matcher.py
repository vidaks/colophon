"""matcher — the logic that gates every auto-write. No network (lookup mocked).
Run: python -m unittest -v tests.test_matcher

The loose/strict split is the safety boundary: _title_match may only ROUTE a book,
_title_match_strict is the bar for a WRITE. The regression that matters most here:
a book titled "Foundation" carrying the hcid of "Foundation and Empire" must never
be healed (and locked) on a substring match.
"""
import unittest
from unittest import mock

from colophon import matcher


def _cand(**kw):
    d = {"hcid": 100, "slug": "s", "title": "T", "isbn": "9780000000000",
         "users_count": 5000, "pages": 300}
    d.update(kw)
    return d


class TitleMatchLoose(unittest.TestCase):
    def test_normalized_equality(self):
        self.assertTrue(matcher._title_match("  The WAY of Kings! ", "the way of kings"))

    def test_substring_matches(self):
        # Documents the loose semantics — good enough to route, never to write.
        self.assertTrue(matcher._title_match("Foundation", "Foundation and Empire"))
        self.assertTrue(matcher._title_match("Dune Messiah", "Dune"))

    def test_disjoint_fails(self):
        self.assertFalse(matcher._title_match("The Hobbit", "Dune"))

    def test_empty_never_matches(self):
        self.assertFalse(matcher._title_match("", "Dune"))
        self.assertFalse(matcher._title_match(None, None))


class TitleMatchStrict(unittest.TestCase):
    def test_normalized_equality(self):
        self.assertTrue(matcher._title_match_strict("  The WAY of Kings ", "The Way of Kings"))

    def test_subtitle_stripped_equality(self):
        self.assertTrue(matcher._title_match_strict("Mistborn: The Final Empire", "Mistborn"))
        self.assertTrue(matcher._title_match_strict("Dune", "Dune: 40th Anniversary Edition"))

    def test_prefix_titles_fail(self):
        # The wrong-volume/wrong-work shape substring matching used to bless.
        self.assertFalse(matcher._title_match_strict("Foundation", "Foundation and Empire"))
        self.assertFalse(matcher._title_match_strict("Dune", "Dune Messiah"))

    def test_colon_siblings_fail(self):
        # Subtitle-strip is one-sided only — prefix-vs-prefix would bless every
        # same-franchise colon-titled sibling volume.
        self.assertFalse(matcher._title_match_strict("Star Wars: Thrawn",
                                                     "Star Wars: Thrawn Ascendancy"))
        self.assertFalse(matcher._title_match_strict("Mistborn: The Final Empire",
                                                     "Mistborn: The Well of Ascension"))

    def test_empty_never_matches(self):
        self.assertFalse(matcher._title_match_strict("", "Dune"))
        self.assertFalse(matcher._title_match_strict("Dune", None))


class IsSet(unittest.TestCase):
    def test_collection_words(self):
        for t in ("Wool Omnibus", "The Complete Robot", "Books 1-3",
                  "The Deed of Paksenarrion Trilogy", "Boxed Set"):
            self.assertTrue(matcher.is_set(_cand(title=t)), t)

    def test_obscure_and_huge_is_a_set(self):
        self.assertTrue(matcher.is_set(_cand(users_count=10, pages=1500)))

    def test_normal_book_is_not(self):
        self.assertFalse(matcher.is_set(_cand()))


class Propose(unittest.TestCase):
    def _propose(self, snap, cand):
        with mock.patch.object(matcher.hardcover, "book_by_id", return_value=cand):
            return matcher.propose(snap)

    def _snap(self, **kw):
        d = {"title": "T", "isbn_13": "", "hardcover_book_id": "100"}
        d.update(kw)
        return d

    def test_no_hcid_needs_resolution(self):
        p = matcher.propose(self._snap(hardcover_book_id=""))
        self.assertEqual(p["action"], "review")

    def test_heal_on_strict_match_and_broken_isbn(self):
        p = self._propose(self._snap(isbn_13="123"), _cand())
        self.assertEqual(p["action"], "heal")
        self.assertEqual((p["isbn"], p["hcid"], p["slug"]), ("9780000000000", "100", "s"))

    def test_loose_only_match_is_never_healed(self):
        # THE regression: same-series sibling title must go to the resolver, not a lock.
        p = self._propose(self._snap(title="Foundation", isbn_13=""),
                          _cand(title="Foundation and Empire"))
        self.assertEqual(p["action"], "review-misseed")
        self.assertIn("loosely", p["reason"])

    def test_title_mismatch_is_misseed(self):
        p = self._propose(self._snap(title="The Hobbit"), _cand(title="Dune"))
        self.assertEqual(p["action"], "review-misseed")

    def test_set_is_flagged_not_healed(self):
        p = self._propose(self._snap(), _cand(title="T Omnibus"))
        self.assertEqual(p["action"], "review-set")

    def test_valid_isbn_is_left_alone(self):
        p = self._propose(self._snap(isbn_13="9780000000000"), _cand())
        self.assertEqual(p["action"], "ok")
        p = self._propose(self._snap(isbn_13="9781111111111"), _cand())
        self.assertEqual(p["action"], "ok-altedition")

    def test_no_canonical_isbn_skips(self):
        p = self._propose(self._snap(), _cand(isbn=None))
        self.assertEqual(p["action"], "skip")

    def test_unknown_hcid_stays_review(self):
        p = self._propose(self._snap(), None)
        self.assertEqual(p["action"], "review")
        self.assertIn("not found", p["reason"])


if __name__ == "__main__":
    unittest.main()
