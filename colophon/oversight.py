"""Plan 20 Phase 4b — weekly oversight (changelog-only; no external calls).

Reviews the heal changelog for a window and emits a verdict. Per the advisor's
reshaping: NO Hardcover re-verify (flaky oracle + it would re-litigate the
set-once locks, against plan 20's convergence principle) and NO auto-pause (a
false-positive that silently stops all healing is worse than the bounded daily
sweep, which is already circuit-broken + reversible). So: detect + surface
loudly; acting stays human. Email is sent ONLY when flagged (DRIFT/REVIEW), so a
clean week is silent.

Signals (all from the changelog — cheap, deterministic):
  oscillation   a book with a real WRITE in >1 run in the window — the genuine
                convergence tripwire (set-once should hold)            → DRIFT
  error rate    failed writes / total, sustained (>=THRESH with volume) → DRIFT
  any errors    some failures, below the drift threshold                → REVIEW
  volume        write count — INFORMATIONAL only (a legit bulk import
                trips it); never alarms on its own
"""
import os
import smtplib
import time
from collections import defaultdict
from email.message import EmailMessage

WINDOW_DAYS = 7
WRITE_ACTIONS = ("heal", "backfill-heal")   # real metadata writes (vs flags/skips)
ERR_RATE_DRIFT = 0.34                        # sustained failure fraction → DRIFT
ERR_MIN_VOL = 5                              # min volume for error-rate to mean anything


def review(store, days=WINDOW_DAYS):
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
    rows = [r for r in store.recent(20000)
            if (r["ts"] or "") >= cutoff and not r["dry_run"]]
    writes_by_book, runs, writes, errors = defaultdict(set), set(), 0, 0
    for r in rows:
        runs.add(r["run_id"])
        if not r["ok"]:
            errors += 1
        elif r["action"] in WRITE_ACTIONS:
            writes += 1
            writes_by_book[r["book_id"]].add(r["run_id"])
    total = writes + errors
    oscillating = {b: sorted(rs) for b, rs in writes_by_book.items() if len(rs) > 1}
    err_rate = (errors / total) if total else 0.0
    if oscillating or (total >= ERR_MIN_VOL and err_rate >= ERR_RATE_DRIFT):
        verdict = "DRIFT"
    elif errors:
        verdict = "REVIEW"
    else:
        verdict = "OK"
    return {"days": days, "runs": len(runs), "writes": writes, "errors": errors,
            "err_rate": err_rate, "oscillating": oscillating, "verdict": verdict,
            "books_written": sorted(writes_by_book)}


def render(res):
    L = [f"Colophon weekly oversight — {time.strftime('%Y-%m-%d %H:%M')}",
         f"Window: last {res['days']} days",
         f"VERDICT: {res['verdict']}",
         "",
         f"  runs            {res['runs']}",
         f"  writes (heals)  {res['writes']}  (books: {len(res['books_written'])})",
         f"  errors          {res['errors']}  (rate {res['err_rate']:.0%})",
         f"  oscillating     {len(res['oscillating'])}"]
    if res["oscillating"]:
        L.append("\nOscillating books (healed in >1 run — convergence may be failing):")
        for b, rs in sorted(res["oscillating"].items()):
            L.append(f"  - book {b}: runs {', '.join(rs)}")
    if res["verdict"] == "OK":
        L.append("\nNo errors, no oscillation — set-once holding. Nothing to do.")
    elif res["verdict"] == "REVIEW":
        L.append("\nSome write errors this week (below drift threshold). Worth a glance at "
                 "`colophon log`; the daily sweep retries and is reversible.")
    else:
        L.append("\nDRIFT: oscillation and/or a sustained error rate. The set-once invariant "
                 "may be breaking. Review `colophon log` + recent reports; consider pausing "
                 "the daily timer (`systemctl disable --now colophon.timer`) until resolved.")
    return "\n".join(L) + "\n"


def _smtp():
    host = os.environ.get("SMTP_HOST")
    pw = os.environ.get("SMTP_PASSWORD")
    if not host or not pw:
        return None
    return {"host": host, "port": int(os.environ.get("SMTP_PORT", "587")),
            "user": os.environ.get("SMTP_USER"), "pw": pw,
            "sender": os.environ.get("SMTP_FROM") or os.environ.get("SMTP_USER"),
            "to": os.environ.get("OVERSIGHT_TO") or os.environ.get("SMTP_USER")}


def send_email(subject, body):
    """Send via the host SMTP relay (Gmail submission 587/STARTTLS). Creds come from
    the env file (vault-populated) — never inlined. Returns (ok, detail)."""
    c = _smtp()
    if not c:
        return False, "no SMTP env (SMTP_HOST/SMTP_PASSWORD unset)"
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = c["sender"]
    msg["To"] = c["to"]
    msg.set_content(body)
    try:
        with smtplib.SMTP(c["host"], c["port"], timeout=30) as s:
            s.starttls()
            s.login(c["user"], c["pw"])
            s.send_message(msg)
        return True, f"emailed {c['to']}"
    except Exception as e:  # noqa: BLE001 — surface, don't crash the run
        return False, f"email failed: {str(e)[:120]}"
