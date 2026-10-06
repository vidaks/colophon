"""Weekly changelog oversight and health reporting (local only, no external API calls).

Analyzes the metadata change history over a specified time window to evaluate health.
Rather than querying external APIs, it inspects local changelog entries to detect
issues and surfaces warnings for manual review. Email alerts are dispatched only when
warnings or errors are detected.

Health signals evaluated from the changelog:
  oscillation   repeated updates to the same book across multiple runs  -> DRIFT
  error rate    sustained failure rate exceeding the threshold          -> DRIFT
  any errors    isolated failures below the drift threshold             -> REVIEW
  volume        total writes in the period (informational only)
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
# Oscillation is only active drift while it is still happening. A book still
# oscillating re-heals on roughly every sweep (hourly here), so once its most recent
# heal is older than this it has stopped — it churned earlier in the window but has
# since converged. Such a book is a resolved heads-up, not actionable drift.
SETTLED_AFTER_DAYS = 1.0


def _ts(epoch):
    """Local ISO-8601 string matching the changelog's `ts` column, so the same lexical
    comparison used for the window cutoff also orders the settled cutoff."""
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(epoch))


def _age(ts, now):
    """Human age of an ISO `ts` relative to now ('3d' / '5h' / '12m'); '' if unparseable."""
    try:
        secs = max(0.0, now - time.mktime(time.strptime(ts, "%Y-%m-%dT%H:%M:%S")))
    except (ValueError, OverflowError):
        return ""
    if secs >= 86400:
        return f"{int(secs // 86400)}d"
    if secs >= 3600:
        return f"{int(secs // 3600)}h"
    return f"{int(secs // 60)}m"


def review(store, days=WINDOW_DAYS, now=None):
    now = time.time() if now is None else now
    cutoff = _ts(now - days * 86400)
    settled_cutoff = _ts(now - SETTLED_AFTER_DAYS * 86400)
    rows = [r for r in store.recent(20000)
            if (r["ts"] or "") >= cutoff and not r["dry_run"]]
    writes_by_book, last_heal, runs, writes, errors = defaultdict(set), {}, set(), 0, 0
    for r in rows:
        runs.add(r["run_id"])
        if not r["ok"]:
            errors += 1
        elif r["action"] in WRITE_ACTIONS:
            writes += 1
            writes_by_book[r["book_id"]].add(r["run_id"])
            ts = r["ts"] or ""
            if ts > last_heal.get(r["book_id"], ""):
                last_heal[r["book_id"]] = ts
    total = writes + errors
    oscillating = {b: sorted(rs) for b, rs in writes_by_book.items() if len(rs) > 1}
    # Split oscillation by the most recent heal: a book quiet past the settled cutoff
    # oscillated earlier but has since converged — surfaced as resolved, not drift.
    active = {b: rs for b, rs in oscillating.items()
              if last_heal.get(b, "") >= settled_cutoff}
    settled = {b: rs for b, rs in oscillating.items() if b not in active}
    err_rate = (errors / total) if total else 0.0
    if active or (total >= ERR_MIN_VOL and err_rate >= ERR_RATE_DRIFT):
        verdict = "DRIFT"
    elif errors or settled:
        verdict = "REVIEW"
    else:
        verdict = "OK"
    return {"days": days, "now": now, "runs": len(runs), "writes": writes,
            "errors": errors, "err_rate": err_rate, "oscillating": oscillating,
            "oscillating_active": active, "oscillating_settled": settled,
            "last_heal": last_heal, "verdict": verdict,
            "books_written": sorted(writes_by_book)}


def render(res):
    active, settled = res["oscillating_active"], res["oscillating_settled"]
    now, last = res["now"], res["last_heal"]
    L = [f"Colophon weekly oversight — {_ts(now)[:16].replace('T', ' ')}",
         f"Window: last {res['days']} days",
         f"VERDICT: {res['verdict']}",
         "",
         f"  runs            {res['runs']}",
         f"  writes (heals)  {res['writes']}  (books: {len(res['books_written'])})",
         f"  errors          {res['errors']}  (rate {res['err_rate']:.0%})",
         f"  oscillating     {len(res['oscillating'])}  (active {len(active)}, settled {len(settled)})"]

    def _line(b, rs, suffix):
        age = _age(last.get(b, ""), now)
        when = last.get(b) or "?"
        return f"  - book {b}: {len(rs)} runs, last heal {when}" + (f" ({age} ago{suffix})" if age else "")

    if active:
        L.append("\nActively oscillating (re-healed within the last day — convergence may be failing now):")
        L += [_line(b, rs, "") for b, rs in sorted(active.items())]
    if settled:
        L.append("\nSettled (oscillated earlier in the window but quiet since — already resolved):")
        L += [_line(b, rs, ", quiet since") for b, rs in sorted(settled.items())]

    if res["verdict"] == "OK":
        L.append("\nNo errors, no oscillation — set-once holding. Nothing to do.")
    elif res["verdict"] == "DRIFT":
        msg = ("\nDRIFT: active oscillation and/or a sustained error rate. The set-once "
               "invariant may be breaking now. Review `colophon log` + recent reports; "
               "consider pausing the daily timer (`systemctl disable --now colophon.timer`) "
               "until resolved.")
        if settled:
            msg += (f" ({len(settled)} other book(s) oscillated earlier but have since settled — "
                    "no action needed for those.)")
        L.append(msg)
    else:  # REVIEW
        parts = []
        if settled and not active:
            parts.append(f"{len(settled)} book(s) oscillated earlier in this window but have since "
                         "settled (no re-heal in over a day) — the convergence issue resolved "
                         "itself; no action needed. This is a heads-up, not active drift.")
        if res["errors"]:
            parts.append("Some write errors this week (below drift threshold). Worth a glance at "
                         "`colophon log`; the daily sweep retries and is reversible.")
        L.append("\n" + " ".join(parts))
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
