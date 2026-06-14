"""Verification tests for news subscription bug fixes.

Covers two main fix areas:
1. Source alias recognition: class-level aliases are merged with full_name/short_name
2. Channel filtering: list_subscriptions uses the user-specified channel, not ctx.channel

Runs without project dependencies by loading modules directly and inspecting source code.
"""
import ast
import importlib.util
import os
import sys
import unittest
from unittest.mock import MagicMock

PROJECT_ROOT = os.path.join(os.path.dirname(__file__), '..')


def _load_module_from_file(name, filepath):
    """Load a single Python module from file path, bypassing package __init__."""
    # Pre-populate mock for aiohttp and discord.ext.commands.BadArgument
    mock_aiohttp = MagicMock()
    mock_commands = MagicMock()
    mock_commands.BadArgument = type('BadArgument', (Exception,), {})

    sys.modules.setdefault('aiohttp', mock_aiohttp)
    sys.modules.setdefault('discord', MagicMock())
    sys.modules.setdefault('discord.ext', MagicMock())
    sys.modules.setdefault('discord.ext.commands', mock_commands)

    spec = importlib.util.spec_from_file_location(name, filepath)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


# Load AbstractSources directly (no package chain)
abstract_mod = _load_module_from_file(
    'dozer.sources.AbstractSources',
    os.path.join(PROJECT_ROOT, 'dozer', 'sources', 'AbstractSources.py')
)
Source = abstract_mod.Source
DataBasedSource = abstract_mod.DataBasedSource


# ============================================================
# Create concrete test subclasses mirroring real source definitions
# ============================================================
class FakeBasicSource(Source):
    """Source with no extra aliases (like FRCBlogPosts)."""
    full_name = "FRC Blog Posts"
    short_name = "frc"


class FakeSpectrumSource(Source):
    """Source with a STRING alias (like SpectrumBlog: aliases = '3847')."""
    full_name = "Spectrum Blog"
    short_name = "spectrum"
    aliases = '3847'


class FakeJVNSource(Source):
    """Source with a TUPLE of extra aliases (like JVNBlog)."""
    full_name = "JVN's Blog"
    short_name = "jvn"
    aliases = ('148', 'robowranglers')


class FakeNoExtraSource(Source):
    """Source with empty aliases tuple (default)."""
    full_name = "Test Source"
    short_name = "test"


# ============================================================
# Test Suite 1: Source Alias Recognition
# ============================================================
class TestSourceAliases(unittest.TestCase):
    """Verify that source aliases include full_name, short_name, AND class-level extras."""

    def _make(self, cls):
        return cls(aiohttp_session=MagicMock(), bot=MagicMock())

    def test_basic_source_has_full_and_short_name(self):
        src = self._make(FakeBasicSource)
        self.assertIn("FRC Blog Posts", src.aliases)
        self.assertIn("frc", src.aliases)
        self.assertEqual(len(src.aliases), 2)

    def test_spectrum_string_alias_included(self):
        """SpectrumBlog defines aliases='3847' (a string, not tuple).
        After fix, '3847' should be recognized alongside full_name and short_name."""
        src = self._make(FakeSpectrumSource)
        self.assertIn("Spectrum Blog", src.aliases, "full_name missing")
        self.assertIn("spectrum", src.aliases, "short_name missing")
        self.assertIn("3847", src.aliases, "class-level string alias '3847' must be included")
        self.assertEqual(len(src.aliases), 3)

    def test_spectrum_alias_is_exact_match_not_substring(self):
        """Verify '3847' is a distinct tuple element, not a substring match."""
        src = self._make(FakeSpectrumSource)
        self.assertIsInstance(src.aliases, tuple)
        self.assertTrue(
            any(a == "3847" for a in src.aliases),
            "alias '3847' should be an exact element"
        )
        # Substring should NOT match via 'in' on the tuple
        self.assertNotIn("384", src.aliases, "'384' substring must not match")

    def test_jvn_extra_aliases_included(self):
        """JVNBlog defines aliases=('148', 'robowranglers'). Both should be present."""
        src = self._make(FakeJVNSource)
        self.assertIn("JVN's Blog", src.aliases, "full_name missing")
        self.assertIn("jvn", src.aliases, "short_name missing")
        self.assertIn("148", src.aliases, "class-level alias '148' must be included")
        self.assertIn("robowranglers", src.aliases, "class-level alias 'robowranglers' must be included")
        self.assertEqual(len(src.aliases), 4)

    def test_no_duplicate_aliases(self):
        for cls in [FakeBasicSource, FakeSpectrumSource, FakeJVNSource, FakeNoExtraSource]:
            src = self._make(cls)
            self.assertEqual(
                len(src.aliases), len(set(src.aliases)),
                f"Duplicate aliases in {cls.__name__}: {src.aliases}"
            )

    def test_aliases_always_tuple(self):
        for cls in [FakeBasicSource, FakeSpectrumSource, FakeJVNSource, FakeNoExtraSource]:
            src = self._make(cls)
            self.assertIsInstance(src.aliases, tuple, f"{cls.__name__}: aliases must be a tuple")

    def test_empty_class_aliases_still_works(self):
        src = self._make(FakeNoExtraSource)
        self.assertEqual(src.aliases, ("Test Source", "test"))


# ============================================================
# Test Suite 2: Channel Filtering in list_subscriptions (source code analysis)
# ============================================================
class TestChannelFiltering(unittest.TestCase):
    """Verify that list_subscriptions uses the correct channel_id for queries."""

    @classmethod
    def setUpClass(cls):
        news_path = os.path.join(PROJECT_ROOT, 'dozer', 'cogs', 'news.py')
        with open(news_path, 'r') as f:
            cls.source_code = f.read()
        cls.tree = ast.parse(cls.source_code)

        # Find the list_subscriptions function
        cls.func_source = None
        cls.func_node = None
        for node in ast.walk(cls.tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                if node.name == 'list_subscriptions':
                    cls.func_node = node
                    cls.func_source = ast.get_source_segment(cls.source_code, node)
                    break

    def test_function_found(self):
        self.assertIsNotNone(self.func_source, "list_subscriptions function not found")

    def test_uses_channel_id_not_ctx_channel_id(self):
        """When channel is provided, query must use channel.id, not ctx.channel.id."""
        self.assertNotIn(
            "ctx.channel.id",
            self.func_source,
            "BUG: still uses ctx.channel.id instead of channel.id"
        )

    def test_channel_arg_correctly_used(self):
        """The channel parameter should be used for channel_id when provided."""
        self.assertIn(
            "channel_id=channel.id",
            self.func_source,
            "Should use channel.id for the query"
        )

    def test_no_variable_shadowing(self):
        """The loop variable should not shadow the 'channel' parameter."""
        for node in ast.walk(self.func_node):
            if isinstance(node, ast.For):
                for child in ast.walk(node):
                    if isinstance(child, ast.Assign):
                        for target in child.targets:
                            if isinstance(target, ast.Name) and target.id == 'channel':
                                self.fail(
                                    "Loop variable 'channel' shadows the parameter"
                                )

    def test_embed_title_reflects_channel_filter(self):
        """When filtering by channel, embed title should mention the channel name."""
        self.assertIn("channel.name", self.func_source,
                      "Embed title should include channel.name when filtering")

    def test_subscription_kind_displayed(self):
        """Subscription display should show the kind (plain/embed) for each entry."""
        self.assertIn("sub.kind", self.func_source,
                      "Display should include the kind field")

    def test_channel_none_check_before_query(self):
        self.assertIn("if channel is not None", self.func_source,
                      "Should check channel is not None before using channel.id")


if __name__ == '__main__':
    unittest.main(verbosity=2)
