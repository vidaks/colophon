"""Phase 1a' — seed bare watch-imports, and remember the ones that never match.

Fresh grabs land via the library *watch* with embedded metadata only; grimmory
does NOT auto-enrich watch-imports, so a book with no Hardcover id never joins its
series or drops from the missing list until something nudges it. This submits a
Hardcover-first REPLACE_MISSING refresh for the un-seeded books — light and
rate-limit-friendly (it never touches the already-seeded library).

The problem this module adds memory for: a book with no usable ISBN (or a
self-pub/KDP edition Hardcover does not carry) can NEVER seed, so a memory-less
sweep re-poked the same books every 30 minutes forever (tens of metadata refreshes
a day on days with no new books). Here each sweep records its observations; after
`stuck_after` failed sweeps a book is marked stuck — dropped from the sweep so the
churn stops, and surfaced ONCE in the daily digest for the human to delete or keep.

Dry-run by default; --apply submits the refresh. The refresh is precondition-gated
(book files are never touched) just like every other colophon write.
"""
import os

from . import grimmory
from .heal import assert_preconditions

# Mark an un-seeded book stuck after this many failed sweeps. Default 6 ≈ 3h at the
# deployed 30-min cadence — long enough to ride out a Hardcover 429/outage, short
# enough to surface same-day. Tunable via the environment.
STUCK_AFTER = int(os.environ.get("COLOPHON_ENRICH_STUCK_AFTER", "6"))

# Books that still lack a Hardcover id (the bare watch-imports), excluding
# soft-deleted ones. Locks are irrelevant — an un-seeded book has no identity to
# protect.
_UNSEEDED = (
    "SELECT bm.book_id FROM book_metadata bm JOIN book b ON b.id=bm.book_id "
    "WHERE (bm.hardcover_book_id IS NULL OR bm.hardcover_book_id='') "
    "AND (b.deleted IS NULL OR b.deleted=0) ORDER BY bm.book_id;"
)


def unseeded_ids():
    return [int(x) for x in grimmory._db(_UNSEEDED).split()]


def run_enrich(g, store, apply=False, stuck_after=STUCK_AFTER):
    """Sweep once: record observations, drop the stuck, refresh the rest."""
    unseeded = unseeded_ids()
    stuck = set(store.enrich_observe(unseeded, stuck_after))
    active = [b for b in unseeded if b not in stuck]
    submitted = False
    if apply and active:
        assert_preconditions(g)
        g.refresh(active, refresh_covers=True, replace_mode="REPLACE_MISSING")
        submitted = True
    return {"apply": apply, "unseeded": unseeded, "active": active,
            "stuck": sorted(stuck), "submitted": submitted, "stuck_after": stuck_after}
