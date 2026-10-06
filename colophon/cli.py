"""Command-line interface for Colophon. Dry-run by default; --apply writes changes.

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


def _reports_dir():
    """Directory reports are written to (created if missing). COLOPHON_REPORTS
    overrides; the default sits next to the changelog (checkout root, or XDG state
    for a packaged install)."""
    from .store import state_dir
    d = os.environ.get("COLOPHON_REPORTS") or os.path.join(state_dir(), "reports")
    os.makedirs(d, exist_ok=True)
    return d


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
    print(f"[{mode}] backfill {res['run_id']}  ({len(res['proposals'])} books surveyed)")
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
    """Trigger initial metadata lookups for newly imported books without provider
    IDs. Books that fail to match after repeated attempts are marked stuck and
    reported for manual review. Dry-run unless --apply."""
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
    path = os.path.join(_reports_dir(), f"audit-{time.strftime('%Y%m%dT%H%M%S')}.md")
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
    path = os.path.join(_reports_dir(), f"resolve-{time.strftime('%Y%m%dT%H%M%S')}.md")
    with open(path, "w") as f:
        f.write(report)
    print(report)
    print(f"\n(report written to {path})")
    return 0


def cmd_series_audit(args, g, store):
    from .series_audit import render, run as series_run
    try:
        res = series_run(limit=args.limit, apply=args.apply, g=g, store=store, force=args.force)
    except PreconditionError as e:
        print(e)
        return 2
    report = render(res)
    path = os.path.join(_reports_dir(), f"series-{time.strftime('%Y%m%dT%H%M%S')}.md")
    with open(path, "w") as f:
        f.write(report)
    print(report)
    print(f"\n(report written to {path})")
    return 0


def cmd_oversight(args, g, store):
    from . import oversight
    res = oversight.review(store, days=args.days)
    report = oversight.render(res)
    path = os.path.join(_reports_dir(), f"oversight-{time.strftime('%Y%m%dT%H%M%S')}.md")
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
        path = os.path.join(_reports_dir(), f"maintain-{time.strftime('%Y%m%dT%H%M%S')}.md")
        with open(path, "w") as f:
            f.write(body)
        print(body)
        print(f"(report written to {path})")
    finally:
        # --notify pushes a summary only on a NOTEWORTHY run (so a frequent, e.g.
        # hourly, schedule stays quiet on a clean no-op) via COLOPHON_NOTIFY_CMD.
        # --email always sends the SMTP heartbeat (the legacy nightly behaviour).
        do_notify = args.notify and M.is_noteworthy(res)
        if do_notify or args.email:
            subject = M.subject(res) if res else "Colophon maintain [CRASH] — see host journal"
            body = body or ("Colophon maintain crashed before producing a summary.\n"
                            "Check `journalctl -u colophon.service` on the host.\n")
            if do_notify:
                ok, detail = M.push(subject, body)
                print(f"notify: {detail}")
            else:
                from . import oversight
                ok, detail = oversight.send_email(subject, body)
                print(f"email: {detail}")
            # Surface each stuck book exactly once — only after it has actually been
            # delivered, and only on a real (apply) run so dry-run testing doesn't
            # consume the one-shot report. Silence thereafter = the human keeps it.
            if ok and args.apply and res and res.get("stuck"):
                store.enrich_mark_reported([s["book_id"] for s in res["stuck"]])
            if ok and args.apply and res and res.get("manual"):
                store.series_manual_mark_reported([m["book_id"] for m in res["manual"]])
    return 0 if (res and res["ok"]) else 1


def cmd_verify(args, g, store):
    """Verify whether the book file at <file> matches the requested work.
    Prints a JSON verdict (match/mismatch/unverifiable) and exits 0/3/4."""
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
    p = argparse.ArgumentParser(
        prog="colophon",
        description="Automated metadata management for Booklore and Edda ebook servers",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("precheck", help="verify server settings before making changes")
    h = sub.add_parser("heal", help="update metadata for a single book (dry-run unless --apply)")
    h.add_argument("book_id", type=int)
    h.add_argument("--isbn", required=True)
    h.add_argument("--hcid")
    h.add_argument("--slug")
    h.add_argument("--apply", action="store_true", help="write changes to the server (default: dry-run)")
    lg = sub.add_parser("log", help="show recent metadata change history")
    lg.add_argument("-n", type=int, default=20)
    rv = sub.add_parser("revert", help="restore prior metadata from the changelog (dry-run unless --apply)")
    rv.add_argument("run_id")
    rv.add_argument("--apply", action="store_true", help="write restored metadata to the server")
    bf = sub.add_parser("backfill", help="find and fix missing or broken ISBNs (dry-run unless --apply)")
    bf.add_argument("--limit", type=int, default=20)
    bf.add_argument("--apply", action="store_true", help="write changes to the server")
    en = sub.add_parser(
        "enrich",
        help="request initial metadata for books without provider IDs (dry-run unless --apply)",
    )
    en.add_argument("--apply", action="store_true", help="submit metadata refresh to the server")
    en.add_argument(
        "--stuck-after",
        type=int,
        default=None,
        help="mark a book stuck after N failed attempts (default: COLOPHON_ENRICH_STUCK_AFTER or 6)",
    )
    au = sub.add_parser("audit", help="audit library metadata and generate a report (read-only)")
    au.add_argument("--limit", type=int, default=None)
    rs = sub.add_parser(
        "resolve",
        help="match misidentified books using Hardcover and language model adjudication (dry-run unless --apply)",
    )
    rs.add_argument("--book", type=int, nargs="*", help="specific book IDs to check (default: all flagged books)")
    rs.add_argument("--limit", type=int, default=None)
    rs.add_argument(
        "--apply",
        action="store_true",
        help="apply updates when match confidence is at or above --min-conf",
    )
    rs.add_argument("--min-conf", type=float, default=0.9, help="minimum match confidence required to apply changes")
    rs.add_argument("--force", action="store_true", help="retry books on the unresolvable skip list")
    rs.add_argument("--clear-skips", action="store_true", help="clear all unresolvable skip list entries and exit")
    sn = sub.add_parser(
        "series-audit",
        help="audit series names and volume numbers against Hardcover (read-only unless --apply)",
    )
    sn.add_argument("--limit", type=int, default=None)
    sn.add_argument(
        "--apply",
        action="store_true",
        help="correct volume numbers and missing series assignments",
    )
    sn.add_argument(
        "--force",
        action="store_true",
        help="ignore cached verdicts and re-query Hardcover for every book",
    )
    ov = sub.add_parser(
        "oversight",
        help="review changelog entries for repeated updates or errors",
    )
    ov.add_argument("--days", type=int, default=7, help="number of days of changelog history to inspect")
    ov.add_argument("--email", action="store_true", help="send email notification if warnings or errors are found")
    mt = sub.add_parser(
        "maintain",
        help="run backfill, resolve, and series audits in one pass (dry-run unless --apply)",
    )
    mt.add_argument("--limit", type=int, default=20, help="maximum books to survey during backfill")
    mt.add_argument(
        "--min-conf",
        type=float,
        default=0.9,
        help="minimum confidence threshold to apply resolution matches",
    )
    mt.add_argument("--apply", action="store_true", help="write changes to the server")
    mt.add_argument("--force", action="store_true", help="retry previously unresolvable books this run")
    mt.add_argument("--email", action="store_true", help="send daily summary report via email")
    mt.add_argument(
        "--notify",
        action="store_true",
        help="send summary via COLOPHON_NOTIFY_CMD only when changes or errors occur",
    )
    ve = sub.add_parser(
        "verify",
        help="verify whether a downloaded ebook file matches a requested book (read-only)",
    )
    ve.add_argument("file", help="path to the downloaded ebook file")
    ve.add_argument("--hcid", help="requested Hardcover work ID")
    ve.add_argument("--title", help="requested title (used when --hcid is not available)")
    ve.add_argument("--author", help="requested author, used with --title")
    args = p.parse_args(argv)

    # One registry: (handler, what it needs). Build only what the command needs —
    # `verify` runs in the acquisition gate's context and `audit` reads the server DB
    # directly; neither may create a stray changelog file just by being dispatched.
    fn, needs = {
        "precheck": (cmd_precheck, "g"), "heal": (cmd_heal, "gs"),
        "log": (cmd_log, "s"), "revert": (cmd_revert, "gs"),
        "backfill": (cmd_backfill, "gs"), "enrich": (cmd_enrich, "gs"),
        "audit": (cmd_audit, ""), "resolve": (cmd_resolve, "gs"),
        "series-audit": (cmd_series_audit, "gs"), "oversight": (cmd_oversight, "s"),
        "maintain": (cmd_maintain, "gs"), "verify": (cmd_verify, ""),
    }[args.cmd]
    g = Grimmory() if "g" in needs else None
    store = Store() if "s" in needs else None
    try:
        return fn(args, g, store)
    except (GrimmoryError, PreconditionError) as e:
        print("ERROR:", e, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
