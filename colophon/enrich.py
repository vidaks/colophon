"""Initial metadata enrichment for newly imported books.

Newly imported books often arrive with embedded file metadata only and lack an
external provider identifier. The server does not automatically query Hardcover
for watch directory imports, leaving these books without series or provider links.
This module triggers a REPLACE_MISSING refresh for unlinked books.

To prevent infinite retries for books that cannot be matched (such as self-published
works or books lacking valid ISBNs), each sweep records its observations. After
`stuck_after` failed attempts, a book is marked as stuck, excluded from future
sweeps, and reported once in the daily summary for manual review.

Dry-run by default; --apply submits the refresh request to the server.
"""
import os

from .grimmory import unseeded_ids  # module attribute on purpose: tests stub enrich.unseeded_ids
from .heal import assert_preconditions

# Mark an un-seeded book stuck after this many failed sweeps. Default 6 ≈ 3h at the
# deployed 30-min cadence — long enough to ride out a Hardcover 429/outage, short
# enough to surface same-day. Tunable via the environment.
STUCK_AFTER = int(os.environ.get("COLOPHON_ENRICH_STUCK_AFTER", "6"))


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
