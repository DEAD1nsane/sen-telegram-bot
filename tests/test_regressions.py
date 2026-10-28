"""Regression tests for the instruction-following fixes.

Every test here maps to a bug that shipped and had to be fixed:

* format requests ignored because persona memories outranked them
* "spam a user" faked by repeating a tag inside one message
* "provide sources" answered with sources for the wrong topic
* rich replies showed RAW HTML (<thead>, <span>, <font>) inside rendered
  tables because sanitize_rich_html let through tags Rich HTML does not
  support - clients display unsupported tags as literal text

Run with: python -m unittest discover -s tests -t .
"""

import os
import re
import unittest

# sen.config refuses to import without credentials; the tests never talk to
# Telegram or Gemini, so placeholder values are enough.
os.environ.setdefault("BOT_TOKEN", "000000000:TESTTOKENPLACEHOLDER0000000000")
os.environ.setdefault("GEMINI_API_KEY", "test-key")

from sen.handlers import (  # noqa: E402
    _MAX_SEND_PARTS,
    _RICH_ALLOWED_TAGS,
    bare_sources_request,
    base_instructions,
    clean_ai_output,
    sanitize_rich_html,
    send_ai_response,
    strip_send_markers,
)
from sen.search import fetch_last_search, remember_last_search  # noqa: E402


class FakeBot:
    """Records what the bot tried to send, without touching the network."""

    def __init__(self):
        self.rich: list[str] = []
        self.plain: list[str] = []

    async def send_rich_message(self, rich_message=None, **kwargs):
        self.rich.append(rich_message.html)
        return "rich"

    async def send_message(self, text=None, link_preview_options=None, **kwargs):
        self.plain.append(text)
        return "plain"


class FakeRedis:
    def __init__(self):
        self.store: dict[str, bytes] = {}

    async def set(self, key, value, ex=None):
        self.store[key] = value.encode() if isinstance(value, str) else value

    async def get(self, key):
        return self.store.get(key)


class BareSourcesRequestTest(unittest.TestCase):
    """A "sources" demand whose topic lives in the conversation, not the message."""

    def test_bare_demands_reuse_the_last_search(self):
        for prompt in (
            "provide sources",
            "Quit being a cunt and provide sources",
            "Provide sources for the motel rates there",
            "give me the actual sources lol",
        ):
            with self.subTest(prompt=prompt):
                self.assertTrue(bare_sources_request(prompt))

    def test_real_topics_still_search_fresh(self):
        for prompt in (
            "cite sources about the 2024 election",
            "give me sources on bitcoin prices",
            "provide sources for catalonia",
            "what is the capital of Peru",
        ):
            with self.subTest(prompt=prompt):
                self.assertFalse(bare_sources_request(prompt))


class InstructionContentTest(unittest.TestCase):
    """The prompt itself carries the fixes; assert the wording survives edits."""

    def setUp(self):
        self.instructions = base_instructions("Wednesday, October 21, 2026")

    def test_format_requests_outrank_persona_directives(self):
        self.assertIn("Formatting requests in the USER'S INSTRUCTION are commands", self.instructions)
        self.assertIn("never whether the requested structure appears", self.instructions)

    def test_collapsibles_are_sanctioned(self):
        self.assertIn("collapsible, expandable, or folded", self.instructions)

    def test_sources_must_match_the_topic(self):
        self.assertIn("Source links are for the topic actually discussed", self.instructions)

    def test_multi_message_marker_is_documented(self):
        self.assertIn("[SEND_NEW_MESSAGE]", self.instructions)
        self.assertIn("up to 10 parts", self.instructions)

    def test_bot_is_honest_about_its_reach(self):
        self.assertIn("you have no way to DM another user", self.instructions)


class SendMarkerTest(unittest.TestCase):
    def test_markers_are_stripped(self):
        self.assertNotIn("SEND_NEW_MESSAGE", strip_send_markers("a [SEND_NEW_MESSAGE] b"))
        self.assertIn("a", strip_send_markers("a [SEND_NEW_MESSAGE] b"))

    def test_marker_variants_are_stripped(self):
        for marker in ("[SEND_NEW_MESSAGE]", "[send new message]", "[Send_New_Message]"):
            with self.subTest(marker=marker):
                self.assertNotIn("SEND", strip_send_markers(f"x {marker} y").upper())


class MultiMessageSendTest(unittest.IsolatedAsyncioTestCase):
    async def test_marker_splits_into_real_messages(self):
        bot = FakeBot()
        await send_ai_response(
            bot,
            chat_id=1,
            msg_id=2,
            response_text="one\n[SEND_NEW_MESSAGE]\ntwo\n[SEND_NEW_MESSAGE]\nthree",
            is_private=True,
        )
        self.assertEqual(len(bot.rich), 3)
        for html in bot.rich:
            self.assertNotIn("SEND_NEW_MESSAGE", html)

    async def test_single_message_is_untouched(self):
        bot = FakeBot()
        await send_ai_response(bot, chat_id=1, msg_id=2, response_text="just one", is_private=True)
        self.assertEqual(len(bot.rich), 1)
        self.assertEqual(len(bot.plain), 0)

    async def test_part_count_is_capped(self):
        bot = FakeBot()
        text = "\n[SEND_NEW_MESSAGE]\n".join(f"part {i}" for i in range(15))
        await send_ai_response(bot, chat_id=1, msg_id=2, response_text=text, is_private=True)
        self.assertEqual(len(bot.rich), _MAX_SEND_PARTS)


class LastSearchMemoryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        import sen.search

        self.real_redis = sen.search.redis_client
        self.fake = FakeRedis()
        sen.search.redis_client = self.fake

    async def asyncTearDown(self):
        import sen.search

        sen.search.redis_client = self.real_redis

    async def test_round_trip_per_chat_and_user(self):
        await remember_last_search("-100", "42", "hotel rates", "Title: Rates\nURL: https://x")
        self.assertEqual(
            await fetch_last_search("-100", "42"),
            ("hotel rates", "Title: Rates\nURL: https://x"),
        )
        self.assertEqual(await fetch_last_search("-100", "99"), ("", ""))
        self.assertEqual(await fetch_last_search("-1", "42"), ("", ""))

    async def test_empty_results_are_not_remembered(self):
        await remember_last_search("-100", "42", "hotel rates", "")
        self.assertEqual(await fetch_last_search("-100", "42"), ("", ""))


class RichHtmlSanitizerTest(unittest.TestCase):
    """Rich clients display unsupported tags literally; nothing may leak."""

    TAG_RE = re.compile(r"</?([a-zA-Z][\w-]*)")

    def _assert_only_supported_tags(self, text: str) -> None:
        for name in self.TAG_RE.findall(text):
            self.assertIn(name.lower(), _RICH_ALLOWED_TAGS, f"unsupported tag leaked: {name} in {text!r}")

    def test_unsupported_table_scaffolding_is_unwrapped(self):
        out = sanitize_rich_html(
            "<table><thead><tr><th>Col</th></tr></thead><tbody><tr><td>Val</td></tr></tbody></table>"
        )
        self._assert_only_supported_tags(out)
        self.assertIn("<th>Col</th>", out)
        self.assertIn("<td>Val</td>", out)
        self.assertNotIn("thead", out)
        self.assertNotIn("tbody", out)

    def test_span_and_font_keep_their_text(self):
        out = sanitize_rich_html('<span>plain span</span> <font color="red">red text</font>')
        self._assert_only_supported_tags(out)
        self.assertIn("plain span", out)
        self.assertIn("red text", out)

    def test_classic_spoiler_span_becomes_tg_spoiler(self):
        out = sanitize_rich_html('<span class="tg-spoiler">hidden</span>')
        self.assertEqual(out, "<tg-spoiler>hidden</tg-spoiler>")

    def test_line_break_tag_survives(self):
        # Rich HTML collapses literal newlines; the old br-to-newline
        # conversion welded the model's lines together.
        out = sanitize_rich_html("line one<br/>line two")
        self.assertRegex(out, r"<br\s*/?>")
        self.assertIn("line one", out)
        self.assertIn("line two", out)

    def test_script_and_style_are_dropped_with_content(self):
        out = sanitize_rich_html("before<script>alert('x')</script><style>b{}</style>after")
        self.assertEqual(out, "beforeafter")

    def test_realistic_rich_demo_ships_only_supported_tags(self):
        demo = (
            "<h3>Rich demo</h3>"
            "<table bordered striped><caption>Stats</caption>"
            "<thead><tr><th>Name</th><th>Score</th></tr></thead>"
            '<tbody><tr><td colspan="2" align="center"><font color="green">Winner</font> '
            '<span class="tg-spoiler">secret</span></td></tr></tbody></table>'
            "<p>Break<br>next line</p>"
        )
        out = sanitize_rich_html(demo)
        self._assert_only_supported_tags(out)
        self.assertIn("<caption>Stats</caption>", out)
        self.assertIn('<td colspan="2" align="center">', out)
        self.assertIn("<tg-spoiler>secret</tg-spoiler>", out)
        self.assertIn("Winner", out)
        self.assertRegex(out, r"<br\s*/?>")

    def test_markdown_spoiler_uses_rich_tag_at_source(self):
        out = clean_ai_output("mark this ||hidden|| as spoiler")
        self.assertIn("<tg-spoiler>hidden</tg-spoiler>", out)
        self.assertNotIn("span", out)


if __name__ == "__main__":
    unittest.main()
