"""Daily maintenance run: execute backfill, resolve, and series audits in one pass.

Runs all maintenance phases with error isolation so a failure in one phase does
not prevent subsequent phases from executing. Generates a summary report of
applied changes and manual review items.
"""
import os
import shlex
import subprocess
import time

from . import backfill, grimmory, series_audit
from .heal import assert_preconditions
from .resolver import run_resolve


def run_maintain(g, store, limit=20, min_conf=0.9, apply=False, force=False):
    res = {"ts": time.strftime("%Y-%m-%d %H:%M"), "apply": apply, "ok": True,
           "aborted": False, "backfill": None, "resolve": None, "series": None, "errors": []}
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
    # Series: numbering + grouping. Auto-heals clean number-mismatch/number-missing
    # and the ungrouped series-name-missing cases; series-mismatch stays for resolve.
    try:
        res["series"] = series_audit.run(apply=apply, g=g, store=store)
        if res["series"]["errors"]:
            res["ok"] = False
    except Exception as e:  # noqa: BLE001
        res["ok"] = False
        res["errors"].append(f"series: {str(e)[:200]}")
    # Informational only — surfacing the enrich stuck-list must never fail the run.
    res["stuck"] = []
    try:
        res["stuck"] = _gather_stuck(store)
    except Exception as e:  # noqa: BLE001
        res["stuck_error"] = str(e)[:200]
    # Identified books the audit can't fix (corrupt name / unhealable variant), each
    # surfaced once with a UI deep-link — the delete/re-download candidates.
    res["manual"] = []
    try:
        for m in (res.get("series") or {}).get("manual", []):
            res["manual"].append({**m, "url": grimmory.book_url(m["book_id"])})
    except Exception as e:  # noqa: BLE001
        res["manual_error"] = str(e)[:200]
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
    series = res.get("series") or {}
    sa = series.get("healed", 0) + series.get("regrouped", 0)
    return bf + rs + sa


def is_noteworthy(res):
    """Whether a run is worth pushing. Routine heals/changes stay SILENT — the system is
    trusted to fix what it can, and a wrong change is something you notice yourself. Only
    a crash, a phase failure, or a book the automation CAN'T fix (a new delete/re-download
    candidate, identified or un-seeded) breaks the silence — that is the human's call."""
    if res is None or not res.get("ok", True):
        return True
    return bool(res.get("manual")) or bool(res.get("stuck"))


def push(title, body):
    """Send a notification through COLOPHON_NOTIFY_CMD — the command is invoked with
    the title as its last argument and the body on stdin, so the deployment wires it
    to whatever push channel it wants (e.g. an ntfy publisher) without colophon
    knowing the channel. Best-effort; returns (ok, detail). No command set = no-op."""
    cmd = os.environ.get("COLOPHON_NOTIFY_CMD")
    if not cmd:
        return False, "COLOPHON_NOTIFY_CMD unset"
    try:
        p = subprocess.run(shlex.split(cmd) + [title], input=body, text=True,
                           capture_output=True, timeout=30)
        return p.returncode == 0, (p.stderr.strip() or "ok")[:120]
    except Exception as e:  # noqa: BLE001 — a notify failure must never break the sweep
        return False, str(e)[:120]


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

    sa = res.get("series")
    if sa:
        cats = sa["categories"]
        smis = len(cats.get("series-mismatch", []))
        L.append(f"Series (numbering + grouping): {sa['total']} scanned "
                 f"({sa.get('cached', 0)} cached) · {sa['healed']} healed · "
                 f"{sa.get('regrouped', 0)} regrouped · {smis} series-mismatch → resolver · {sa['errors']} errors"
                 + ("  ABORTED (circuit-breaker)" if sa.get("errors", 0) >= series_audit.ABORT_ERRORS else ""))
        for k in ("number-mismatch", "number-missing", "series-name-missing", "series-name-variant"):
            for rec in cats.get(k, []):
                if rec.get("applied"):
                    b = rec["book"]
                    L.append(f"  + book {b['book_id']} {b['title']!r} — {rec['reason']}")
    else:
        L.append("Series: did not run")

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

    manual = res.get("manual") or []
    if manual:
        L.append("")
        L.append(f"Series names to fix ({len(manual)}) — colophon flagged these but can't rename "
                 "them automatically (the book is already on its canonical edition). Apply the "
                 "rename in grimmory's metadata editor, or delete + re-download. Shown ONCE.")
        for m in manual:
            L.append(f"  • book {m['book_id']} {m['title']!r}")
            if m.get("rename_to"):
                L.append(f"      series name is wrong — rename:  "
                         f"{(m.get('series_name') or '—')!r}  →  {m['rename_to']!r}")
            else:
                num = f"#{m['series_number']}" if m.get("series_number") else ""
                L.append(f"      [{(m.get('series_name') or '—')}{num}] — {m['reason']}")
            if m.get("url"):
                L.append(f"      {m['url']}")

    L += ["", "Full reports under reports/ on the host."]
    return "\n".join(L) + "\n"
