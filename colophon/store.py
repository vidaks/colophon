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

    @staticmethod
    def loads(row, field):
        v = row.get(field)
        return json.loads(v) if v else None
