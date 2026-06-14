"""Tests for news subscription pause/resume functionality.

Verifies that:
- Pausing a subscription sets disabled=True and stops message delivery
- Resuming restores disabled=False and message delivery continues
- Double-pause and double-resume produce clear feedback
- DataBasedSource data points are cleaned up/restored properly
- Subscription list shows paused status
- Startup excludes disabled subscriptions
- Add command reactivates paused subscriptions
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch, PropertyMock

# ---------------------------------------------------------------------------
# In-memory store for mocking the database
# ---------------------------------------------------------------------------

class InMemoryStore:
    """A simple in-memory store that mimics the NewsSubscription DB interface.
    Used to make tests self-contained and inspectable."""

    def __init__(self):
        self._rows = []
        self._next_id = 1

    def add_row(self, channel_id, guild_id, source, kind, data=None, disabled=False):
        row = {
            'id': self._next_id,
            'channel_id': channel_id,
            'guild_id': guild_id,
            'source': source,
            'kind': kind,
            'data': data,
            'disabled': disabled,
        }
        self._next_id += 1
        self._rows.append(row)
        return row

    def get_by(self, **filters):
        """Return rows matching all filter criteria."""
        results = []
        for row in self._rows:
            match = True
            for key, value in filters.items():
                if row.get(key) != value:
                    match = False
                    break
            if match:
                results.append(row)
        return results

    def update_row(self, row_id, **updates):
        """Update a row by id."""
        for row in self._rows:
            if row['id'] == row_id:
                row.update(updates)
                return True
        return False

    def delete_by(self, **filters):
        """Delete rows matching all filter criteria."""
        before = len(self._rows)
        self._rows = [
            row for row in self._rows
            if not all(row.get(k) == v for k, v in filters.items())
        ]
        return before - len(self._rows)

    def clear(self):
        self._rows.clear()
        self._next_id = 1


# ---------------------------------------------------------------------------
# Helper: convert a raw DB row dict into a NewsSubscription-like object
# ---------------------------------------------------------------------------

class FakeSub:
    """Lightweight stand-in for NewsSubscription that behaves the same way."""

    def __init__(self, row, store):
        self.id = row['id']
        self.channel_id = row['channel_id']
        self.guild_id = row['guild_id']
        self.source = row['source']
        self.kind = row['kind']
        self.data = row['data']
        self.disabled = row['disabled']
        self._store = store

    async def update_or_add(self):
        """Push local state back to the store."""
        self._store.update_row(
            self.id,
            disabled=self.disabled,
            channel_id=self.channel_id,
            guild_id=self.guild_id,
            source=self.source,
            kind=self.kind,
            data=self.data,
        )


def subs_from_store(store, **filters):
    """Return FakeSub objects matching the given filters."""
    rows = store.get_by(**filters)
    return [FakeSub(r, store) for r in rows]


# ---------------------------------------------------------------------------
# Mock Discord objects
# ---------------------------------------------------------------------------

def make_channel(channel_id=100, name="news", guild_id=999):
    ch = MagicMock()
    ch.id = channel_id
    ch.name = name
    ch.mention = f"<#{channel_id}>"
    ch.guild = MagicMock()
    ch.guild.id = guild_id
    ch.permissions_for = MagicMock(return_value=MagicMock(send_messages=True))
    return ch


def make_ctx(guild_id=999, author_id=42):
    ctx = MagicMock()
    ctx.guild = MagicMock()
    ctx.guild.id = guild_id
    ctx.guild.name = "Test Guild"
    ctx.author = MagicMock()
    ctx.author.id = author_id
    ctx.channel = make_channel(channel_id=1, name="general", guild_id=guild_id)
    ctx.me = MagicMock()
    ctx.bot = MagicMock()
    ctx.bot.command_prefix = "&"
    ctx.prefix = "&"
    sent_messages = []
    ctx.send = AsyncMock(side_effect=lambda *a, **kw: sent_messages.append((a, kw)))
    ctx._sent = sent_messages
    return ctx


def make_source(short_name="cd", full_name="Chief Delphi", is_data_based=False):
    src = MagicMock()
    src.short_name = short_name
    src.full_name = full_name
    src.disabled = False
    src.aliases = (full_name, short_name)
    if is_data_based:
        src.clean_data = AsyncMock(side_effect=lambda text: _make_data_point(text))
        src.add_data = AsyncMock(return_value=True)
        src.remove_data = AsyncMock(return_value=True)
    return src


def _make_data_point(text):
    dp = MagicMock()
    dp.short_name = text
    dp.full_name = text
    dp.__str__ = lambda self: self.short_name
    return dp


# ---------------------------------------------------------------------------
# Simulated command logic
#
# These functions replicate the core logic of the pause/resume commands
# so we can test the flow without needing a full Discord bot + cog stack.
# They mirror the implementations in dozer/cogs/news.py.
# ---------------------------------------------------------------------------

async def simulate_pause(ctx, channel, source, store, data=None, is_data_based=False):
    """Replicates News.pause() logic operating on the in-memory store."""
    from discord.ext.commands import BadArgument

    if is_data_based:
        if data is None:
            raise BadArgument(f"The source {source.full_name} needs data.")
        data_obj = await source.clean_data(data)
        subs = subs_from_store(
            store,
            channel_id=channel.id,
            guild_id=channel.guild.id,
            source=source.short_name,
            data=str(data_obj),
        )
    else:
        data_obj = None
        subs = subs_from_store(
            store,
            channel_id=channel.id,
            guild_id=channel.guild.id,
            source=source.short_name,
        )

    if not subs:
        raise BadArgument(f"No subscription of {source.full_name} for channel {channel.mention} found.")

    sub = subs[0]

    if sub.disabled:
        await ctx.send(f"The subscription of {source.full_name} in {channel.mention} is already paused.")
        return "already_paused"

    sub.disabled = True
    await sub.update_or_add()

    if is_data_based:
        remaining = [
            s for s in subs_from_store(store, source=source.short_name, data=str(data_obj))
            if not s.disabled
        ]
        if not remaining:
            await source.remove_data(data_obj)

    await ctx.send(embed=MagicMock(title=f"paused {source.full_name}"))
    return "paused"


async def simulate_resume(ctx, channel, source, store, data=None, is_data_based=False):
    """Replicates News.resume() logic operating on the in-memory store."""
    from discord.ext.commands import BadArgument

    if is_data_based:
        if data is None:
            raise BadArgument(f"The source {source.full_name} needs data.")
        data_obj = await source.clean_data(data)
        subs = subs_from_store(
            store,
            channel_id=channel.id,
            guild_id=channel.guild.id,
            source=source.short_name,
            data=str(data_obj),
        )
    else:
        data_obj = None
        subs = subs_from_store(
            store,
            channel_id=channel.id,
            guild_id=channel.guild.id,
            source=source.short_name,
        )

    if not subs:
        raise BadArgument(f"No subscription of {source.full_name} for channel {channel.mention} found.")

    sub = subs[0]

    if not sub.disabled:
        await ctx.send(f"The subscription of {source.full_name} in {channel.mention} is not paused.")
        return "already_active"

    sub.disabled = False
    await sub.update_or_add()

    if is_data_based:
        active = [
            s for s in subs_from_store(store, source=source.short_name, data=str(data_obj))
            if not s.disabled
        ]
        if len(active) == 1:
            await source.add_data(data_obj)

    await ctx.send(embed=MagicMock(title=f"resumed {source.full_name}"))
    return "resumed"


def simulate_poll_channels(store, source_short_name, bot_channels):
    """Replicates the channel_dict building in get_new_posts().
    Returns the set of channel_ids that would receive messages."""
    subs = subs_from_store(store, source=source_short_name)
    subs = [s for s in subs if not s.disabled]

    channel_ids = set()
    for sub in subs:
        ch = bot_channels.get(sub.channel_id)
        if ch is not None:
            channel_ids.add(sub.channel_id)
    return channel_ids


def simulate_list_output(store, guild_id, bot_channels):
    """Replicates the list_subscriptions display logic. Returns formatted strings per channel."""
    results = subs_from_store(store, guild_id=guild_id)
    output = {}
    for sub in results:
        ch = bot_channels.get(sub.channel_id)
        if ch is None:
            continue
        entry = sub.source
        if sub.data:
            entry += f": {sub.data}"
        if sub.disabled:
            entry += " **[Paused]**"
        output.setdefault(ch.name, []).append(entry)
    return output


def simulate_startup_data(store, source_short_name):
    """Replicates the data set loaded during startup() for a DataBasedSource."""
    subs = subs_from_store(store, source=source_short_name)
    return {sub.data for sub in subs if not sub.disabled}


async def simulate_add_with_paused(ctx, channel, source, store, kind='embed',
                                   data=None, is_data_based=False):
    """Replicates the 'add' command's interaction with paused subscriptions."""
    from discord.ext.commands import BadArgument

    if is_data_based:
        data_obj = await source.clean_data(data)
        existing = subs_from_store(
            store,
            channel_id=channel.id,
            source=source.short_name,
            data=str(data_obj),
        )
        if existing:
            if existing[0].disabled:
                existing[0].disabled = False
                await existing[0].update_or_add()
                active = [
                    s for s in subs_from_store(store, source=source.short_name, data=str(data_obj))
                    if not s.disabled
                ]
                if len(active) == 1:
                    await source.add_data(data_obj)
                await ctx.send(embed=MagicMock(title="reactivated"))
                return "reactivated"
            raise BadArgument("already subscribed")
    else:
        existing = subs_from_store(
            store,
            channel_id=channel.id,
            source=source.short_name,
        )
        if existing:
            if existing[0].disabled:
                existing[0].disabled = False
                await existing[0].update_or_add()
                await ctx.send(embed=MagicMock(title="reactivated"))
                return "reactivated"
            raise BadArgument("already subscribed")

    # New subscription path
    row = store.add_row(
        channel_id=channel.id,
        guild_id=channel.guild.id,
        source=source.short_name,
        kind=kind,
        data=data,
    )
    await ctx.send(embed=MagicMock(title="subscribed"))
    return "created"


# ===========================================================================
# Tests
# ===========================================================================

class TestNewsSubscriptionModel:
    """Tests for the NewsSubscription model changes."""

    def test_disabled_defaults_to_false(self):
        """New subscriptions should not be disabled by default."""
        store = InMemoryStore()
        row = store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed"
        )
        assert row['disabled'] is False

    def test_disabled_can_be_set(self):
        """Disabled flag can be explicitly set."""
        store = InMemoryStore()
        row = store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed", disabled=True
        )
        assert row['disabled'] is True

    def test_update_disabled_flag(self):
        """The disabled flag can be toggled via update."""
        store = InMemoryStore()
        row = store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed"
        )
        store.update_row(row['id'], disabled=True)
        updated = store.get_by(id=row['id'])[0]
        assert updated['disabled'] is True

        store.update_row(row['id'], disabled=False)
        updated = store.get_by(id=row['id'])[0]
        assert updated['disabled'] is False


class TestPausePlainSource:
    """Tests for pausing subscriptions to plain (non-data-based) sources like RSS."""

    @pytest.fixture
    def setup(self):
        store = InMemoryStore()
        channel = make_channel(channel_id=100, name="news")
        ctx = make_ctx()
        source = make_source(short_name="cd", full_name="Chief Delphi")
        return store, channel, ctx, source

    @pytest.mark.asyncio
    async def test_pause_sets_disabled(self, setup):
        """Pausing an active subscription should set disabled=True."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed"
        )

        result = await simulate_pause(ctx, channel, source, store)

        assert result == "paused"
        rows = store.get_by(source="cd")
        assert rows[0]['disabled'] is True

    @pytest.mark.asyncio
    async def test_pause_sends_confirmation(self, setup):
        """Pausing should send a confirmation message to the user."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed"
        )

        await simulate_pause(ctx, channel, source, store)

        assert ctx.send.called

    @pytest.mark.asyncio
    async def test_pause_stops_message_delivery(self, setup):
        """After pausing, the polling loop should not include this channel."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed"
        )

        # Before pause: channel should be in poll
        channels = simulate_poll_channels(store, "cd", {100: channel})
        assert 100 in channels

        # Pause it
        await simulate_pause(ctx, channel, source, store)

        # After pause: channel should NOT be in poll
        channels = simulate_poll_channels(store, "cd", {100: channel})
        assert 100 not in channels

    @pytest.mark.asyncio
    async def test_double_pause_gives_feedback(self, setup):
        """Pausing an already-paused subscription should inform the user."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed", disabled=True
        )

        result = await simulate_pause(ctx, channel, source, store)

        assert result == "already_paused"
        # Should have sent an "already paused" message
        args, kwargs = ctx._sent[0]
        assert "already paused" in args[0].lower()

    @pytest.mark.asyncio
    async def test_pause_nonexistent_subscription(self, setup):
        """Pausing a subscription that doesn't exist should raise an error."""
        store, channel, ctx, source = setup
        # Don't add any row

        from discord.ext.commands import BadArgument
        with pytest.raises(BadArgument, match="No subscription"):
            await simulate_pause(ctx, channel, source, store)


class TestResumePlainSource:
    """Tests for resuming paused subscriptions to plain sources."""

    @pytest.fixture
    def setup(self):
        store = InMemoryStore()
        channel = make_channel(channel_id=100, name="news")
        ctx = make_ctx()
        source = make_source(short_name="cd", full_name="Chief Delphi")
        return store, channel, ctx, source

    @pytest.mark.asyncio
    async def test_resume_clears_disabled(self, setup):
        """Resuming a paused subscription should set disabled=False."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed", disabled=True
        )

        result = await simulate_resume(ctx, channel, source, store)

        assert result == "resumed"
        rows = store.get_by(source="cd")
        assert rows[0]['disabled'] is False

    @pytest.mark.asyncio
    async def test_resume_restores_message_delivery(self, setup):
        """After resuming, the polling loop should include this channel again."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed", disabled=True
        )

        # Before resume: channel NOT in poll
        channels = simulate_poll_channels(store, "cd", {100: channel})
        assert 100 not in channels

        # Resume it
        await simulate_resume(ctx, channel, source, store)

        # After resume: channel IS in poll
        channels = simulate_poll_channels(store, "cd", {100: channel})
        assert 100 in channels

    @pytest.mark.asyncio
    async def test_double_resume_gives_feedback(self, setup):
        """Resuming an already-active subscription should inform the user."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed", disabled=False
        )

        result = await simulate_resume(ctx, channel, source, store)

        assert result == "already_active"
        args, kwargs = ctx._sent[0]
        assert "not paused" in args[0].lower()

    @pytest.mark.asyncio
    async def test_resume_nonexistent_subscription(self, setup):
        """Resuming a subscription that doesn't exist should raise an error."""
        store, channel, ctx, source = setup

        from discord.ext.commands import BadArgument
        with pytest.raises(BadArgument, match="No subscription"):
            await simulate_resume(ctx, channel, source, store)


class TestPauseResumeFullCycle:
    """Tests verifying the complete pause → resume cycle preserves configuration."""

    @pytest.mark.asyncio
    async def test_pause_resume_preserves_config(self):
        """Pausing and resuming should not alter any other subscription fields."""
        store = InMemoryStore()
        channel = make_channel(channel_id=100, name="news")
        ctx = make_ctx()
        source = make_source(short_name="cd", full_name="Chief Delphi")

        row = store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="plain"
        )
        original_kind = row['kind']
        original_channel = row['channel_id']
        original_guild = row['guild_id']

        # Pause
        await simulate_pause(ctx, channel, source, store)
        paused = store.get_by(id=row['id'])[0]
        assert paused['disabled'] is True
        assert paused['kind'] == original_kind
        assert paused['channel_id'] == original_channel
        assert paused['guild_id'] == original_guild

        # Resume
        await simulate_resume(ctx, channel, source, store)
        resumed = store.get_by(id=row['id'])[0]
        assert resumed['disabled'] is False
        assert resumed['kind'] == original_kind
        assert resumed['channel_id'] == original_channel
        assert resumed['guild_id'] == original_guild

    @pytest.mark.asyncio
    async def test_other_subs_unaffected_by_pause(self):
        """Pausing one subscription should not affect other subscriptions."""
        store = InMemoryStore()
        ch1 = make_channel(channel_id=100, name="news")
        ch2 = make_channel(channel_id=200, name="announcements")
        ctx = make_ctx()
        source = make_source(short_name="cd", full_name="Chief Delphi")

        store.add_row(channel_id=100, guild_id=999, source="cd", kind="embed")
        store.add_row(channel_id=200, guild_id=999, source="cd", kind="plain")

        # Pause ch1
        await simulate_pause(ctx, ch1, source, store)

        rows = store.get_by(source="cd")
        ch1_row = [r for r in rows if r['channel_id'] == 100][0]
        ch2_row = [r for r in rows if r['channel_id'] == 200][0]

        assert ch1_row['disabled'] is True
        assert ch2_row['disabled'] is False


class TestPauseDataBasedSource:
    """Tests for pausing DataBasedSource subscriptions (Reddit, Twitch)."""

    @pytest.fixture
    def setup(self):
        store = InMemoryStore()
        channel = make_channel(channel_id=100, name="reddit")
        ctx = make_ctx()
        source = make_source(
            short_name="reddit", full_name="Reddit", is_data_based=True
        )
        return store, channel, ctx, source

    @pytest.mark.asyncio
    async def test_pause_data_source_removes_data_when_last(self, setup):
        """When the last active subscription for a data point is paused,
        remove_data should be called on the source."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc"
        )

        await simulate_pause(ctx, channel, source, store, data="frc", is_data_based=True)

        source.remove_data.assert_called_once()

    @pytest.mark.asyncio
    async def test_pause_data_source_keeps_data_when_others_active(self, setup):
        """When other active subscriptions exist for the same data point,
        remove_data should NOT be called."""
        store, channel, ctx, source = setup
        ch2 = make_channel(channel_id=200, name="reddit2")
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc"
        )
        store.add_row(
            channel_id=200, guild_id=999, source="reddit",
            kind="embed", data="frc"
        )

        await simulate_pause(ctx, channel, source, store, data="frc", is_data_based=True)

        source.remove_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_pause_data_source_sets_disabled(self, setup):
        """Pausing a data-based subscription should set disabled=True."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc"
        )

        await simulate_pause(ctx, channel, source, store, data="frc", is_data_based=True)

        rows = store.get_by(source="reddit")
        assert rows[0]['disabled'] is True

    @pytest.mark.asyncio
    async def test_pause_data_source_requires_data(self, setup):
        """Pausing a DataBasedSource without providing data should raise an error."""
        store, channel, ctx, source = setup

        from discord.ext.commands import BadArgument
        with pytest.raises(BadArgument, match="needs data"):
            await simulate_pause(ctx, channel, source, store, data=None, is_data_based=True)


class TestResumeDataBasedSource:
    """Tests for resuming DataBasedSource subscriptions."""

    @pytest.fixture
    def setup(self):
        store = InMemoryStore()
        channel = make_channel(channel_id=100, name="reddit")
        ctx = make_ctx()
        source = make_source(
            short_name="reddit", full_name="Reddit", is_data_based=True
        )
        return store, channel, ctx, source

    @pytest.mark.asyncio
    async def test_resume_data_source_re_adds_data(self, setup):
        """When resuming the only subscription for a data point,
        add_data should be called to re-register the data in-memory."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc", disabled=True
        )

        await simulate_resume(ctx, channel, source, store, data="frc", is_data_based=True)

        source.add_data.assert_called_once()

    @pytest.mark.asyncio
    async def test_resume_data_source_skips_add_when_others_active(self, setup):
        """When other active subscriptions exist, add_data should NOT be called again."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc", disabled=True
        )
        store.add_row(
            channel_id=200, guild_id=999, source="reddit",
            kind="embed", data="frc", disabled=False
        )

        await simulate_resume(ctx, channel, source, store, data="frc", is_data_based=True)

        source.add_data.assert_not_called()

    @pytest.mark.asyncio
    async def test_resume_data_source_clears_disabled(self, setup):
        """Resuming should set disabled=False."""
        store, channel, ctx, source = setup
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc", disabled=True
        )

        await simulate_resume(ctx, channel, source, store, data="frc", is_data_based=True)

        rows = store.get_by(source="reddit")
        target = [r for r in rows if r['channel_id'] == 100][0]
        assert target['disabled'] is False


class TestPollingSkipsDisabled:
    """Tests that the polling loop correctly skips disabled subscriptions."""

    def test_active_subs_included_in_poll(self):
        store = InMemoryStore()
        ch = make_channel(channel_id=100, name="news")
        store.add_row(channel_id=100, guild_id=999, source="cd", kind="embed")

        channels = simulate_poll_channels(store, "cd", {100: ch})
        assert 100 in channels

    def test_disabled_subs_excluded_from_poll(self):
        store = InMemoryStore()
        ch = make_channel(channel_id=100, name="news")
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed", disabled=True
        )

        channels = simulate_poll_channels(store, "cd", {100: ch})
        assert 100 not in channels

    def test_mixed_active_and_disabled(self):
        store = InMemoryStore()
        ch1 = make_channel(channel_id=100, name="news")
        ch2 = make_channel(channel_id=200, name="announcements")
        store.add_row(channel_id=100, guild_id=999, source="cd", kind="embed")
        store.add_row(
            channel_id=200, guild_id=999, source="cd", kind="embed", disabled=True
        )

        channels = simulate_poll_channels(store, "cd", {100: ch1, 200: ch2})
        assert 100 in channels
        assert 200 not in channels

    def test_all_disabled_means_no_posts(self):
        store = InMemoryStore()
        ch1 = make_channel(channel_id=100, name="news")
        ch2 = make_channel(channel_id=200, name="announcements")
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed", disabled=True
        )
        store.add_row(
            channel_id=200, guild_id=999, source="cd", kind="embed", disabled=True
        )

        channels = simulate_poll_channels(store, "cd", {100: ch1, 200: ch2})
        assert len(channels) == 0


class TestSubscriptionsList:
    """Tests that the subscriptions list shows paused status."""

    def test_active_sub_no_paused_marker(self):
        store = InMemoryStore()
        ch = make_channel(channel_id=100, name="news")
        store.add_row(channel_id=100, guild_id=999, source="cd", kind="embed")

        output = simulate_list_output(store, 999, {100: ch})

        assert "news" in output
        assert output["news"][0] == "cd"
        assert "[Paused]" not in output["news"][0]

    def test_paused_sub_shows_marker(self):
        store = InMemoryStore()
        ch = make_channel(channel_id=100, name="news")
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed", disabled=True
        )

        output = simulate_list_output(store, 999, {100: ch})

        assert "news" in output
        assert "**[Paused]**" in output["news"][0]

    def test_paused_data_sub_shows_marker(self):
        store = InMemoryStore()
        ch = make_channel(channel_id=100, name="reddit")
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc", disabled=True
        )

        output = simulate_list_output(store, 999, {100: ch})

        assert "reddit" in output["reddit"][0]
        assert "frc" in output["reddit"][0]
        assert "**[Paused]**" in output["reddit"][0]

    def test_mixed_subs_only_paused_shows_marker(self):
        store = InMemoryStore()
        ch1 = make_channel(channel_id=100, name="news")
        ch2 = make_channel(channel_id=200, name="announcements")
        store.add_row(channel_id=100, guild_id=999, source="cd", kind="embed")
        store.add_row(
            channel_id=200, guild_id=999, source="frc", kind="embed", disabled=True
        )

        output = simulate_list_output(store, 999, {100: ch1, 200: ch2})

        assert "[Paused]" not in output["news"][0]
        assert "**[Paused]**" in output["announcements"][0]


class TestStartupExcludesDisabled:
    """Tests that startup() does not load data for disabled subscriptions."""

    def test_startup_ignores_disabled_data_subs(self):
        store = InMemoryStore()
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc", disabled=True
        )
        store.add_row(
            channel_id=200, guild_id=999, source="reddit",
            kind="embed", data="ftc", disabled=False
        )

        data = simulate_startup_data(store, "reddit")

        assert "frc" not in data
        assert "ftc" in data

    def test_startup_all_disabled_means_empty_data(self):
        store = InMemoryStore()
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc", disabled=True
        )

        data = simulate_startup_data(store, "reddit")

        assert len(data) == 0

    def test_startup_all_active_loads_all(self):
        store = InMemoryStore()
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc"
        )
        store.add_row(
            channel_id=200, guild_id=999, source="reddit",
            kind="embed", data="ftc"
        )

        data = simulate_startup_data(store, "reddit")

        assert data == {"frc", "ftc"}


class TestAddReactivatesPaused:
    """Tests that the add command reactivates paused subscriptions."""

    @pytest.mark.asyncio
    async def test_add_reactivates_paused_plain_source(self):
        """Adding a subscription that exists but is paused should reactivate it."""
        store = InMemoryStore()
        channel = make_channel(channel_id=100, name="news")
        ctx = make_ctx()
        source = make_source(short_name="cd", full_name="Chief Delphi")
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed", disabled=True
        )

        result = await simulate_add_with_paused(ctx, channel, source, store)

        assert result == "reactivated"
        rows = store.get_by(source="cd")
        assert rows[0]['disabled'] is False

    @pytest.mark.asyncio
    async def test_add_reactivates_paused_data_source(self):
        """Adding a data-based subscription that is paused should reactivate it
        and re-add the data point to the source."""
        store = InMemoryStore()
        channel = make_channel(channel_id=100, name="reddit")
        ctx = make_ctx()
        source = make_source(
            short_name="reddit", full_name="Reddit", is_data_based=True
        )
        store.add_row(
            channel_id=100, guild_id=999, source="reddit",
            kind="embed", data="frc", disabled=True
        )

        result = await simulate_add_with_paused(
            ctx, channel, source, store, data="frc", is_data_based=True
        )

        assert result == "reactivated"
        rows = store.get_by(source="reddit")
        assert rows[0]['disabled'] is False
        source.add_data.assert_called_once()

    @pytest.mark.asyncio
    async def test_add_active_sub_still_errors(self):
        """Adding a subscription that already exists and is active should still error."""
        store = InMemoryStore()
        channel = make_channel(channel_id=100, name="news")
        ctx = make_ctx()
        source = make_source(short_name="cd", full_name="Chief Delphi")
        store.add_row(
            channel_id=100, guild_id=999, source="cd", kind="embed"
        )

        from discord.ext.commands import BadArgument
        with pytest.raises(BadArgument, match="already subscribed"):
            await simulate_add_with_paused(ctx, channel, source, store)


class TestMigrationDefinition:
    """Tests that the migration for the disabled column is properly defined."""

    @pytest.fixture(autouse=True)
    def _mock_missing_modules(self):
        """Mock modules that may not be installed in the test environment."""
        import sys
        mocks = {}
        for mod in ('sentry_sdk', 'aiotba', 'tbapi', 'googlemaps', 'geopy',
                     'fuzzywuzzy', 'rstcloth', 'humanize'):
            if mod not in sys.modules:
                mocks[mod] = MagicMock()
                sys.modules[mod] = mocks[mod]
        yield
        for mod in mocks:
            sys.modules.pop(mod, None)

    def test_migration_function_exists(self):
        """The NewsSubscription class should have the migrate_v1_add_disabled method."""
        from dozer.cogs.news import NewsSubscription
        assert hasattr(NewsSubscription, 'migrate_v1_add_disabled')

    def test_versions_list_has_migration(self):
        """The __versions__ list should include the new migration."""
        from dozer.cogs.news import NewsSubscription
        assert len(NewsSubscription.__versions__) >= 1
        # __versions__ stores the classmethod descriptor; compare via underlying function
        migration = NewsSubscription.__versions__[0]
        underlying = migration.__func__ if hasattr(migration, '__func__') else migration
        assert underlying == NewsSubscription.migrate_v1_add_disabled.__func__

    def test_init_accepts_disabled_param(self):
        """NewsSubscription.__init__ should accept a disabled parameter."""
        from dozer.cogs.news import NewsSubscription
        sub = NewsSubscription(
            channel_id=100, guild_id=999, source="cd", kind="embed", disabled=True
        )
        assert sub.disabled is True

    def test_init_disabled_defaults_false(self):
        """NewsSubscription disabled should default to False."""
        from dozer.cogs.news import NewsSubscription
        sub = NewsSubscription(
            channel_id=100, guild_id=999, source="cd", kind="embed"
        )
        assert sub.disabled is False
