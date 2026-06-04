"""Stage 2 — Haiku resolver for mis-seeds (propose-only).

For a flagged mis-seed: search Hardcover by title+author, hand Haiku the candidates
(+ the book) with their popularity/pages/compilation signals, and get the correct
identity or NONE. Validates the chosen id against the candidate set (no invented
ids). Proposes; writes nothing. Auto-apply is a later, agreement-gated stage.
"""
import json
import os
import time

from . import anthropic, audit, epub, grimmory, hardcover, matcher
from .heal import assert_preconditions, heal_book

AUTO_CONF = 0.9          # auto-apply re-seeds at or above this confidence
ABORT_ERRORS = 3         # circuit-breaker
# Days after which a cached-unresolvable book is re-queried anyway. 0 = never
# (the default): once placed on the skip-list a book is only re-tried when its
# title/author changes (fingerprint miss) or on an explicit `resolve --force`.
RETRY_DAYS = int(os.environ.get("COLOPHON_RESOLVE_RETRY_DAYS", "0") or "0")


def _fingerprint(book):
    """The normalized title+author the resolve query is built from. Stable across
    processes (plain text, not the per-process-salted builtin hash())."""
    return matcher._norm(book.get("title", "")) + " :: " + matcher._norm(book.get("authors", ""))


def _expired(last_seen, days=RETRY_DAYS):
    if days <= 0:
        return False
    cutoff = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(time.time() - days * 86400))
    return (last_seen or "") < cutoff


def _cached_skip(book, skips, days=RETRY_DAYS):
    """Return the live skip row for this book, or None if it should be re-queried
    (no entry / title-or-author changed / entry expired)."""
    row = skips.get(book["book_id"])
    if row and row["fingerprint"] == _fingerprint(book) and not _expired(row["last_seen"], days):
        return row
    return None


def _record_skip(store, book, p):
    """After an apply-mode resolve, keep the skip-list current for this book."""
    if p.get("applied"):
        store.skip_clear(book["book_id"])          # resolved — no longer unresolvable
        return
    if p.get("apply_error") or p["action"] == "error":
        return                                      # transient failure — retry next run
    fp = _fingerprint(book)
    if p["action"] == "none":
        store.skip_put(book["book_id"], book.get("title"), fp, "none",
                       reason=p.get("reason"), conf=p.get("confidence"))
    elif p["action"] == "propose":                  # matched, but below the auto-apply gate
        store.skip_put(book["book_id"], book.get("title"), fp, "propose-below",
                       reason=p.get("reason"), conf=p.get("confidence"),
                       chosen_id=p.get("chosen_id"), chosen_title=p.get("chosen_title"),
                       isbn=p.get("isbn"))

SYSTEM = (
    "You identify the correct Hardcover book for a library entry whose current "
    "match is wrong. You are given the book's title + author and candidate Hardcover "
    "books (title, subtitle, authors, users_count=popularity, pages, release_year, "
    "compilation flag, series). Choose the candidate `id` that is the SAME WORK as "
    "the book, preferring the canonical / most-tracked edition (high users_count). "
    "Rules: a candidate with compilation=true is an omnibus/collection — pick it "
    "ONLY if the book itself is that collection, never for a single title; the "
    "author must match; if no candidate is clearly the same work, return NONE. "
    "The book's own colophon / copyright page may be included — use its printed ISBN, "
    "publisher and edition as strong evidence, but chosen_id must still be a candidate id. "
    "Never invent an id — chosen_id must be one of the given ids or \"NONE\". Be "
    "conservative: NONE is better than a wrong fix."
)

SCHEMA = {
    "type": "object",
    "properties": {
        "chosen_id": {"type": "string", "description": "a candidate id, or NONE"},
        "is_set": {"type": "boolean", "description": "is the chosen candidate an omnibus/collection"},
        "confidence": {"type": "number"},
        "reason": {"type": "string"},
    },
    "required": ["chosen_id", "confidence", "reason"],
}


def _resolve_by_isbn(bid, title, authors, isbns):
    """Deterministic: an OPF ISBN that resolves to a same-author Hardcover book needs
    no LLM. Heals to the exact edition (the ISBN-13 from the file) when valid, else the
    book's canonical ISBN. Returns a propose dict (source=epub-opf) or None."""
    for isbn in isbns:
        try:
            cand = hardcover.book_by_isbn(isbn)
        except Exception:
            continue
        if not cand:
            continue
        if authors and not matcher._title_match(authors, ", ".join(cand.get("authors") or [])):
            continue
        isbn13 = isbn if (len(isbn) == 13 and isbn.isdigit()) else cand.get("isbn")
        if not isbn13:
            continue
        return {"book_id": bid, "title": title, "action": "propose", "source": "epub-opf",
                "chosen_id": str(cand["hcid"]), "chosen_title": cand.get("title"),
                "isbn": isbn13, "slug": cand.get("slug"), "confidence": 0.97,
                "is_set": None, "reason": f"OPF ISBN {isbn} → {cand.get('title')!r}"}
    return None


def _epub_signals(book_id):
    """Read-only: the book's local EPUB → epub.inspect() dict, or None. Never raises;
    None when there is no books root, no EPUB, or no usable signal in the file."""
    try:
        path = grimmory.epub_path(book_id)
        if not path or not os.path.exists(path):
            return None
        sig = epub.inspect(path)
        return sig if (sig and (sig.get("opf_isbns") or sig.get("colophon_text"))) else None
    except Exception:
        return None


def resolve(book, file_signals=None):
    """book: {book_id, title, authors, hcid, ...}. Returns a proposal dict.

    With file_signals (from epub.inspect): a real OPF ISBN that resolves to a
    same-author Hardcover book short-circuits the LLM (source=epub-opf); otherwise the
    colophon text is folded into the adjudication prompt as extra evidence."""
    bid, title, authors = book["book_id"], book["title"], book.get("authors", "")
    if file_signals:
        sc = _resolve_by_isbn(bid, title, authors, file_signals.get("opf_isbns") or [])
        if sc:
            return sc
    cands = hardcover.search(f"{title} {authors}".strip(), per_page=8)
    catalog = [{
        "id": str(c["hcid"]), "title": c["title"], "subtitle": c.get("subtitle"),
        "authors": c["authors"], "users_count": c["users_count"], "pages": c["pages"],
        "release_year": c["release_year"], "compilation": c["compilation"],
        "series": c["series_names"],
    } for c in cands if c.get("hcid")]
    if not catalog:
        return {"book_id": bid, "title": title, "action": "none", "reason": "no search candidates"}
    valid_ids = {c["id"] for c in catalog}
    colophon = (file_signals or {}).get("colophon_text") or ""
    user = ("BOOK TO IDENTIFY:\n" + json.dumps({"title": title, "authors": authors}) +
            ("\n\nBOOK'S OWN COLOPHON / COPYRIGHT PAGE:\n" + colophon if colophon else "") +
            "\n\nCANDIDATES:\n" + json.dumps(catalog, ensure_ascii=False) +
            "\n\nReturn the correct candidate id, or NONE.")
    try:
        d = anthropic.adjudicate(SYSTEM, user, SCHEMA)
    except Exception as e:
        return {"book_id": bid, "title": title, "action": "error", "reason": str(e)[:120]}
    chosen = str(d.get("chosen_id") or "NONE")
    conf, reason = d.get("confidence"), d.get("reason", "")
    if chosen not in valid_ids:  # NONE or a hallucinated id → no proposal
        return {"book_id": bid, "title": title, "action": "none", "confidence": conf,
                "reason": reason or "Haiku: no confident match"}
    detail = None
    try:
        detail = hardcover.book_by_id(chosen)
    except Exception:
        pass
    return {
        "book_id": bid, "title": title, "action": "propose", "chosen_id": chosen,
        "chosen_title": (detail or {}).get("title") or next(c["title"] for c in catalog if c["id"] == chosen),
        "isbn": (detail or {}).get("isbn"), "slug": (detail or {}).get("slug"),
        "confidence": conf, "is_set": d.get("is_set"), "reason": reason,
        "source": "epub-colophon" if colophon else None,
    }


def run_resolve(limit=None, book_ids=None, apply=False, min_conf=AUTO_CONF, g=None,
                store=None, force=False):
    """Resolve the given books (or every audit-flagged mis-seed). With apply=True,
    heal the proposals at confidence >= min_conf via the real heal engine (set the
    resolved ISBN + id + locks → refresh); the rest stay propose-only.

    Books already on the skip-list (resolved to no-match / below-threshold on a prior
    run, title+author unchanged) are filtered out and returned under `skipped` instead
    of being re-queried. `force` ignores the skip-list; an explicit `book_ids` is always
    treated as a deliberate re-check (skip-list bypassed for those)."""
    if apply:
        assert_preconditions(g)
    explicit = bool(book_ids)
    if book_ids:
        allb = {b["book_id"]: b for b in audit.all_books()}
        books = [allb[i] for i in book_ids if i in allb]
    else:
        books = [b for b, _ in audit.run_audit(limit=limit)["categories"].get("review-misseed", [])]
    skipped = []
    if store and not explicit and not force:
        skips = store.skip_map()
        active = []
        for b in books:
            row = _cached_skip(b, skips)
            (skipped if row else active).append(row or b)
        books = active
    run_id = (store.new_run_id() + "-resolve") if (apply and store) else None
    proposals, errors = [], 0
    for i, b in enumerate(books):
        if i > 0:
            time.sleep(1.0)
        p = resolve(b)
        # Below the gate (or no match)? Inspect the actual EPUB and re-adjudicate with
        # its OPF ISBN + colophon as extra signal — the only books that pay for file I/O.
        if not p.get("source") and (p["action"] == "none" or (p.get("confidence") or 0) < min_conf):
            sig = _epub_signals(b["book_id"])
            if sig:
                p2 = resolve(b, file_signals=sig)
                if (p2.get("confidence") or 0) > (p.get("confidence") or 0):
                    p = p2
        if (apply and p["action"] == "propose"
                and (p.get("confidence") or 0) >= min_conf and p.get("isbn")):
            try:
                heal_book(g, store, run_id, b["book_id"], p["isbn"], p["chosen_id"], p["slug"], dry_run=False)
                p["applied"] = True
            except Exception as e:
                p["applied"], p["apply_error"] = False, str(e)[:100]
                errors += 1
        if apply and store:
            _record_skip(store, b, p)
        proposals.append(p)
        if p.get("apply_error") and errors >= ABORT_ERRORS:
            p["aborted"] = True
            break
    return {"count": len(proposals), "proposals": proposals, "run_id": run_id,
            "applied": sum(1 for p in proposals if p.get("applied")), "skipped": skipped}


def render(result):
    props = result["proposals"]
    proposed = [p for p in props if p["action"] == "propose"]
    none = [p for p in props if p["action"] == "none"]
    err = [p for p in props if p["action"] == "error"]
    applied = [p for p in proposed if p.get("applied")]
    skipped = result.get("skipped", [])
    L = [f"# Colophon mis-seed resolution — {time.strftime('%Y-%m-%d %H:%M')}",
         f"\n**{len(props)}** mis-seeds · **{len(applied)}** auto-applied · "
         f"**{len(proposed) - len(applied)}** proposed (below threshold) · "
         f"**{len(none)}** no-match · **{len(err)}** error · "
         f"**{len(skipped)}** cached-skipped\n",
         "## Re-seeds\n"]
    for p in sorted(proposed, key=lambda p: -(p.get("confidence") or 0)):
        tag = "✓ APPLIED" if p.get("applied") else ("apply-FAILED" if p.get("apply_error") else "proposed")
        via = f"  _via {p['source']}_" if p.get("source") else ""
        L.append(f"- `{p['book_id']}` {p['title']!r}  [{tag}]{via}")
        L.append(f"    → **{p['chosen_title']!r}**  (hcid {p['chosen_id']}, isbn {p.get('isbn')}, "
                 f"conf {p.get('confidence')}{', SET' if p.get('is_set') else ''})")
        if p.get("apply_error"):
            L.append(f"    apply error: {p['apply_error']}")
        if p.get("reason"):
            L.append(f"    _{p['reason']}_")
    if none:
        L.append("\n## No confident match — leave flagged / manual\n")
        for p in none:
            L.append(f"- `{p['book_id']}` {p['title']!r} — {p.get('reason', '')}")
    if err:
        L.append("\n## Errors\n")
        for p in err:
            L.append(f"- `{p['book_id']}` {p['title']!r} — {p.get('reason', '')}")
    if skipped:
        below = [s for s in skipped if s.get("action") == "propose-below"]
        L.append(f"\n## Cached unresolvable — not re-queried ({len(skipped)})\n")
        if below:
            L.append("Standing proposals (a match was found but below the auto-apply "
                     "gate — apply manually if correct):\n")
            for s in sorted(below, key=lambda s: -(s.get("conf") or 0)):
                L.append(f"- `{s['book_id']}` {s.get('title')!r} → **{s.get('chosen_title')!r}** "
                         f"(hcid {s.get('chosen_id')}, isbn {s.get('isbn')}, conf {s.get('conf')})")
                L.append(f"    apply: `colophon resolve --book {s['book_id']} --apply --min-conf 0`")
        n_none = len(skipped) - len(below)
        if n_none:
            L.append(f"\n{n_none} cached no-match. `colophon resolve --force` re-checks every "
                     "skipped book; `colophon resolve --clear-skips` forgets them.")
    return "\n".join(L) + "\n"
