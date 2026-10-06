"""REST client and database snapshot utilities for Booklore-family servers (grimmory/Edda).

All writes, refreshes, and setting changes execute through the server REST API.
Fast read-only library surveys and state snapshots query the database container
directly.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request

# All configurable via environment (see .env.example). Defaults are generic so the
# repo carries no deployment-specific identity.
GRIMMORY_URL = os.environ.get("GRIMMORY_URL", "http://localhost:6060/api/v1")
ADMIN_USER = os.environ.get("COLOPHON_ADMIN_USER", "admin")
ADMIN_GROUP = os.environ.get("COLOPHON_ADMIN_GROUP", "admin")
# Shared secret sent in X-Edda-Proxy-Auth for proxy verification. When set,
# the server requires this secret to authorize Remote-User token generation.
# Unset = omit the header (for servers that do not require proxy secrets).
PROXY_AUTH_SECRET = os.environ.get("COLOPHON_PROXY_AUTH_SECRET", "")
DB_CONTAINER = os.environ.get("COLOPHON_DB_CONTAINER", "grimmory-db")
DB_NAME = os.environ.get("COLOPHON_DB_NAME", "grimmory")
# Host path the grimmory library mounts from (e.g. /mnt/media/.../library). Set this
# to enable EPUB inspection in resolve; book_file paths are relative to it. Unset =
# the feature stays off (the resolver simply never inspects files).
BOOKS_ROOT = os.environ.get("COLOPHON_BOOKS_ROOT")
# Public base URL of the grimmory UI (e.g. https://books.example.com). Set this to
# put a deep-link to each book's page in the daily digest's manual-review list.
# Unset = the digest lists the books without links. The UI route is /book/<id>.
BOOKSTORE_URL = os.environ.get("COLOPHON_BOOKSTORE_URL")


class GrimmoryError(Exception):
    pass


class Grimmory:
    def __init__(self, base=GRIMMORY_URL):
        self.base = base
        self._token = None

    def token(self):
        if self._token:
            return self._token
        headers = {"Remote-User": ADMIN_USER, "Remote-Groups": ADMIN_GROUP}
        if PROXY_AUTH_SECRET:
            headers["X-Edda-Proxy-Auth"] = PROXY_AUTH_SECRET
        req = urllib.request.Request(
            f"{self.base}/auth/remote",
            headers=headers,
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                self._token = json.load(r).get("accessToken")
        except (urllib.error.HTTPError, urllib.error.URLError) as e:
            # A bad proxy-auth secret or unreachable server surfaces here; make it a
            # clean GrimmoryError (the CLI handler catches that, not raw urllib errors).
            raise GrimmoryError(f"token mint failed at {self.base}/auth/remote: {e}")
        if not self._token:
            raise GrimmoryError("failed to mint admin token (Remote-User auth)")
        return self._token

    def _call(self, method, path, body=None, _retried=False):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
            if e.code == 401 and not _retried:
                # Token expired mid-run (minted once per process) — re-mint once.
                self._token = None
                return self._call(method, path, body, _retried=True)
            return e.code, e.read().decode()

    def settings(self):
        st, body = self._call("GET", "/settings")
        if st != 200:
            raise GrimmoryError(f"settings GET returned {st}")
        return json.loads(body)

    def preconditions(self):
        """The two settings that guarantee the matcher never touches book files.
        Returns (ok: bool, details: dict)."""
        mp = self.settings().get("metadataPersistenceSettings") or {}
        move = mp.get("moveFilesToLibraryPattern", True)
        save = (mp.get("saveToOriginalFile") or {}).get("anyFormatEnabled", True)
        details = {"moveFilesToLibraryPattern": move, "saveToOriginalFile.anyFormatEnabled": save}
        return (move is False and save is False), details

    def put_metadata(self, book_id, metadata, replace_mode="REPLACE_ALL"):
        """Low-level metadata PUT. clearFlags:{} is required — a null clearFlags
        NPEs server-side (MetadataChangeDetector.shouldClear)."""
        st, resp = self._call(
            "PUT", f"/books/{int(book_id)}/metadata?replaceMode={replace_mode}&mergeCategories=false",
            {"metadata": metadata, "clearFlags": {}},
        )
        if st != 200:
            raise GrimmoryError(f"PUT metadata returned {st}: {resp[:200]}")
        return resp

    def put_identity(self, book_id, isbn=None, hcid=None, slug=None):
        """Set + LOCK the identity fields (the proven heal write)."""
        meta = {}
        if isbn is not None:
            meta["isbn13"] = isbn
            meta["isbn13Locked"] = True
        if hcid is not None:
            meta["hardcoverBookId"] = str(hcid)
            meta["hardcoverBookIdLocked"] = True
        if slug is not None:
            meta["hardcoverId"] = slug
            meta["hardcoverIdLocked"] = True
        if not meta:
            raise GrimmoryError("put_identity needs at least an isbn")
        return self.put_metadata(book_id, meta)

    def refresh(self, book_ids, refresh_covers=True, replace_mode="REPLACE_ALL"):
        """Trigger a Hardcover-first refresh; grimmory fills from the (locked) ISBN."""
        ro = dict(self.settings().get("defaultMetadataRefreshOptions") or {})
        ro["replaceMode"] = replace_mode
        ro["reviewBeforeApply"] = False
        ro["refreshCovers"] = refresh_covers
        st, resp = self._call("POST", "/tasks/start", {
            "taskType": "REFRESH_METADATA_MANUAL",
            "options": {"refreshType": "BOOKS", "bookIds": [int(b) for b in book_ids], "refreshOptions": ro},
        })
        if st not in (200, 202):
            raise GrimmoryError(f"refresh task start returned {st}: {resp[:200]}")
        return resp

    def delete_books(self, book_ids):
        """Hard-delete books by id: DELETE /api/v1/books?ids=<csv>. `ids` is a
        @RequestParam Set<Long> — a QUERY parameter, NOT a JSON body (a body 500s;
        confirmed against a live throwaway record). grimmory removes the record AND
        its files; the response reports any `failedFileDeletions`, for which the
        caller's own file unlink is the fallback. Used only by the plan-22 gate —
        the ONE call that destroys files, so it refuses unless the deployment
        opts in explicitly via COLOPHON_ALLOW_DELETE=1."""
        if os.environ.get("COLOPHON_ALLOW_DELETE") != "1":
            raise GrimmoryError(
                "delete_books refused — removes records AND files; "
                "set COLOPHON_ALLOW_DELETE=1 to enable (the acquisition gate does)")
        ids = [int(b) for b in book_ids]
        if not ids:
            return None
        st, resp = self._call("DELETE", "/books?ids=" + ",".join(str(i) for i in ids), None)
        if st not in (200, 204):
            raise GrimmoryError(f"DELETE /books?ids={ids} returned {st}: {resp[:200]}")
        return resp


# --- read-only snapshot ------------------------------------------------------
SNAPSHOT_COLS = [
    "title", "subtitle", "series_name", "series_number", "series_total",
    "isbn_13", "isbn_10", "hardcover_book_id", "hardcover_id", "page_count",
    "language", "cover_updated_on",
    "isbn_13_locked", "hardcover_book_id_locked", "hardcover_id_locked",
]


def _db(sql):
    # root (the deployed timer) calls podman directly; the dev user needs sudo.
    podman = ["podman"] if os.geteuid() == 0 else ["sudo", "podman"]
    r = subprocess.run(
        podman + ["exec", DB_CONTAINER, "mariadb", "-uroot", DB_NAME, "-N", "-e", sql],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise GrimmoryError(f"db read failed: {r.stderr.strip()[:200]}")
    return r.stdout


def book_ids_by_filename(file_name):
    """book_id(s) whose `book_file.file_name` matches — read-only. Lets the
    acquisition gate resolve a just-landed grab to its grimmory record(s) before
    deleting it. The name is passed as a hex literal: filenames come from indexers
    (attacker-influenced), and quote-escaping alone is bypassable via backslash under
    MariaDB's default SQL mode — `mariadb -e` would then run the injected statement
    as root. CAST back to CHAR so the comparison keeps the column's collation
    (case-insensitive), same matching as the old quoted literal."""
    if not file_name:
        return []
    hexname = file_name.encode("utf-8").hex()
    out = _db("SELECT book_id FROM book_file WHERE file_name="
              f"CAST(x'{hexname}' AS CHAR CHARACTER SET utf8mb4);").strip()
    return [int(x) for x in out.split() if x.strip().isdigit()]


def book_url(book_id):
    """Deep-link to the book's page in the grimmory UI (Authelia-gated), or None if
    COLOPHON_BOOKSTORE_URL is unset. The digest links here; deletion happens via the
    UI's own button — no destructive link is ever put in an email."""
    if not BOOKSTORE_URL:
        return None
    return f"{BOOKSTORE_URL.rstrip('/')}/book/{int(book_id)}"


def briefs(book_ids):
    """{book_id: {title, isbn, authors}} for the given ids — read-only, for the
    digest's manual-review list. Empty in, empty out."""
    ids = [int(b) for b in book_ids]
    if not ids:
        return {}
    csv = ",".join(str(b) for b in ids)
    out = _db(
        "SELECT bm.book_id, IFNULL(bm.title,''), IFNULL(bm.isbn_13,''), "
        "IFNULL((SELECT GROUP_CONCAT(a.name ORDER BY m.sort_order SEPARATOR ', ') "
        "  FROM book_metadata_author_mapping m JOIN author a ON a.id=m.author_id "
        "  WHERE m.book_id=bm.book_id),'') "
        f"FROM book_metadata bm WHERE bm.book_id IN ({csv});"
    )
    res = {}
    for line in out.splitlines():
        col = line.split("\t")
        if len(col) < 4:
            continue
        res[int(col[0])] = {"title": col[1], "isbn": col[2], "authors": col[3]}
    return res


def snapshot(book_id):
    """Current metadata for a book as a dict, or None if it doesn't exist."""
    bid = int(book_id)
    pairs = ",".join(f"'{c}',{c}" for c in SNAPSHOT_COLS)
    out = _db(f"SELECT JSON_OBJECT({pairs}) FROM book_metadata WHERE book_id={bid};").strip()
    if not out:
        return None
    snap = json.loads(out)
    snap["authors"] = _db(
        "SELECT IFNULL(GROUP_CONCAT(a.name ORDER BY m.sort_order SEPARATOR ', '),'') "
        "FROM book_metadata_author_mapping m JOIN author a ON a.id=m.author_id "
        f"WHERE m.book_id={bid};"
    ).strip()
    return snap


def epub_path(book_id):
    """Host filesystem path of the book's largest EPUB, or None — read-only.

    Requires BOOKS_ROOT (COLOPHON_BOOKS_ROOT): `book_file` stores paths relative to
    grimmory's in-container library root, so we re-root them on the host. Returns None
    when BOOKS_ROOT is unset, the book has no EPUB, or no row matches."""
    if not BOOKS_ROOT:
        return None
    out = _db(
        "SELECT IFNULL(file_sub_path,''), file_name FROM book_file "
        f"WHERE book_id={int(book_id)} AND LOWER(file_name) LIKE '%.epub' "
        "ORDER BY file_size_kb DESC LIMIT 1;"
    ).strip()
    if not out:
        return None
    parts = out.split("\t")
    sub, name = (parts[0], parts[1]) if len(parts) == 2 else ("", parts[-1])
    return os.path.join(BOOKS_ROOT, sub, name) if sub else os.path.join(BOOKS_ROOT, name)


# --- read-only surveys -------------------------------------------------------
# Every query that knows grimmory's schema lives HERE, so porting to another
# Booklore-family server really is a matter of reimplementing this one module
# (the README makes that claim; this keeps it true). Callers get plain dicts.

_SURVEY_BROKEN_ISBN = (
    "SELECT book_id FROM book_metadata "
    "WHERE hardcover_book_id IS NOT NULL AND hardcover_book_id<>'' "
    "AND (isbn_13 IS NULL OR isbn_13='' OR LENGTH(REPLACE(isbn_13,'-',''))<>13) "
    "AND (isbn_13_locked IS NULL OR isbn_13_locked=0) "
    "ORDER BY book_id LIMIT {limit};"
)

_UNSEEDED = (
    "SELECT bm.book_id FROM book_metadata bm JOIN book b ON b.id=bm.book_id "
    "WHERE (bm.hardcover_book_id IS NULL OR bm.hardcover_book_id='') "
    "AND (b.deleted IS NULL OR b.deleted=0) ORDER BY bm.book_id;"
)

_ALL_BOOKS = (
    "SELECT bm.book_id, IFNULL(bm.title,''), IFNULL(bm.isbn_13,''), "
    "IFNULL(bm.hardcover_book_id,''), IFNULL(bm.isbn_13_locked,0), "
    "IFNULL((SELECT GROUP_CONCAT(a.name ORDER BY m.sort_order SEPARATOR ', ') "
    "  FROM book_metadata_author_mapping m JOIN author a ON a.id=m.author_id WHERE m.book_id=bm.book_id),''), "
    "IFNULL((SELECT MAX(f.file_size_kb) FROM book_file f WHERE f.book_id=bm.book_id),0), "
    "IFNULL(DATE(b.added_on),'') "
    "FROM book_metadata bm JOIN book b ON b.id=bm.book_id "
    "WHERE b.deleted IS NULL OR b.deleted=0;"
)

_SERIES_BOOKS = (
    "SELECT bm.book_id, IFNULL(bm.title,''), IFNULL(bm.hardcover_book_id,''), "
    "IFNULL(bm.series_name,''), IFNULL(bm.series_number,''), IFNULL(bm.isbn_13,''), "
    "IFNULL(bm.series_number_locked,0), IFNULL(bm.series_name_locked,0) "
    "FROM book_metadata bm JOIN book b ON b.id=bm.book_id "
    "WHERE (b.deleted IS NULL OR b.deleted=0) "
    "AND bm.series_name IS NOT NULL AND bm.series_name<>'';"
)

_UNGROUPED = (
    "SELECT bm.book_id, IFNULL(bm.title,''), IFNULL(bm.hardcover_book_id,''), "
    "IFNULL(bm.isbn_13,''), IFNULL(bm.series_name_locked,0) "
    "FROM book_metadata bm JOIN book b ON b.id=bm.book_id "
    "WHERE (b.deleted IS NULL OR b.deleted=0) "
    "AND bm.hardcover_book_id IS NOT NULL AND bm.hardcover_book_id<>'' "
    "AND (bm.series_name IS NULL OR bm.series_name='') "
    "AND bm.isbn_13 IS NOT NULL AND LENGTH(REPLACE(bm.isbn_13,'-',''))=13 "
    # RAND() so a residual set of stuck/standalone ungrouped books can't permanently
    # monopolise the per-run cap and starve newly-ungrouped books of a survey slot.
    "ORDER BY RAND() LIMIT {limit};"
)


def survey_broken_isbn(limit):
    """book_ids with a Hardcover id but a missing/malformed, UNLOCKED ISBN — the
    high-confidence backfill candidates. Locked (settled) books never reappear."""
    out = _db(_SURVEY_BROKEN_ISBN.format(limit=int(limit)))
    return [int(x) for x in out.split()]


def unseeded_ids():
    """book_ids that still lack a Hardcover id (bare watch-imports), excluding
    soft-deleted ones. Locks are irrelevant — no identity to protect yet."""
    return [int(x) for x in _db(_UNSEEDED).split()]


def all_books():
    """Every live book as {book_id, title, isbn, hcid, locked, authors, kb, added}."""
    books = []
    for line in _db(_ALL_BOOKS).splitlines():
        c = line.split("\t")
        if len(c) < 8:
            continue
        books.append({"book_id": int(c[0]), "title": c[1], "isbn": c[2], "hcid": c[3],
                      "locked": c[4] == "1", "authors": c[5], "kb": int(c[6] or 0), "added": c[7]})
    return books


def series_books():
    """Every live book that carries a series_name, with its number + lock flags."""
    rows = []
    for line in _db(_SERIES_BOOKS).splitlines():
        c = line.split("\t")
        if len(c) < 8:
            continue
        rows.append({"book_id": int(c[0]), "title": c[1], "hcid": c[2].strip(),
                     "series_name": c[3], "series_number": c[4].strip(),
                     "isbn": c[5].strip(), "num_locked": c[6] == "1", "name_locked": c[7] == "1"})
    return rows


def ungrouped_candidates(limit):
    """hcid'd books with a clean 13-char ISBN but no series_name — possible ungrouped
    series members. Bounded and randomised; healed books drop out next run."""
    if not limit:
        return []
    rows = []
    for line in _db(_UNGROUPED.format(limit=int(limit))).splitlines():
        c = line.split("\t")
        if len(c) < 5:
            continue
        rows.append({"book_id": int(c[0]), "title": c[1], "hcid": c[2].strip(),
                     "series_name": "", "series_number": "", "isbn": c[3].strip(),
                     "num_locked": False, "name_locked": c[4] == "1"})
    return rows


def signature(snap):
    """The fields whose change means a refresh has landed."""
    if not snap:
        return None
    return (snap.get("title"), snap.get("isbn_13"), snap.get("page_count"), snap.get("cover_updated_on"))


def wait_for_change(book_id, before_sig, tries=25, settle=5, interval=3):
    """Poll until the snapshot signature changes, then let the cover settle."""
    after = None
    for _ in range(tries):
        time.sleep(interval)
        after = snapshot(book_id)
        if signature(after) != before_sig:
            break
    for _ in range(settle):
        time.sleep(interval)
    return snapshot(book_id)
