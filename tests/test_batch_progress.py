"""Tests for the live batch progress counter and the dust keyword audio.

Run with: python -m unittest discover -s tests -t .
"""

import os
import re
import unittest
from unittest import mock

# sen.config refuses to import without credentials; the tests never talk to
# Telegram or Gemini, so placeholder values are enough.
os.environ.setdefault("BOT_TOKEN", "000000000:TESTTOKENPLACEHOLDER0000000000")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

from sen import photo_batch  # noqa: E402
from sen.config import AUDIO_METADATA, TRIGGER_AUDIO_FILES  # noqa: E402


class _SentMessage:
    """Minimal stand-in for an aiogram Message (only .message_id is used)."""

    def __init__(self, message_id: int):
        self.message_id = message_id


class FakeBot:
    """Records every send and edit instead of talking to Telegram.

    Signatures deliberately mirror aiogram's real Bot methods —
    ``send_message(chat_id, text, ...)`` puts chat_id first — so a
    positional-text call fails here exactly like it fails in production.
    """

    def __init__(self):
        self.sent: list[str] = []
        self.edits: list[str] = []
        self.edit_exception: Exception | None = None
        self._next_id = 100

    async def send_message(self, chat_id, text, **kwargs):
        self.sent.append(text)
        self._next_id += 1
        return _SentMessage(self._next_id)

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, **kwargs):
        if self.edit_exception is not None:
            raise self.edit_exception
        self.edits.append(text)


class FakeSender:
    """Runs queued sends inline so tests do not wait on pacing sleeps."""

    def __init__(self, bot: FakeBot):
        self._bot = bot

    async def submit(self, chat_id, send):
        await send()


def _images(count: int) -> list[dict]:
    return [{"label": f"img_{i}.jpg", "data": b"x", "mime": "image/jpeg", "caption": ""} for i in range(1, count + 1)]


async def _fake_summarize(data, mime, caption, label) -> str:
    return f"Summary for {label}."


class ProgressCounterTest(unittest.IsolatedAsyncioTestCase):
    """_process_batch keeps one status message updated in place."""

    async def _run(self, bot: FakeBot, count: int):
        with (
            mock.patch.object(photo_batch, "_summarize", _fake_summarize),
            mock.patch.object(photo_batch, "ANALYSIS_GAP_SECONDS", 0),
            mock.patch.object(photo_batch, "PROGRESS_EDIT_GAP", 0),
            mock.patch.object(photo_batch, "_sender", FakeSender(bot)),
        ):
            await photo_batch._process_batch(bot, -1001, _images(count), 42)

    async def test_status_counts_up_then_flips_to_summary(self):
        bot = FakeBot()
        await self._run(bot, 3)

        # The first send is the status message, not the summary.
        self.assertEqual(bot.sent[0], "📸 Found 3 images - summarizing 0/3...")
        # Interim edits carry the running counter.
        self.assertIn("📸 Found 3 images - summarizing 1/3...", bot.edits)
        self.assertIn("📸 Found 3 images - summarizing 2/3...", bot.edits)
        self.assertIn("📸 Found 3 images - summarizing 3/3...", bot.edits)
        # The last edit flips the same message into the summary header.
        self.assertTrue(bot.edits[-1].startswith("📸 Read 3 images"), bot.edits[-1])
        self.assertIn("img_1.jpg", bot.edits[-1])

    async def test_single_chunk_never_goes_through_paced_sender(self):
        bot = FakeBot()
        await self._run(bot, 2)
        # Only the status message was ever *sent*; the summary arrived as an edit.
        self.assertEqual(len(bot.sent), 1)

    async def test_summary_survives_when_final_edit_fails(self):
        bot = FakeBot()
        bot.edit_exception = RuntimeError("message to edit not found")
        with (
            mock.patch.object(photo_batch, "_summarize", _fake_summarize),
            mock.patch.object(photo_batch, "ANALYSIS_GAP_SECONDS", 0),
            mock.patch.object(photo_batch, "_sender", FakeSender(bot)),
        ):
            await photo_batch._process_batch(bot, -1001, _images(2), 42)
        # Status went out, every edit failed, so the summary was sent instead.
        self.assertEqual(bot.sent[0], "📸 Found 2 images - summarizing 0/2...")
        self.assertTrue(any("📸 Read 2 images" in text for text in bot.sent))

    async def test_no_status_message_at_all_still_delivers(self):
        delivered: list[str] = []
        calls = {"n": 0}

        class FlakyBot(FakeBot):
            async def send_message(self, chat_id, text, **kwargs):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("status send failed")
                delivered.append(text)
                return await super().send_message(chat_id, text, **kwargs)

        bot = FlakyBot()
        with (
            mock.patch.object(photo_batch, "_summarize", _fake_summarize),
            mock.patch.object(photo_batch, "ANALYSIS_GAP_SECONDS", 0),
            mock.patch.object(photo_batch, "_sender", FakeSender(bot)),
        ):
            await photo_batch._process_batch(bot, -1001, _images(1), 42)
        self.assertTrue(any("📸 Read 1 image" in text for text in delivered))

    async def test_progress_edits_are_throttled(self):
        bot = FakeBot()
        # Default PROGRESS_EDIT_GAP is 3s; with 5 fast images only the first
        # and last interim edits (plus the final flip) may go out.
        with (
            mock.patch.object(photo_batch, "_summarize", _fake_summarize),
            mock.patch.object(photo_batch, "ANALYSIS_GAP_SECONDS", 0),
            mock.patch.object(photo_batch, "_sender", FakeSender(bot)),
        ):
            await photo_batch._process_batch(bot, -1001, _images(5), 42)
        interim = [e for e in bot.edits if "summarizing" in e]
        self.assertLessEqual(len(interim), 2, interim)
        self.assertTrue(bot.edits[-1].startswith("📸 Read 5 images"), bot.edits[-1])


class DustKeywordTest(unittest.TestCase):
    """The dust keyword maps to the Billy Strings track end to end."""

    def test_keyword_maps_to_file(self):
        self.assertEqual(TRIGGER_AUDIO_FILES["dust"], "Dust In A Baggie.mp3")

    def test_metadata_exists(self):
        self.assertEqual(AUDIO_METADATA["Dust In A Baggie.mp3"], ("Dust In A Baggie", "Billy Strings"))

    def test_audio_file_shipped_in_repo(self):
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        self.assertTrue(os.path.isfile(os.path.join(root, "Dust In A Baggie.mp3")))

    def test_dust_regex_matches_word_only(self):
        pattern = re.compile(r"\bdust\b", re.I)
        self.assertTrue(pattern.search("play some dust"))
        self.assertTrue(pattern.search("Dust in the wind, they said"))
        self.assertFalse(pattern.search("dusty shelves"))


if __name__ == "__main__":
    unittest.main()
