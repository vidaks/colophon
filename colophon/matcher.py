"""Deterministic matcher — produce a heal proposal for a book.

Phase 1a (low-risk, high-volume): a book that already carries a Hardcover id whose
title matches → fetch the canonical ISBN and propose healing when the stored ISBN
is missing/malformed. Sets/omnibuses and title-mismatches (possible mis-seeds) are
flagged for review, never auto-healed — that's Phase 1b (title/author resolution +
Haiku tiebreaker).

Proposal actions:
  heal            — set canonical ISBN (+id/slug) and refresh   [auto-eligible]
  ok              — already canonical                            [no-op]
  ok-altedition   — valid but non-canonical edition; leave it    [no-op]
  skip            — no canonical ISBN available                  [no-op]
  review-set      — looks like a set/omnibus                     [human/Phase 1b]
  review-misseed  — hcid title disagrees with the book           [human/Phase 1b]
  review          — no hcid (needs title/author resolution)      [Phase 1b]
"""
import re

from . import hardcover

_COLLECTION = re.compile(
    r"(?i)(books?\s+\d+\s*[-–]\s*\d+)|\bomnibus\b|\bcollection\b|\bboxed?\s*set\b"
    r"|\btrilogy\b|\banthology\b|\bcomplete\b")


def _norm(s):
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def _title_match(a, b):
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na in nb or nb in na:
        return True
    ta, tb = set(na.split()), set(nb.split())
    return len(ta & tb) / max(1, min(len(ta), len(tb))) >= 0.7


def _digits(s):
    return re.sub(r"\D", "", s or "")


def is_set(cand):
    """Set/omnibus heuristic from POC1: collection title, or low users + big pages."""
    if _COLLECTION.search(cand.get("title") or ""):
        return True
    return (cand.get("users_count") or 0) < 100 and (cand.get("pages") or 0) >= 1000


def propose(snap):
    out = {"book_id": None, "action": "review", "reason": "",
           "isbn": None, "hcid": None, "slug": None, "confidence": 0.0,
           "hc_title": None}
    if not snap:
        out["reason"] = "book not found"
        return out
    hcid = (snap.get("hardcover_book_id") or "").strip()
    cur = (snap.get("isbn_13") or "").strip()
    if not hcid:
        out["reason"] = "no hardcover id (needs title/author resolution — Phase 1b)"
        return out
    try:
        cand = hardcover.book_by_id(hcid)
    except Exception as e:
        out["reason"] = f"hardcover lookup failed: {e}"
        return out
    if not cand:
        out["reason"] = f"hardcover id {hcid} not found"
        return out
    out["hc_title"] = cand["title"]
    if is_set(cand):
        out.update(action="review-set",
                   reason=f"set/omnibus (users={cand['users_count']}, pages={cand['pages']})")
        return out
    if not _title_match(snap.get("title"), cand["title"]):
        out.update(action="review-misseed",
                   reason=f"title mismatch: {snap.get('title')!r} vs hcid {cand['title']!r}")
        return out
    canon = cand["isbn"]
    if not canon:
        out.update(action="skip", reason="no canonical ISBN in Hardcover")
        return out
    if len(_digits(cur)) == 13:
        out.update(action=("ok" if _digits(cur) == canon else "ok-altedition"),
                   reason=("already canonical" if _digits(cur) == canon
                           else f"valid non-canonical edition ({cur}); left as-is"))
        return out
    # stored ISBN is missing/malformed and the identity is right → heal.
    out.update(action="heal", isbn=canon, hcid=str(cand["hcid"]), slug=cand["slug"],
               confidence=0.95, reason=f"set canonical ISBN {canon} (was {cur or 'none'})")
    return out
