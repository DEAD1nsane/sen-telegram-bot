"""Routing rules for the batch image path.

The rule under test: the bot summarises images ONLY when explicitly
instructed — an @tag, a reply to the bot, or a ZIP of images. A bare
photo or album in a batch chat must stay silent. This regressed once:
every bare screenshot produced a "📸 Read 1 image" reply, because the
filter matched any photo until the send path actually started working.

Run with: python -m unittest discover -s tests -t .
"""

import os
import unittest
from datetime import UTC, datetime
from unittest import mock

# sen.config refuses to import without credentials; the tests never talk to
# Telegram or Gemini, so placeholder values are enough.
os.environ.setdefault("BOT_TOKEN", "000000000:TESTTOKENPLACEHOLDER0000000000")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

from aiogram.types import Chat, Document, Message, PhotoSize, User  # noqa: E402

from sen import photo_batch  # noqa: E402

BATCH_CHAT = -1001
OTHER_CHAT = -2002

BOT_USER = User(id=777, is_bot=True, first_name="Sen", username="SenAnythangBot")
HUMAN = User(id=5, is_bot=False, first_name="Human")


def _message(**kwargs) -> Message:
    defaults = dict(
        message_id=10,
        date=datetime(2026, 10, 28, 1, 0, tzinfo=UTC),
        chat=Chat(id=BATCH_CHAT, type="group"),
        from_user=HUMAN,
    )
    defaults.update(kwargs)
    return Message(**defaults)


def photo_message(caption=None, group=None, reply=None, message_id=10, chat_id=BATCH_CHAT, from_user=HUMAN) -> Message:
    photo = PhotoSize(file_id="f1", file_unique_id="u1", width=90, height=90, file_size=123)
    return _message(
        message_id=message_id,
        chat=Chat(id=chat_id, type="group"),
        from_user=from_user,
        photo=[photo],
        caption=caption,
        media_group_id=group,
        reply_to_message=reply,
    )


def zip_message(caption=None, chat_id=BATCH_CHAT) -> Message:
    document = Document(file_id="z1", file_unique_id="zu1", file_name="dump.zip", mime_type="application/zip")
    return _message(chat=Chat(id=chat_id, type="group"), document=document, caption=caption)


class BatchRoutingTest(unittest.IsolatedAsyncioTestCase):
    """Filter-level rule: tag/reply/ZIP summarise, bare images stay silent."""

    def setUp(self):
        self._patches = [
            mock.patch.object(photo_batch._cfg, "PHOTO_BATCH_CHAT_IDS", {BATCH_CHAT}),
            mock.patch.object(photo_batch._cfg, "BOT_INFO", BOT_USER),
        ]
        for patch in self._patches:
            patch.start()
        self.filter = photo_batch.BatchPhotoFilter()

    def tearDown(self):
        for patch in reversed(self._patches):
            patch.stop()
        photo_batch._batch_albums.clear()
        photo_batch._pending_albums.clear()

    # -- what must stay silent -------------------------------------------

    async def test_bare_single_photo_is_ignored(self):
        """The reported bug: any bare screenshot used to be summarised."""
        self.assertFalse(await self.filter(photo_message()))

    async def test_bare_photo_with_caption_is_ignored(self):
        self.assertFalse(await self.filter(photo_message(caption="look at this")))

    async def test_bare_album_is_ignored(self):
        for part in (1, 2, 3):
            with self.subTest(part=part):
                self.assertFalse(await self.filter(photo_message(group="g1", message_id=part)))

    async def test_other_chats_never_batch(self):
        self.assertFalse(await self.filter(photo_message(caption="@SenAnythangBot", chat_id=OTHER_CHAT)))
        self.assertFalse(await self.filter(zip_message(chat_id=OTHER_CHAT)))

    async def test_bot_messages_never_match(self):
        self.assertFalse(await self.filter(photo_message(caption="@SenAnythangBot", from_user=BOT_USER)))

    async def test_plain_documents_are_not_batched(self):
        document = Document(file_id="p1", file_unique_id="pu1", file_name="report.pdf", mime_type="application/pdf")
        self.assertFalse(await self.filter(_message(document=document)))

    # -- what must be summarised ----------------------------------------

    async def test_tagged_photo_is_summarised(self):
        self.assertTrue(await self.filter(photo_message(caption="summarize this @SenAnythangBot")))

    async def test_gemini_alias_is_summarised(self):
        self.assertTrue(await self.filter(photo_message(caption="@gemini what am I looking at")))

    async def test_reply_to_bot_photo_is_summarised(self):
        replied = _message(message_id=9, from_user=BOT_USER)
        self.assertTrue(await self.filter(photo_message(reply=replied)))

    async def test_tagged_album_follows_even_without_caption_on_later_parts(self):
        # Telegram stamps the caption (and the reply) on the first message only.
        first = photo_message(group="g1", caption="@SenAnythangBot", message_id=1)
        later = photo_message(group="g1", message_id=2)
        self.assertTrue(await self.filter(first))
        self.assertTrue(await self.filter(later))

    async def test_replied_album_follows_even_without_reply_on_later_parts(self):
        replied = _message(message_id=9, from_user=BOT_USER)
        first = photo_message(group="g2", reply=replied, message_id=1)
        later = photo_message(group="g2", message_id=2)
        self.assertTrue(await self.filter(first))
        self.assertTrue(await self.filter(later))

    async def test_bare_zip_is_summarised(self):
        self.assertTrue(await self.filter(zip_message()))

    async def test_tagged_zip_is_summarised(self):
        self.assertTrue(await self.filter(zip_message(caption="dump from yesterday @SenAnythangBot")))


if __name__ == "__main__":
    unittest.main()
