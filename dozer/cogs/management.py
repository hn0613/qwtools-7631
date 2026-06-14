"""General, basic commands that are common for Discord bots"""

import asyncio
import json
import math
import os
from datetime import timezone, datetime

import discord
from dateutil import parser
from discord.ext.commands import has_permissions, BadArgument
from discord.utils import escape_markdown
from loguru import logger

from dozer.bot import Dozer
from dozer.context import DozerContext
from ._utils import *
from .general import blurple
from .. import db

TIMEZONE_FILE = "timezones.json"


class Management(Cog):
    """A cog housing Guild management/utility commands."""

    def __init__(self, bot: Dozer):
        super().__init__(bot)
        self.started_timers = False
        self.timers = {}
        if os.path.isfile(TIMEZONE_FILE):
            logger.info("Loaded timezone configurations")
            with open(TIMEZONE_FILE) as f:
                self.timezones = json.load(f)
        else:
            logger.error("Unable to load timezone configurations")
            self.timezones = {}

    def cog_unload(self):
        """Cancel all pending timer tasks when the cog is unloaded."""
        for request_id, task in self.timers.items():
            task.cancel()
        if self.timers:
            logger.info(f"Cancelled {len(self.timers)} scheduled message timers on cog unload")
        self.timers.clear()

    @Cog.listener('on_ready')
    async def on_ready(self):
        """Restore time based event schedulers"""
        messages = await ScheduledMessages.get_by()
        started = 0
        if not self.started_timers:
            for message in messages:
                task = self.bot.loop.create_task(self.msg_timer(message))
                self.timers[message.request_id] = task
                started += 1
            self.started_timers = True
            logger.info(f"Started {started}/{len(messages)} scheduled messages")
        else:
            logger.info("Client Resumed: Timers still running")

    async def msg_timer(self, db_entry):
        """Holds the futures for sending a message"""
        try:
            delay = db_entry.time - datetime.now(tz=timezone.utc)
            if delay.total_seconds() > 0:
                await asyncio.sleep(delay.total_seconds())
            try:
                await self.send_scheduled_msg(db_entry)
            except Exception as exc:
                logger.error(f"Failed to send scheduled message (entry_id={db_entry.entry_id}, "
                             f"guild={db_entry.guild_id}, channel={db_entry.channel_id}): {exc}")
        except asyncio.CancelledError:
            logger.info(f"Scheduled message timer cancelled (entry_id={db_entry.entry_id})")
            raise
        finally:
            await ScheduledMessages.delete(request_id=db_entry.request_id)
            self.timers.pop(db_entry.request_id, None)

    async def send_scheduled_msg(self, db_entry, channel_override: int = None):
        """Formats and sends scheduled message"""
        embed = discord.Embed(title=db_entry.header if db_entry.header else "Scheduled Message",
                              description=db_entry.content)
        guild = self.bot.get_guild(db_entry.guild_id)
        if not guild:
            logger.warning(
                f"Attempted to schedulesend message in guild({db_entry.guild_id}); Guild no longer exist")
            return
        channel = guild.get_channel(db_entry.channel_id if not channel_override else channel_override)
        if not channel:
            logger.warning(f"Attempted to schedulesend message in guild({guild}), channel({db_entry.channel_id});"
                           f" Channel no longer exist")
            return
        embed.colour = blurple
        perms = channel.permissions_for(guild.me)
        if db_entry.requester_id:
            try:
                member = await guild.fetch_member(db_entry.requester_id)
                embed.set_footer(text=f"Author: {escape_markdown(member.display_name)}")
            except discord.NotFound:
                logger.warning(f"Scheduled message author (id={db_entry.requester_id}) no longer in guild {guild.id}")
            except discord.HTTPException as exc:
                logger.warning(f"Failed to fetch scheduled message author (id={db_entry.requester_id}): {exc}")
        if perms.send_messages:
            await channel.send(embed=embed)
        else:
            logger.warning((f"Attempted to schedulesend message in guild({guild}:{guild.id}), channel({channel});"
                            f" Client lacks send permissions"))

    @group(invoke_without_command=True)
    @has_permissions(manage_messages=True)
    async def schedulesend(self, ctx: DozerContext):
        """Allows a message to be sent at a particular time
        Commands: add, preview, delete, list
        """
        await ctx.send("Allows a message to be sent at a particular time\nCommands: add, preview, delete, list")

    @schedulesend.command()
    @has_permissions(manage_messages=True)
    async def add(self, ctx: DozerContext, channel: discord.TextChannel, time, *, content):
        """Allows a message to be sent at a particular time
        Headers are separated by the characters `-/-`
        """
        perms = channel.permissions_for(ctx.guild.me)
        if not perms.send_messages:
            raise BadArgument(f"Dozer does not have permissions to send messages in {channel.mention}")
        try:
            send_time = parser.parse(time, tzinfos=self.timezones)
        except (ValueError, TypeError):
            raise BadArgument("Unknown Date Format. Please use a recognisable date/time string "
                              "(e.g. `2024-06-15 14:00 EST`).")
        except OverflowError:
            raise BadArgument("Date exceeds max value")
        if send_time.tzinfo is None:
            send_time = send_time.replace(tzinfo=timezone.utc)
            await ctx.send("```Warning! No timezone detected, defaulting to UTC. "
                           "Include a timezone abbreviation (e.g. EST, PST) to avoid this.```")

        # Split header from content using -/- separator
        parts = content.split("-/-", 1)
        if len(parts) == 2:
            header = parts[0].strip() or None
            message = parts[1]
        else:
            header = None
            message = parts[0]

        if header is not None and len(header) > 256:
            await ctx.send("```Warning! Header larger than max 256 characters, header has been truncated```")
            header = header[:256]

        if len(message) > 4096:
            await ctx.send(f"```Warning! Message content ({len(message)} chars) exceeds the embed limit of 4096 "
                           f"characters and has been truncated.```")
            message = message[:4096]

        entry = ScheduledMessages(
            guild_id=ctx.guild.id,
            channel_id=channel.id,
            time=send_time,
            content=message,
            header=header,
            requester_id=ctx.author.id,
            request_id=ctx.message.id
        )
        await entry.update_or_add()
        entries = await ScheduledMessages.get_by(request_id=entry.request_id)
        if not entries:
            raise BadArgument("Failed to save scheduled message to the database. Please try again.")
        entry = entries[0]
        task = self.bot.loop.create_task(self.msg_timer(entry))
        self.timers[entry.request_id] = task
        await ctx.send(f"Scheduled message (ID: {entry.entry_id}) saved, and will be sent in {channel.mention} on "
                       f"{send_time.strftime('%B %d %H:%M%z %Y')}\nMessage preview:")
        await self.send_scheduled_msg(entry, channel_override=ctx.message.channel.id)

    add.example_usage = """
    `{prefix}schedulesend add #announcments "1/0/1970 0:00:00 GMT" Epoch -/- 00000`: Dozer will send a message on the unix epoch in #announcments
    """

    @schedulesend.command()
    @has_permissions(manage_messages=True)
    async def preview(self, ctx: DozerContext, entry_id: int):
        """Preview a scheduled message without sending it"""
        entries = await ScheduledMessages.get_by(entry_id=entry_id)
        if not entries:
            raise BadArgument(f"No scheduled message found with ID: {entry_id}")
        entry = entries[0]
        embed = discord.Embed(
            title=entry.header if entry.header else "Scheduled Message",
            description=entry.content,
            colour=blurple
        )
        channel = ctx.guild.get_channel(entry.channel_id)
        channel_mention = channel.mention if channel else f"Unknown channel ({entry.channel_id})"
        embed.add_field(name="Target Channel", value=channel_mention, inline=True)
        embed.add_field(name="Scheduled Time", value=entry.time.strftime('%Y-%m-%d %H:%M %Z'), inline=True)
        embed.add_field(name="Entry ID", value=str(entry.entry_id), inline=True)
        if entry.requester_id:
            try:
                member = await ctx.guild.fetch_member(entry.requester_id)
                embed.set_footer(text=f"Author: {escape_markdown(member.display_name)}")
            except (discord.NotFound, discord.HTTPException):
                embed.set_footer(text=f"Author: Unknown (was ID {entry.requester_id})")
        await ctx.send("Scheduled message preview:", embed=embed)

    preview.example_usage = """
    `{prefix}schedulesend preview 5`: Shows a preview of the scheduled message with ID 5
    """

    @schedulesend.command()
    @has_permissions(manage_messages=True)
    async def delete(self, ctx: DozerContext, entry_id: int):
        """Delete a scheduled message"""
        entries = await ScheduledMessages.get_by(entry_id=entry_id)
        e = discord.Embed(color=blurple)
        if entries:
            entry = entries[0]
            response = await ScheduledMessages.delete(request_id=entry.request_id)
            task = self.timers.pop(entry.request_id, None)
            if task is not None:
                task.cancel()
            e.add_field(name='Success',
                        value=f"Deleted entry with ID: {entry_id}"
                              + (" and cancelled planned send" if task is not None else ""))
            e.set_footer(text='Triggered by ' + escape_markdown(ctx.author.display_name))
            await ctx.send(embed=e)
        else:
            e.add_field(name='Error', value=f"No entry with ID: {entry_id} found")
            e.set_footer(text='Triggered by ' + escape_markdown(ctx.author.display_name))
            await ctx.send(embed=e)

    delete.example_usage = """
    `{prefix}schedulesend delete 5`: Deletes the scheduled message with the ID of 5
    """

    @schedulesend.command()
    @has_permissions(manage_messages=True)
    async def list(self, ctx: DozerContext):
        """Displays currently scheduled messages"""
        messages = await ScheduledMessages.get_by(guild_id=ctx.guild.id)
        if not messages:
            await ctx.send("No scheduled messages for this server.")
            return
        pages = []
        for page_num, page in enumerate(chunk(messages, 3)):
            embed = discord.Embed(title=f"Currently scheduled messages for {ctx.guild}")
            pages.append(embed)
            for message in page:
                try:
                    member = await ctx.guild.fetch_member(message.requester_id) if message.requester_id else None
                    author_display = member.mention if member else "Unknown"
                except (discord.NotFound, discord.HTTPException):
                    author_display = f"Unknown (was <@{message.requester_id}>)"

                header_text = escape_markdown(message.header) if message.header else "*(no header)*"
                content_preview = message.content if len(message.content) <= 200 else message.content[:197] + "..."

                embed.add_field(
                    name=f"ID: {message.entry_id} | {header_text[:220]}",
                    value=f"Channel: <#{message.channel_id}>"
                          f"\nTime: {message.time.strftime('%Y-%m-%d %H:%M %Z')}"
                          f"\nAuthor: {author_display}"
                          f"\nContent: {escape_markdown(content_preview)}",
                    inline=False
                )
            embed.set_footer(text=f"Page {page_num + 1} of {math.ceil(len(messages) / 3)}")
        await paginate(ctx, pages)

    list.example_usage = """
    `{prefix}schedulesend list`: Lists all scheduled messages for the current guild
    """


class ScheduledMessages(db.DatabaseTable):
    """Stores messages that are scheduled to be sent"""
    __tablename__ = 'scheduled_messages'
    __uniques__ = 'entry_id, request_id'

    @classmethod
    async def initial_create(cls):
        """Create the table in the database"""
        async with db.Pool.acquire() as conn:
            await conn.execute(f"""
            CREATE TABLE {cls.__tablename__} (
            entry_id serial,
            request_id bigint UNIQUE NOT NULL, 
            guild_id bigint NOT NULL,
            channel_id bigint NOT NULL,
            requester_id bigint NULL, 
            time timestamptz NOT NULL,
            header text NULL,
            content text NOT NULL,
            PRIMARY KEY (entry_id, request_id)
            )""")

    def __init__(self, guild_id: int, channel_id: int, time: datetime.time, content: str, request_id: str,
                 header: str = None, requester_id: int = None, entry_id: int = None):
        super().__init__()
        self.guild_id = guild_id
        self.channel_id = channel_id
        self.requester_id = requester_id
        self.request_id = request_id
        self.time = time
        self.header = header
        self.content = content
        self.entry_id = entry_id

    @classmethod
    async def get_by(cls, **kwargs):
        results = await super().get_by(**kwargs)
        result_list = []
        for result in results:
            obj = ScheduledMessages(guild_id=result.get("guild_id"), channel_id=result.get("channel_id"),
                                    header=result.get("header"),
                                    requester_id=result.get("requester_id"), time=result.get("time"),
                                    content=result.get("content"),
                                    entry_id=result.get("entry_id"), request_id=result.get("request_id"))
            result_list.append(obj)
        return result_list


async def setup(bot):
    """Adds the Management cog to the bot"""
    await bot.add_cog(Management(bot))
