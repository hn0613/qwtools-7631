"""
Verification tests for the news subscription fixes.

Covers two main lines:
  1. Source alias preservation (class-level aliases not overwritten by __init__)
  2. Channel-filtered subscription listing (uses the correct channel parameter)

Run with: python3 -m unittest tests/test_news_fixes.py -v
"""

import sys
import types
import os
import unittest
from unittest.mock import MagicMock

# ---------------------------------------------------------------------------
# We need to stub out external deps (discord, aiohttp, loguru, etc.) so that
# the source files can be imported without installing the full runtime.
# We also need to avoid triggering dozer/__init__.py which imports Dozer bot.
# ---------------------------------------------------------------------------

# --- discord stubs -----------------------------------------------------------
_discord = types.ModuleType('discord')
_discord.colour = types.ModuleType('discord.colour')


class _FakeColor:
    def __init__(self, *a, **kw):
        pass

    @classmethod
    def blurple(cls):
        return cls()

    @classmethod
    def dark_blue(cls):
        return cls()

    @classmethod
    def orange(cls):
        return cls()

    @classmethod
    def purple(cls):
        return cls()

    @classmethod
    def dark_orange(cls):
        return cls()

    @classmethod
    def green(cls):
        return cls()

    @classmethod
    def red(cls):
        return cls()

    @classmethod
    def from_rgb(cls, *a):
        return cls()


_discord.colour.Color = _FakeColor
_discord.Embed = MagicMock()
_discord.TextChannel = type("TextChannel", (), {})
_discord.abc = types.ModuleType('discord.abc')
_discord.abc.GuildChannel = type("GuildChannel", (), {})

sys.modules['discord'] = _discord
sys.modules['discord.colour'] = _discord.colour
sys.modules['discord.abc'] = _discord.abc

_discord_ext = types.ModuleType('discord.ext')
sys.modules['discord.ext'] = _discord_ext

_discord_ext_commands = types.ModuleType('discord.ext.commands')
_discord_ext_commands.BadArgument = type("BadArgument", (Exception,), {})
sys.modules['discord.ext.commands'] = _discord_ext_commands

_discord_ext_tasks = types.ModuleType('discord.ext.tasks')
_discord_ext_tasks.loop = MagicMock(return_value=lambda f: f)
sys.modules['discord.ext.tasks'] = _discord_ext_tasks

# --- aiohttp stub ------------------------------------------------------------
_aiohttp = types.ModuleType('aiohttp')
_aiohttp.ClientSession = MagicMock()
sys.modules['aiohttp'] = _aiohttp

# --- loguru stub -------------------------------------------------------------
_loguru = types.ModuleType('loguru')
_loguru.logger = MagicMock()
sys.modules['loguru'] = _loguru

# --- dateutil stub -----------------------------------------------------------
_dateutil = types.ModuleType('dateutil')
_dateutil_parser = types.ModuleType('dateutil.parser')
_dateutil_parser.isoparse = MagicMock()
sys.modules['dateutil'] = _dateutil
sys.modules['dateutil.parser'] = _dateutil_parser

# --- Stub dozer package to avoid triggering __init__.py's bot import ---------
# We create a minimal dozer package that only has what we need.
_project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _project_root not in sys.path:
    sys.path.insert(0, _project_root)

# Create the dozer package manually without running its __init__.py
_dozer_pkg = types.ModuleType('dozer')
_dozer_pkg.__path__ = [os.path.join(_project_root, 'dozer')]
_dozer_pkg.__package__ = 'dozer'
sys.modules['dozer'] = _dozer_pkg

# Create dozer.sources package manually
_sources_pkg = types.ModuleType('dozer.sources')
_sources_pkg.__path__ = [os.path.join(_project_root, 'dozer', 'sources')]
_sources_pkg.__package__ = 'dozer.sources'
sys.modules['dozer.sources'] = _sources_pkg

# Now import the actual source files
import importlib.util


def _load_module(name, filepath, parent_pkg=None):
    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    if parent_pkg:
        sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


_abs_sources = _load_module(
    'dozer.sources.AbstractSources',
    os.path.join(_project_root, 'dozer', 'sources', 'AbstractSources.py'),
)
_rss_sources = _load_module(
    'dozer.sources.RSSSources',
    os.path.join(_project_root, 'dozer', 'sources', 'RSSSources.py'),
)

Source = _abs_sources.Source
DataBasedSource = _abs_sources.DataBasedSource
SpectrumBlog = _rss_sources.SpectrumBlog
JVNBlog = _rss_sources.JVNBlog
CDLatest = _rss_sources.CDLatest
FRCBlogPosts = _rss_sources.FRCBlogPosts
FTCQA = _rss_sources.FTCQA
FRCQA = _rss_sources.FRCQA
TBABlog = _rss_sources.TBABlog
FTCBlogPosts = _rss_sources.FTCBlogPosts
FTCForum = _rss_sources.FTCForum


# ============================================================================
# TEST CASES
# ============================================================================

class TestSourceAliasPreservation(unittest.TestCase):
    """Verify that Source.__init__ preserves class-level aliases."""

    def _make_instance(self, cls):
        session = MagicMock()
        bot = MagicMock()
        return cls(aiohttp_session=session, bot=bot)

    # --- SpectrumBlog: class defines aliases = '3847' -----------------------

    def test_spectrum_preserves_custom_alias_3847(self):
        """SpectrumBlog declares aliases = '3847'. After init, '3847' must
        still be in the instance's aliases tuple."""
        instance = self._make_instance(SpectrumBlog)
        self.assertIn('3847', instance.aliases,
                       "Custom alias '3847' was lost after __init__")

    def test_spectrum_still_has_full_and_short_name(self):
        instance = self._make_instance(SpectrumBlog)
        self.assertIn('Spectrum Blog', instance.aliases)
        self.assertIn('spectrum', instance.aliases)

    # --- JVNBlog: class defines aliases = '148', 'robowranglers' ------------

    def test_jvn_preserves_custom_aliases(self):
        """JVNBlog declares aliases = '148', 'robowranglers'. Both must survive."""
        instance = self._make_instance(JVNBlog)
        self.assertIn('148', instance.aliases,
                       "Custom alias '148' was lost after __init__")
        self.assertIn('robowranglers', instance.aliases,
                       "Custom alias 'robowranglers' was lost after __init__")

    def test_jvn_still_has_full_and_short_name(self):
        instance = self._make_instance(JVNBlog)
        self.assertIn("JVN's Blog", instance.aliases)
        self.assertIn('jvn', instance.aliases)

    # --- Sources WITHOUT custom aliases should still work --------------------

    def test_cd_has_default_aliases(self):
        instance = self._make_instance(CDLatest)
        self.assertIn('Chief Delphi', instance.aliases)
        self.assertIn('cd', instance.aliases)
        self.assertEqual(len(instance.aliases), 2)

    def test_frc_blog_has_default_aliases(self):
        instance = self._make_instance(FRCBlogPosts)
        self.assertIn('FRC Blog Posts', instance.aliases)
        self.assertIn('frc', instance.aliases)

    def test_tba_blog_has_default_aliases(self):
        instance = self._make_instance(TBABlog)
        self.assertIn('The Blue Alliance Blog', instance.aliases)
        self.assertIn('tba-blog', instance.aliases)

    def test_ftc_qa_has_default_aliases(self):
        instance = self._make_instance(FTCQA)
        self.assertIn('FTC Q&A Answers', instance.aliases)
        self.assertIn('ftc-qa', instance.aliases)

    def test_ftcforum_has_default_aliases(self):
        instance = self._make_instance(FTCForum)
        self.assertIn('FTC Forum Posts', instance.aliases)
        self.assertIn('ftcforum', instance.aliases)

    # --- Consistency: convert() uses same aliases as list_sources displays ---

    def test_convert_would_match_custom_alias(self):
        """'3847' should be findable in aliases, matching convert() behavior."""
        instance = self._make_instance(SpectrumBlog)
        self.assertIn('3847', instance.aliases)

    def test_convert_matches_short_name(self):
        instance = self._make_instance(CDLatest)
        self.assertIn('cd', instance.aliases)

    def test_convert_matches_full_name(self):
        instance = self._make_instance(CDLatest)
        self.assertIn('Chief Delphi', instance.aliases)


class TestListSubscriptionsChannelFilter(unittest.TestCase):
    """Verify that list_subscriptions uses the correct channel ID."""

    def _get_list_subscriptions_source(self):
        with open(os.path.join(_project_root, 'dozer', 'cogs', 'news.py'), 'r') as f:
            source_code = f.read()
        func_start = source_code.find('async def list_subscriptions')
        self.assertGreater(func_start, 0, "list_subscriptions not found")
        return source_code[func_start:func_start + 2500]

    def test_uses_channel_parameter_not_ctx_channel(self):
        """The source code must reference channel.id (the parameter) in the
        filtered query, not ctx.channel.id (the invoking channel)."""
        func_body = self._get_list_subscriptions_source()

        self.assertIn('channel_id=channel.id', func_body,
                       "Should use channel.id (the parameter)")
        self.assertNotIn('channel_id=ctx.channel.id', func_body,
                         "Still uses ctx.channel.id instead of channel.id")


class TestListSubscriptionsDisplay(unittest.TestCase):
    """Verify display improvements in list_subscriptions."""

    def _get_list_subscriptions_source(self):
        with open(os.path.join(_project_root, 'dozer', 'cogs', 'news.py'), 'r') as f:
            source_code = f.read()
        func_start = source_code.find('async def list_subscriptions')
        return source_code[func_start:func_start + 2500]

    def test_shows_full_source_name(self):
        func_body = self._get_list_subscriptions_source()
        self.assertIn('self.sources.get(sub.source)', func_body,
                       "Display should resolve source short_name to full_name")
        self.assertIn('full_name', func_body)

    def test_handles_missing_channels(self):
        func_body = self._get_list_subscriptions_source()
        self.assertIn('missing_channels', func_body,
                       "Should collect subscriptions with missing channels")

    def test_handles_unavailable_sources(self):
        func_body = self._get_list_subscriptions_source()
        self.assertIn('source unavailable', func_body)

    def test_channel_specific_title(self):
        func_body = self._get_list_subscriptions_source()
        self.assertIn('channel.name', func_body,
                       "Channel-filtered view should include channel name in title")

    def test_no_results_message_is_channel_aware(self):
        """When filtering by channel and no results, the message should
        mention the specific channel."""
        func_body = self._get_list_subscriptions_source()
        self.assertIn('channel.mention', func_body,
                       "Empty-result message should mention the specific channel")

    def test_shows_subscription_kind(self):
        """Display should show the kind (embed/plain) for each subscription."""
        func_body = self._get_list_subscriptions_source()
        self.assertIn('sub.kind', func_body,
                       "Display should show subscription kind")


class TestSourceAliasesDisplayedConsistently(unittest.TestCase):
    """Verify that list_sources and convert() see the same aliases."""

    def test_list_sources_shows_instance_aliases(self):
        instance = SpectrumBlog(aiohttp_session=MagicMock(), bot=MagicMock())
        aliases_str = ", ".join(instance.aliases)
        self.assertIn('3847', aliases_str)
        self.assertIn('Spectrum Blog', aliases_str)
        self.assertIn('spectrum', aliases_str)

    def test_all_sources_have_consistent_aliases(self):
        for cls in [CDLatest, FRCBlogPosts, TBABlog, FRCQA, FTCQA,
                    FTCBlogPosts, FTCForum, SpectrumBlog]:
            instance = cls(aiohttp_session=MagicMock(), bot=MagicMock())
            self.assertIn(instance.full_name, instance.aliases,
                          f"{cls.__name__} missing full_name in aliases")
            self.assertIn(instance.short_name, instance.aliases,
                          f"{cls.__name__} missing short_name in aliases")


class TestEdgeCases(unittest.TestCase):
    """Edge case coverage for the fixes."""

    def test_string_alias_normalized_to_tuple(self):
        """If a class defines aliases as a plain string, __init__ should
        normalize it."""
        instance = SpectrumBlog(aiohttp_session=MagicMock(), bot=MagicMock())
        self.assertIsInstance(instance.aliases, tuple)
        self.assertIn('3847', instance.aliases)

    def test_no_duplicate_aliases(self):
        """If a class explicitly lists full_name or short_name in aliases,
        they should not appear twice."""

        class DupSource(Source):
            full_name = "Dup Source"
            short_name = "dup"
            aliases = ('Dup Source', 'dup', 'extra')
            description = "Test"
            base_url = ""

        instance = DupSource(aiohttp_session=MagicMock(), bot=MagicMock())
        for alias in set(instance.aliases):
            count = instance.aliases.count(alias)
            self.assertEqual(count, 1,
                             f"Alias '{alias}' appears {count} times")

    def test_empty_class_aliases_still_gives_full_and_short(self):
        class PlainSource(Source):
            full_name = "Plain Source"
            short_name = "plain"
            description = "Test"
            base_url = ""

        instance = PlainSource(aiohttp_session=MagicMock(), bot=MagicMock())
        self.assertEqual(set(instance.aliases), {"Plain Source", "plain"})

    def test_tuple_class_aliases_preserved(self):
        """If a class defines aliases as a tuple, all entries survive."""

        class TupleAliasSource(Source):
            full_name = "Tuple Source"
            short_name = "tup"
            aliases = ('Tuple Source', 'tup', 'nickname', '42')
            description = "Test"
            base_url = ""

        instance = TupleAliasSource(aiohttp_session=MagicMock(), bot=MagicMock())
        for alias in ('Tuple Source', 'tup', 'nickname', '42'):
            self.assertIn(alias, instance.aliases)


if __name__ == '__main__':
    unittest.main()
