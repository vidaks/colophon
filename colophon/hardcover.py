"""Hardcover access (read-only).

Reads the API key from `COLOPHON_HARDCOVER_KEY` (or `HARDCOVER_API_KEY`) and calls
the API directly. Optionally, if `COLOPHON_HARDCOVER_QUERY_CMD` is set, the key is
never read into this process: that command is run with the GraphQL on stdin and must
return the JSON response (useful when the key lives in a secrets manager). A leading
`Bearer ` on the key is stripped (some stores keep it prefixed).
"""
import json
import os
import subprocess
import urllib.request

# Optional command that takes a GraphQL query on stdin and returns the JSON response,
# so the API key need not enter this process. Unset by default — use the key instead.
QUERY_SH = os.environ.get("COLOPHON_HARDCOVER_QUERY_CMD")
HC_ENDPOINT = "https://api.hardcover.app/v1/graphql"


class HardcoverError(Exception):
    pass


def _api_key():
    k = os.environ.get("COLOPHON_HARDCOVER_KEY") or os.environ.get("HARDCOVER_API_KEY")
    if not k:
        return None
    k = k.strip()
    return k[7:].strip() if k.lower().startswith("bearer ") else k


def query(graphql):
    key = _api_key()
    if key:
        req = urllib.request.Request(
            HC_ENDPOINT, data=json.dumps({"query": graphql}).encode(),
            headers={"Authorization": f"Bearer {key}", "Content-Type": "application/json"},
        )
        data = json.load(urllib.request.urlopen(req, timeout=60))
    elif QUERY_SH:  # delegate so the key never enters this process
        r = subprocess.run([QUERY_SH], input=graphql, capture_output=True, text=True, timeout=60)
        if r.returncode != 0:
            raise HardcoverError(f"query command failed: {r.stderr.strip()[:200]}")
        data = json.loads(r.stdout)
    else:
        raise HardcoverError("no Hardcover credentials — set COLOPHON_HARDCOVER_KEY "
                             "(or COLOPHON_HARDCOVER_QUERY_CMD)")
    if data.get("errors"):
        raise HardcoverError(f"hardcover errors: {data['errors']}")
    return data.get("data") or {}


def book_by_id(hcid):
    """Canonical identity + signals for a Hardcover book id, or None."""
    q = (
        "query { b: books_by_pk(id: %d) { id slug title pages users_count canonical_id "
        "default_physical_edition { isbn_13 } "
        "editions(where:{isbn_13:{_is_null:false}}, limit:1){ isbn_13 } "
        "book_series(order_by:{position:asc}){ position series { name } } "
        "contributions { author { name } } } }" % int(hcid)
    )
    b = query(q).get("b")
    if not b:
        return None
    isbn = (b.get("default_physical_edition") or {}).get("isbn_13")
    if not isbn:
        eds = b.get("editions") or []
        isbn = eds[0]["isbn_13"] if eds else None
    bs = b.get("book_series") or []
    authors = [c["author"]["name"] for c in (b.get("contributions") or []) if c.get("author")]
    return {
        "hcid": b["id"], "slug": b.get("slug"), "title": b.get("title"),
        "isbn": isbn, "pages": b.get("pages"), "users_count": b.get("users_count") or 0,
        "canonical_id": b.get("canonical_id"),
        "series": bs[0]["series"]["name"] if bs else None,
        "position": bs[0]["position"] if bs else None,
        "authors": authors,
    }


def search(query_text, per_page=8):
    """Full-text book search (Hardcover's Typesense `search` — the broad Hasura
    filter is 403-restricted). Returns a list of candidate dicts."""
    safe = json.dumps(query_text)  # GraphQL string literal, escaped
    gql = (f'query {{ search(query: {safe}, query_type: "Book", per_page: {int(per_page)}, '
           f'page: 1) {{ results }} }}')
    results = (search_raw := query(gql)).get("search") or {}
    out = []
    for h in (results.get("results") or {}).get("hits") or []:
        d = h.get("document") or {}
        out.append({
            "hcid": d.get("id"), "slug": d.get("slug"), "title": d.get("title"),
            "subtitle": d.get("subtitle"), "authors": d.get("author_names") or [],
            "users_count": d.get("users_count") or 0, "pages": d.get("pages"),
            "release_year": d.get("release_year"), "compilation": bool(d.get("compilation")),
            "series_names": d.get("series_names") or [], "isbns": d.get("isbns") or [],
        })
    return out
