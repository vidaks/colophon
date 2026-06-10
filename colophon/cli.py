"""Colophon Phase-0 CLI. Dry-run by default; --apply actually writes.

    python3 -m colophon.cli precheck
    python3 -m colophon.cli heal 592 --isbn 9780385263481 --hcid 427460 --slug hyperion [--apply]
    python3 -m colophon.cli log [-n 20]
    python3 -m colophon.cli revert <run_id> [--apply]
"""
import argparse
import json
import sys

import os
import time

from .audit import render_report, run_audit
from .backfill import run as backfill_run
from .grimmory import Grimmory, GrimmoryError
from .heal import assert_preconditions, heal_book, revert_run, PreconditionError
from .store import Store
from .verify import verify


def _fmt(snap, keys=("title", "series_name", "series_number", "isbn_13",
                     "hardcover_book_id", "page_count", "authors")):
    if not snap:
        return "(none)"
    return " | ".join(f"{k}={snap.get(k)}" for k in keys)


def cmd_precheck(args, g, store):
    ok, details = g.preconditions()
    print("preconditions (book files must never be touched):")
    for k, v in details.items():
        print(f"  {k} = {v}   {'OK' if v is False else 'BAD — must be false/off'}")
    print("RESULT:", "OK — safe to write" if ok else "ABORT — do not write")
    return 0 if ok else 2


def cmd_heal(args, g, store):
    apply = args.apply
    if apply:
        try:
            assert_preconditions(g)
        except PreconditionError as e:
            print(e)
            return 2
    run_id = store.new_run_id()
    res = heal_book(g, store, run_id, args.book_id, args.isbn, args.hcid, args.slug, dry_run=not apply)
    mode = "APPLIED" if apply else "DRY-RUN (no write)"
    print(f"[{mode}] run {run_id}  book {args.book_id}")
    print("  target :", json.dumps(res.get("target") or {"isbn13": args.isbn, "hardcoverBookId": args.hcid, "hardcoverId": args.slug}))
    print("  before :", _fmt(res.get("before")))
    if apply:
        print("  after  :", _fmt(res.get("after")))
    if not res.get("ok"):
        print("  ERROR  :", res.get("error"))
        return 1
    if not apply:
        print("  (re-run with --apply to write; revert later with: revert " + run_id + " --apply)")
    return 0


def cmd_log(args, g, store):
    rows = store.recent(args.n)
    if not rows:
        print("(changelog empty)")
        return 0
    for r in reversed(rows):
        tag = "DRY " if r["dry_run"] else ("OK  " if r["ok"] else "FAIL")
        b = Store.loads(r, "before_json") or {}
        a = Store.loads(r, "after_json") or {}
        t = Store.loads(r, "target_json") or {}
        print(f"{r['ts']}  {r['run_id']}  [{tag}] {r['action']} book {r['book_id']}")
        print(f"    {b.get('title')!r}/{b.get('isbn_13')}  ->  target isbn {t.get('isbn13')}"
              + (f"  =>  {a.get('title')!r}/{a.get('isbn_13')}" if a else ""))
        if r["error"]:
            print(f"    error: {r['error']}")
    return 0


def cmd_revert(args, g, store):
    apply = args.apply
    if apply:
        try:
            assert_preconditions(g)
        except PreconditionError as e:
            print(e)
            return 2
    rev_run, results = revert_run(g, store, args.run_id, dry_run=not apply)
    mode = "APPLIED" if apply else "DRY-RUN (no write)"
    print(f"[{mode}] revert of {args.run_id}  ({len(results)} book(s))  -> {rev_run}")
    for r in results:
        print(f"  book {r['book_id']}: {'ok' if r.get('ok') else 'FAIL ' + str(r.get('error'))}"
              + (" (dry)" if r.get("dry_run") else ""))
    return 0


def cmd_backfill(args, g, store):
    apply = args.apply
    if apply:
        try:
            assert_preconditions(g)
        except PreconditionError as e:
            print(e)
            return 2
    res = backfill_run(g, store, limit=args.limit, apply=apply)
    mode = "APPLIED" if apply else "DRY-RUN (no writes)"
    print(f"[{mode}] backfill {res['run_id']}  epoch {res['epoch']}  "
          f"({len(res['proposals'])} books surveyed)")
    print("  summary:", res["summary"])
    if apply:
        print(f"  healed={res['healed']} errors={res['errors']} aborted={res['aborted']}")
    heals = [p for p in res["proposals"] if p["action"] == "heal"]
    if heals:
        print(f"\n  HEAL candidates ({len(heals)}):")
        for p in heals:
            print(f"    book {p['book_id']}: {p['reason']}  [hcid {p['hcid']} {p.get('hc_title')!r}]")
    flags = [p for p in res["proposals"] if p["action"].startswith("review")]
    if flags:
        print(f"\n  flagged for review ({len(flags)}):")
        for p in flags[:25]:
            print(f"    book {p['book_id']}: {p['action']} — {p['reason']}")
    if not apply and heals:
        print(f"\n  re-run with --apply to heal the {len(heals)} candidate(s).")
    return 0


def cmd_enrich(args, g, store):
    """Seed bare watch-imports (no Hardcover id) via a REPLACE_MISSING refresh, with
    memory: a book that never seeds is marked stuck after N failed sweeps — dropped
    from the sweep (the churn stops) and surfaced once in the daily digest. Dry-run
    unless --apply."""
    from . import enrich as E
    stuck_after = args.stuck_after if args.stuck_after is not None else E.STUCK_AFTER
    res = E.run_enrich(g, store, apply=args.apply, stuck_after=stuck_after)
    mode = "APPLIED" if args.apply else "DRY-RUN (no write)"
    print(f"[{mode}] enrich — {len(res['unseeded'])} un-seeded · "
          f"{len(res['active'])} {'refreshed' if res['submitted'] else 'to refresh'} · "
          f"{len(res['stuck'])} stuck (excluded; stuck_after={res['stuck_after']})")
    if res["stuck"]:
        print("  stuck — need manual review (surfaced in the daily digest): "
              + " ".join(str(b) for b in res["stuck"]))
    return 0


def cmd_audit(args, g, store):
    res = run_audit(limit=args.limit)
    report = render_report(res)
    reports_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "reports"))
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"audit-{time.strftime('%Y%m%dT%H%M%S')}.md")
    with open(path, "w") as f:
        f.write(report)
    print(report)
    print(f"\n(report written to {path})")
    return 0


def cmd_resolve(args, g, store):
    from . import anthropic
    from .resolver import render, run_resolve
    if args.clear_skips:
        n = store.skip_clear()
        print(f"cleared {n} cached-unresolvable entr{'y' if n == 1 else 'ies'}")
        return 0
    if not anthropic.have_key():
        print("(no ANTHROPIC_API_KEY — using the `claude` CLI; production needs the vaulted key)")
    try:
        res = run_resolve(limit=args.limit, book_ids=args.book or None, apply=args.apply,
                          min_conf=args.min_conf, g=g, store=store, force=args.force)
    except PreconditionError as e:
        print(e)
        return 2
    report = render(res)
    reports_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "reports"))
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"resolve-{time.strftime('%Y%m%dT%H%M%S')}.md")
    with open(path, "w") as f:
        f.write(report)
    print(report)
    print(f"\n(report written to {path})")
    return 0


def cmd_series_audit(args, g, store):
    from .series_audit import render, run as series_run
    try:
        res = series_run(limit=args.limit, apply=args.apply, g=g, store=store)
    except PreconditionError as e:
        print(e)
        return 2
    report = render(res)
    reports_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "reports"))
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"series-{time.strftime('%Y%m%dT%H%M%S')}.md")
    with open(path, "w") as f:
        f.write(report)
    print(report)
    print(f"\n(report written to {path})")
    return 0


def cmd_oversight(args, g, store):
    from . import oversight
    res = oversight.review(store, days=args.days)
    report = oversight.render(res)
    reports_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "reports"))
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, f"oversight-{time.strftime('%Y%m%dT%H%M%S')}.md")
    with open(path, "w") as f:
        f.write(report)
    print(report)
    print(f"(report written to {path})")
    if args.email and res["verdict"] != "OK":
        ok, detail = oversight.send_email(f"Colophon oversight: {res['verdict']}", report)
        print(f"email: {detail}")
    return 0


def cmd_maintain(args, g, store):
    """Run the nightly sweep (backfill + resolve) and report. With --email, always
    send the summary (a daily heartbeat) — even on an aborted run. Exits non-zero
    when a phase failed so the systemd unit fails + the next timer fire retries; an
    SMTP failure does NOT change the exit code (the heal work still happened)."""
    from . import maintain as M
    res = body = None
    try:
        res = M.run_maintain(g, store, limit=args.limit, min_conf=args.min_conf,
                             apply=args.apply, force=args.force)
        body = M.render_summary(res)
        reports_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir, "reports"))
        os.makedirs(reports_dir, exist_ok=True)
        path = os.path.join(reports_dir, f"maintain-{time.strftime('%Y%m%dT%H%M%S')}.md")
        with open(path, "w") as f:
            f.write(body)
        print(body)
        print(f"(report written to {path})")
    finally:
        if args.email:
            from . import oversight
            subject = M.subject(res) if res else "Colophon daily [CRASH] — see host journal"
            mail = body or ("Colophon maintain crashed before producing a summary.\n"
                            "Check `journalctl -u colophon.service` on the host.\n")
            ok, detail = oversight.send_email(subject, mail)
            print(f"email: {detail}")
            # Surface each stuck book exactly once — only after it has actually been
            # emailed, and only on a real (apply) run so dry-run testing doesn't
            # consume the one-shot report. Silence thereafter = the human keeps it.
            if ok and args.apply and res and res.get("stuck"):
                store.enrich_mark_reported([s["book_id"] for s in res["stuck"]])
    return 0 if (res and res["ok"]) else 1


def cmd_verify(args, g, store):
    """Acquisition gate: is the file at <file> the requested work? Read-only — prints a
    JSON verdict (match/mismatch/unverifiable) and exits 0/3/4 for scripting. The gate's
    hook shim reads the JSON, not the exit code."""
    requested = {}
    if args.hcid:
        requested["hcid"] = args.hcid
    if args.title:
        requested["title"] = args.title
    if args.author:
        requested["authors"] = args.author
    result = verify(requested, args.file)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return {"match": 0, "mismatch": 3, "unverifiable": 4}.get(result["verdict"], 4)


def main(argv=None):
    p = argparse.ArgumentParser(prog="colophon", description="autonomous library metadata healer (Phase 0)")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("precheck", help="assert the files-never-touched preconditions")
    h = sub.add_parser("heal", help="heal one book (dry-run unless --apply)")
    h.add_argument("book_id", type=int)
    h.add_argument("--isbn", required=True)
    h.add_argument("--hcid")
    h.add_argument("--slug")
    h.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    lg = sub.add_parser("log", help="show recent changelog")
    lg.add_argument("-n", type=int, default=20)
    rv = sub.add_parser("revert", help="restore a run from the changelog (dry-run unless --apply)")
    rv.add_argument("run_id")
    rv.add_argument("--apply", action="store_true")
    bf = sub.add_parser("backfill", help="survey the library + propose heals (dry-run unless --apply)")
    bf.add_argument("--limit", type=int, default=20)
    bf.add_argument("--apply", action="store_true")
    en = sub.add_parser("enrich", help="seed bare watch-imports (no hcid) + remember the unresolvable (dry-run unless --apply)")
    en.add_argument("--apply", action="store_true", help="actually submit the refresh (default: dry-run)")
    en.add_argument("--stuck-after", type=int, default=None,
                    help="mark a book stuck after N failed sweeps (default: COLOPHON_ENRICH_STUCK_AFTER or 6)")
    au = sub.add_parser("audit", help="read-only library audit + report (writes nothing)")
    au.add_argument("--limit", type=int, default=None)
    rs = sub.add_parser("resolve", help="Haiku-resolve mis-seeds (propose-only unless --apply)")
    rs.add_argument("--book", type=int, nargs="*", help="specific book ids (default: all flagged mis-seeds)")
    rs.add_argument("--limit", type=int, default=None)
    rs.add_argument("--apply", action="store_true", help="auto-apply re-seeds at conf >= --min-conf")
    rs.add_argument("--min-conf", type=float, default=0.9)
    rs.add_argument("--force", action="store_true", help="re-query books on the cached-unresolvable skip-list")
    rs.add_argument("--clear-skips", action="store_true", help="forget all cached-unresolvable entries, then exit")
    sn = sub.add_parser("series-audit", help="Phase 3 — series numbering + grouping audit (read-only unless --apply)")
    sn.add_argument("--limit", type=int, default=None)
    sn.add_argument("--apply", action="store_true",
                    help="heal clean number-mismatch / number-missing / series-name-missing (reuses the heal path)")
    ov = sub.add_parser("oversight", help="Phase 4b — weekly changelog oversight + verdict (emails only if flagged)")
    ov.add_argument("--days", type=int, default=7)
    ov.add_argument("--email", action="store_true", help="email the digest only when flagged (DRIFT/REVIEW)")
    mt = sub.add_parser("maintain", help="nightly sweep: backfill + resolve in one run, with a summary (dry-run unless --apply)")
    mt.add_argument("--limit", type=int, default=20, help="max books for the backfill survey")
    mt.add_argument("--min-conf", type=float, default=0.9, help="auto-apply gate for resolve re-seeds")
    mt.add_argument("--apply", action="store_true", help="actually write (default: dry-run)")
    mt.add_argument("--force", action="store_true", help="re-query cached-unresolvable mis-seeds this run")
    mt.add_argument("--email", action="store_true", help="always email the summary (a daily heartbeat)")
    ve = sub.add_parser("verify", help="acquisition gate: is a downloaded file the requested work? (read-only)")
    ve.add_argument("file", help="path to the downloaded book file")
    ve.add_argument("--hcid", help="requested Hardcover work id (primary anchor)")
    ve.add_argument("--title", help="requested title (degraded fallback when no --hcid)")
    ve.add_argument("--author", help="requested author(s), used with --title")
    args = p.parse_args(argv)

    g, store = Grimmory(), Store()
    fn = {"precheck": cmd_precheck, "heal": cmd_heal, "log": cmd_log,
          "revert": cmd_revert, "backfill": cmd_backfill, "enrich": cmd_enrich,
          "audit": cmd_audit, "resolve": cmd_resolve, "series-audit": cmd_series_audit,
          "oversight": cmd_oversight, "maintain": cmd_maintain,
          "verify": cmd_verify}[args.cmd]
    try:
        return fn(args, g, store)
    except (GrimmoryError, PreconditionError) as e:
        print("ERROR:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
