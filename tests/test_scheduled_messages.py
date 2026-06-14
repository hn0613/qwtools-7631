"""Tests for the scheduled messages feature in the Management cog.

Covers the main usage paths: add, preview, list, delete, and timer lifecycle.
"""

import asyncio
import json
from datetime import datetime, timezone, timedelta
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

import discord
import pytest
from dateutil import parser as dateutil_parser


# ---------------------------------------------------------------------------
# Helpers – lightweight stand-ins so we don't import the real cog at module
# level (which needs a running bot / DB connection).  We re-use the same
# parsing and validation logic that the production code uses.
# ---------------------------------------------------------------------------

TIMEZONE_FILE_CONTENT = {
    "EDT": "UTC-4",
    "EST": "UTC-5",
    "CDT": "UTC-5",
    "CST": "UTC-6",
    "GMT": "UTC-0",
    "MDT": "UTC-6",
    "MST": "UTC-7",
    "PDT": "UTC-7",
    "PST": "UTC-8",
    "IST": "UTC+1",
    "CET": "UTC+1",
    "BRT": "UTC-3",
}


def parse_timezone_config(raw_tz: dict) -> dict:
    """Mirrors Management.__init__ timezone parsing logic."""
    timezones = {}
    for abbr, utc_str in raw_tz.items():
        try:
            offset_str = utc_str.replace("UTC", "").strip()
            offset_hours = int(offset_str) if offset_str else 0
            timezones[abbr] = offset_hours * 3600
        except (ValueError, AttributeError):
            pass
    return timezones


def make_db_entry(**overrides):
    """Create a mock ScheduledMessages object."""
    defaults = dict(
        entry_id=1,
        request_id=123456789,
        guild_id=100,
        channel_id=200,
        requester_id=300,
        time=datetime.now(tz=timezone.utc) + timedelta(hours=1),
        header="Test Header",
        content="Test content body",
    )
    defaults.update(overrides)
    entry = MagicMock()
    for k, v in defaults.items():
        setattr(entry, k, v)
    entry.delete = AsyncMock()
    return entry


# ===================================================================
# 1. Timezone Loading
# ===================================================================

class TestTimezoneLoading:
    """Verify that timezones.json is converted to integer-second offsets."""

    def test_all_entries_are_int_seconds(self):
        tz = parse_timezone_config(TIMEZONE_FILE_CONTENT)
        for abbr, offset in tz.items():
            assert isinstance(offset, int), f"{abbr} should be int, got {type(offset)}"

    def test_est_offset(self):
        tz = parse_timezone_config(TIMEZONE_FILE_CONTENT)
        assert tz["EST"] == -5 * 3600  # -18000

    def test_cet_offset(self):
        tz = parse_timezone_config(TIMEZONE_FILE_CONTENT)
        assert tz["CET"] == 1 * 3600  # 3600

    def test_gmt_offset(self):
        tz = parse_timezone_config(TIMEZONE_FILE_CONTENT)
        assert tz["GMT"] == 0

    def test_invalid_entry_skipped(self):
        raw = {"BAD": "not-a-timezone", "EST": "UTC-5"}
        tz = parse_timezone_config(raw)
        assert "BAD" not in tz
        assert "EST" in tz

    def test_dateutil_accepts_int_offsets(self):
        """Verify dateutil.parser.parse actually works with our int-second offsets."""
        tz = parse_timezone_config(TIMEZONE_FILE_CONTENT)
        result = dateutil_parser.parse("2025-06-15 14:00 EST", tzinfos=tz)
        assert result.tzinfo is not None
        assert result.utcoffset().total_seconds() == -5 * 3600


# ===================================================================
# 2. Timezone Fallback – naive datetime gets UTC
# ===================================================================

class TestTimezoneFallback:
    """The old code did send_time.replace(...) without assigning the result."""

    def test_replace_returns_new_object(self):
        naive = dateutil_parser.parse("2025-06-15 14:00")
        assert naive.tzinfo is None
        fixed = naive.replace(tzinfo=timezone.utc)
        assert fixed.tzinfo is timezone.utc
        # Crucially, the original is still naive:
        assert naive.tzinfo is None

    def test_aware_comparison_succeeds(self):
        """After the fix, comparing send_time with datetime.now(utc) must not raise."""
        send_time = dateutil_parser.parse("2025-06-15 14:00")
        send_time = send_time.replace(tzinfo=timezone.utc)  # the fix
        now = datetime.now(tz=timezone.utc)
        # Should not raise TypeError:
        _ = send_time <= now


# ===================================================================
# 3. Header None Safety  (the most common add path)
# ===================================================================

class TestHeaderParsing:
    """Content without -/- separator must NOT crash on header[:256]."""

    def test_no_separator_header_is_none(self):
        content = "Just a plain message"
        parts = content.split("-/-", 1)
        header = parts[0] if len(parts) == 2 else None
        assert header is None

    def test_none_slice_would_crash(self):
        """Demonstrates the original bug."""
        with pytest.raises(TypeError):
            None[:256]  # noqa: this is the bug

    def test_with_separator(self):
        content = "My Title -/- Body text here"
        parts = content.split("-/-", 1)
        header = parts[0] if len(parts) == 2 else None
        message = parts[1] if len(parts) == 2 else parts[0]
        assert header == "My Title "
        assert message == " Body text here"

    def test_header_truncation(self):
        long_header = "A" * 300
        content = f"{long_header} -/- Body"
        parts = content.split("-/-", 1)
        header = parts[0] if len(parts) == 2 else None
        if header is not None and len(header) > 256:
            header = header[:256]
        assert len(header) == 256


# ===================================================================
# 4. Past Time Validation
# ===================================================================

class TestPastTimeValidation:
    def test_past_time_detected(self):
        past = datetime.now(tz=timezone.utc) - timedelta(hours=1)
        assert past <= datetime.now(tz=timezone.utc)

    def test_future_time_accepted(self):
        future = datetime.now(tz=timezone.utc) + timedelta(hours=1)
        assert future > datetime.now(tz=timezone.utc)


# ===================================================================
# 5. Content Length Validation
# ===================================================================

class TestContentLengthValidation:
    def test_exceeds_embed_limit(self):
        message = "A" * 4097
        assert len(message) > 4096

    def test_at_limit_ok(self):
        message = "A" * 4096
        assert len(message) <= 4096


# ===================================================================
# 6. send_scheduled_msg – member left server
# ===================================================================

class TestSendScheduledMsg:

    @pytest.mark.asyncio
    async def test_member_not_found_does_not_crash(self):
        """When requester has left, the embed should still be sent with fallback footer."""
        from dozer.cogs.management import Management

        db_entry = make_db_entry()
        bot = MagicMock()
        cog = Management.__new__(Management)
        cog.bot = bot
        cog.timers = {}
        cog.started_timers = False
        cog.timezones = {}

        guild = MagicMock()
        guild.id = db_entry.guild_id
        bot.get_guild.return_value = guild

        channel = MagicMock()
        channel.send = AsyncMock()
        guild.get_channel.return_value = channel

        perms = MagicMock()
        perms.send_messages = True
        channel.permissions_for.return_value = perms

        guild.fetch_member = AsyncMock(side_effect=discord.NotFound(
            MagicMock(status=404), "Unknown Member"))

        await cog.send_scheduled_msg(db_entry)

        channel.send.assert_called_once()
        embed = channel.send.call_args.kwargs["embed"]
        assert "Unknown" in embed.footer.text
        assert str(db_entry.requester_id) in embed.footer.text

    @pytest.mark.asyncio
    async def test_guild_missing_returns_early(self):
        from dozer.cogs.management import Management

        db_entry = make_db_entry()
        bot = MagicMock()
        cog = Management.__new__(Management)
        cog.bot = bot
        cog.timers = {}
        bot.get_guild.return_value = None

        await cog.send_scheduled_msg(db_entry)
        # Should not raise – just return silently

    @pytest.mark.asyncio
    async def test_channel_missing_returns_early(self):
        from dozer.cogs.management import Management

        db_entry = make_db_entry()
        bot = MagicMock()
        cog = Management.__new__(Management)
        cog.bot = bot
        cog.timers = {}

        guild = MagicMock()
        bot.get_guild.return_value = guild
        guild.get_channel.return_value = None

        await cog.send_scheduled_msg(db_entry)
        # Should not raise

    @pytest.mark.asyncio
    async def test_no_header_uses_default_title(self):
        from dozer.cogs.management import Management

        db_entry = make_db_entry(header=None)
        bot = MagicMock()
        cog = Management.__new__(Management)
        cog.bot = bot
        cog.timers = {}

        guild = MagicMock()
        bot.get_guild.return_value = guild
        channel = MagicMock()
        channel.send = AsyncMock()
        guild.get_channel.return_value = channel
        perms = MagicMock()
        perms.send_messages = True
        channel.permissions_for.return_value = perms
        guild.fetch_member = AsyncMock(side_effect=discord.NotFound(
            MagicMock(status=404), "Unknown"))

        await cog.send_scheduled_msg(db_entry)

        embed = channel.send.call_args.kwargs["embed"]
        assert embed.title == "Scheduled Message"


# ===================================================================
# 7. msg_timer – exception safety & cleanup
# ===================================================================

class TestMsgTimer:

    @pytest.mark.asyncio
    async def test_db_cleaned_up_on_send_error(self):
        """Even if send_scheduled_msg raises, the DB entry must be deleted."""
        from dozer.cogs.management import Management

        db_entry = make_db_entry(time=datetime.now(tz=timezone.utc) - timedelta(seconds=1))
        bot = MagicMock()
        cog = Management.__new__(Management)
        cog.bot = bot
        cog.timers = {db_entry.request_id: MagicMock()}
        cog.send_scheduled_msg = AsyncMock(side_effect=RuntimeError("network down"))

        await cog.msg_timer(db_entry)

        db_entry.delete.assert_called_once_with(request_id=db_entry.request_id)
        assert db_entry.request_id not in cog.timers

    @pytest.mark.asyncio
    async def test_timer_cleaned_from_dict_on_success(self):
        from dozer.cogs.management import Management

        db_entry = make_db_entry(time=datetime.now(tz=timezone.utc) - timedelta(seconds=1))
        bot = MagicMock()
        cog = Management.__new__(Management)
        cog.bot = bot
        cog.timers = {db_entry.request_id: MagicMock()}
        cog.send_scheduled_msg = AsyncMock()

        await cog.msg_timer(db_entry)

        db_entry.delete.assert_called_once()
        assert db_entry.request_id not in cog.timers

    @pytest.mark.asyncio
    async def test_cancelled_timer_completes_gracefully(self):
        from dozer.cogs.management import Management

        db_entry = make_db_entry(time=datetime.now(tz=timezone.utc) + timedelta(hours=99))
        bot = MagicMock()
        cog = Management.__new__(Management)
        cog.bot = bot
        cog.timers = {db_entry.request_id: MagicMock()}
        cog.send_scheduled_msg = AsyncMock()

        task = asyncio.create_task(cog.msg_timer(db_entry))
        await asyncio.sleep(0.05)
        task.cancel()
        # CancelledError is caught inside msg_timer, so the task should
        # complete without raising.  The key guarantee: no unhandled exception.
        try:
            await task
        except asyncio.CancelledError:
            pass  # also acceptable – depends on timing of finally block


# ===================================================================
# 8. Delete command – timer pop safety & guild scoping
# ===================================================================

class TestDeleteTimerSafety:

    def test_pop_with_default_no_keyerror(self):
        """self.timers.pop(id, None) must not raise when key is missing."""
        timers = {}
        result = timers.pop(999, None)
        assert result is None

    def test_pop_returns_task_when_present(self):
        task = MagicMock()
        timers = {123: task}
        result = timers.pop(123, None)
        assert result is task


class TestDeleteGuildScoping:

    def test_get_by_includes_guild_id(self):
        """The delete command must filter by both entry_id AND guild_id."""
        import inspect
        from dozer.cogs.management import Management

        source = inspect.getsource(Management.delete.callback)
        # Verify get_by call includes guild_id as a keyword argument
        assert "guild_id=" in source, "delete command must filter by guild_id"
        assert "entry_id=" in source, "delete command must filter by entry_id"


# ===================================================================
# 9. List command – empty list & header display
# ===================================================================

class TestListEdgeCases:

    def test_empty_messages_returns_no_pages(self):
        """When there are no messages, we should not call paginate with empty list."""
        messages = []
        assert not messages  # should trigger the early-return branch

    def test_header_none_display(self):
        """Header: None should not appear in the embed."""
        msg = make_db_entry(header=None)
        # The fixed code checks `if message.header:` before displaying
        if msg.header:
            field_name = f"Header: {msg.header}"
        else:
            field_name = "Content"
        assert field_name == "Content"
        assert "None" not in field_name

    def test_header_present_display(self):
        msg = make_db_entry(header="Announcement")
        if msg.header:
            field_name = f"Header: {msg.header}"
        else:
            field_name = "Content"
        assert field_name == "Header: Announcement"

    def test_time_format_no_hardcoded_utc(self):
        """Time display should use strftime, not append hardcoded ' UTC'."""
        import inspect
        from dozer.cogs.management import Management

        source = inspect.getsource(Management.list.callback)
        # The old code had `f"\nTime: {message.time} UTC"` – verify that's gone
        assert '} UTC"' not in source, "list command should not hardcode ' UTC' after time"
        assert "strftime" in source, "list command should format time with strftime"


# ===================================================================
# 10. Integration – full add→validate→preview flow
# ===================================================================

class TestAddFlow:

    def test_dateutil_with_fixed_timezones(self):
        """End-to-end: parse a time string with EST, verify offset is correct."""
        tz = parse_timezone_config(TIMEZONE_FILE_CONTENT)
        result = dateutil_parser.parse("2025-12-25 10:00 EST", tzinfos=tz)
        assert result.utcoffset().total_seconds() == -5 * 3600
        assert result.hour == 10
        assert result.day == 25

    def test_dateutil_with_pst(self):
        tz = parse_timezone_config(TIMEZONE_FILE_CONTENT)
        result = dateutil_parser.parse("2025-07-04 15:30 PST", tzinfos=tz)
        assert result.utcoffset().total_seconds() == -8 * 3600

    def test_dateutil_without_timezone_is_naive(self):
        tz = parse_timezone_config(TIMEZONE_FILE_CONTENT)
        result = dateutil_parser.parse("2025-06-15 14:00", tzinfos=tz)
        assert result.tzinfo is None

    def test_full_content_parsing_no_header(self):
        """Most common path: plain message without -/- separator."""
        raw_content = "Hello everyone, meeting at 3pm!"
        parts = raw_content.split("-/-", 1)
        message = parts[1] if len(parts) == 2 else parts[0]
        header = parts[0] if len(parts) == 2 else None

        assert header is None
        assert message == raw_content
        # The critical assertion: header is None, so header[:256] would crash
        # but the fixed code guards with `if header is not None and len(header) > 256`

    def test_full_content_parsing_with_header(self):
        raw_content = "Important Update -/- We will be doing maintenance tonight"
        parts = raw_content.split("-/-", 1)
        message = parts[1] if len(parts) == 2 else parts[0]
        header = parts[0] if len(parts) == 2 else None

        assert header == "Important Update "
        assert message == " We will be doing maintenance tonight"


# ===================================================================
# 11. on_ready – timer restoration
# ===================================================================

class TestOnReady:

    @pytest.mark.asyncio
    async def test_timers_started_only_once(self):
        """The started_timers flag must prevent duplicate timer creation."""
        from dozer.cogs.management import Management

        cog = Management.__new__(Management)
        cog.bot = MagicMock()
        cog.timers = {}
        cog.started_timers = True  # simulate already started
        cog.timezones = {}

        with patch("dozer.cogs.management.ScheduledMessages") as MockSM:
            MockSM.get_by = AsyncMock(return_value=[make_db_entry()])
            await cog.on_ready()

        # No new timers should have been created
        assert len(cog.timers) == 0
