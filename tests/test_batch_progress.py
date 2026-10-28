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
    Rich sends/edits record the same way, keyed on ``rich_message.html``.
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

    async def send_rich_message(self, chat_id, rich_message, **kwargs):
        self.sent.append(rich_message.html)
        self._next_id += 1
        return _SentMessage(self._next_id)

    async def edit_message_text(self, text=None, chat_id=None, message_id=None, rich_message=None, **kwargs):
        if self.edit_exception is not None:
            raise self.edit_exception
        self.edits.append(rich_message.html if rich_message is not None else text)


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
        self.assertIn("📸 Read 3 images", bot.edits[-1])
        self.assertIn("<b>img_1.jpg</b>", bot.edits[-1])

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
        class FlakyBot(FakeBot):
            """Both the rich and the plain status-send attempts die."""

            def __init__(self):
                super().__init__()
                self.rich_fails = 1
                self.plain_fails = 1

            async def send_rich_message(self, chat_id, rich_message, **kwargs):
                if self.rich_fails > 0:
                    self.rich_fails -= 1
                    raise RuntimeError("status send failed")
                return await super().send_rich_message(chat_id, rich_message, **kwargs)

            async def send_message(self, chat_id, text, **kwargs):
                if self.plain_fails > 0:
                    self.plain_fails -= 1
                    raise RuntimeError("status send failed")
                return await super().send_message(chat_id, text, **kwargs)

        bot = FlakyBot()
        with (
            mock.patch.object(photo_batch, "_summarize", _fake_summarize),
            mock.patch.object(photo_batch, "ANALYSIS_GAP_SECONDS", 0),
            mock.patch.object(photo_batch, "_sender", FakeSender(bot)),
        ):
            await photo_batch._process_batch(bot, -1001, _images(1), 42)
        self.assertTrue(any("📸 Read 1 image" in text for text in bot.sent))

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
        self.assertIn("📸 Read 5 images", bot.edits[-1])

    async def test_large_batch_lands_in_one_rich_message(self):
        """39 images ≈ 20k chars: one message, not the six plain chunks."""
        bot = FakeBot()
        images = [{"label": f"IMG_{i:03}.jpg", "data": b"x", "mime": "image/jpeg", "caption": ""} for i in range(39)]

        async def long_summarize(data, mime, caption, label) -> str:
            return f"Summary for {label}. " + "A" * 460

        with (
            mock.patch.object(photo_batch, "_summarize", long_summarize),
            mock.patch.object(photo_batch, "ANALYSIS_GAP_SECONDS", 0),
            mock.patch.object(photo_batch, "PROGRESS_EDIT_GAP", 0),
            mock.patch.object(photo_batch, "_sender", FakeSender(bot)),
        ):
            await photo_batch._process_batch(bot, -1001, images, 42)

        # Only the status message was ever *sent*; the whole summary is its
        # single final edit. Any overflow would show up as extra sends.
        self.assertEqual(len(bot.sent), 1, bot.sent)
        final = bot.edits[-1]
        self.assertIn("<b>IMG_000.jpg</b>", final)
        self.assertIn("<b>IMG_038.jpg</b>", final)  # all 39 in one edit
        self.assertLessEqual(len(final.encode("utf-8")), photo_batch.RICH_CHUNK_BYTES)

    async def test_overflow_groups_split_on_block_boundaries(self):
        bot = FakeBot()
        with (
            mock.patch.object(photo_batch, "_summarize", _fake_summarize),
            mock.patch.object(photo_batch, "ANALYSIS_GAP_SECONDS", 0),
            mock.patch.object(photo_batch, "PROGRESS_EDIT_GAP", 0),
            mock.patch.object(photo_batch, "RICH_CHUNK_BYTES", 300),
            mock.patch.object(photo_batch, "_sender", FakeSender(bot)),
        ):
            await photo_batch._process_batch(bot, -1001, _images(16), 42)

        groups = bot.sent[1:]  # after the status message
        self.assertGreaterEqual(len(groups), 2, bot.sent)
        for group in groups:
            self.assertEqual(group.count("<b>"), group.count("</b>"), group)
            self.assertLessEqual(len(group.encode("utf-8")), photo_batch.RICH_CHUNK_BYTES)

    async def test_rich_failure_falls_back_to_plain(self):
        class PlainOnlyBot(FakeBot):
            async def send_rich_message(self, chat_id, rich_message, **kwargs):
                raise RuntimeError("rich messages unavailable")

        bot = PlainOnlyBot()
        await self._run(bot, 2)
        # Plain status sent, plain summary flip — same behaviour as before.
        self.assertEqual(bot.sent[0], "📸 Found 2 images - summarizing 0/2...")
        self.assertIn("📸 Read 2 images", bot.edits[-1])


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
