"""Heal orchestration: precondition gate, dry-run, changelog, revert.

heal_book = the validated recipe: PUT correct ISBN + locks (via the API) → refresh
(REPLACE_ALL, refreshCovers) → grimmory fills the rest from the locked ISBN. Every
write is recorded; revert replays the changelog's before-state through the API.
"""
import re

from .grimmory import GrimmoryError, snapshot, signature, wait_for_change
from .store import Store


class PreconditionError(Exception):
    pass


class ConvergenceError(GrimmoryError):
    """The write went through but the refresh never landed the target identity."""


def _digits(s):
    return re.sub(r"\D", "", s or "")


def assert_preconditions(g):
    """ABORT before any write unless grimmory can't touch book files."""
    ok, details = g.preconditions()
    if not ok:
        bad = ", ".join(f"{k}={v}" for k, v in details.items() if v is not False)
        raise PreconditionError(
            f"ABORT — book files could be modified; these must be off: {bad}")
    return details


def heal_book(g, store, run_id, book_id, isbn, hcid=None, slug=None, dry_run=True):
    before = snapshot(book_id)
    target = {"isbn13": isbn, "hardcoverBookId": hcid, "hardcoverId": slug}
    if before is None:
        store.record(run_id, book_id, "heal", dry_run, False, "book not found", None, None, target)
        return {"book_id": book_id, "ok": False, "error": "book not found"}
    if dry_run:
        store.record(run_id, book_id, "heal", True, True, None, before, None, target)
        return {"book_id": book_id, "ok": True, "dry_run": True, "before": before, "target": target}
    sig0 = signature(before)
    try:
        g.put_identity(book_id, isbn, hcid, slug)
        g.refresh([book_id])
        after = wait_for_change(book_id, sig0)
        # wait_for_change returning is not success — it also returns on timeout. This
        # verifies the IDENTITY landed (the PUT persisted the target ISBN); it cannot
        # see a failed refresh, because the PUT alone already changes the signature
        # and leaves isbn_13 == target. A book whose refresh silently failed keeps the
        # right locked identity and stale derived fields — the next enrich/series pass
        # can still fill those. What must never happen is recording a heal whose
        # identity write did NOT stick as ok.
        if not after or _digits(after.get("isbn_13")) != _digits(isbn):
            raise ConvergenceError(
                f"identity did not land: isbn_13={(after or {}).get('isbn_13')!r} "
                f"!= target {isbn!r}")
        store.record(run_id, book_id, "heal", False, True, None, before, after, target)
        return {"book_id": book_id, "ok": True, "before": before, "after": after}
    except Exception as e:
        store.record(run_id, book_id, "heal", False, False, str(e), before, snapshot(book_id), target)
        raise


def _restore_meta(before):
    """A metadata PUT body that restores the pre-heal identity + its lock states."""
    def lock(v):
        return str(v) in ("1", "True", "true")
    return {
        "isbn13": before.get("isbn_13") or None,
        "isbn10": before.get("isbn_10") or None,
        "hardcoverBookId": before.get("hardcover_book_id") or None,
        "hardcoverId": before.get("hardcover_id") or None,
        "isbn13Locked": lock(before.get("isbn_13_locked")),
        "hardcoverBookIdLocked": lock(before.get("hardcover_book_id_locked")),
        "hardcoverIdLocked": lock(before.get("hardcover_id_locked")),
    }


def revert_run(g, store, run_id, dry_run=True):
    """Undo a run: restore each healed book's pre-heal identity, then refresh.

    Includes FAILED (ok=0) non-dry heal rows: a heal that raised after put_identity
    has already written and locked the target — filtering on ok would make exactly
    the writes most worth undoing unreachable. Replaying the before-state onto a book
    whose write never landed is a harmless no-op (it restores what is already there)."""
    rows = [r for r in store.run_changes(run_id)
            if r["action"] == "heal" and not r["dry_run"]]
    rev_run = Store.new_run_id() + "-revert"
    results = []
    for r in rows:
        before = Store.loads(r, "before_json")
        bid = r["book_id"]
        if not before:
            continue
        if dry_run:
            store.record(rev_run, bid, "revert", True, True, None, snapshot(bid), None, before)
            results.append({"book_id": bid, "ok": True, "dry_run": True})
            continue
        sig0 = signature(snapshot(bid))
        try:
            g.put_metadata(bid, _restore_meta(before))
            g.refresh([bid])
            after = wait_for_change(bid, sig0)
            store.record(rev_run, bid, "revert", False, True, None, None, after, before)
            results.append({"book_id": bid, "ok": True, "after": after})
        except Exception as e:
            store.record(rev_run, bid, "revert", False, False, str(e), None, snapshot(bid), before)
            results.append({"book_id": bid, "ok": False, "error": str(e)})
    return rev_run, results
