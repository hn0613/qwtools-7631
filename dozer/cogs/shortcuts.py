"""Adds simple text-shortcuts to the bot, with optional dynamic suffix support."""

import time

import discord
from discord.ext import commands
from discord.ext.commands import BadArgument, guild_only, has_permissions

from dozer.context import DozerContext
from dozer import utils as dozer_utils
from ._utils import *
from .. import db
from ..db import *


class Shortcuts(Cog):
    """Adds simple text-shortcuts to the bot, with optional dynamic suffix support."""
    MAX_LEN = 20
    MAX_SUFFIX_LEN = 1800  # cap appended text so total payload stays within Discord's 2000-char limit
    TRIGGER_COOLDOWN = 3.0  # seconds – per (guild, channel, user) to prevent spam

    def __init__(self, bot):
        """cog init"""
        super().__init__(bot)
        self.settings_cache = db.ConfigCache(ShortcutSetting)
        self.cache = db.ConfigCache(ShortcutEntry)
        # (guild_id, channel_id, user_id) -> last trigger timestamp
        self._last_trigger: dict = {}

    # ------------------------------------------------------------------
    # Helper
    # ------------------------------------------------------------------
    @staticmethod
    def _normalize_name(name: str) -> str:
        """Lower-case + strip so that 'Hello' and 'hello ' map to the same shortcut."""
        return name.strip().lower()

    # ------------------------------------------------------------------
    # Management commands
    # ------------------------------------------------------------------
    @guild_only()
    @has_permissions(manage_messages=True)
    @group(invoke_without_command=True)
    async def shortcuts(self, ctx):
        """
        Display shortcut information and usage hints.
        """
        settings: ShortcutSetting = await self.settings_cache.query_one(guild_id=ctx.guild.id)

        if settings is None:
            raise BadArgument("This server has no shortcut configuration. Use `shortcuts setprefix <prefix>` first.")

        e = discord.Embed()
        e.title = "Server shortcut configuration"
        e.add_field(name="Shortcut prefix", value=settings.prefix or "[unset]")
        e.add_field(
            name="Dynamic suffix",
            value=(
                f"Users can append extra text after a shortcut name and it will be included in the reply.\n"
                f"Example: `{settings.prefix}hello John` → template text + ` John`"
            ),
            inline=False,
        )
        e.add_field(name="Trigger cooldown", value=f"{self.TRIGGER_COOLDOWN:g}s per user per channel", inline=False)
        await ctx.send(embed=e)

    # ---- setprefix ---------------------------------------------------
    @guild_only()
    @has_permissions(manage_messages=True)
    @shortcuts.command()
    async def setprefix(self, ctx, prefix):
        """Set the prefix used to trigger shortcuts on this server."""
        setting: ShortcutSetting = await self.settings_cache.query_one(guild_id=ctx.guild.id)

        if setting:
            setting.prefix = prefix
        else:
            setting = ShortcutSetting(guild_id=ctx.guild.id, prefix=prefix)

        await setting.update_or_add()
        self.settings_cache.invalidate_entry(guild_id=ctx.guild.id)

        await ctx.send(f"Set shortcut prefix to: `{prefix}`")

    # ---- set / add ---------------------------------------------------
    @guild_only()
    @has_permissions(manage_messages=True)
    @shortcuts.command(aliases=["add"])
    async def set(self, ctx, cmd_name, *, cmd_msg):
        """Set the reply template for a shortcut.

        The template is the fixed part of the reply.  When users trigger the
        shortcut they may append extra text which will be placed after the
        template automatically.
        """
        settings: ShortcutSetting = await self.settings_cache.query_one(guild_id=ctx.guild.id)
        if settings is None:
            raise BadArgument("Set a prefix first! Use `shortcuts setprefix <prefix>`.")

        normalised = self._normalize_name(cmd_name)
        if not normalised:
            raise BadArgument("Shortcut name cannot be empty or whitespace.")
        if len(normalised) > self.MAX_LEN:
            raise BadArgument(f"Shortcut names can only be up to {self.MAX_LEN} characters long.")
        if not cmd_msg or not cmd_msg.strip():
            raise BadArgument("Shortcut reply template cannot be empty.")

        # Check for an existing entry (case-insensitive) to avoid duplicates
        # like 'Hello' vs 'hello'.
        existing: ShortcutEntry = await self.cache.query_one(guild_id=ctx.guild.id, name=normalised)

        action = "Updated"
        if existing:
            existing.value = cmd_msg
            ent = existing
        else:
            ent = ShortcutEntry(guild_id=ctx.guild.id, name=normalised, value=cmd_msg)
            action = "Created"

        await ent.update_or_add()
        self.cache.invalidate_entry(guild_id=ctx.guild.id, name=normalised)

        await ctx.send(
            f"{action} shortcut `{settings.prefix}{normalised}` successfully.\n"
            f"Users can trigger it with `{settings.prefix}{normalised}` or "
            f"`{settings.prefix}{normalised} <extra text>` to append context."
        )

    set.example_usage = """
    `{prefix}shortcuts set hello Hello, World!` – creates/updates `!hello`.
    `{prefix}shortcuts set rules Please read the rules in #rules.` – creates `!rules`.
    Trigger with `!hello` or `!hello @user` to append extra text.
    """

    # ---- remove ------------------------------------------------------
    @guild_only()
    @has_permissions(manage_messages=True)
    @shortcuts.command()
    async def remove(self, ctx, cmd_name):
        """Remove a shortcut by name (case-insensitive)."""
        settings: ShortcutSetting = await self.settings_cache.query_one(guild_id=ctx.guild.id)
        normalised = self._normalize_name(cmd_name)
        prefix = settings.prefix if settings else ""

        ent: ShortcutEntry = await self.cache.query_one(guild_id=ctx.guild.id, name=normalised)

        if ent:
            await ShortcutEntry.delete(guild_id=ctx.guild.id, name=ent.name)
            self.cache.invalidate_entry(guild_id=ctx.guild.id, name=normalised)
            await ctx.send(f"Removed shortcut `{prefix}{ent.name}` successfully.")
        else:
            await ctx.send(f"No shortcut named `{cmd_name}` found on this server.")

    remove.example_usage = """
    `{prefix}shortcuts remove hello` – removes the `hello` shortcut.
    """

    # ---- list --------------------------------------------------------
    @guild_only()
    @shortcuts.command()
    async def list(self, ctx):
        """List all shortcuts for the server."""
        settings: ShortcutSetting = await self.settings_cache.query_one(guild_id=ctx.guild.id)

        ents: List[ShortcutEntry] = await ShortcutEntry.get_by(guild_id=ctx.guild.id)

        if not ents:
            await ctx.send("No shortcuts configured for this server.")
            return

        prefix = settings.prefix if settings else ""
        embed = None
        for i, e in enumerate(ents):
            if i % 20 == 0:
                if embed is not None:
                    await ctx.send(embed=embed)
                embed = discord.Embed()
                embed.title = "Shortcuts for this server"
                embed.set_footer(text=f"Tip: append extra text after a shortcut name to include it in the reply.")

            template_preview = e.value[:1024]
            embed.add_field(
                name=f"{prefix}{e.name}",
                value=f"{template_preview}\n_…or `{prefix}{e.name} <extra>` to append text_",
            )

        if embed and embed.fields:
            await ctx.send(embed=embed)

    list.example_usage = """
    `{prefix}shortcuts list` – lists all configured shortcuts.
    """

    # ---- info --------------------------------------------------------
    @guild_only()
    @shortcuts.command()
    async def info(self, ctx, cmd_name):
        """Show details for a single shortcut."""
        settings: ShortcutSetting = await self.settings_cache.query_one(guild_id=ctx.guild.id)
        if settings is None:
            raise BadArgument("This server has no shortcut configuration.")

        normalised = self._normalize_name(cmd_name)
        prefix = settings.prefix or ""
        ent: ShortcutEntry = await self.cache.query_one(guild_id=ctx.guild.id, name=normalised)

        if not ent:
            await ctx.send(f"No shortcut named `{cmd_name}` found on this server.")
            return

        e = discord.Embed()
        e.title = f"Shortcut: {prefix}{ent.name}"
        e.add_field(name="Reply template", value=ent.value[:1024], inline=False)
        e.add_field(
            name="Usage",
            value=(
                f"• `{prefix}{ent.name}` – sends the template as-is\n"
                f"• `{prefix}{ent.name} <extra text>` – sends template + extra text"
            ),
            inline=False,
        )
        await ctx.send(embed=e)

    info.example_usage = """
    `{prefix}shortcuts info hello` – shows details for the `hello` shortcut.
    """

    # ------------------------------------------------------------------
    # Message listener – the actual trigger logic
    # ------------------------------------------------------------------
    @Cog.listener()
    async def on_message(self, msg):
        """Scan incoming messages for shortcut triggers."""
        # --- Edge-case guards -----------------------------------------
        if not msg.guild or msg.author.bot:
            return

        setting = await self.settings_cache.query_one(guild_id=msg.guild.id)
        if setting is None or not setting.prefix:
            return

        content = msg.content
        prefix = setting.prefix

        # Must start with the configured prefix
        if len(content) < len(prefix) or not content.startswith(prefix):
            return

        # Everything after the prefix, stripped of leading/trailing whitespace
        remainder = content[len(prefix):].strip()
        if not remainder:
            # User typed only the prefix (e.g. "!") – nothing to match
            return

        remainder_lower = remainder.lower()

        shortcuts = await ShortcutEntry.get_by(guild_id=msg.guild.id)
        if not shortcuts:
            return

        # --- Rate limit ---------------------------------------------------
        if self._is_on_cooldown(msg):
            return

        # --- Match against registered shortcuts -----------------------
        # We match shortcut names case-insensitively.  A trigger is valid when:
        #   remainder_lower == name.lower()           (exact, no suffix)
        #   remainder_lower starts with name + " "    (name followed by extra text)
        # This keeps exact-match behaviour identical to the original code.
        for shortcut in shortcuts:
            name_lower = shortcut.name.lower()
            if remainder_lower == name_lower:
                # Exact match – no extra text appended
                await self._send_reply(msg, shortcut.value)
                await self._record_trigger(msg)
                return
            if remainder_lower.startswith(name_lower + " "):
                # Dynamic match – user supplied extra text after the name
                raw_suffix = remainder[len(name_lower):]  # preserves user casing for the suffix
                suffix = raw_suffix.strip()

                if not suffix:
                    # Whitespace-only suffix – treat as exact match
                    await self._send_reply(msg, shortcut.value)
                    await self._record_trigger(msg)
                    return

                # Safety: strip mass-mention pings (@everyone / @here) from the
                # user-supplied suffix so it cannot be used to ping the whole server.
                safe_suffix = dozer_utils.clean(msg, suffix, mass=True, member=False, role=False, channel=False)

                # Enforce suffix length cap
                if len(safe_suffix) > self.MAX_SUFFIX_LEN:
                    safe_suffix = safe_suffix[: self.MAX_SUFFIX_LEN] + "…"

                # Add a separator space when the template doesn't already end
                # with whitespace so the suffix isn't glued to the last word.
                separator = "" if shortcut.value and shortcut.value[-1] in " \t\n" else " "
                reply = shortcut.value + separator + safe_suffix

                # Final safety: if the combined reply exceeds Discord's limit, truncate
                if len(reply) > 2000:
                    reply = reply[:1997] + "…"

                await self._send_reply(msg, reply)
                await self._record_trigger(msg)
                return

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    async def _send_reply(self, msg, text: str):
        """Send a shortcut reply, bypassing DozerContext.clean (we already cleaned)."""
        await msg.channel.send(text)

    async def _record_trigger(self, msg):
        """Record the current timestamp for rate-limit tracking."""
        key = (msg.guild.id, msg.channel.id, msg.author.id)
        self._last_trigger[key] = time.monotonic()

    def _is_on_cooldown(self, msg) -> bool:
        """Return True if the user is still within the trigger cooldown window."""
        key = (msg.guild.id, msg.channel.id, msg.author.id)
        last = self._last_trigger.get(key)
        if last is None:
            return False
        return (time.monotonic() - last) < self.TRIGGER_COOLDOWN


async def setup(bot):
    """Adds the shortcuts cog to the main bot project."""
    await bot.add_cog(Shortcuts(bot))


# ======================================================================
# Database Tables
# ======================================================================


class ShortcutSetting(db.DatabaseTable):
    """Tracks the shortcut prefix for each guild."""
    __tablename__ = 'shortcut_settings'
    __uniques__ = "guild_id"

    @classmethod
    async def initial_create(cls):
        """Create the table in the database"""
        async with db.Pool.acquire() as conn:
            await conn.execute(f"""
            CREATE TABLE {cls.__tablename__} (
            guild_id bigint PRIMARY KEY NOT NULL,
            prefix varchar NOT NULL
            )""")

    def __init__(self, guild_id: int, prefix: str):
        super().__init__()
        self.guild_id = guild_id
        self.prefix = prefix

    @classmethod
    async def get_by(cls, **kwargs):
        results = await super().get_by(**kwargs)
        result_list = []
        for result in results:
            obj = ShortcutSetting(guild_id=result.get("guild_id"), prefix=result.get("prefix"))
            result_list.append(obj)
        return result_list


class ShortcutEntry(db.DatabaseTable):
    """Stores individual shortcut name/value pairs per guild."""
    __tablename__ = 'shortcuts'
    __uniques__ = 'guild_id, name'

    @classmethod
    async def initial_create(cls):
        """Create the table in the database"""
        async with db.Pool.acquire() as conn:
            await conn.execute(f"""
            CREATE TABLE {cls.__tablename__} (
            guild_id bigint NOT NULL,
            name varchar NOT NULL,
            value text NOT NULL,
            PRIMARY KEY (guild_id, name)
            )""")

    def __init__(self, guild_id: int, name: str, value: str):
        super().__init__()
        self.guild_id = guild_id
        self.name = name
        self.value = value

    @classmethod
    async def get_by(cls, **kwargs):
        results = await super().get_by(**kwargs)
        result_list = []
        for result in results:
            obj = ShortcutEntry(guild_id=result.get("guild_id"),
                                name=result.get("name"),
                                value=result.get("value"))
            result_list.append(obj)
        return result_list
