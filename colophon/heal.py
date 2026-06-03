"""Heal orchestration: precondition gate, dry-run, changelog, revert.

heal_book = the validated recipe: PUT correct ISBN + locks (via the API) → refresh
(REPLACE_ALL, refreshCovers) → grimmory fills the rest from the locked ISBN. Every
write is recorded; revert replays the changelog's before-state through the API.
"""
from .grimmory import snapshot, signature, wait_for_change
from .store import Store

MAX_PER_RUN = 50  # rate-limit: refuse to change more than this in one run


class PreconditionError(Exception):
    pass


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
    """Undo a run: restore each healed book's pre-heal identity, then refresh."""
    rows = [r for r in store.run_changes(run_id)
            if r["action"] == "heal" and r["ok"] and not r["dry_run"]]
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
