"""verify() — work-level acquisition gate. Network-free (epub/hardcover mocked).
Run: python -m unittest -v tests.test_verify"""
import unittest
from unittest import mock

from colophon import resolver, verify


def _book(hcid, title="T", canonical_id=None):
    return {"hcid": hcid, "title": title, "canonical_id": canonical_id, "authors": []}


def _run(requested, sig, isbn_map=None, by_id=None):
    isbn_map, by_id = isbn_map or {}, by_id or {}
    with mock.patch.object(verify.epub, "inspect", return_value=sig), \
         mock.patch.object(verify.hardcover, "book_by_isbn", side_effect=lambda i: isbn_map.get(i)), \
         mock.patch.object(verify.hardcover, "book_by_id", side_effect=lambda h: by_id.get(str(h))):
        return verify.verify(requested, "x.epub")


def _run_llm(requested, sig, resolve_ret, by_id=None):
    """No-ISBN path: book_by_isbn resolves nothing, resolver.resolve is stubbed."""
    by_id = by_id or {}
    with mock.patch.object(verify.epub, "inspect", return_value=sig), \
         mock.patch.object(verify.hardcover, "book_by_isbn", return_value=None), \
         mock.patch.object(verify.hardcover, "book_by_id", side_effect=lambda h: by_id.get(str(h))), \
         mock.patch.object(resolver, "resolve", return_value=resolve_ret):
        return verify.verify(requested, "x.epub")


class Verify(unittest.TestCase):
    def test_match_by_id(self):
        # the live Endymion case: file ISBN -> the requested work id
        r = _run({"hcid": "438682"}, {"opf_isbns": ["9780307781925"]},
                 isbn_map={"9780307781925": _book("438682", "The Rise of Endymion")},
                 by_id={"438682": _book("438682", "The Rise of Endymion")})
        self.assertEqual(r["verdict"], verify.MATCH)
        self.assertEqual(r["source"], "isbn-id")

    def test_match_via_canonical(self):
        # file resolves to a duplicate record whose canonical IS the requested work
        r = _run({"hcid": "100"}, {"opf_isbns": ["978"]},
                 isbn_map={"978": _book("200", "T", canonical_id="100")},
                 by_id={"100": _book("100", "T")})
        self.assertEqual(r["verdict"], verify.MATCH)

    def test_mismatch_box_set(self):
        # requested a single book; the file embeds the omnibus ISBN -> different work
        r = _run({"hcid": "438682"}, {"opf_isbns": ["9999"]},
                 isbn_map={"9999": _book("555", "Hyperion Cantos 01-04")},
                 by_id={"438682": _book("438682", "The Rise of Endymion")})
        self.assertEqual(r["verdict"], verify.MISMATCH)
        self.assertEqual(r["file_hcid"], "555")

    def test_unverifiable_no_file_signal(self):
        r = _run({"hcid": "1"}, None)  # epub.inspect returns None (non-EPUB / unreadable)
        self.assertEqual(r["verdict"], verify.UNVERIFIABLE)
        self.assertEqual(r["source"], "no-file-signal")

    def test_unverifiable_isbn_unresolved_no_title(self):
        # ISBN doesn't resolve and the file has no title to adjudicate on → held, no LLM
        r = _run({"hcid": "1"}, {"opf_isbns": ["nope"]}, isbn_map={})
        self.assertEqual(r["verdict"], verify.UNVERIFIABLE)
        self.assertEqual(r["source"], "no-signal")

    def test_llm_match(self):
        sig = {"opf_isbns": [], "opf_title": "The Rise of Endymion", "opf_author": "Dan Simmons"}
        prop = {"action": "propose", "chosen_id": "438682",
                "chosen_title": "The Rise of Endymion", "confidence": 0.95}
        r = _run_llm({"hcid": "438682"}, sig, prop,
                     by_id={"438682": _book("438682", "The Rise of Endymion")})
        self.assertEqual(r["verdict"], verify.MATCH)
        self.assertEqual(r["source"], "llm-adjudicated")
        self.assertLessEqual(r["confidence"], 0.9)  # capped below the deterministic 0.97

    def test_llm_mismatch_summary(self):
        # the "Summary of Hooked" trap: a confident but different work
        sig = {"opf_isbns": [], "opf_title": "Summary of Hooked", "opf_author": "X"}
        prop = {"action": "propose", "chosen_id": "888238",
                "chosen_title": "Summary of Hooked", "confidence": 0.92}
        r = _run_llm({"hcid": "111"}, sig, prop,
                     by_id={"888238": _book("888238"), "111": _book("111")})
        self.assertEqual(r["verdict"], verify.MISMATCH)

    def test_llm_lowconf_is_unverifiable(self):
        sig = {"opf_isbns": [], "opf_title": "Ambiguous", "opf_author": ""}
        prop = {"action": "propose", "chosen_id": "1", "chosen_title": "Ambiguous", "confidence": 0.6}
        r = _run_llm({"hcid": "1"}, sig, prop, by_id={"1": _book("1")})
        self.assertEqual(r["verdict"], verify.UNVERIFIABLE)
        self.assertEqual(r["source"], "llm-lowconf")

    def test_llm_nomatch_is_unverifiable(self):
        r = _run_llm({"hcid": "1"}, {"opf_isbns": [], "opf_title": "Obscure Indie", "opf_author": ""},
                     {"action": "none", "reason": "no candidates"})
        self.assertEqual(r["verdict"], verify.UNVERIFIABLE)

    def test_llm_error_is_unverifiable(self):
        sig = {"opf_isbns": [], "opf_title": "T", "opf_author": ""}
        with mock.patch.object(verify.epub, "inspect", return_value=sig), \
             mock.patch.object(verify.hardcover, "book_by_isbn", return_value=None), \
             mock.patch.object(resolver, "resolve", side_effect=RuntimeError("api down")):
            r = verify.verify({"hcid": "1"}, "x.epub")
        self.assertEqual(r["verdict"], verify.UNVERIFIABLE)
        self.assertEqual(r["source"], "llm-error")

    def test_no_title_short_circuits_before_llm(self):
        with mock.patch.object(verify.epub, "inspect", return_value={"opf_isbns": []}), \
             mock.patch.object(verify.hardcover, "book_by_isbn", return_value=None), \
             mock.patch.object(resolver, "resolve") as res:
            r = verify.verify({"hcid": "1"}, "x.epub")
        res.assert_not_called()
        self.assertEqual(r["source"], "no-signal")

    def test_title_fallback_match(self):
        r = _run({"title": "The Rise of Endymion"}, {"opf_isbns": ["978"]},
                 isbn_map={"978": _book("438682", "The Rise of Endymion")})
        self.assertEqual(r["verdict"], verify.MATCH)
        self.assertEqual(r["source"], "isbn-title")

    def test_title_fallback_mismatch(self):
        r = _run({"title": "Some Other Book"}, {"opf_isbns": ["978"]},
                 isbn_map={"978": _book("438682", "The Rise of Endymion")})
        self.assertEqual(r["verdict"], verify.MISMATCH)

    def test_req_unresolved_holds_not_mismatch(self):
        # Requested work fails to resolve (provider hiccup) AND the file's id differs:
        # must HOLD (unverifiable), never MISMATCH — id-equality alone would wrongly
        # reject a same-work/different-edition file and hard-delete a good book.
        r = _run({"hcid": "438682"}, {"opf_isbns": ["978"]},
                 isbn_map={"978": _book("999", "Some Edition")},
                 by_id={})  # requested hc#438682 does not resolve
        self.assertEqual(r["verdict"], verify.UNVERIFIABLE)
        self.assertEqual(r["source"], "req-unresolved")

    def test_req_unresolved_same_id_still_matches(self):
        # Requested work fails to resolve but the file's id IS the requested id — the one
        # safe positive (raw id match), so MATCH rather than a needless hold.
        r = _run({"hcid": "438682"}, {"opf_isbns": ["978"]},
                 isbn_map={"978": _book("438682", "The Rise of Endymion")},
                 by_id={})
        self.assertEqual(r["verdict"], verify.MATCH)
        self.assertEqual(r["source"], "isbn-id")

    def test_llm_req_unresolved_holds_not_mismatch(self):
        # Same guard on the LLM-adjudicated path: chosen work resolves, requested does not,
        # ids differ → HOLD, not MISMATCH.
        sig = {"opf_isbns": [], "opf_title": "Some Book", "opf_author": "X"}
        prop = {"action": "propose", "chosen_id": "888238",
                "chosen_title": "Some Book", "confidence": 0.92}
        r = _run_llm({"hcid": "111"}, sig, prop,
                     by_id={"888238": _book("888238")})  # requested 111 does not resolve
        self.assertEqual(r["verdict"], verify.UNVERIFIABLE)
        self.assertEqual(r["source"], "req-unresolved")

    def test_isbn_lookup_raising_is_swallowed(self):
        # a flaky hardcover call must not crash the gate — it falls through to unverifiable
        with mock.patch.object(verify.epub, "inspect", return_value={"opf_isbns": ["x"]}), \
             mock.patch.object(verify.hardcover, "book_by_isbn", side_effect=RuntimeError("429")):
            r = verify.verify({"hcid": "1"}, "x.epub")
        self.assertEqual(r["verdict"], verify.UNVERIFIABLE)


if __name__ == "__main__":
    unittest.main()
