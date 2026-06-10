"""Plan 20 Phase 3 — whole-library series-numbering audit (read-only by default).

Compares each owned book's grimmory (series_name, series_number) against Hardcover's
authoritative (series, book_series.position). A wrong number is almost always a wrong
*edition* (a foreign/alt ISBN whose embedded series position differs) — so the fix is
the SAME proven heal as everything else: set the canonical ISBN + hcid, lock, refresh,
and grimmory repopulates series_number from Hardcover's position (verified live on book
590 "Zero Hour": German-edition ISBN → number 2; heal → canonical ISBN → number 5).

Runs as a guarded phase of the nightly sweep (`maintain`) and standalone via
`series-audit` (read-only unless `--apply`). `--apply` heals the clean cases —
number-mismatch / number-missing and the grouping repairs series-name-missing /
series-name-variant — gated, dry-run by default. Only a *true* mis-seed
(series-mismatch: the name AND the title disagree with the hcid) stays deferred
to the resolver, which validates the identity against candidates before locking.

Categories:
  number-mismatch     : series matches, grimmory number != Hardcover position  → FIX (heal)
  number-missing      : series matches, grimmory number null, Hardcover has one → FIX (heal)
  series-name-missing : grimmory has no series_name, Hardcover puts it in one   → FIX (heal)
  series-name-variant : name differs but the book TITLE matches the hcid        → FIX (heal)
  series-mismatch     : name AND title differ → the hcid itself is suspect       → resolver
  dup-overlap         : hcid shared with another owned book                      → plan 21
  no-position         : Hardcover has no position for this id                    → leave
  no-series           : no series in grimmory or Hardcover (standalone)          → leave
  no-hcid             : in a series but unidentified                            → leave
  number-ok           : grimmory number == Hardcover position

The `series-name-missing` path (Symptom 1 — owned books that fall out of their
series because grimmory never derived a `series_name`) reuses the exact heal
recipe: set the canonical ISBN + hcid, lock, refresh, and grimmory repopulates
BOTH series_name and series_number. It heals only books on a *non-canonical*
13-char edition (current ISBN != Hardcover's canonical) — books already on the
canonical edition with a null name are a grimmory-side derivation gap, not ours
to churn on. Broken-ISBN books are left to the backfill phase. A *disagreeing*
name (series-mismatch) is still deferred to the resolver: the disagreement is
itself evidence the hcid may be wrong, so it needs adjudication before a lock.
"""
import re
import time
from collections import defaultdict

from . import audit, grimmory, hardcover, matcher
from .heal import assert_preconditions, heal_book

ABORT_ERRORS = 3
UNGROUPED_LIMIT = 60  # per-run cap on the ungrouped (null series_name) survey

_SQL = (
    "SELECT bm.book_id, IFNULL(bm.title,''), IFNULL(bm.hardcover_book_id,''), "
    "IFNULL(bm.series_name,''), IFNULL(bm.series_number,''), IFNULL(bm.isbn_13,''), "
    "IFNULL(bm.series_number_locked,0), IFNULL(bm.series_name_locked,0) "
    "FROM book_metadata bm JOIN book b ON b.id=bm.book_id "
    "WHERE (b.deleted IS NULL OR b.deleted=0) "
    "AND bm.series_name IS NOT NULL AND bm.series_name<>'';"
)


_UNGROUPED_SQL = (
    "SELECT bm.book_id, IFNULL(bm.title,''), IFNULL(bm.hardcover_book_id,''), "
    "IFNULL(bm.isbn_13,''), IFNULL(bm.series_name_locked,0) "
    "FROM book_metadata bm JOIN book b ON b.id=bm.book_id "
    "WHERE (b.deleted IS NULL OR b.deleted=0) "
    "AND bm.hardcover_book_id IS NOT NULL AND bm.hardcover_book_id<>'' "
    "AND (bm.series_name IS NULL OR bm.series_name='') "
    "AND bm.isbn_13 IS NOT NULL AND LENGTH(REPLACE(bm.isbn_13,'-',''))=13 "
    "ORDER BY bm.book_id LIMIT {limit};"
)


def _series_books():
    out, rows = grimmory._db(_SQL), []
    for line in out.splitlines():
        c = line.split("\t")
        if len(c) < 8:
            continue
        rows.append({"book_id": int(c[0]), "title": c[1], "hcid": c[2].strip(),
                     "series_name": c[3], "series_number": c[4].strip(),
                     "isbn": c[5].strip(), "num_locked": c[6] == "1", "name_locked": c[7] == "1"})
    return rows


def _ungrouped_candidates(limit):
    """hcid'd books with a clean 13-char ISBN but no series_name — possible
    ungrouped series members (Symptom 1). Bounded: the cap keeps the nightly
    Hardcover lookups cheap; healed books gain a name and drop out next run."""
    if not limit:
        return []
    out, rows = grimmory._db(_UNGROUPED_SQL.format(limit=int(limit))), []
    for line in out.splitlines():
        c = line.split("\t")
        if len(c) < 5:
            continue
        rows.append({"book_id": int(c[0]), "title": c[1], "hcid": c[2].strip(),
                     "series_name": "", "series_number": "", "isbn": c[3].strip(),
                     "num_locked": False, "name_locked": c[4] == "1"})
    return rows


def _as_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _isbn_digits(s):
    return re.sub(r"\D", "", s or "")


def audit_one(b, hcid_counts):
    """Return (category, reason, fix|None). fix = (isbn, hcid) to heal, when applicable."""
    hcid = b["hcid"]
    if not hcid:
        return "no-hcid", "in a series but no Hardcover id", None
    if hcid_counts.get(hcid, 0) > 1:
        return "dup-overlap", f"hcid {hcid} shared with another owned book → dedup (plan 21)", None
    cand = audit._book_by_id(hcid)
    if isinstance(cand, tuple):
        return "error", f"hardcover lookup failed: {cand[1][:50]}", None
    if not cand:
        return "series-mismatch", f"hcid {hcid} not found in Hardcover", None
    hc_series, hc_pos, hc_isbn = cand.get("series"), cand.get("position"), cand.get("isbn")
    if not b["series_name"]:
        if not hc_series:
            return "no-series", "standalone — no series in grimmory or Hardcover", None
        if b.get("name_locked"):
            return "series-name-missing", f"ungrouped from {hc_series!r} (series_name LOCKED — manual)", None
        if not hc_isbn:
            return "series-name-missing", f"ungrouped from {hc_series!r} — no canonical ISBN to heal", None
        if _isbn_digits(b["isbn"]) == _isbn_digits(hc_isbn):
            return ("series-name-missing",
                    f"ungrouped from {hc_series!r}; already on canonical ISBN — grimmory derived no series (leave)",
                    None)
        pos = "" if hc_pos is None else f" #{float(hc_pos):g}"
        return "series-name-missing", f"ungrouped → series {hc_series!r}{pos}", (hc_isbn, hcid)
    if hc_series and not matcher._title_match(b["series_name"], hc_series):
        # Series name disagrees. If the book TITLE corroborates the hcid, it's a
        # stale/variant name — heal to canonical so grimmory re-derives the right
        # one (same heal as number-mismatch). If the title ALSO disagrees, the
        # hcid itself is suspect (a real mis-seed): leave it for the resolver,
        # which validates the identity against candidates before locking.
        if not (cand.get("title") and matcher._title_match(b["title"], cand["title"])):
            return ("series-mismatch",
                    f"grimmory series {b['series_name']!r} != hcid series {hc_series!r} → mis-seed (resolver)",
                    None)
        if b["name_locked"]:
            return "series-name-variant", f"variant name {b['series_name']!r} → {hc_series!r} (series_name LOCKED — manual)", None
        if not hc_isbn:
            return "series-name-variant", f"variant name {b['series_name']!r} → {hc_series!r} — no canonical ISBN to heal", None
        if _isbn_digits(b["isbn"]) == _isbn_digits(hc_isbn):
            return ("series-name-variant",
                    f"variant name {b['series_name']!r} → {hc_series!r}; already on canonical ISBN (leave)",
                    None)
        return ("series-name-variant",
                f"grimmory series {b['series_name']!r} → {hc_series!r} (title matches hcid)",
                (hc_isbn, hcid))
    if hc_pos is None:
        return "no-position", "Hardcover has no series position for this id", None
    gn = _as_float(b["series_number"])
    hp = float(hc_pos)
    fix = (hc_isbn, hcid) if hc_isbn else None
    if gn is None:
        cat = "number-missing"
        reason = f"grimmory number missing; Hardcover position {hp:g}"
    elif gn != hp:
        cat = "number-mismatch"
        reason = f"grimmory {gn:g} != Hardcover position {hp:g}"
    else:
        return "number-ok", f"position {hp:g}", None
    if b["num_locked"]:
        reason += " (series_number LOCKED — heal won't override; manual)"
        fix = None
    elif not hc_isbn:
        reason += " (no canonical ISBN in Hardcover — can't heal)"
    return cat, reason, fix


def run(limit=None, apply=False, g=None, store=None, ungrouped_limit=None):
    books = _series_books()
    if limit:
        books = books[:limit]
    # Default cap walks the ungrouped set nightly; a targeted --limit mirrors it.
    ug_cap = ungrouped_limit if ungrouped_limit is not None else (limit or UNGROUPED_LIMIT)
    books += _ungrouped_candidates(ug_cap)
    hcid_counts = defaultdict(int)
    for b in books:
        if b["hcid"]:
            hcid_counts[b["hcid"]] += 1
    if apply:
        assert_preconditions(g)
    run_id = (store.new_run_id() + "-seriesnum") if (apply and store) else None
    cats = defaultdict(list)
    healed, errors = 0, 0
    for b in sorted(books, key=lambda x: (x["series_name"], _as_float(x["series_number"]) or 0)):
        cat, reason, fix = audit_one(b, hcid_counts)
        rec = {"book": b, "reason": reason, "fix": fix, "applied": False}
        if apply and fix and cat in ("number-mismatch", "number-missing",
                                     "series-name-missing", "series-name-variant"):
            try:
                heal_book(g, store, run_id, b["book_id"], fix[0], fix[1], None, dry_run=False)
                rec["applied"] = True
                healed += 1
            except Exception as e:  # noqa: BLE001 — log + circuit-break
                rec["apply_error"] = str(e)[:100]
                errors += 1
                if errors >= ABORT_ERRORS:
                    rec["aborted"] = True
                    cats[cat].append(rec)
                    break
        cats[cat].append(rec)
    return {"total": len(books), "categories": cats, "run_id": run_id,
            "healed": healed, "errors": errors, "apply": apply}


_ORDER = ["number-mismatch", "number-missing", "series-name-missing", "series-name-variant",
          "series-mismatch", "dup-overlap", "no-position", "no-series", "no-hcid", "error", "number-ok"]
_LABEL = {"number-mismatch": "Wrong number (heal-fixable)",
          "number-missing": "Missing number (heal-fixable)",
          "series-name-missing": "Ungrouped — missing series name (heal-fixable)",
          "series-name-variant": "Variant series name (heal-fixable)",
          "series-mismatch": "Series mismatch → mis-seed (resolver)",
          "dup-overlap": "Duplicate hcid → dedup (plan 21)",
          "no-position": "Hardcover has no position (leave)",
          "no-series": "Standalone — not in any series (leave)",
          "no-hcid": "Unidentified in a series (leave)",
          "error": "Provider lookup error", "number-ok": "Correct"}


def render(res):
    cats = res["categories"]
    L = [f"# Colophon series-numbering audit — {time.strftime('%Y-%m-%d %H:%M')}",
         f"\n**{res['total']}** books in a series scanned. "
         + (f"**{res['healed']}** healed, **{res['errors']}** errors."
            if res["apply"] else "Read-only — nothing changed.") + "\n",
         "## Summary\n"]
    for k in _ORDER:
        if cats.get(k):
            L.append(f"- **{len(cats[k])}** {_LABEL[k]}")
    for k in _ORDER:
        if k in ("number-ok", "no-series") or not cats.get(k):
            continue
        L.append(f"\n## {_LABEL[k]} ({len(cats[k])})\n")
        for rec in sorted(cats[k], key=lambda r: (r["book"]["series_name"], r["book"]["book_id"])):
            b = rec["book"]
            tag = ""
            if rec.get("applied"):
                tag = "  [✓ HEALED]"
            elif rec.get("apply_error"):
                tag = f"  [apply-FAILED: {rec['apply_error']}]"
            L.append(f"- `{b['book_id']}` {b['series_name']}#{b['series_number'] or '—'} "
                     f"{b['title']!r} — {rec['reason']}{tag}")
    return "\n".join(L) + "\n"
