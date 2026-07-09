"""SQLite changelog — the only recovery store (per plan 20).

Records every heal (book, before, after, target identity, run, ok/error). This is
what powers attribution + batch revert; there is no separate per-field snapshot
mechanism by design.
"""
import json
import os
import sqlite3
import time
from contextlib import contextmanager

def _checkout_root():
    """The repo root when running from a checkout, else None. A pip/pipx install
    doesn't ship pyproject.toml next to the package — that's the discriminator."""
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    return root if os.path.exists(os.path.join(root, "pyproject.toml")) else None


def state_dir():
    """Where local state lands without an explicit override: the checkout root when
    run in place (the historical location), else XDG state (~/.local/state/colophon)
    — never inside site-packages, which a pipx venv makes read-only-ish and hides.

    Compat: an install that already has a colophon.db at the old package-parent
    location keeps using it — the changelog is the only recovery mechanism, and a
    path change on upgrade must not silently orphan it. Only FRESH installs land
    in XDG state."""
    root = _checkout_root()
    if root:
        return root
    legacy = os.path.abspath(os.path.join(os.path.dirname(__file__), os.pardir))
    if os.path.exists(os.path.join(legacy, "colophon.db")):
        return legacy
    base = os.environ.get("XDG_STATE_HOME") or os.path.expanduser("~/.local/state")
    return os.path.join(base, "colophon")


# COLOPHON_DB overrides (e.g. a persistent path for a deployed service).
DB_PATH = os.environ.get("COLOPHON_DB") or os.path.join(state_dir(), "colophon.db")


class Store:
    def __init__(self, path=DB_PATH):
        self.path = os.path.abspath(path)
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        with self._conn() as c:
            c.execute(
                """CREATE TABLE IF NOT EXISTS changes(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    ts TEXT NOT NULL,
                    book_id INTEGER NOT NULL,
                    action TEXT NOT NULL,
                    dry_run INTEGER NOT NULL,
                    ok INTEGER NOT NULL,
                    error TEXT,
                    before_json TEXT,
                    after_json TEXT,
                    target_json TEXT
                )"""
            )
            # Mis-seeds the resolver could not place (no match, or a match below the
            # auto-apply threshold). Keyed by book; `fingerprint` is the normalized
            # title+author the resolve query was built from — if the book's title or
            # author later changes, the fingerprint no longer matches and the book is
            # re-queried. This is what stops the nightly sweep re-asking Hardcover +
            # Haiku about the same unresolvable books every run.
            c.execute(
                """CREATE TABLE IF NOT EXISTS resolve_skip(
                    book_id INTEGER PRIMARY KEY,
                    title TEXT,
                    fingerprint TEXT NOT NULL,
                    action TEXT NOT NULL,
                    reason TEXT,
                    conf REAL,
                    chosen_id TEXT,
                    chosen_title TEXT,
                    isbn TEXT,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 1
                )"""
            )
            # Un-seeded books (no Hardcover id — the bare watch-imports) that the
            # enrich sweep keeps failing to match. `fail_count` is the number of
            # sweeps the book has been seen still un-seeded; at the stuck threshold
            # it is marked `stuck` (dropped from the 30-min sweep so it stops being
            # re-poked) and surfaced ONCE in the daily digest (`reported`) for the
            # human to delete or keep. A book that seeds or is deleted drops out of
            # the un-seeded set and is pruned from this table.
            c.execute(
                """CREATE TABLE IF NOT EXISTS enrich_state(
                    book_id INTEGER PRIMARY KEY,
                    fail_count INTEGER NOT NULL DEFAULT 0,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    stuck INTEGER NOT NULL DEFAULT 0,
                    stuck_since TEXT,
                    reported INTEGER NOT NULL DEFAULT 0
                )"""
            )
            # Settled, non-actionable series-audit verdicts (correct / standalone /
            # no-position), cached so the nightly sweep stops re-querying Hardcover for
            # books that aren't going to change. `fingerprint` is the book identity the
            # verdict depends on (hcid + isbn + series name/number) — if it changes the
            # entry no longer matches and the book is re-queried. `expires_at` is a
            # per-entry JITTERED epoch so a bulk-populated cache expires as a trickle,
            # not all on one night (no nightly thundering herd).
            c.execute(
                """CREATE TABLE IF NOT EXISTS series_verdict(
                    book_id INTEGER PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    verdict TEXT NOT NULL,
                    cached_at TEXT NOT NULL,
                    expires_at REAL NOT NULL
                )"""
            )
            # Identified books the series audit can't fix (corrupt name / unhealable
            # variant) — the delete/re-download candidates. `reported` surfaces each in
            # the digest exactly once; a fingerprint change (the metadata moved) re-opens
            # it. Pruned when a book drops out of the unfixable set.
            c.execute(
                """CREATE TABLE IF NOT EXISTS series_manual(
                    book_id INTEGER PRIMARY KEY,
                    fingerprint TEXT NOT NULL,
                    first_seen TEXT NOT NULL,
                    last_seen TEXT NOT NULL,
                    reported INTEGER NOT NULL DEFAULT 0
                )"""
            )

    @contextmanager
    def _conn(self):
        """A connection that commits on clean exit and ALWAYS closes — the bare
        sqlite3 context manager commits but leaks the handle (fd leak + a noisy
        ResourceWarning in the long-running maintain process). timeout=30 is the
        busy handler: the 30-min enrich timer and the nightly maintain can overlap
        on this file, and WAL lets the reader side proceed while one writes."""
        c = sqlite3.connect(self.path, timeout=30.0)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA journal_mode=WAL")
        try:
            yield c
            c.commit()
        finally:
            c.close()

    @staticmethod
    def new_run_id():
        return time.strftime("run-%Y%m%dT%H%M%S")

    def record(self, run_id, book_id, action, dry_run, ok, error=None,
               before=None, after=None, target=None):
        with self._conn() as c:
            c.execute(
                "INSERT INTO changes(run_id,ts,book_id,action,dry_run,ok,error,"
                "before_json,after_json,target_json) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (run_id, time.strftime("%Y-%m-%dT%H:%M:%S"), int(book_id), action,
                 int(bool(dry_run)), int(bool(ok)), error,
                 json.dumps(before), json.dumps(after), json.dumps(target)),
            )

    def run_changes(self, run_id):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM changes WHERE run_id=? ORDER BY id", (run_id,))]

    def recent(self, n=20):
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM changes ORDER BY id DESC LIMIT ?", (int(n),))]

    # --- resolve skip-list (don't re-query the unresolvable) ---

    def skip_map(self):
        """All skip entries as {book_id: row-dict}."""
        with self._conn() as c:
            return {r["book_id"]: dict(r) for r in c.execute("SELECT * FROM resolve_skip")}

    def skip_put(self, book_id, title, fingerprint, action, reason=None, conf=None,
                 chosen_id=None, chosen_title=None, isbn=None):
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT INTO resolve_skip(book_id,title,fingerprint,action,reason,conf,"
                "chosen_id,chosen_title,isbn,first_seen,last_seen,attempts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,1) "
                "ON CONFLICT(book_id) DO UPDATE SET title=excluded.title, "
                "fingerprint=excluded.fingerprint, action=excluded.action, "
                "reason=excluded.reason, conf=excluded.conf, chosen_id=excluded.chosen_id, "
                "chosen_title=excluded.chosen_title, isbn=excluded.isbn, "
                "last_seen=excluded.last_seen, attempts=resolve_skip.attempts+1",
                (int(book_id), title, fingerprint, action, reason, conf, chosen_id,
                 chosen_title, isbn, now, now),
            )

    def skip_clear(self, book_id=None):
        """Drop one skip entry (book_id given) or all of them. Returns rows removed."""
        with self._conn() as c:
            if book_id is None:
                return c.execute("DELETE FROM resolve_skip").rowcount
            return c.execute("DELETE FROM resolve_skip WHERE book_id=?", (int(book_id),)).rowcount

    # --- enrich memory (don't keep re-poking the unseedable; surface them once) ---

    def enrich_observe(self, unseeded_ids, stuck_after):
        """Record one enrich sweep. `unseeded_ids` is the full current set of books
        that still lack a Hardcover id. Each is upserted with fail_count+1; any
        previously-tracked id NOT in the set has seeded or been deleted, so it is
        pruned. A book crosses to `stuck` at fail_count >= stuck_after. Returns the
        set of currently-stuck book_ids."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        ids = {int(b) for b in unseeded_ids}
        with self._conn() as c:
            tracked = {r["book_id"] for r in c.execute("SELECT book_id FROM enrich_state")}
            gone = tracked - ids
            if gone:
                c.executemany("DELETE FROM enrich_state WHERE book_id=?", [(b,) for b in gone])
            for b in ids:
                c.execute(
                    "INSERT INTO enrich_state(book_id,fail_count,first_seen,last_seen) "
                    "VALUES(?,1,?,?) ON CONFLICT(book_id) DO UPDATE SET "
                    "fail_count=fail_count+1, last_seen=excluded.last_seen",
                    (b, now, now),
                )
            c.execute("UPDATE enrich_state SET stuck=1, stuck_since=? "
                      "WHERE stuck=0 AND fail_count>=?", (now, int(stuck_after)))
            return [r["book_id"] for r in
                    c.execute("SELECT book_id FROM enrich_state WHERE stuck=1")]

    def enrich_stuck_ids(self):
        """All currently-stuck book_ids (excluded from the sweep)."""
        with self._conn() as c:
            return [r["book_id"] for r in
                    c.execute("SELECT book_id FROM enrich_state WHERE stuck=1")]

    def enrich_stuck_unreported(self):
        """Stuck books not yet surfaced in a digest (the actionable list)."""
        with self._conn() as c:
            return [dict(r) for r in c.execute(
                "SELECT * FROM enrich_state WHERE stuck=1 AND reported=0 ORDER BY book_id")]

    def enrich_mark_reported(self, book_ids):
        """Mark stuck books as surfaced — they will not appear in a later digest
        (silence = keep). Returns rows touched."""
        with self._conn() as c:
            return c.executemany(
                "UPDATE enrich_state SET reported=1 WHERE book_id=?",
                [(int(b),) for b in book_ids]).rowcount

    # --- series-audit verdict cache (don't re-query settled, unchanged books) ---

    def series_verdict_map(self):
        """All cached verdicts as {book_id: (fingerprint, verdict, expires_at)}."""
        with self._conn() as c:
            return {r["book_id"]: (r["fingerprint"], r["verdict"], r["expires_at"])
                    for r in c.execute(
                        "SELECT book_id, fingerprint, verdict, expires_at FROM series_verdict")}

    def series_verdict_put(self, book_id, fingerprint, verdict, expires_at):
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        with self._conn() as c:
            c.execute(
                "INSERT INTO series_verdict(book_id,fingerprint,verdict,cached_at,expires_at) "
                "VALUES(?,?,?,?,?) ON CONFLICT(book_id) DO UPDATE SET "
                "fingerprint=excluded.fingerprint, verdict=excluded.verdict, "
                "cached_at=excluded.cached_at, expires_at=excluded.expires_at",
                (int(book_id), fingerprint, verdict, now, float(expires_at)),
            )

    def series_verdict_delete(self, book_id):
        """Drop one cached verdict (a book that is no longer settled). Returns rows removed."""
        with self._conn() as c:
            return c.execute("DELETE FROM series_verdict WHERE book_id=?", (int(book_id),)).rowcount

    def series_verdict_prune(self, before=None):
        """Drop entries expired before `before` (default: now). Bounds the table so a
        deleted book's row, which the survey will never revisit, can't linger forever."""
        cutoff = time.time() if before is None else float(before)
        with self._conn() as c:
            return c.execute("DELETE FROM series_verdict WHERE expires_at < ?", (cutoff,)).rowcount

    # --- series manual-review list (identified books the audit can't fix) ---

    def series_manual_observe(self, items):
        """Record the current set of unfixable series books — `items` is the full
        [(book_id, fingerprint)] the audit flagged but could not heal. A book whose
        fingerprint changed (its metadata moved) re-opens (reported=0); books no longer
        in the set are pruned. Returns the book_ids not yet reported — the new ones to
        surface once in the digest (delete/re-download candidates)."""
        now = time.strftime("%Y-%m-%dT%H:%M:%S")
        cur = {int(b): fp for b, fp in items}
        with self._conn() as c:
            tracked = {r["book_id"]: r["fingerprint"]
                       for r in c.execute("SELECT book_id, fingerprint FROM series_manual")}
            gone = set(tracked) - set(cur)
            if gone:
                c.executemany("DELETE FROM series_manual WHERE book_id=?", [(b,) for b in gone])
            for b, fp in cur.items():
                if tracked.get(b) == fp:
                    continue  # unchanged — keep its reported flag
                c.execute(
                    "INSERT INTO series_manual(book_id,fingerprint,first_seen,last_seen,reported) "
                    "VALUES(?,?,?,?,0) ON CONFLICT(book_id) DO UPDATE SET "
                    "fingerprint=excluded.fingerprint, last_seen=excluded.last_seen, reported=0",
                    (b, fp, now, now))
            return [r["book_id"] for r in
                    c.execute("SELECT book_id FROM series_manual WHERE reported=0")]

    def series_manual_mark_reported(self, book_ids):
        """Mark surfaced books reported — they won't reappear in a later digest unless
        their fingerprint changes. Returns rows touched."""
        with self._conn() as c:
            return c.executemany("UPDATE series_manual SET reported=1 WHERE book_id=?",
                                 [(int(b),) for b in book_ids]).rowcount

    @staticmethod
    def loads(row, field):
        v = row.get(field)
        return json.loads(v) if v else None
