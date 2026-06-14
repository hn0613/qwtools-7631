"""Tests for the Shortcuts cog — covers fixed replies, dynamic content, and edge cases."""

import asyncio
import sys
import os
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

# Mock heavy dependencies that are not installed in test env
for mod_name in ('sentry_sdk', 'asyncpg', 'loguru'):
    if mod_name not in sys.modules:
        sys.modules[mod_name] = MagicMock()

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dozer.cogs.shortcuts import Shortcuts, ShortcutEntry, ShortcutSetting


def run_async(coro):
    """Helper to run async test coroutines."""
    return asyncio.get_event_loop().run_until_complete(coro)


def make_message(content, *, guild_id=1, is_bot=False, is_dm=False):
    """Create a mock Discord message."""
    msg = MagicMock()
    msg.content = content
    msg.author.bot = is_bot
    if is_dm:
        msg.guild = None
    else:
        msg.guild = MagicMock()
        msg.guild.id = guild_id
    msg.channel.send = AsyncMock()
    return msg


def make_cog():
    """Create a Shortcuts cog with mocked bot and caches."""
    bot = MagicMock()
    cog = Shortcuts.__new__(Shortcuts)
    cog.bot = bot
    cog.settings_cache = MagicMock()
    cog.settings_cache.query_one = AsyncMock()
    cog.cache = MagicMock()
    cog.cache.query_one = AsyncMock()
    return cog


def make_setting(prefix="!"):
    """Create a mock ShortcutSetting."""
    s = MagicMock(spec=ShortcutSetting)
    s.prefix = prefix
    return s


def make_entry(name, value, guild_id=1):
    """Create a ShortcutEntry instance."""
    return ShortcutEntry(guild_id=guild_id, name=name, value=value)


# ---------------------------------------------------------------------------
# _sanitize_extra (pure function, no mocking needed)
# ---------------------------------------------------------------------------
class TestSanitizeExtra(unittest.TestCase):
    """Tests for the _sanitize_extra static method."""

    def test_plain_text_unchanged(self):
        result = Shortcuts._sanitize_extra("some extra context")
        self.assertEqual(result, "some extra context")

    def test_escapes_everyone(self):
        result = Shortcuts._sanitize_extra("hey @everyone look")
        self.assertNotIn("@everyone", result)
        self.assertIn("@\u200beveryone", result)

    def test_escapes_here(self):
        result = Shortcuts._sanitize_extra("yo @here check this")
        self.assertNotIn("@here", result)
        self.assertIn("@\u200bhere", result)

    def test_strips_user_mention(self):
        result = Shortcuts._sanitize_extra("hello <@123456> world")
        self.assertEqual(result, "hello  world")

    def test_strips_nickname_mention(self):
        result = Shortcuts._sanitize_extra("hello <@!789> world")
        self.assertEqual(result, "hello  world")

    def test_strips_role_mention(self):
        result = Shortcuts._sanitize_extra("hello <@&999> world")
        self.assertEqual(result, "hello  world")

    def test_truncates_long_content(self):
        long_text = "a" * 1000
        result = Shortcuts._sanitize_extra(long_text)
        self.assertEqual(len(result), Shortcuts.MAX_EXTRA_LEN)

    def test_empty_string(self):
        result = Shortcuts._sanitize_extra("")
        self.assertEqual(result, "")

    def test_only_whitespace(self):
        result = Shortcuts._sanitize_extra("   ")
        self.assertEqual(result, "")

    def test_only_mentions(self):
        result = Shortcuts._sanitize_extra("<@123> <@!456> <@&789>")
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# on_message — fixed reply (backward compatibility)
# ---------------------------------------------------------------------------
class TestOnMessageFixedReply(unittest.TestCase):
    """Original fixed-reply shortcuts must keep working identically."""

    def test_exact_match_sends_template(self):
        """!hello -> sends template value (no extra content)."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!hello")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[make_entry("hello", "Hello, World!")]):
            run_async(cog.on_message(msg))

        msg.channel.send.assert_called_once_with("Hello, World!")

    def test_case_insensitive_match(self):
        """!HELLO should still match 'hello'."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!HELLO")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[make_entry("hello", "Hello, World!")]):
            run_async(cog.on_message(msg))

        msg.channel.send.assert_called_once_with("Hello, World!")

    def test_no_match_no_response(self):
        """!unknown should not produce any response."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!unknown")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[make_entry("hello", "Hello, World!")]):
            run_async(cog.on_message(msg))

        msg.channel.send.assert_not_called()

    def test_multichar_prefix(self):
        """Prefix can be multiple characters, e.g. '!!'."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!!")
        msg = make_message("!!hello")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[make_entry("hello", "Hi there")]):
            run_async(cog.on_message(msg))

        msg.channel.send.assert_called_once_with("Hi there")


# ---------------------------------------------------------------------------
# on_message — dynamic content (new feature)
# ---------------------------------------------------------------------------
class TestOnMessageDynamic(unittest.TestCase):
    """Dynamic content appended after the shortcut name."""

    def test_extra_text_appended(self):
        """!hello check this out -> template + newline + extra."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!hello check this out")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[make_entry("hello", "Hello, World!")]):
            run_async(cog.on_message(msg))

        msg.channel.send.assert_called_once_with("Hello, World!\ncheck this out")

    def test_extra_with_url(self):
        """URLs in extra content pass through safely."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!info https://example.com/page")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[make_entry("info", "See details:")]):
            run_async(cog.on_message(msg))

        msg.channel.send.assert_called_once_with("See details:\nhttps://example.com/page")

    def test_extra_mentions_sanitized(self):
        """@everyone in extra content is escaped, not sent raw."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!announce @everyone look at this")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[make_entry("announce", "Announcement:")]):
            run_async(cog.on_message(msg))

        sent_text = msg.channel.send.call_args[0][0]
        self.assertIn("Announcement:", sent_text)
        self.assertNotIn("@everyone", sent_text)
        self.assertIn("@\u200beveryone", sent_text)

    def test_extra_user_mention_stripped(self):
        """<@123456> in extra content is removed."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!greet <@123456> welcome")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[make_entry("greet", "Welcome!")]):
            run_async(cog.on_message(msg))

        sent_text = msg.channel.send.call_args[0][0]
        self.assertNotIn("<@123456>", sent_text)
        self.assertIn("welcome", sent_text)

    def test_extra_only_whitespace_ignored(self):
        """'!hello   ' (trailing spaces) should send template only."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!hello   ")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[make_entry("hello", "Hello, World!")]):
            run_async(cog.on_message(msg))

        msg.channel.send.assert_called_once_with("Hello, World!")

    def test_case_insensitive_with_extra(self):
        """!HELLO extra text -> still matches and appends."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!HeLLo some context")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[make_entry("hello", "Hi!")]):
            run_async(cog.on_message(msg))

        msg.channel.send.assert_called_once_with("Hi!\nsome context")


# ---------------------------------------------------------------------------
# on_message — edge cases and guards
# ---------------------------------------------------------------------------
class TestOnMessageEdgeCases(unittest.TestCase):
    """Edge cases: DMs, bots, prefix-only, no prefix configured."""

    def test_dm_ignored(self):
        """Messages in DMs are silently ignored."""
        cog = make_cog()
        msg = make_message("!hello", is_dm=True)
        run_async(cog.on_message(msg))
        msg.channel.send.assert_not_called()

    def test_bot_message_ignored(self):
        """Messages from bots are silently ignored."""
        cog = make_cog()
        msg = make_message("!hello", is_bot=True)
        run_async(cog.on_message(msg))
        msg.channel.send.assert_not_called()

    def test_prefix_only_ignored(self):
        """Just '!' with nothing after it is silently ignored."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!")
        run_async(cog.on_message(msg))
        msg.channel.send.assert_not_called()

    def test_prefix_with_spaces_only_ignored(self):
        """'!   ' (prefix + only spaces) is silently ignored."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!   ")
        run_async(cog.on_message(msg))
        msg.channel.send.assert_not_called()

    def test_no_setting_configured(self):
        """If no prefix is configured for the guild, nothing happens."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = None
        msg = make_message("!hello")
        run_async(cog.on_message(msg))
        msg.channel.send.assert_not_called()

    def test_no_shortcuts_registered(self):
        """If the guild has a prefix but no shortcuts, nothing happens."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("!hello")

        with patch.object(ShortcutEntry, 'get_by', new_callable=AsyncMock,
                          return_value=[]):
            run_async(cog.on_message(msg))

        msg.channel.send.assert_not_called()

    def test_message_shorter_than_prefix(self):
        """Message shorter than the prefix is ignored."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!!")
        msg = make_message("!")
        run_async(cog.on_message(msg))
        msg.channel.send.assert_not_called()

    def test_wrong_prefix_ignored(self):
        """Message with a different prefix doesn't trigger."""
        cog = make_cog()
        cog.settings_cache.query_one.return_value = make_setting("!")
        msg = make_message("?hello")
        run_async(cog.on_message(msg))
        msg.channel.send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
