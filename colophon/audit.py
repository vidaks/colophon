"""Library-wide audit — READ-ONLY. Writes nothing to grimmory.

Catalogs every book and produces a report so we can see the true scope before
pointing any destructive logic at it:
  - mis-seeds      hcid title disagrees with the book (likely wrong identity)
  - sets/omnibus   hcid looks like a collection (needs the keep-latest policy)
  - low-confidence title matches but the hcid is obscure / a non-canonical edition
  - heal-isbn      identity right, ISBN broken (the auto-heal band)
  - no-hcid        unidentified (needs title/author resolution)
  - duplicates     groups of the same book (for the dedup/keep-latest stage)

Per-book Hardcover lookups are by id (indexed/cheap), cached in-process, rate-limited,
and tolerant of provider 403s (broad search is restricted — see plan 20).
"""
import re
import time
from collections import defaultdict

from . import grimmory, hardcover, matcher

RATE_SLEEP = 0.3            # be gentle on Hardcover (403s on volume)
LOW_USERS = 50             # below this, an assigned edition is suspect
_cache = {}                # hcid -> resolved dict | ("error", msg) | None


def _book_by_id(hcid):
    if hcid in _cache:
        return _cache[hcid]
    try:
        time.sleep(RATE_SLEEP)
        v = hardcover.book_by_id(hcid)
    except Exception as e:  # 403 / network / parse — tolerate, keep going
        v = ("error", str(e))
    _cache[hcid] = v
    return v


def audit_book(b):
    """Return (category, reason) for one book row."""
    title = b["title"]
    hcid = (b["hcid"] or "").strip()
    isbn = (b["isbn"] or "").strip()
    if not hcid:
        return "no-hcid", "no Hardcover id (needs title/author resolution)"
    cand = _book_by_id(hcid)
    if isinstance(cand, tuple):
        return "error", f"hardcover lookup failed: {cand[1][:50]}"
    if not cand:
        return "review-misseed", f"hcid {hcid} not found in Hardcover"
    if matcher.is_set(cand):
        return "review-set", f"set/omnibus (users={cand['users_count']}, pages={cand['pages']}) → {cand['title']!r}"
    if not matcher._title_match(title, cand["title"]):
        return "review-misseed", f"title {title!r} != hcid {cand['title']!r}"
    flags = []
    if cand.get("canonical_id"):
        flags.append("non-canonical edition")
    if (cand.get("users_count") or 0) < LOW_USERS:
        flags.append(f"obscure (users={cand['users_count']})")
    if len(re.sub(r"\D", "", isbn)) != 13:
        return "heal-isbn", f"identity ok, ISBN broken/missing ({isbn or 'none'})"
    if flags:
        return "review-lowconf", "; ".join(flags)
    return "ok", "matches"


_ALL = (
    "SELECT bm.book_id, IFNULL(bm.title,''), IFNULL(bm.isbn_13,''), "
    "IFNULL(bm.hardcover_book_id,''), IFNULL(bm.isbn_13_locked,0), "
    "IFNULL((SELECT GROUP_CONCAT(a.name ORDER BY m.sort_order SEPARATOR ', ') "
    "  FROM book_metadata_author_mapping m JOIN author a ON a.id=m.author_id WHERE m.book_id=bm.book_id),''), "
    "IFNULL((SELECT MAX(f.file_size_kb) FROM book_file f WHERE f.book_id=bm.book_id),0), "
    "IFNULL(DATE(b.added_on),'') "
    "FROM book_metadata bm JOIN book b ON b.id=bm.book_id "
    "WHERE b.deleted IS NULL OR b.deleted=0;"
)


def all_books():
    out = grimmory._db(_ALL)
    books = []
    for line in out.splitlines():
        c = line.split("\t")
        if len(c) < 8:
            continue
        books.append({"book_id": int(c[0]), "title": c[1], "isbn": c[2], "hcid": c[3],
                      "locked": c[4] == "1", "authors": c[5], "kb": int(c[6] or 0), "added": c[7]})
    return books


def find_duplicates(books):
    """Group by normalized title+author; return groups with >1 book, with a
    suggested keeper (largest file = most complete; tie-break newest)."""
    groups = defaultdict(list)
    for b in books:
        key = matcher._norm(b["title"]) + " :: " + matcher._norm(b["authors"])
        if key.strip(" :"):
            groups[key].append(b)
    dups = []
    for key, members in groups.items():
        if len(members) < 2:
            continue
        keeper = sorted(members, key=lambda b: (b["kb"], b["added"]), reverse=True)[0]
        dups.append({"key": key, "members": members, "keeper": keeper["book_id"]})
    return sorted(dups, key=lambda d: -len(d["members"]))


def run_audit(limit=None):
    books = all_books()
    if limit:
        books = books[:limit]
    cats = defaultdict(list)
    for b in books:
        if b["locked"]:
            cats["ok-locked"].append((b, "healed + locked by Colophon"))
            continue
        cat, reason = audit_book(b)
        cats[cat].append((b, reason))
    return {"total": len(books), "categories": cats, "duplicates": find_duplicates(books)}


_LABELS = {"ok": "healthy", "ok-locked": "healed + locked", "heal-isbn": "ISBN auto-heal band",
           "review-lowconf": "low-confidence", "review-set": "set / omnibus",
           "review-misseed": "likely mis-seed", "no-hcid": "unidentified", "error": "lookup error"}
_ORDER = ["ok", "ok-locked", "heal-isbn", "review-lowconf", "review-set", "review-misseed", "no-hcid", "error"]


def render_report(result):
    cats, dups = result["categories"], result["duplicates"]
    L = [f"# Colophon library audit — {time.strftime('%Y-%m-%d %H:%M')}",
         f"\n**{result['total']} books** scanned. Read-only — nothing was changed.\n",
         "## Summary\n"]
    for k in _ORDER:
        if cats.get(k):
            L.append(f"- **{len(cats[k])}** {_LABELS.get(k, k)}")
    redundant = sum(len(d["members"]) - 1 for d in dups)
    L.append(f"- **{len(dups)}** duplicate groups ({redundant} redundant copies)")

    def section(title, key, note=""):
        if not cats.get(key):
            return
        L.append(f"\n## {title} ({len(cats[key])}){note}\n")
        for b, reason in sorted(cats[key], key=lambda x: x[0]["book_id"]):
            L.append(f"- `{b['book_id']}` {b['title']!r} — {reason}")

    section("Likely mis-seeds (wrong identity)", "review-misseed", " — Haiku-resolve or manual")
    section("Sets / omnibus", "review-set", " — keep-latest policy")
    section("Low-confidence matches", "review-lowconf", " — obscure/non-canonical edition")
    section("Unidentified (no Hardcover id)", "no-hcid")
    section("ISBN auto-heal candidates", "heal-isbn")
    section("Provider lookup errors (403/etc.)", "error")
    if dups:
        L.append(f"\n## Duplicate groups ({len(dups)}) — keep-latest, soft-delete the rest\n")
        for d in dups:
            L.append(f"\n**{d['members'][0]['title']!r}** — {len(d['members'])} copies "
                     f"(suggest keep `{d['keeper']}`, the largest):")
            for b in sorted(d["members"], key=lambda b: -b["kb"]):
                mark = "  ← keep" if b["book_id"] == d["keeper"] else ""
                L.append(f"  - `{b['book_id']}`  {b['kb']} KB  added {b['added']}{mark}")
    return "\n".join(L) + "\n"
