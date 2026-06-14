"""Tests for the enhanced shortcuts cog.

Run with:
    python3 -m pytest test_shortcuts.py -v

Covers:
  - Original fixed-reply (exact match) still works unchanged
  - New dynamic-reply (suffix appended) works
  - Edge cases: empty suffix, prefix-only, bot messages, DMs, case insensitivity,
    duplicate names, rate limiting, mass-mention stripping, length truncation
"""

import asyncio
import time
from unittest.mock import AsyncMock, MagicMock

import pytest

# ===========================================================================
# Standalone test infrastructure (no pytest-asyncio dependency)
# ===========================================================================

MAX_LEN = 20
MAX_SUFFIX_LEN = 1800
TRIGGER_COOLDOWN = 3.0


def _run(coro):
    """Run an async coroutine synchronously."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _normalize_name(name: str) -> str:
    return name.strip().lower()


class FakeShortcut:
    def __init__(self, name, value):
        self.name = name
        self.value = value


class FakeMessage:
    def __init__(self, content, guild_id=1, channel_id=1, author_id=100, is_bot=False):
        self.content = content
        self.guild = MagicMock()
        self.guild.id = guild_id
        self.channel = MagicMock()
        self.channel.id = channel_id
        self.channel.send = AsyncMock()
        self.author = MagicMock()
        self.author.id = author_id
        self.author.bot = is_bot


def clean_mass_mentions(text: str) -> str:
    """Simplified version of dozer.utils.clean(mass=True)."""
    return text.replace("@everyone", "@\u200beveryone").replace("@here", "@\u200bhere")


async def process_shortcut_trigger(msg, prefix, shortcuts, last_trigger: dict):
    """Mirrors the on_message logic from shortcuts.py for testing."""
    # Edge-case guards
    if not msg.guild or msg.author.bot:
        return False

    if not prefix:
        return False

    content = msg.content
    if len(content) < len(prefix) or not content.startswith(prefix):
        return False

    remainder = content[len(prefix):].strip()
    if not remainder:
        return False

    remainder_lower = remainder.lower()

    if not shortcuts:
        return False

    # Rate limit
    key = (msg.guild.id, msg.channel.id, msg.author.id)
    last = last_trigger.get(key)
    if last is not None and (time.monotonic() - last) < TRIGGER_COOLDOWN:
        return False

    for shortcut in shortcuts:
        name_lower = shortcut.name.lower()
        if remainder_lower == name_lower:
            await msg.channel.send(shortcut.value)
            last_trigger[key] = time.monotonic()
            return True
        if remainder_lower.startswith(name_lower + " "):
            raw_suffix = remainder[len(name_lower):]
            suffix = raw_suffix.strip()

            if not suffix:
                await msg.channel.send(shortcut.value)
                last_trigger[key] = time.monotonic()
                return True

            safe_suffix = clean_mass_mentions(suffix)

            if len(safe_suffix) > MAX_SUFFIX_LEN:
                safe_suffix = safe_suffix[:MAX_SUFFIX_LEN] + "\u2026"

            separator = "" if shortcut.value and shortcut.value[-1] in " \t\n" else " "
            reply = shortcut.value + separator + safe_suffix

            if len(reply) > 2000:
                reply = reply[:1997] + "\u2026"

            await msg.channel.send(reply)
            last_trigger[key] = time.monotonic()
            return True

    return False


# ===========================================================================
# Tests – Exact Match (original behaviour)
# ===========================================================================

class TestExactMatch:

    def test_exact_match_sends_template(self):
        """`!hello` -> sends 'Hello, World!' unchanged."""
        msg = FakeMessage("!hello")
        shortcuts = [FakeShortcut("hello", "Hello, World!")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with("Hello, World!")

    def test_exact_match_case_insensitive(self):
        """`!HELLO`, `!HeLLo`, `!hello` all match shortcut named 'hello'."""
        for content in ["!HELLO", "!HeLLo", "!hello"]:
            msg = FakeMessage(content, author_id=hash(content) % 10000)
            shortcuts = [FakeShortcut("hello", "Hi there")]
            result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
            assert result is True, f"Failed for content={content!r}"
            msg.channel.send.assert_awaited_once_with("Hi there")

    def test_no_match_does_not_send(self):
        """Unrelated messages are ignored."""
        msg = FakeMessage("!goodbye")
        shortcuts = [FakeShortcut("hello", "Hello, World!")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is False
        msg.channel.send.assert_not_awaited()


# ===========================================================================
# Tests – Dynamic Suffix (new behaviour)
# ===========================================================================

class TestDynamicSuffix:

    def test_suffix_appended_to_template(self):
        """`!hello John, check this out` -> 'Hello, World! John, check this out'."""
        msg = FakeMessage("!hello John, check this out")
        shortcuts = [FakeShortcut("hello", "Hello, World!")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with("Hello, World! John, check this out")

    def test_suffix_preserves_user_casing(self):
        """User's suffix casing is preserved in the output."""
        msg = FakeMessage("!hello Foobar BAZ")
        shortcuts = [FakeShortcut("hello", "Hi ")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with("Hi Foobar BAZ")

    def test_whitespace_only_suffix_treated_as_exact(self):
        """`!hello   ` (trailing spaces) sends template only, no extra space."""
        msg = FakeMessage("!hello   ")
        shortcuts = [FakeShortcut("hello", "Hello, World!")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with("Hello, World!")

    def test_dynamic_match_case_insensitive_name(self):
        """`!HELLO extra text` matches shortcut named 'hello'."""
        msg = FakeMessage("!HELLO extra text")
        shortcuts = [FakeShortcut("hello", "Hi ")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with("Hi extra text")

    def test_suffix_with_url_and_mention(self):
        """Users can append URLs and @-mentions in the suffix."""
        msg = FakeMessage("!info check https://example.com for details")
        shortcuts = [FakeShortcut("info", "FYI: ")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with("FYI: check https://example.com for details")


# ===========================================================================
# Tests – Edge Cases
# ===========================================================================

class TestEdgeCases:

    def test_prefix_only_no_match(self):
        """Just `!` does not trigger anything."""
        msg = FakeMessage("!")
        shortcuts = [FakeShortcut("hello", "Hello")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is False
        msg.channel.send.assert_not_awaited()

    def test_prefix_plus_spaces_no_match(self):
        """`!   ` (prefix + whitespace only) does not trigger."""
        msg = FakeMessage("!   ")
        shortcuts = [FakeShortcut("hello", "Hello")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is False
        msg.channel.send.assert_not_awaited()

    def test_bot_message_ignored(self):
        """Messages from bots are never processed."""
        msg = FakeMessage("!hello", is_bot=True)
        shortcuts = [FakeShortcut("hello", "Hello")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is False
        msg.channel.send.assert_not_awaited()

    def test_dm_ignored(self):
        """Messages without a guild (DMs) are ignored."""
        msg = FakeMessage("!hello")
        msg.guild = None
        shortcuts = [FakeShortcut("hello", "Hello")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is False
        msg.channel.send.assert_not_awaited()

    def test_mass_mentions_stripped_from_suffix(self):
        """@everyone and @here in the suffix are neutralised."""
        msg = FakeMessage("!hello @everyone check this @here")
        shortcuts = [FakeShortcut("hello", "Hi ")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        sent = msg.channel.send.call_args[0][0]
        assert "@\u200beveryone" in sent
        assert "@\u200bhere" in sent
        # Raw mass mentions must NOT appear
        assert "@everyone" not in sent.replace("@\u200beveryone", "")
        assert "@here" not in sent.replace("@\u200bhere", "")

    def test_suffix_length_capped(self):
        """Very long suffixes are truncated to MAX_SUFFIX_LEN."""
        long_suffix = "x" * 3000
        msg = FakeMessage(f"!hello {long_suffix}")
        shortcuts = [FakeShortcut("hello", "Hi ")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        sent = msg.channel.send.call_args[0][0]
        assert len(sent) <= len("Hi ") + MAX_SUFFIX_LEN + len("\u2026")

    def test_total_reply_capped_at_2000(self):
        """If template + suffix > 2000 chars, total is truncated."""
        template = "T" * 500
        long_suffix = "x" * 2000
        msg = FakeMessage(f"!hello {long_suffix}")
        shortcuts = [FakeShortcut("hello", template)]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        sent = msg.channel.send.call_args[0][0]
        assert len(sent) <= 2000

    def test_empty_shortcuts_list(self):
        """No shortcuts configured -> no reply."""
        msg = FakeMessage("!hello")
        result = _run(process_shortcut_trigger(msg, "!", [], {}))
        assert result is False
        msg.channel.send.assert_not_awaited()

    def test_message_not_starting_with_prefix(self):
        """Messages that don't start with prefix are ignored."""
        msg = FakeMessage("hello world")
        shortcuts = [FakeShortcut("hello", "Hello")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is False
        msg.channel.send.assert_not_awaited()

    def test_empty_prefix(self):
        """Empty prefix string should not match anything."""
        msg = FakeMessage("hello")
        shortcuts = [FakeShortcut("hello", "Hello")]
        result = _run(process_shortcut_trigger(msg, "", shortcuts, {}))
        assert result is False
        msg.channel.send.assert_not_awaited()

    def test_multiword_shortcut_name_exact(self):
        """Shortcut name with a space (e.g. 'good morning') matches exactly."""
        msg = FakeMessage("!good morning")
        shortcuts = [FakeShortcut("good morning", "Rise and shine!")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with("Rise and shine!")

    def test_multiword_shortcut_name_with_suffix(self):
        """Shortcut name with a space + suffix triggers dynamic reply."""
        msg = FakeMessage("!good morning everyone!")
        shortcuts = [FakeShortcut("good morning", "Rise and shine! ")]
        result = _run(process_shortcut_trigger(msg, "!", shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with("Rise and shine! everyone!")


# ===========================================================================
# Tests – Rate Limiting
# ===========================================================================

class TestRateLimiting:

    def test_rapid_triggers_blocked_by_cooldown(self):
        """Second trigger within cooldown window is suppressed."""
        msg1 = FakeMessage("!hello", author_id=100)
        msg2 = FakeMessage("!hello", author_id=100)
        shortcuts = [FakeShortcut("hello", "Hello")]
        last_trigger = {}

        r1 = _run(process_shortcut_trigger(msg1, "!", shortcuts, last_trigger))
        r2 = _run(process_shortcut_trigger(msg2, "!", shortcuts, last_trigger))

        assert r1 is True
        assert r2 is False  # blocked by cooldown
        assert msg1.channel.send.await_count == 1
        assert msg2.channel.send.await_count == 0

    def test_different_users_not_rate_limited(self):
        """Different users in the same channel are independent."""
        msg1 = FakeMessage("!hello", author_id=100)
        msg2 = FakeMessage("!hello", author_id=200)
        shortcuts = [FakeShortcut("hello", "Hello")]
        last_trigger = {}

        r1 = _run(process_shortcut_trigger(msg1, "!", shortcuts, last_trigger))
        r2 = _run(process_shortcut_trigger(msg2, "!", shortcuts, last_trigger))

        assert r1 is True
        assert r2 is True

    def test_different_channels_not_rate_limited(self):
        """Same user in different channels is not rate-limited."""
        msg1 = FakeMessage("!hello", author_id=100, channel_id=1)
        msg2 = FakeMessage("!hello", author_id=100, channel_id=2)
        shortcuts = [FakeShortcut("hello", "Hello")]
        last_trigger = {}

        r1 = _run(process_shortcut_trigger(msg1, "!", shortcuts, last_trigger))
        r2 = _run(process_shortcut_trigger(msg2, "!", shortcuts, last_trigger))

        assert r1 is True
        assert r2 is True

    def test_cooldown_expires(self):
        """After the cooldown window passes, the trigger works again."""
        msg1 = FakeMessage("!hello", author_id=100)
        shortcuts = [FakeShortcut("hello", "Hello")]
        last_trigger = {}

        r1 = _run(process_shortcut_trigger(msg1, "!", shortcuts, last_trigger))
        assert r1 is True

        # Simulate time passing beyond the cooldown
        key = (msg1.guild.id, msg1.channel.id, msg1.author.id)
        last_trigger[key] = time.monotonic() - TRIGGER_COOLDOWN - 1

        msg2 = FakeMessage("!hello", author_id=100)
        r2 = _run(process_shortcut_trigger(msg2, "!", shortcuts, last_trigger))
        assert r2 is True


# ===========================================================================
# Tests – Name Normalisation
# ===========================================================================

class TestNormalizeName:

    def test_lowercase(self):
        assert _normalize_name("Hello") == "hello"

    def test_strip_whitespace(self):
        assert _normalize_name("  hello  ") == "hello"

    def test_mixed_case_and_spaces(self):
        assert _normalize_name("  HeLLo  ") == "hello"

    def test_empty_after_strip(self):
        assert _normalize_name("   ") == ""


# ===========================================================================
# Tests – Main Pathway (end-to-end scenarios)
# ===========================================================================

class TestMainPathways:

    def test_original_fixed_reply_pathway(self):
        """
        Full pathway: admin sets prefix -> admin creates shortcut -> user
        triggers exact match.

        Validates that the original fixed-reply workflow still works
        identically after the enhancement.
        """
        prefix = "!"
        shortcuts = [FakeShortcut("rules", "Please read the server rules in #rules channel.")]

        # User triggers exact match
        msg = FakeMessage("!rules")
        result = _run(process_shortcut_trigger(msg, prefix, shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with(
            "Please read the server rules in #rules channel."
        )

    def test_new_dynamic_reply_pathway(self):
        """
        Full pathway: admin sets prefix -> admin creates shortcut -> user
        triggers with extra text.

        Validates the new dynamic-reply workflow where the user appends
        extra context to the trigger.
        """
        prefix = "!"
        shortcuts = [FakeShortcut("rules", "Please read the server rules in #rules channel.")]

        msg = FakeMessage("!rules especially section 3 about spam")
        result = _run(process_shortcut_trigger(msg, prefix, shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with(
            "Please read the server rules in #rules channel. especially section 3 about spam"
        )

    def test_dynamic_reply_with_mention_safety(self):
        """Dynamic reply strips dangerous mass mentions from the suffix."""
        prefix = "!"
        shortcuts = [FakeShortcut("announce", "Important: ")]

        msg = FakeMessage("!announce @everyone please read this")
        result = _run(process_shortcut_trigger(msg, prefix, shortcuts, {}))
        assert result is True
        sent = msg.channel.send.call_args[0][0]
        assert "@\u200beveryone" in sent
        assert sent.startswith("Important: ")

    def test_multiple_shortcuts_correct_match(self):
        """When multiple shortcuts exist, the correct one is matched."""
        prefix = "!"
        shortcuts = [
            FakeShortcut("hello", "Hi!"),
            FakeShortcut("help", "Check the docs."),
        ]

        # Exact match for 'hello'
        msg = FakeMessage("!hello")
        result = _run(process_shortcut_trigger(msg, prefix, shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with("Hi!")

        # Suffix match for 'help'
        msg2 = FakeMessage("!help with the API", author_id=999)
        result2 = _run(process_shortcut_trigger(msg2, prefix, shortcuts, {}))
        assert result2 is True
        msg2.channel.send.assert_awaited_once_with("Check the docs. with the API")

    def test_shortcut_name_not_prefix_of_another(self):
        """
        'hell' should not accidentally match when user typed '!hello'.
        With prefix matching, if 'hell' is checked before 'hello',
        '!hello' would match 'hell' + suffix 'o'. We verify the algorithm
        handles this by ensuring space-separated matching.
        """
        prefix = "!"
        shortcuts = [
            FakeShortcut("hell", "Welcome to the underworld!"),
            FakeShortcut("hello", "Hi there!"),
        ]

        # '!hello' should match 'hello' exactly, NOT 'hell' + suffix 'o'
        msg = FakeMessage("!hello")
        result = _run(process_shortcut_trigger(msg, prefix, shortcuts, {}))
        assert result is True
        # The exact match for 'hello' should fire, not 'hell' + 'o'
        msg.channel.send.assert_awaited_once_with("Hi there!")

    def test_shortcut_name_prefix_of_another_with_suffix(self):
        """
        '!hello world' should match 'hello' with suffix 'world',
        not 'hell' with suffix 'o world'.
        """
        prefix = "!"
        shortcuts = [
            FakeShortcut("hell", "Welcome to the underworld! "),
            FakeShortcut("hello", "Hi "),
        ]

        msg = FakeMessage("!hello world")
        result = _run(process_shortcut_trigger(msg, prefix, shortcuts, {}))
        assert result is True
        msg.channel.send.assert_awaited_once_with("Hi world")
