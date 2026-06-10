"""SQLite changelog — the only recovery store (per plan 20).

Records every heal (book, before, after, target identity, run, ok/error). This is
what powers attribution + batch revert; there is no separate per-field snapshot
mechanism by design.
"""
import json
import os
import sqlite3
import time

DB_PATH = os.path.join(os.path.dirname(__file__), os.pardir, "colophon.db")


class Store:
    def __init__(self, path=DB_PATH):
        self.path = os.path.abspath(path)
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
            c.execute("CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT)")
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

    def epoch(self):
        """Re-audit epoch. Settled (locked) books are only re-opened when this is
        bumped (i.e. when the matcher logic materially improves)."""
        with self._conn() as c:
            r = c.execute("SELECT value FROM meta WHERE key='epoch'").fetchone()
            return int(r["value"]) if r else 1

    def bump_epoch(self):
        e = self.epoch() + 1
        with self._conn() as c:
            c.execute("INSERT INTO meta(key,value) VALUES('epoch',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(e),))
        return e

    def _conn(self):
        c = sqlite3.connect(self.path)
        c.row_factory = sqlite3.Row
        return c

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

    @staticmethod
    def loads(row, field):
        v = row.get(field)
        return json.loads(v) if v else None
