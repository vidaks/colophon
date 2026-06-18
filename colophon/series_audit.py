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
series because grimmory never derived a `series_name`) heals two ways. When the
book sits on a *non-canonical* 13-char edition (current ISBN != Hardcover's
canonical), the ISBN-swap heal runs: set the canonical ISBN + hcid, lock, refresh,
and grimmory repopulates BOTH series_name and series_number. When there is no
canonical ISBN to swap to, or the book is already on it, a plain `REPLACE_MISSING`
refresh of the existing identity runs instead — grimmory re-derives the series
from the edition it already has (verified: a book whose ISBN matches a Hardcover
record with a `featured_book_series` re-derives the name on refresh), with no
ISBN swap and no field clobbering (REPLACE_MISSING fills only empty fields).
Standalones (no Hardcover series) fall into `no-series` and are never refreshed.
Broken-ISBN books are left to the backfill phase. A *disagreeing* name
(series-mismatch) is still deferred to the resolver: the disagreement is itself
evidence the hcid may be wrong, so it needs adjudication before a lock.
"""
import os
import random
import re
import time
from collections import defaultdict

from . import audit, grimmory, hardcover, matcher
from .heal import assert_preconditions, heal_book

ABORT_ERRORS = 3
# Per-run cap on the ungrouped (null series_name) survey. Sized to cover the whole
# personal-scale library each run (the actual lookup cost scales with the real
# ungrouped count, not the cap); RAND() below still rotates coverage if a library
# ever grows past it. A verdict cache for known-standalone books would avoid the
# nightly re-lookup of the genuine-standalone tail — deferred until it matters.
UNGROUPED_LIMIT = 250
# Fix marker: re-derive the series via a plain REPLACE_MISSING refresh of the book's
# existing identity (no ISBN swap), for ungrouped books we can't or needn't re-ISBN.
REFRESH_MISSING = "refresh-missing"

# Verdict cache: settled (non-actionable) verdicts are cached so the sweep stops
# re-querying Hardcover for unchanged books — which is what lets the schedule run
# far more often than nightly. The TTL is jittered per entry (expiry in [TTL, 2*TTL])
# so a bulk-populated cache expires as a trickle, never all on one run. Only verdicts
# with no cross-book dependency are cached (dup-overlap is excluded — it is decided
# from the shared-hcid count, which a different book can change).
SERIES_VERDICT_TTL_DAYS = float(os.environ.get("COLOPHON_SERIES_VERDICT_TTL_DAYS", "14"))
_CACHEABLE = {"number-ok", "no-series", "no-position", "no-hcid", "alt-series"}

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
    # RAND() so a residual set of stuck/standalone ungrouped books can't permanently
    # monopolise the per-run cap and starve newly-ungrouped books of a survey slot.
    "ORDER BY RAND() LIMIT {limit};"
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


def _fingerprint(b):
    """The book identity a cached verdict depends on. A change to any part (a heal,
    a regroup, a manual edit) no longer matches the cached entry, re-opening the book.
    The cacheable verdicts (number-ok / no-series / no-position / no-hcid) turn only on
    these fields — none reads the title — so this is a complete invalidation key."""
    return "|".join((
        (b["hcid"] or ""), _isbn_digits(b["isbn"]),
        (b["series_name"] or "").strip().lower(), str(b["series_number"] or "").strip()))


def _verdict_expiry():
    """A jittered epoch in [TTL/2, TTL] — TTL is the strict max age, the jitter only
    pulls expiries earlier so a bulk-cached set never all expires on one run."""
    base = SERIES_VERDICT_TTL_DAYS * 86400.0
    return time.time() + base - random.uniform(0.0, base / 2.0)


def _resolve_membership(cand):
    """The (series, position) the audit compares against. grimmory derives a book's
    series from its FEATURED Hardcover membership, so the audit must compare grimmory's
    number against the FEATURED position — not the lowest-position membership that
    book_by_id returns by default. A book in several series has a different position in
    each (an Expanse novella: featured "The Expanse" #2.7 vs "The Expanse (Chronological)"
    #0.1), so comparing against the wrong one is a false mismatch that re-heals forever
    without converging. Using the featured series also keeps a genuine mis-seed/variant
    (grimmory's name disagrees with the featured series) on the name path, not reclassified
    into a number-mismatch. Falls back to the default when Hardcover exposes no featured."""
    feat = cand.get("featured")
    if feat and feat.get("series"):
        return feat.get("series"), feat.get("position")
    return cand.get("series"), cand.get("position")


def _matching_membership(cand, series_name):
    """The Hardcover membership whose series name normalizes to grimmory's series_name,
    or None. Lets the audit accept a valid ALTERNATE grouping — the user keeping a
    Human Division episode under "Old Man's War" rather than the featured sub-series —
    instead of treating that disagreement as a variant to move or a mis-seed to flag."""
    want = matcher._norm(series_name)
    if not want:
        return None
    for m in (cand.get("memberships") or []):
        if matcher._norm(m.get("series")) == want:
            return m
    return None


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
    hc_series, hc_pos = _resolve_membership(cand)
    hc_isbn = cand.get("isbn")
    if not b["series_name"]:
        if not hc_series:
            return "no-series", "standalone — no series in grimmory or Hardcover", None
        if b.get("name_locked"):
            return "series-name-missing", f"ungrouped from {hc_series!r} (series_name LOCKED — manual)", None
        if not hc_isbn:
            return ("series-name-missing",
                    f"ungrouped from {hc_series!r} — no canonical ISBN; refresh to re-derive",
                    (REFRESH_MISSING, hcid))
        if _isbn_digits(b["isbn"]) == _isbn_digits(hc_isbn):
            return ("series-name-missing",
                    f"ungrouped from {hc_series!r}; already on canonical ISBN — refresh to re-derive",
                    (REFRESH_MISSING, hcid))
        pos = "" if hc_pos is None else f" #{float(hc_pos):g}"
        return "series-name-missing", f"ungrouped → series {hc_series!r}{pos}", (hc_isbn, hcid)
    if hc_series and not matcher._title_match(b["series_name"], hc_series):
        # grimmory's series disagrees with the FEATURED series. First: is grimmory's
        # series nonetheless a real membership of this book — a valid alternate grouping
        # the user may have chosen (a Human Division episode kept under "Old Man's War")?
        # If so, accept it; never move it to featured. If the stored name only NORMALIZES
        # to a real series but isn't it verbatim (a stray "[" or other junk), it can't be
        # auto-fixed without moving the book, so surface it for a manual fix / re-download.
        member = _matching_membership(cand, b["series_name"])
        if member:
            clean = (member.get("series") or "").strip()
            if b["series_name"].strip() == clean:
                return "alt-series", f"valid alternate series {clean!r} (featured is {hc_series!r}) — accepted", None
            return ("manual",
                    f"series name {b['series_name']!r} is a corrupt form of {clean!r} — fix in grimmory or re-download",
                    None)
        # Not a real series for this book. If the book TITLE corroborates the hcid, it's
        # a stale/variant name — heal to canonical so grimmory re-derives the right one.
        # If the title ALSO disagrees, the hcid itself is suspect (a real mis-seed): leave
        # it for the resolver, which validates the identity against candidates before lock.
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


def run(limit=None, apply=False, g=None, store=None, ungrouped_limit=None, force=False):
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
    # Skip the Hardcover lookup for books whose settled verdict is cached, still fresh,
    # and identity-unchanged (--force bypasses). A currently shared-hcid book is never
    # cache-skipped: dup-overlap is cross-book and must be re-decided each run.
    verdict_cache = {} if (force or store is None) else store.series_verdict_map()
    now = time.time()
    cats = defaultdict(list)
    healed, regrouped, errors, cached_hits = 0, 0, 0, 0
    regroup = []  # (rec, book_id) — re-derived together via one batched refresh
    for b in sorted(books, key=lambda x: (x["series_name"], _as_float(x["series_number"]) or 0)):
        fp = _fingerprint(b)
        hit = verdict_cache.get(b["book_id"])
        # A book that shares its hcid with another (dup-overlap) is cross-book and must
        # be re-decided every run; no-hcid books aren't counted, so they stay cacheable.
        unique_hcid = not b["hcid"] or hcid_counts.get(b["hcid"], 0) <= 1
        if hit and hit[0] == fp and hit[2] > now and unique_hcid:
            cats[hit[1]].append({"book": b, "reason": "cached — settled, not re-queried",
                                 "fix": None, "applied": False, "cached": True})
            cached_hits += 1
            continue
        cat, reason, fix = audit_one(b, hcid_counts)
        rec = {"book": b, "reason": reason, "fix": fix, "applied": False}
        if apply and fix and fix[0] == REFRESH_MISSING:
            regroup.append((rec, b["book_id"]))
        elif apply and fix and cat in ("number-mismatch", "number-missing",
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
        # Cache a settled, non-actionable verdict; drop any stale entry otherwise.
        if store:
            if fix is None and cat in _CACHEABLE:
                store.series_verdict_put(b["book_id"], fp, cat, _verdict_expiry())
            elif hit:
                store.series_verdict_delete(b["book_id"])
        cats[cat].append(rec)
    # One batched REPLACE_MISSING refresh re-derives the series for the ungrouped
    # books that need no ISBN swap; grimmory throttles its own Hardcover calls, and
    # REPLACE_MISSING fills only empty fields so it can't clobber existing metadata.
    if apply and regroup:
        try:
            g.refresh([bid for _, bid in regroup], refresh_covers=False, replace_mode="REPLACE_MISSING")
            for rec, _ in regroup:
                rec["applied"] = True
            regrouped = len(regroup)
        except Exception as e:  # noqa: BLE001 — log; the heal work already happened
            for rec, _ in regroup:
                rec["apply_error"] = str(e)[:100]
            errors += 1
    if store:
        store.series_verdict_prune()  # drop expired rows (incl. orphans of deleted books)
    # The identified books the audit can't fix — a corrupt series name or an unhealable
    # variant (delete / re-download candidates). On a full apply run, track them so each
    # is surfaced only once (until its metadata changes); a dry run just lists them.
    manual_recs = list(cats.get("manual", []))
    manual_recs += [r for r in cats.get("series-name-variant", []) if r["fix"] is None]
    manual = [{"book_id": r["book"]["book_id"], "title": r["book"]["title"],
               "series_name": r["book"]["series_name"], "series_number": r["book"]["series_number"],
               "reason": r["reason"]} for r in manual_recs]
    if apply and store is not None and not limit:
        unreported = set(store.series_manual_observe(
            [(r["book"]["book_id"], _fingerprint(r["book"])) for r in manual_recs]))
        manual = [m for m in manual if m["book_id"] in unreported]
    return {"total": len(books), "categories": cats, "run_id": run_id,
            "healed": healed, "regrouped": regrouped, "errors": errors,
            "cached": cached_hits, "manual": manual, "apply": apply}


_ORDER = ["manual", "number-mismatch", "number-missing", "series-name-missing", "series-name-variant",
          "series-mismatch", "dup-overlap", "no-position", "no-series", "no-hcid", "alt-series",
          "error", "number-ok"]
_LABEL = {"manual": "Unfixable — needs a manual fix / re-download",
          "number-mismatch": "Wrong number (heal-fixable)",
          "number-missing": "Missing number (heal-fixable)",
          "series-name-missing": "Ungrouped — missing series name (heal-fixable)",
          "series-name-variant": "Variant series name (heal-fixable)",
          "series-mismatch": "Series mismatch → mis-seed (resolver)",
          "dup-overlap": "Duplicate hcid → dedup (plan 21)",
          "no-position": "Hardcover has no position (leave)",
          "no-series": "Standalone — not in any series (leave)",
          "no-hcid": "Unidentified in a series (leave)",
          "alt-series": "Valid alternate series (accepted)",
          "error": "Provider lookup error", "number-ok": "Correct"}


def render(res):
    cats = res["categories"]
    cached = res.get("cached", 0)
    cached_note = f" ({cached} from cache, not re-queried)" if cached else ""
    L = [f"# Colophon series-numbering audit — {time.strftime('%Y-%m-%d %H:%M')}",
         f"\n**{res['total']}** books in a series scanned{cached_note}. "
         + (f"**{res['healed']}** healed, **{res.get('regrouped', 0)}** regrouped, **{res['errors']}** errors."
            if res["apply"] else "Read-only — nothing changed.") + "\n",
         "## Summary\n"]
    for k in _ORDER:
        if cats.get(k):
            L.append(f"- **{len(cats[k])}** {_LABEL[k]}")
    for k in _ORDER:
        if k in ("number-ok", "no-series", "alt-series") or not cats.get(k):
            continue
        L.append(f"\n## {_LABEL[k]} ({len(cats[k])})\n")
        for rec in sorted(cats[k], key=lambda r: (r["book"]["series_name"], r["book"]["book_id"])):
            b = rec["book"]
            tag = ""
            if rec.get("applied"):
                tag = "  [✓ REGROUPED]" if (rec["fix"] and rec["fix"][0] == REFRESH_MISSING) else "  [✓ HEALED]"
            elif rec.get("apply_error"):
                tag = f"  [apply-FAILED: {rec['apply_error']}]"
            L.append(f"- `{b['book_id']}` {b['series_name']}#{b['series_number'] or '—'} "
                     f"{b['title']!r} — {rec['reason']}{tag}")
    return "\n".join(L) + "\n"
