"""Regression tests for ZIP-as-document handling and summary cleanup.

Every test here maps to a bug that shipped and had to be fixed:

* .zip files were classified as unsupported media in the conversation path
* batch image summaries leaked markdown formatting into plain-text replies

Run with: python -m unittest discover -s tests -t .
"""

import io
import os
import unittest
import zipfile

# sen.config refuses to import without credentials; the tests never talk to
# Telegram or Gemini, so placeholder values are enough.
os.environ.setdefault("BOT_TOKEN", "000000000:TESTTOKENPLACEHOLDER0000000000")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

from sen.media import (  # noqa: E402
    describe_document,
    document_mime_type,
    is_readable_document,
    is_readable_document_mime,
    prepare_document,
)
from sen.photo_batch import _is_zip, _plain_summary, _response_text  # noqa: E402


class FakeDocument:
    """The bits of a Telegram document the media helpers look at."""

    def __init__(self, file_name=None, mime_type=None):
        self.file_name = file_name
        self.mime_type = mime_type
        self.file_size = None


def make_zip(members: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, payload in members.items():
            archive.writestr(name, payload)
    return buffer.getvalue()


class ZipClassificationTest(unittest.TestCase):
    """A .zip is a document everywhere, not unsupported media."""

    def test_zip_extension_resolves(self):
        document = FakeDocument(file_name="photos.zip", mime_type="application/octet-stream")
        self.assertEqual(document_mime_type(document), "application/zip")
        self.assertTrue(is_readable_document(document))

    def test_zip_declared_mimes_normalize(self):
        for mime in ("application/zip", "application/x-zip-compressed", "multipart/x-zip"):
            with self.subTest(mime=mime):
                self.assertEqual(document_mime_type(FakeDocument("x.zip", mime)), "application/zip")
                self.assertTrue(is_readable_document_mime(mime))

    def test_zip_describes_as_archive(self):
        self.assertEqual(describe_document(FakeDocument("photos.zip", "application/zip")), 'ZIP archive "photos.zip"')

    def test_zip_matches_the_batch_filter(self):
        self.assertTrue(_is_zip(FakeDocument("photos.zip", "application/octet-stream")))
        self.assertTrue(_is_zip(FakeDocument(None, "application/x-zip-compressed")))
        self.assertFalse(_is_zip(FakeDocument("report.pdf", "application/pdf")))


class ZipListingTest(unittest.TestCase):
    """The conversation path reads a ZIP as its member listing."""

    def test_listing_names_members(self):
        data = make_zip({"a.png": b"x" * 10, "dir/b.jpg": b"y" * 20})
        text, mime, description = prepare_document(FakeDocument("dump.zip", "application/zip"), data)
        self.assertEqual(mime, "text/plain")
        self.assertIn("a.png", text.decode())
        self.assertIn("dir/b.jpg", text.decode())
        self.assertEqual(description, 'ZIP archive "dump.zip"')

    def test_corrupt_zip_falls_back_to_raw_bytes(self):
        text, mime, _ = prepare_document(FakeDocument("bad.zip", "application/zip"), b"not a zip at all")
        self.assertEqual(text, b"not a zip at all")
        self.assertEqual(mime, "application/zip")


class PlainSummaryTest(unittest.TestCase):
    """Batch summaries are 2-4 unformatted sentences, however the model answers."""

    def test_markdown_is_stripped(self):
        raw = "### Summary\n**Bold** start with `code` and __emphasis__.\n- a bullet line"
        cleaned = _plain_summary(raw)
        for noise in ("###", "**", "`", "__", "- "):
            self.assertNotIn(noise, cleaned)
        self.assertIn("Bold start with code and emphasis.", cleaned)
        self.assertIn("a bullet line", cleaned)

    def test_wrap_quotes_are_removed(self):
        self.assertEqual(_plain_summary('"A screenshot of a settings page."'), "A screenshot of a settings page.")

    def test_answers_are_capped_at_four_sentences(self):
        raw = "One. Two. Three. Four. Five. Six."
        self.assertEqual(_plain_summary(raw), "One. Two. Three. Four.")

    def test_short_answers_are_untouched(self):
        self.assertEqual(_plain_summary("A cat sits on a windowsill."), "A cat sits on a windowsill.")


class ResponseTextTest(unittest.TestCase):
    """Gemini responses come in shapes that make .text raise or return nothing."""

    def test_text_property_is_used_when_present(self):
        class Response:
            text = "  hello  "

        self.assertEqual(_response_text(Response()), "hello")

    def test_raising_text_property_falls_back_to_parts(self):
        class Part:
            text = "piece one"

        class Content:
            parts = [Part()]

        class Candidate:
            content = Content()

        class Response:
            candidates = [Candidate()]

            @property
            def text(self):
                raise ValueError("No text parts")

        self.assertEqual(_response_text(Response()), "piece one")

    def test_empty_response_returns_empty_string(self):
        class Response:
            text = None

        self.assertEqual(_response_text(Response()), "")


if __name__ == "__main__":
    unittest.main()
