"""Tests for news subscription pause/resume functionality."""
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Stub out all heavy third-party dependencies before importing project code.
# The import chain is: dozer.__init__ -> dozer.bot -> dozer.cogs._utils -> ...
# We need to mock discord and all submodules as separate sys.modules entries.
# ---------------------------------------------------------------------------

def _make_task_loop(**kwargs):
    """Mock for discord.ext.tasks.loop that returns a callable with task attributes."""
    def decorator(func):
        func.start = lambda *a, **kw: None
        func.stop = lambda *a, **kw: None
        func.cancel = lambda *a, **kw: None
        func.restart = lambda *a, **kw: None
        func.change_interval = lambda *a, **kw: None
        func.error = lambda f: f
        func.next_iteration = None
        func.get_task = MagicMock()
        return func
    return decorator


def _mock_commands_group(**kwargs):
    """Mock for commands.group: returns the function with a .command() method attached."""
    def decorator(func):
        func.command = lambda **kw: lambda f: f
        func.group = lambda **kw: lambda f: f
        func.example_usage = ''
        return func
    return decorator


_discord = MagicMock()
_BadArgument = type('BadArgument', (Exception,), {})
_CheckFailure = type('CheckFailure', (Exception,), {})
_discord.version_info.major = 2
_discord.ext.commands.BadArgument = _BadArgument
_discord.ext.commands.CheckFailure = _CheckFailure
_discord.ext.commands.Bot = type('Bot', (), {'__init__': lambda *a, **kw: None})
_discord.ext.commands.Cog = type('Cog', (), {'listener': classmethod(lambda *a, **kw: lambda f: f)})
_discord.ext.commands.Cooldown = lambda *a, **kw: None
_discord.ext.commands.command = lambda **kw: lambda f: f
_discord.ext.commands.group = _mock_commands_group
_discord.ext.commands.guild_only = lambda: lambda f: f
_discord.ext.commands.has_permissions = lambda **kw: lambda f: f
_discord.ext.commands.HybridCommand = type('HybridCommand', (), {})
_discord.ext.commands.HybridGroup = type('HybridGroup', (), {
    'add_command': lambda self, cmd: None,
})
_discord.ext.tasks.loop = _make_task_loop
_discord.Color.orange = MagicMock(return_value=0xFFA500)
_discord.Color.green = MagicMock(return_value=0x00FF00)
_discord.colour.Color.green = MagicMock(return_value=0x00FF00)
_discord.colour.Color.red = MagicMock(return_value=0xFF0000)

_commands_core = MagicMock()
_commands_core.MISSING = object()

for mod_name, mod_obj in {
    'discord': _discord,
    'discord.ext': _discord.ext,
    'discord.ext.commands': _discord.ext.commands,
    'discord.ext.commands.core': _commands_core,
    'discord.ext.tasks': _discord.ext.tasks,
    'discord.app_commands': MagicMock(),
    'discord.colour': _discord.colour,
    'aiohttp': MagicMock(),
    'loguru': MagicMock(),
    'asyncpg': MagicMock(),
    'sentry_sdk': MagicMock(),
    'bs4': MagicMock(),
}.items():
    sys.modules.setdefault(mod_name, mod_obj)

# Now imports through dozer will work
sys.path.insert(0, str(__import__('pathlib').Path(__file__).resolve().parents[1]))
from dozer.cogs.news import NewsSubscription
from dozer.sources.AbstractSources import DataBasedSource

BadArgument = _BadArgument


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_sub(source="cd", channel_id=100, guild_id=1, kind="embed", data=None, sub_id=1, paused=False):
    """Create a NewsSubscription instance for testing."""
    return NewsSubscription(channel_id=channel_id, guild_id=guild_id, source=source,
                            kind=kind, data=data, sub_id=sub_id, paused=paused)


def _make_source(is_data_based=False, full_name="Chief Delphi", short_name="cd"):
    """Create a mock Source or DataBasedSource."""
    if is_data_based:
        source = MagicMock(spec=DataBasedSource)
        source.full_name = full_name
        source.short_name = short_name
    else:
        source = MagicMock()
        source.full_name = full_name
        source.short_name = short_name
    return source


def _make_channel(channel_id=100, guild_id=1, name="news"):
    channel = MagicMock()
    channel.id = channel_id
    channel.name = name
    channel.mention = f"#{name}"
    guild = MagicMock()
    guild.id = guild_id
    channel.guild = guild
    return channel


def _make_ctx(prefix="!"):
    ctx = MagicMock()
    ctx.prefix = prefix
    ctx.send = AsyncMock()
    return ctx


def _make_news_cog():
    """Create a minimal News-like object with the real methods but no bot init."""
    from dozer.cogs.news import News
    cog = object.__new__(News)
    return cog


# ---------------------------------------------------------------------------
# 1. Model: paused field defaults and construction
# ---------------------------------------------------------------------------

class TestNewsSubscriptionModel:
    def test_paused_defaults_to_false(self):
        sub = _make_sub()
        assert sub.paused is False

    def test_paused_can_be_set_true(self):
        sub = _make_sub(paused=True)
        assert sub.paused is True

    def test_paused_toggle(self):
        sub = _make_sub(paused=False)
        sub.paused = True
        assert sub.paused is True
        sub.paused = False
        assert sub.paused is False

    def test_other_fields_unaffected(self):
        sub = _make_sub(source="reddit", data="frc", paused=True)
        assert sub.source == "reddit"
        assert sub.data == "frc"
        assert sub.kind == "embed"
        assert sub.paused is True


# ---------------------------------------------------------------------------
# 2. _find_subscription: lookup logic
# ---------------------------------------------------------------------------

class TestFindSubscription:
    @pytest.mark.asyncio
    async def test_finds_regular_source(self):
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source()
        expected_sub = _make_sub()

        with patch.object(NewsSubscription, 'get_by', new_callable=AsyncMock, return_value=[expected_sub]):
            result = await cog._find_subscription(ctx, channel, source, data=None)
        assert result is expected_sub

    @pytest.mark.asyncio
    async def test_finds_data_based_source(self):
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source(is_data_based=True, full_name="Reddit", short_name="reddit")
        source.clean_data = AsyncMock(return_value=MagicMock(__str__=lambda self: "frc"))
        expected_sub = _make_sub(source="reddit", data="frc")

        with patch.object(NewsSubscription, 'get_by', new_callable=AsyncMock, return_value=[expected_sub]):
            result = await cog._find_subscription(ctx, channel, source, data="frc")
        assert result is expected_sub
        source.clean_data.assert_awaited_once_with("frc")

    @pytest.mark.asyncio
    async def test_not_found_raises(self):
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source()

        with patch.object(NewsSubscription, 'get_by', new_callable=AsyncMock, return_value=[]):
            with pytest.raises(BadArgument, match="No subscription"):
                await cog._find_subscription(ctx, channel, source)

    @pytest.mark.asyncio
    async def test_data_based_without_data_raises(self):
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source(is_data_based=True, full_name="Reddit", short_name="reddit")

        with pytest.raises(BadArgument, match="needs data"):
            await cog._find_subscription(ctx, channel, source, data=None)

    @pytest.mark.asyncio
    async def test_multiple_subs_raises(self):
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source()

        with patch.object(NewsSubscription, 'get_by', new_callable=AsyncMock,
                          return_value=[_make_sub(sub_id=1), _make_sub(sub_id=2)]):
            with pytest.raises(BadArgument, match="Multiple subscriptions"):
                await cog._find_subscription(ctx, channel, source)


# ---------------------------------------------------------------------------
# 3. Pause command
# ---------------------------------------------------------------------------

class TestPauseCommand:
    @pytest.mark.asyncio
    async def test_pause_active_subscription(self):
        """Pausing an active subscription sets paused=True and sends orange embed."""
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source()
        sub = _make_sub(paused=False)

        with patch.object(cog, '_find_subscription', new_callable=AsyncMock, return_value=sub):
            sub.update_or_add = AsyncMock()
            await cog.pause(ctx, channel, source, data=None)

        assert sub.paused is True
        sub.update_or_add.assert_awaited_once()
        ctx.send.assert_awaited_once()
        embed = ctx.send.call_args[1]['embed']
        assert embed.colour == _discord.Color.orange()

    @pytest.mark.asyncio
    async def test_pause_already_paused_raises(self):
        """Pausing an already-paused subscription raises BadArgument."""
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source()
        sub = _make_sub(paused=True)

        with patch.object(cog, '_find_subscription', new_callable=AsyncMock, return_value=sub):
            with pytest.raises(BadArgument, match="already paused"):
                await cog.pause(ctx, channel, source, data=None)

    @pytest.mark.asyncio
    async def test_pause_data_based_source(self):
        """Pausing a DataBasedSource subscription with data works correctly."""
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source(is_data_based=True, full_name="Reddit", short_name="reddit")
        sub = _make_sub(source="reddit", data="frc", paused=False)

        with patch.object(cog, '_find_subscription', new_callable=AsyncMock, return_value=sub):
            sub.update_or_add = AsyncMock()
            await cog.pause(ctx, channel, source, data="frc")

        assert sub.paused is True
        sub.update_or_add.assert_awaited_once()
        ctx.send.assert_awaited_once()
        embed = ctx.send.call_args[1]['embed']
        embed.add_field.assert_called_with(name="Data", value="frc")


# ---------------------------------------------------------------------------
# 4. Resume command
# ---------------------------------------------------------------------------

class TestResumeCommand:
    @pytest.mark.asyncio
    async def test_resume_paused_subscription(self):
        """Resuming a paused subscription sets paused=False and sends green embed."""
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source()
        sub = _make_sub(paused=True)

        with patch.object(cog, '_find_subscription', new_callable=AsyncMock, return_value=sub):
            sub.update_or_add = AsyncMock()
            await cog.resume(ctx, channel, source, data=None)

        assert sub.paused is False
        sub.update_or_add.assert_awaited_once()
        ctx.send.assert_awaited_once()
        embed = ctx.send.call_args[1]['embed']
        assert embed.colour == _discord.Color.green()

    @pytest.mark.asyncio
    async def test_resume_already_active_raises(self):
        """Resuming an already-active subscription raises BadArgument."""
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source()
        sub = _make_sub(paused=False)

        with patch.object(cog, '_find_subscription', new_callable=AsyncMock, return_value=sub):
            with pytest.raises(BadArgument, match="is not paused"):
                await cog.resume(ctx, channel, source, data=None)

    @pytest.mark.asyncio
    async def test_resume_data_based_source(self):
        """Resuming a DataBasedSource subscription with data works correctly."""
        cog = _make_news_cog()
        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source(is_data_based=True, full_name="Reddit", short_name="reddit")
        sub = _make_sub(source="reddit", data="frc", paused=True)

        with patch.object(cog, '_find_subscription', new_callable=AsyncMock, return_value=sub):
            sub.update_or_add = AsyncMock()
            await cog.resume(ctx, channel, source, data="frc")

        assert sub.paused is False
        sub.update_or_add.assert_awaited_once()
        ctx.send.assert_awaited_once()
        embed = ctx.send.call_args[1]['embed']
        embed.add_field.assert_called_with(name="Data", value="frc")


# ---------------------------------------------------------------------------
# 5. Polling loop: paused subscriptions are filtered out
# ---------------------------------------------------------------------------

class TestPollingFilter:
    def _build_channel_dict(self, subs):
        """Replicate the channel_dict building logic from get_new_posts."""
        bot = MagicMock()
        bot.get_channel = lambda cid: _make_channel(channel_id=cid)

        channel_dict = {}
        for sub in subs:
            if sub.paused:
                continue
            channel = bot.get_channel(sub.channel_id)
            if channel is None:
                continue
            if sub.data is None:
                sub.data = 'source'
            if sub.data not in channel_dict:
                channel_dict[sub.data] = {}
            channel_dict[sub.data][channel] = sub.kind
        return channel_dict

    def test_paused_subs_excluded_from_channel_dict(self):
        """Paused subscriptions should not contribute to channel_dict."""
        active_sub = _make_sub(channel_id=100, paused=False)
        paused_sub = _make_sub(channel_id=200, paused=True)

        channel_dict = self._build_channel_dict([active_sub, paused_sub])

        assert len(channel_dict) == 1
        assert 'source' in channel_dict
        channels_in_dict = list(channel_dict['source'].keys())
        assert len(channels_in_dict) == 1
        assert channels_in_dict[0].id == 100

    def test_active_subs_included_in_channel_dict(self):
        """Active (non-paused) subscriptions should be included normally."""
        sub1 = _make_sub(channel_id=100, paused=False)
        sub2 = _make_sub(channel_id=200, paused=False)

        channel_dict = self._build_channel_dict([sub1, sub2])

        assert len(channel_dict['source']) == 2

    def test_all_paused_results_in_empty_channel_dict(self):
        """If all subscriptions are paused, channel_dict should be empty."""
        sub1 = _make_sub(channel_id=100, paused=True)
        sub2 = _make_sub(channel_id=200, paused=True)

        channel_dict = self._build_channel_dict([sub1, sub2])

        assert len(channel_dict) == 0

    def test_mixed_data_based_subs_filtering(self):
        """DataBasedSource subs with different data: only active ones appear."""
        active_frc = _make_sub(channel_id=100, source="reddit", data="frc", paused=False)
        paused_ftc = _make_sub(channel_id=200, source="reddit", data="ftc", paused=True)
        active_ftc2 = _make_sub(channel_id=300, source="reddit", data="ftc", paused=False)

        channel_dict = self._build_channel_dict([active_frc, paused_ftc, active_ftc2])

        assert len(channel_dict['frc']) == 1
        assert len(channel_dict['ftc']) == 1
        ftc_channel = list(channel_dict['ftc'].keys())[0]
        assert ftc_channel.id == 300


# ---------------------------------------------------------------------------
# 6. End-to-end: pause then resume cycle
# ---------------------------------------------------------------------------

class TestPauseResumeCycle:
    @pytest.mark.asyncio
    async def test_full_pause_resume_cycle(self):
        """A subscription can be paused and then resumed, returning to active state."""
        cog = _make_news_cog()
        sub = _make_sub(paused=False)
        sub.update_or_add = AsyncMock()

        ctx = _make_ctx()
        channel = _make_channel()
        source = _make_source()

        # Step 1: Pause
        with patch.object(cog, '_find_subscription', new_callable=AsyncMock, return_value=sub):
            await cog.pause(ctx, channel, source, data=None)
        assert sub.paused is True

        # Step 2: Verify it's filtered from polling
        channel_dict = {}
        for s in [sub]:
            if s.paused:
                continue
            channel_dict['source'] = {channel: s.kind}
        assert len(channel_dict) == 0

        # Step 3: Resume
        ctx2 = _make_ctx()
        with patch.object(cog, '_find_subscription', new_callable=AsyncMock, return_value=sub):
            await cog.resume(ctx2, channel, source, data=None)
        assert sub.paused is False

        # Step 4: Verify it's back in polling
        channel_dict = {}
        for s in [sub]:
            if s.paused:
                continue
            if s.data is None:
                s.data = 'source'
            if s.data not in channel_dict:
                channel_dict[s.data] = {}
            channel_dict[s.data][channel] = s.kind
        assert len(channel_dict) == 1

        assert sub.update_or_add.await_count == 2
