"""EPUB inspection — pure / stdlib only, no network. Run: python -m unittest -v"""
import os
import tempfile
import unittest
import zipfile

from colophon import epub

CONTAINER = ('<?xml version="1.0"?><container version="1.0" '
             'xmlns="urn:oasis:names:tc:opendocument:xmlns:container"><rootfiles>'
             '<rootfile full-path="OEBPS/content.opf" '
             'media-type="application/oebps-package+xml"/></rootfiles></container>')

COPYRIGHT = ("<html><body><h1>Copyright</h1><p>LOCK IN. Copyright © 2014 by John Scalzi. "
             "All rights reserved. ISBN 978-0-7653-7586-5. Published by Tom Doherty "
             "Associates, LLC. First Edition: August 2014.</p></body></html>")
CHAPTER = "<html><body><p>It was a bright cold morning and the clock struck thirteen.</p></body></html>"
COVER = "<html><body><p>Cover</p></body></html>"


def _opf(identifiers, with_copyright=True):
    ids = "".join(identifiers)
    items = ['<item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>',
             '<item id="ch1" href="chap1.xhtml" media-type="application/xhtml+xml"/>']
    spine = ['<itemref idref="cover"/>', '<itemref idref="ch1"/>']
    if with_copyright:
        items.insert(1, '<item id="copy" href="copyright.xhtml" media-type="application/xhtml+xml"/>')
        spine.insert(1, '<itemref idref="copy"/>')
    return ('<?xml version="1.0"?><package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
            'unique-identifier="bookid"><metadata '
            'xmlns:dc="http://purl.org/dc/elements/1.1/">' + ids +
            '<dc:title>Lock In</dc:title><dc:creator>John Scalzi</dc:creator>'
            '<dc:publisher>Tor</dc:publisher><dc:date>2014-08-26T00:00:00+00:00</dc:date>'
            '</metadata><manifest>' + "".join(items) + '</manifest><spine>' +
            "".join(spine) + '</spine></package>')


def _write_epub(path, opf, with_copyright=True):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("mimetype", "application/epub+zip")
        z.writestr("META-INF/container.xml", CONTAINER)
        z.writestr("OEBPS/content.opf", opf)
        z.writestr("OEBPS/cover.xhtml", COVER)
        z.writestr("OEBPS/chap1.xhtml", CHAPTER)
        if with_copyright:
            z.writestr("OEBPS/copyright.xhtml", COPYRIGHT)


class Inspect(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()

    def _epub(self, identifiers, with_copyright=True, name="b.epub"):
        p = os.path.join(self.dir, name)
        _write_epub(p, _opf(identifiers, with_copyright), with_copyright)
        return p

    def test_opf_isbns_normalized_and_ordered(self):
        r = epub.inspect(self._epub([
            '<dc:identifier id="bookid">urn:isbn:9781466849358</dc:identifier>',
            '<dc:identifier>978-0-7653-7586-5</dc:identifier>']))
        self.assertEqual(r["opf_isbns"], ["9781466849358", "9780765375865"])

    def test_uuid_identifier_is_not_an_isbn(self):
        r = epub.inspect(self._epub([
            '<dc:identifier>urn:uuid:17ecd1a7-81f1-4703-adb6-0b02375770c4</dc:identifier>']))
        self.assertEqual(r["opf_isbns"], [])

    def test_isbn10_taken_only_when_marked_or_valid(self):
        # marked ISBN-10 (valid checksum 0765375863) is kept
        r = epub.inspect(self._epub(['<dc:identifier>ISBN 0-7653-7586-3</dc:identifier>']))
        self.assertIn("0765375863", r["opf_isbns"])

    def test_colophon_text_found(self):
        r = epub.inspect(self._epub(['<dc:identifier>urn:isbn:9781466849358</dc:identifier>']))
        self.assertIn("All rights reserved", r["colophon_text"])
        self.assertIn("Copyright", r["colophon_text"])

    def test_metadata_fields(self):
        r = epub.inspect(self._epub(['<dc:identifier>urn:isbn:9781466849358</dc:identifier>']))
        self.assertEqual(r["opf_title"], "Lock In")
        self.assertEqual(r["publisher"], "Tor")
        self.assertEqual(r["year"], "2014")

    def test_no_colophon_page_yields_empty_text(self):
        r = epub.inspect(self._epub(['<dc:identifier>urn:isbn:9781466849358</dc:identifier>'],
                                     with_copyright=False))
        self.assertEqual(r["colophon_text"], "")

    def test_malformed_file_returns_none(self):
        p = os.path.join(self.dir, "bad.epub")
        with open(p, "w") as f:
            f.write("not a zip")
        self.assertIsNone(epub.inspect(p))


if __name__ == "__main__":
    unittest.main()
