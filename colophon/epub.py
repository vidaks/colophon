"""EPUB inspection — read-only, standard library only.

Pulls the two identity signals colophon falls back on when title/author resolution
is weak: the OPF `dc:identifier` ISBN(s) — deterministic, free — and the book's
colophon / copyright page text, for the Haiku adjudicator (the printed *print* ISBN
there often matches Hardcover's canonical edition better than the OPF ebook ISBN).
Never raises for a malformed/DRM'd file: returns None.
"""
import os
import re
import zipfile

# Keywords that mark a colophon / copyright page (front- or back-matter).
_COLO = re.compile(r'(?i)\b(isbn|all rights reserved|first published|copyright|©|'
                   r'published by|a cip|library of congress|e-?book|edition)\b')
_MAX_COLOPHON = 4000          # ~300 tokens — the marginal cost measured for the resolve call
_FRONT_BACK = 6              # how many leading/trailing spine items count as matter


def _strip(html):
    html = re.sub(r'(?is)<(script|style)[^>]*>.*?</\1>', ' ', html)
    return re.sub(r'\s+', ' ', re.sub(r'<[^>]+>', ' ', html)).strip()


def _isbn10_ok(s):
    if len(s) != 10:
        return False
    try:
        total = sum((10 - i) * (10 if c in "Xx" else int(c)) for i, c in enumerate(s))
    except ValueError:
        return False
    return total % 11 == 0


def _isbns(opf):
    """Normalized ISBN-13/10 from every `dc:identifier`, in document order, deduped.
    A 10-digit value is only taken when the element says ISBN or it checksums — a bare
    digit run (e.g. a UUID) is not an ISBN."""
    out = []
    for raw in re.findall(r'(?is)<dc:identifier[^>]*>(.*?)</dc:identifier>', opf):
        txt = _strip(raw)
        d = re.sub(r'[^0-9Xx]', '', txt)
        if len(d) == 13 and d.isdigit() and d[:3] in ("978", "979"):
            out.append(d)
        elif len(d) == 10 and ("isbn" in txt.lower() or _isbn10_ok(d)):
            out.append(d.upper())
    return list(dict.fromkeys(out))


def _dc(opf, tag):
    m = re.search(rf'(?is)<dc:{tag}[^>]*>(.*?)</dc:{tag}>', opf)
    return _strip(m.group(1)) if m else None


def inspect(path):
    """Return {opf_isbns, colophon_text, opf_title, opf_author, publisher, year} or None.

    `colophon_text` is "" when no page looks like a colophon (e.g. indie EPUBs with
    only a cover) — the caller should treat empty as "no usable colophon signal"."""
    try:
        z = zipfile.ZipFile(path)
        container = z.read('META-INF/container.xml').decode('utf-8', 'replace')
        opf_path = re.search(r'full-path="([^"]+)"', container).group(1)
        opf = z.read(opf_path).decode('utf-8', 'replace')
    except Exception:
        return None

    opf_dir = os.path.dirname(opf_path)
    manifest = {}
    for item in re.findall(r'(?is)<item\b[^>]*>', opf):
        mid, href = re.search(r'id="([^"]+)"', item), re.search(r'href="([^"]+)"', item)
        if mid and href:
            manifest[mid.group(1)] = href.group(1)
    spine = re.findall(r'(?is)<itemref\b[^>]*idref="([^"]+)"', opf)

    pages = []
    for idref in spine:
        href = manifest.get(idref)
        if not href:
            continue
        zp = href.split('#')[0]
        zp = (opf_dir + '/' + zp).lstrip('/') if opf_dir else zp
        try:
            pages.append(_strip(z.read(zp).decode('utf-8', 'replace')))
        except KeyError:
            continue

    best_text, best_score, best_hits = "", -1, 0
    n = len(pages)
    for i, t in enumerate(pages):
        if not t:
            continue
        hits = len(_COLO.findall(t[:3000]))
        score = hits + (2 if (i < _FRONT_BACK or i >= n - _FRONT_BACK) else 0) + (1 if len(t) < 4000 else 0)
        if score > best_score:
            best_text, best_score, best_hits = t, score, hits
    return {
        "opf_isbns": _isbns(opf),
        "colophon_text": best_text[:_MAX_COLOPHON] if best_hits else "",
        "opf_title": _dc(opf, "title"),
        "opf_author": _dc(opf, "creator"),
        "publisher": _dc(opf, "publisher"),
        "year": (_dc(opf, "date") or "")[:4] or None,
    }
