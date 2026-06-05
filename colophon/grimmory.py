"""grimmory REST client + read-only book snapshot.

Writes/refresh/settings go through grimmory's API (the standalone path). Book-state
snapshots are read read-only from the rootful grimmory-db container (on-host, no
password) — simpler and more reliable than reverse-engineering a single-book
metadata GET, and the plan permits read-only DB access on the host.
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
DB_CONTAINER = os.environ.get("COLOPHON_DB_CONTAINER", "grimmory-db")
DB_NAME = os.environ.get("COLOPHON_DB_NAME", "grimmory")
# Host path the grimmory library mounts from (e.g. /mnt/media/.../library). Set this
# to enable EPUB inspection in resolve; book_file paths are relative to it. Unset =
# the feature stays off (the resolver simply never inspects files).
BOOKS_ROOT = os.environ.get("COLOPHON_BOOKS_ROOT")


class GrimmoryError(Exception):
    pass


class Grimmory:
    def __init__(self, base=GRIMMORY_URL):
        self.base = base
        self._token = None

    def token(self):
        if self._token:
            return self._token
        req = urllib.request.Request(
            f"{self.base}/auth/remote",
            headers={"Remote-User": ADMIN_USER, "Remote-Groups": ADMIN_GROUP},
        )
        self._token = json.load(urllib.request.urlopen(req, timeout=30)).get("accessToken")
        if not self._token:
            raise GrimmoryError("failed to mint admin token (Remote-User auth)")
        return self._token

    def _call(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            f"{self.base}{path}", data=data, method=method,
            headers={"Authorization": f"Bearer {self.token()}", "Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=90) as r:
                return r.status, r.read().decode()
        except urllib.error.HTTPError as e:
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
        caller's own file unlink is the fallback. Used only by the plan-22 gate."""
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
    """book_id(s) whose `book_file.file_name` matches exactly — read-only. Lets the
    acquisition gate resolve a just-landed grab to its grimmory record(s) before
    deleting it. Single quotes in the name are SQL-escaped (titles like
    "Salvation's Child")."""
    safe = (file_name or "").replace("'", "''")
    out = _db(f"SELECT book_id FROM book_file WHERE file_name='{safe}';").strip()
    return [int(x) for x in out.split() if x.strip().isdigit()]


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
