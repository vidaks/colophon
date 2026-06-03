"""Stage 2 — Haiku resolver for mis-seeds (propose-only).

For a flagged mis-seed: search Hardcover by title+author, hand Haiku the candidates
(+ the book) with their popularity/pages/compilation signals, and get the correct
identity or NONE. Validates the chosen id against the candidate set (no invented
ids). Proposes; writes nothing. Auto-apply is a later, agreement-gated stage.
"""
import json
import time

from . import anthropic, audit, hardcover
from .heal import assert_preconditions, heal_book

AUTO_CONF = 0.9          # auto-apply re-seeds at or above this confidence
ABORT_ERRORS = 3         # circuit-breaker

SYSTEM = (
    "You identify the correct Hardcover book for a library entry whose current "
    "match is wrong. You are given the book's title + author and candidate Hardcover "
    "books (title, subtitle, authors, users_count=popularity, pages, release_year, "
    "compilation flag, series). Choose the candidate `id` that is the SAME WORK as "
    "the book, preferring the canonical / most-tracked edition (high users_count). "
    "Rules: a candidate with compilation=true is an omnibus/collection — pick it "
    "ONLY if the book itself is that collection, never for a single title; the "
    "author must match; if no candidate is clearly the same work, return NONE. "
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


def resolve(book):
    """book: {book_id, title, authors, hcid, ...}. Returns a proposal dict."""
    bid, title, authors = book["book_id"], book["title"], book.get("authors", "")
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
    user = ("BOOK TO IDENTIFY:\n" + json.dumps({"title": title, "authors": authors}) +
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
    }


def run_resolve(limit=None, book_ids=None, apply=False, min_conf=AUTO_CONF, g=None, store=None):
    """Resolve the given books (or every audit-flagged mis-seed). With apply=True,
    heal the proposals at confidence >= min_conf via the real heal engine (set the
    resolved ISBN + id + locks → refresh); the rest stay propose-only."""
    if apply:
        assert_preconditions(g)
    if book_ids:
        allb = {b["book_id"]: b for b in audit.all_books()}
        books = [allb[i] for i in book_ids if i in allb]
    else:
        books = [b for b, _ in audit.run_audit(limit=limit)["categories"].get("review-misseed", [])]
    run_id = (store.new_run_id() + "-resolve") if (apply and store) else None
    proposals, errors = [], 0
    for b in books:
        p = resolve(b)
        if (apply and p["action"] == "propose"
                and (p.get("confidence") or 0) >= min_conf and p.get("isbn")):
            try:
                heal_book(g, store, run_id, b["book_id"], p["isbn"], p["chosen_id"], p["slug"], dry_run=False)
                p["applied"] = True
            except Exception as e:
                p["applied"], p["apply_error"] = False, str(e)[:100]
                errors += 1
                if errors >= ABORT_ERRORS:
                    p["aborted"] = True
                    proposals.append(p)
                    break
        proposals.append(p)
    return {"count": len(proposals), "proposals": proposals, "run_id": run_id,
            "applied": sum(1 for p in proposals if p.get("applied"))}


def render(result):
    props = result["proposals"]
    proposed = [p for p in props if p["action"] == "propose"]
    none = [p for p in props if p["action"] == "none"]
    err = [p for p in props if p["action"] == "error"]
    applied = [p for p in proposed if p.get("applied")]
    L = [f"# Colophon mis-seed resolution — {time.strftime('%Y-%m-%d %H:%M')}",
         f"\n**{len(props)}** mis-seeds · **{len(applied)}** auto-applied · "
         f"**{len(proposed) - len(applied)}** proposed (below threshold) · "
         f"**{len(none)}** no-match · **{len(err)}** error\n",
         "## Re-seeds\n"]
    for p in sorted(proposed, key=lambda p: -(p.get("confidence") or 0)):
        tag = "✓ APPLIED" if p.get("applied") else ("apply-FAILED" if p.get("apply_error") else "proposed")
        L.append(f"- `{p['book_id']}` {p['title']!r}  [{tag}]")
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
    return "\n".join(L) + "\n"
