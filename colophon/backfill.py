"""Backfill: survey books needing attention, propose via the matcher, then dry-run
(write nothing) or apply (gated). Settled (locked) books are excluded by the survey —
set-once means they are never re-litigated. Rate-limited, with a circuit-breaker that
aborts on a high error rate.
"""
from collections import Counter

from . import grimmory
from .grimmory import snapshot
from .heal import assert_preconditions, heal_book
from .matcher import propose

MAX_PER_RUN = 50
ABORT_MIN_ATTEMPTS = 4
ABORT_ERROR_RATE = 0.5

# The survey SQL lives in grimmory (the server seam); this alias is the module's
# patch point for tests.
survey = grimmory.survey_broken_isbn


def run(g, store, limit=20, apply=False):
    if apply:
        assert_preconditions(g)
        limit = min(limit or MAX_PER_RUN, MAX_PER_RUN)
    run_id = store.new_run_id() + ("" if apply else "-dry")
    proposals = []
    healed = errors = 0
    aborted = False
    for bid in survey(limit or MAX_PER_RUN):
        snap = snapshot(bid)
        p = propose(snap)
        p["book_id"] = bid
        proposals.append(p)
        action = p["action"]
        # error=None on ok rows: the reason already travels in the proposal
        # (target_json); stuffing it into the error column made `log` print
        # healthy surveys as errors.
        if action != "heal":
            store.record(run_id, bid, f"backfill-{action}", not apply, True, None, snap, None, p)
            continue
        if not apply:
            store.record(run_id, bid, "backfill-heal", True, True, None, snap, None, p)
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
    return {"run_id": run_id, "apply": apply, "proposals": proposals,
            "summary": dict(summary), "healed": healed, "errors": errors, "aborted": aborted}
