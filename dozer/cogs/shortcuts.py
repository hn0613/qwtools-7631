"""Adds simple text-shortcuts to the bot"""

import re

import discord
from discord.ext import commands
from discord.ext.commands import BadArgument, guild_only, has_permissions

from dozer.context import DozerContext
from ._utils import *
from .. import db
from ..db import *

# Pattern matching @everyone, @here, <@userid>, <@!userid>, <@&roleid>
_MENTION_RE = re.compile(r'<@[!&]?\d+>')

class Shortcuts(Cog):
    """Adds simple text-shortcuts to the bot"""
    MAX_LEN = 20
    MAX_EXTRA_LEN = 500
    def __init__(self, bot):
        """cog init"""
        super().__init__(bot)
        self.settings_cache = db.ConfigCache(ShortcutSetting)
        self.cache = db.ConfigCache(ShortcutEntry)

    @staticmethod
    def _sanitize_extra(text):
        """Sanitize user-supplied dynamic content to prevent mention abuse and spam."""
        text = text[:Shortcuts.MAX_EXTRA_LEN]
        # Escape @everyone and @here by inserting a zero-width space after @
        text = text.replace('@everyone', '@\u200beveryone').replace('@here', '@\u200bhere')
        # Strip user/role mention markup
        text = _MENTION_RE.sub('', text)
        return text.strip()

    """Commands for managing shortcuts/macros."""
    @guild_only()
    @has_permissions(manage_messages=True)
    @group(invoke_without_command=True)
    async def shortcuts(self, ctx):
        """
        Display shortcut information
        """
        settings: ShortcutSetting = await self.settings_cache.query_one(guild_id=ctx.guild.id)

        if settings is None:
            raise BadArgument("This server has no shortcut configuration, set a prefix.")

        e = discord.Embed()
        e.title = "Server shortcut configuration"
        e.add_field(name="Shortcut prefix", value=settings.prefix or "[unset]")
        await ctx.send(embed=e)

    @guild_only()
    @has_permissions(manage_messages=True)
    @shortcuts.command()
    async def setprefix(self, ctx, prefix):
        """Set the prefix to be used to respond to shortcuts for the server."""
        setting: ShortcutSetting = await self.settings_cache.query_one(guild_id=ctx.guild.id)

        if setting:
            setting.prefix = prefix
        else:
            setting = ShortcutSetting(guild_id=ctx.guild.id, prefix=prefix)

        await setting.update_or_add()
        self.settings_cache.invalidate_entry(guild_id=ctx.guild.id)

        await ctx.send(f"Set prefix to: {prefix}")

    @guild_only()
    @has_permissions(manage_messages=True)
    @shortcuts.command(aliases=["add"])
    async def set(self, ctx, cmd_name, *, cmd_msg):
        """Set the message to be sent for a given shortcut name."""
        settings: ShortcutSetting = await self.settings_cache.query_one(guild_id=ctx.guild.id)
        if settings is None:
            raise BadArgument("Set a prefix first!")
        if len(cmd_name) > self.MAX_LEN:
            raise BadArgument(f"command names can only be up to {self.MAX_LEN} chars long")
        if not cmd_msg:
            raise BadArgument("can't have null message")

        ent: ShortcutEntry = await self.cache.query_one(guild_id=ctx.guild.id, name=cmd_name)

        if ent:
            ent.value = cmd_msg
        else:
            ent = ShortcutEntry(guild_id=ctx.guild.id, name=cmd_name, value=cmd_msg)

        await ent.update_or_add()
        self.cache.invalidate_entry(guild_id=ctx.guild.id, name=cmd_name)

        await ctx.send("Updated command successfully.")

    set.example_usage = """
    `{prefix}shortcuts set hello Hello, World!!!!` - set !hello for the server
    Shortcuts support dynamic content: `!hello check out this link` will send the template followed by the extra text.
    """

    @guild_only()
    @has_permissions(manage_messages=True)
    @shortcuts.command()
    async def remove(self, ctx, cmd_name):
        """Removes a shortcut from the server by name."""
        ent: ShortcutEntry = await self.cache.query_one(guild_id=ctx.guild.id, name=cmd_name)

        if ent:
            await ShortcutEntry.delete(guild_id=ctx.guild.id, name=cmd_name)
            self.cache.invalidate_entry(guild_id=ctx.guild.id, name=cmd_name)
            await ctx.send(f"Removed command {cmd_name} successfully.")
        else:
            await ctx.send(f"No command named {cmd_name} found!")

    remove.example_usage = """
    `{prefix}shortcuts remove hello  - removes !hello
    """

    @guild_only()
    @shortcuts.command()
    async def list(self, ctx):
        """Lists all shortcuts for the server."""
        settings: ShortcutSetting = await self.settings_cache.query_one(guild_id=ctx.guild.id)

        ents: List[ShortcutEntry] = await ShortcutEntry.get_by(guild_id=ctx.guild.id)

        if not ents:
            await ctx.send("No shortcuts for this server!")
            return

        embed = None
        for i, e in enumerate(ents):
            if i % 20 == 0:
                if embed is not None:
                    await ctx.send(embed=embed)
                embed = discord.Embed()
                embed.title = "Shortcuts for this server"
                embed.description = (
                    f"Tip: You can append extra text when triggering a shortcut.\n"
                    f"Example: `{settings.prefix}name your extra message here`"
                )
            embed.add_field(name=settings.prefix + e.name, value=e.value[:1024])

        if embed.fields:
            await ctx.send(embed=embed)

    list.example_usage = """
    `{prefix}shortcuts list` - lists all shortcuts
    You can trigger any shortcut with optional extra text: `!name some extra context`
    """

    @Cog.listener()
    async def on_message(self, msg):
        """prefix scanner"""
        if not msg.guild or msg.author.bot:
            return
        setting = await self.settings_cache.query_one(guild_id=msg.guild.id)
        if setting is None:
            return

        c = msg.content
        if len(c) < len(setting.prefix):
            return

        if not c.startswith(setting.prefix):
            return

        after_prefix = c[len(setting.prefix):]
        if not after_prefix.strip():
            return

        # Split into command name and optional extra content
        parts = after_prefix.split(None, 1)
        cmd_name = parts[0]
        extra = parts[1] if len(parts) > 1 else ""

        shortcuts = await ShortcutEntry.get_by(guild_id=msg.guild.id)
        if not shortcuts:
            return

        for shortcut in shortcuts:
            if cmd_name.lower() == shortcut.name.lower():
                response = shortcut.value
                if extra:
                    extra = self._sanitize_extra(extra)
                    if extra:
                        response = f"{response}\n{extra}"
                await msg.channel.send(response)
                return

async def setup(bot):
    """Adds the shortcuts cog to the main bot project."""
    await bot.add_cog(Shortcuts(bot))


"""Database Tables"""


class ShortcutSetting(db.DatabaseTable):
    """Provides a DB config to track shortcut setting per guild."""
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
    """Provides a DB config to track shortcut entries."""
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
