"""Backfill: survey books needing attention, propose via the matcher, then dry-run
(write nothing) or apply (gated). Skips settled/locked books unless the epoch is
bumped. Rate-limited, with a circuit-breaker that aborts on a high error rate.
"""
from collections import Counter

from . import grimmory
from .grimmory import snapshot
from .heal import assert_preconditions, heal_book
from .matcher import propose

MAX_PER_RUN = 50
ABORT_MIN_ATTEMPTS = 4
ABORT_ERROR_RATE = 0.5

# Books that carry a Hardcover id but a missing/malformed ISBN, and are NOT yet
# settled (isbn locked). These are the high-confidence, low-risk heal candidates.
_SURVEY = (
    "SELECT book_id FROM book_metadata "
    "WHERE hardcover_book_id IS NOT NULL AND hardcover_book_id<>'' "
    "AND (isbn_13 IS NULL OR isbn_13='' OR LENGTH(REPLACE(isbn_13,'-',''))<>13) "
    "AND (isbn_13_locked IS NULL OR isbn_13_locked=0) "
    "ORDER BY book_id LIMIT {limit};"
)


def survey(limit):
    out = grimmory._db(_SURVEY.format(limit=int(limit)))
    return [int(x) for x in out.split()]


def run(g, store, limit=20, apply=False):
    if apply:
        assert_preconditions(g)
        limit = min(limit or MAX_PER_RUN, MAX_PER_RUN)
    run_id = store.new_run_id() + ("" if apply else "-dry")
    epoch = store.epoch()
    proposals = []
    healed = errors = 0
    aborted = False
    for bid in survey(limit or MAX_PER_RUN):
        snap = snapshot(bid)
        p = propose(snap)
        p["book_id"] = bid
        proposals.append(p)
        action = p["action"]
        if action != "heal":
            store.record(run_id, bid, f"backfill-{action}", not apply, True, p["reason"], snap, None, p)
            continue
        if not apply:
            store.record(run_id, bid, "backfill-heal", True, True, p["reason"], snap, None, p)
            continue
        try:
            heal_book(g, store, run_id, bid, p["isbn"], p["hcid"], p["slug"], dry_run=False)
            healed += 1
        except Exception as e:
            errors += 1
            attempts = healed + errors
            if attempts >= ABORT_MIN_ATTEMPTS and errors / attempts > ABORT_ERROR_RATE:
                store.record(run_id, bid, "ABORT", False, False,
                             f"circuit-breaker: {errors}/{attempts} errored", None, None, None)
                aborted = True
                break
    summary = Counter(p["action"] for p in proposals)
    return {"run_id": run_id, "epoch": epoch, "apply": apply, "proposals": proposals,
            "summary": dict(summary), "healed": healed, "errors": errors, "aborted": aborted}
