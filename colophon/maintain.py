"""Daily maintenance run — backfill + resolve in one process, with a summary.

Runs the two write phases the timer fires nightly, each guarded so a failure in
one does not skip the other, then composes a tight summary (counts + what changed
+ standing manual items + a one-line health verdict). The CLI emails it — always,
including on an aborted run — so the summary doubles as a heartbeat. The process
still exits non-zero when a phase failed, so the systemd unit is marked failed and
the next timer fire retries.

This module never raises for a phase or precondition failure: it captures the
failure into the result so the caller can always render + send a report.
"""
import time

from . import backfill, grimmory
from .heal import assert_preconditions
from .resolver import run_resolve


def run_maintain(g, store, limit=20, min_conf=0.9, apply=False, force=False):
    res = {"ts": time.strftime("%Y-%m-%d %H:%M"), "apply": apply, "ok": True,
           "aborted": False, "backfill": None, "resolve": None, "errors": []}
    if apply:
        try:
            assert_preconditions(g)
        except Exception as e:  # noqa: BLE001 — precondition/network failure: abort cleanly, still report
            res["ok"], res["aborted"] = False, True
            res["errors"].append(f"preconditions: {str(e)[:200]}")
            return res
    try:
        res["backfill"] = backfill.run(g, store, limit=limit, apply=apply)
        if res["backfill"]["errors"] or res["backfill"]["aborted"]:
            res["ok"] = False
    except Exception as e:  # noqa: BLE001
        res["ok"] = False
        res["errors"].append(f"backfill: {str(e)[:200]}")
    try:
        res["resolve"] = run_resolve(apply=apply, min_conf=min_conf, g=g, store=store, force=force)
        props = res["resolve"]["proposals"]
        if any(p.get("apply_error") for p in props) or any(p.get("aborted") for p in props):
            res["ok"] = False
    except Exception as e:  # noqa: BLE001
        res["ok"] = False
        res["errors"].append(f"resolve: {str(e)[:200]}")
    # Informational only — surfacing the enrich stuck-list must never fail the run.
    res["stuck"] = []
    try:
        res["stuck"] = _gather_stuck(store)
    except Exception as e:  # noqa: BLE001
        res["stuck_error"] = str(e)[:200]
    return res


def _gather_stuck(store):
    """The actionable manual-review list: un-seeded books the enrich sweep gave up
    on and has not yet surfaced. Decorated with title/author/isbn + a UI deep-link."""
    rows = store.enrich_stuck_unreported()
    if not rows:
        return []
    briefs = grimmory.briefs([r["book_id"] for r in rows])
    out = []
    for r in rows:
        b = briefs.get(r["book_id"], {})
        out.append({"book_id": r["book_id"], "title": b.get("title") or "(unknown)",
                    "authors": b.get("authors") or "", "isbn": b.get("isbn") or "",
                    "fail_count": r["fail_count"], "url": grimmory.book_url(r["book_id"])})
    return out


def verdict(res):
    if res["aborted"]:
        return "ABORTED"
    return "OK" if res["ok"] else "ERRORS"


def _healed_count(res):
    bf = (res["backfill"] or {}).get("healed", 0)
    rs = sum(1 for p in (res["resolve"] or {}).get("proposals", []) if p.get("applied"))
    return bf + rs


def subject(res):
    n = _healed_count(res)
    mode = "" if res["apply"] else " [dry-run]"
    return f"Colophon daily [{verdict(res)}]{mode} — {n} healed ({res['ts']})"


def render_summary(res):
    L = [f"Colophon daily maintenance — {res['ts']}",
         f"STATUS: {verdict(res)}" + ("" if res["apply"] else "  (dry-run — nothing written)"), ""]
    if res.get("errors"):
        L.append("Errors:")
        L += [f"  - {e}" for e in res["errors"]]
        L.append("")

    bf = res["backfill"]
    if bf:
        heals = [p for p in bf["proposals"] if p["action"] == "heal"]
        flags = [p for p in bf["proposals"] if p["action"].startswith("review")]
        L.append(f"Backfill (broken ISBN → canonical): {len(bf['proposals'])} surveyed · "
                 f"{bf['healed']} healed · {len(flags)} flagged · {bf['errors']} errors"
                 + ("  ABORTED (circuit-breaker)" if bf["aborted"] else ""))
        for p in heals:
            L.append(f"  + book {p['book_id']} → hcid {p.get('hcid')} {p.get('hc_title')!r}")
    else:
        L.append("Backfill: did not run")

    rs = res["resolve"]
    if rs:
        props = rs["proposals"]
        applied = [p for p in props if p.get("applied")]
        proposed = [p for p in props if p["action"] == "propose" and not p.get("applied")]
        none = [p for p in props if p["action"] == "none"]
        err = [p for p in props if p["action"] == "error"]
        skipped = rs.get("skipped", [])
        L.append(f"Resolve (mis-seed → correct identity): {len(props)} queried · "
                 f"{len(applied)} auto-healed · {len(proposed)} below-threshold · "
                 f"{len(none)} no-match · {len(err)} error · {len(skipped)} cached-skipped")
        for p in applied:
            L.append(f"  + book {p['book_id']} {p['title']!r} → {p.get('chosen_title')!r} "
                     f"(conf {p.get('confidence')})")
        # New + standing below-threshold matches are the actionable items (human nudge).
        standing = [(p["book_id"], p.get("title"), p.get("chosen_title"), p.get("confidence"))
                    for p in proposed]
        standing += [(s["book_id"], s.get("title"), s.get("chosen_title"), s.get("conf"))
                     for s in skipped if s.get("action") == "propose-below"]
        if standing:
            L.append("  Below-threshold matches (apply manually if right — "
                     "`colophon resolve --book <id> --apply --min-conf 0`):")
            for bid, title, chosen, conf in standing:
                L.append(f"    ? book {bid} {title!r} → {chosen!r} (conf {conf})")
    else:
        L.append("Resolve: did not run")

    stuck = res.get("stuck") or []
    if stuck:
        L.append("")
        L.append(f"Unresolvable — manual review ({len(stuck)}): bare imports Hardcover can't "
                 "match (no/odd ISBN, self-pub).")
        L.append("  Delete or keep each via its link; listed here ONCE, then silence = keep.")
        for s in stuck:
            who = f" — {s['authors']}" if s["authors"] else ""
            isbn = f" · isbn {s['isbn']}" if s["isbn"] else " · no isbn"
            L.append(f"  ? book {s['book_id']} {s['title']!r}{who}{isbn} (failed {s['fail_count']}x)")
            if s.get("url"):
                L.append(f"      {s['url']}")

    L += ["", "Full reports under reports/ on the host."]
    return "\n".join(L) + "\n"
