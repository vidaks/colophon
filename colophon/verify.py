"""Acquisition-side verification — is a grabbed file the requested work?

Read-only, no writes, no grimmory DB. Given a requested identity (a Hardcover
work id — *primary*; or title/author — *degraded fallback*) and a downloaded book
file, decide match / mismatch / unverifiable at **work** granularity. Reuses
`epub.inspect` + `hardcover` + `matcher`; never originates an identifier.

This is the comparator the acquisition gate (plan 22) calls per grab. The gate
acts on the returned verdict (promote / hold) — never on a process exit code.

Deterministic happy path (no LLM): the file's embedded OPF ISBN resolves to a
Hardcover book; compare its work to the requested work (id, via the shared
canonical). A file with no resolvable ISBN is held as `unverifiable` — the
title/colophon LLM adjudication is a later increment, not a guess.
"""
from . import epub, hardcover, matcher

MATCH = "match"
MISMATCH = "mismatch"
UNVERIFIABLE = "unverifiable"

# Below this adjudicator confidence the file is held (unverifiable), never matched or
# rejected on a guess. LLM verdicts are also capped below the deterministic-ISBN 0.97.
_LLM_MIN = 0.8


def _result(verdict, confidence, reason, **evidence):
    out = {"verdict": verdict, "confidence": round(confidence, 2), "reason": reason}
    out.update({k: v for k, v in evidence.items() if v is not None})
    return out


def _canon(book):
    """Work-level key for a Hardcover book dict: its canonical id, else its own id."""
    return str(book.get("canonical_id") or book.get("hcid"))


def _same_work(a, b):
    """Two Hardcover book dicts are the same work when their ids or canonicals agree
    (an edition/duplicate record points at the work via `canonical_id`)."""
    return _canon(a) == _canon(b) or str(a.get("hcid")) == str(b.get("hcid"))


def _file_book_from_isbns(isbns):
    """First OPF ISBN that resolves to a Hardcover book → (isbn, book), else (None, None)."""
    for isbn in isbns:
        try:
            book = hardcover.book_by_isbn(isbn)
        except Exception:
            continue
        if book and book.get("hcid"):
            return isbn, book
    return None, None


def verify(requested, file_path):
    """requested: {"hcid": <id>} (primary) or {"title": ..., "authors": ...} (fallback).
    file_path: the downloaded book file. Returns a verdict dict
    {"verdict", "confidence", "reason", ...evidence}."""
    sig = epub.inspect(file_path)
    if not sig:
        return _result(UNVERIFIABLE, 0.0,
                       "no embedded signals (non-EPUB or unreadable file)",
                       source="no-file-signal")

    isbns = sig.get("opf_isbns") or []
    req_hcid = str(requested["hcid"]) if requested.get("hcid") else None
    req_title = requested.get("title")

    isbn, file_book = _file_book_from_isbns(isbns)

    # --- Deterministic path: the file's embedded ISBN resolves to a Hardcover work ---
    if file_book:
        file_hcid = str(file_book.get("hcid"))
        file_title = file_book.get("title")
        if req_hcid:
            try:
                req_book = hardcover.book_by_id(req_hcid)
            except Exception:
                req_book = None
            same = _same_work(file_book, req_book) if req_book else (file_hcid == req_hcid)
            verdict = MATCH if same else MISMATCH
            rel = "same work as" if same else "different work from"
            return _result(verdict, 0.97 if same else 0.9,
                           f"file ISBN {isbn} -> hc#{file_hcid} {file_title!r}; {rel} requested hc#{req_hcid}",
                           source="isbn-id", isbn=isbn, file_hcid=file_hcid, file_title=file_title)
        if req_title:
            same = matcher._title_match(file_title or "", req_title)
            verdict = MATCH if same else MISMATCH
            rel = "~ requested title" if same else "!= requested title"
            return _result(verdict, 0.80 if same else 0.75,
                           f"file ISBN {isbn} -> hc#{file_hcid} {file_title!r} {rel} {req_title!r} (no id given)",
                           source="isbn-title", isbn=isbn, file_hcid=file_hcid, file_title=file_title)
        return _result(UNVERIFIABLE, 0.0, "no requested identity supplied", source="no-request")

    # --- No resolvable embedded ISBN: identify the file via the resolver's adjudicator
    # (search Hardcover by the file's own title/author, fold in its colophon text), then
    # compare that work to the requested one. The LLM only ever selects a real candidate. ---
    opf_title, opf_author = sig.get("opf_title"), sig.get("opf_author")
    if not opf_title:
        return _result(UNVERIFIABLE, 0.0,
                       "no embedded ISBN and no title in the file to adjudicate on",
                       source="no-signal")

    from . import resolver  # lazy: keeps verify's import surface light (epub/hardcover/matcher)
    try:
        prop = resolver.resolve({"book_id": None, "title": opf_title, "authors": opf_author or ""},
                                file_signals=sig)
    except Exception as e:
        return _result(UNVERIFIABLE, 0.0, f"adjudication failed: {str(e)[:80]}", source="llm-error")

    conf = prop.get("confidence") or 0.0
    chosen = str(prop["chosen_id"]) if prop.get("chosen_id") else None
    if prop.get("action") != "propose" or not chosen or conf < _LLM_MIN:
        return _result(UNVERIFIABLE, round(conf, 2),
                       f"file not confidently identified ({prop.get('reason') or prop.get('action')})",
                       source="llm-lowconf", opf_title=opf_title, chosen_id=chosen)

    file_title = prop.get("chosen_title")
    if req_hcid:
        try:
            same = _same_work(hardcover.book_by_id(chosen), hardcover.book_by_id(req_hcid))
        except Exception:
            same = chosen == req_hcid
    elif req_title:
        same = matcher._title_match(file_title or "", req_title)
    else:
        return _result(UNVERIFIABLE, 0.0, "no requested identity supplied", source="no-request")

    verdict = MATCH if same else MISMATCH
    rel = "same work as" if same else "different work from"
    return _result(verdict, round(min(conf, 0.9), 2),
                   f"file adjudicated -> hc#{chosen} {file_title!r}; {rel} requested",
                   source="llm-adjudicated", file_hcid=chosen, file_title=file_title)
