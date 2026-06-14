"""Maintenance commands for bot developers"""
from __future__ import annotations

import asyncio
import os

import discord
from discord.ext.commands import NotOwner
from loguru import logger

from dozer.context import DozerContext
from ._utils import *


# ---------------------------------------------------------------------------
# Git helpers (async, non-blocking)
# ---------------------------------------------------------------------------

_GIT_TIMEOUT = 30  # seconds – generous for slow CI/Docker networks


async def _git(*args: str) -> tuple[int, str, str]:
    """Run a git command asynchronously.

    Returns (return_code, stdout, stderr).
    Uses *asyncio.create_subprocess_exec* so the Discord event loop is never
    blocked – important because ``git fetch`` may take several seconds over
    the network.
    """
    proc = await asyncio.create_subprocess_exec(
        'git', *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=_GIT_TIMEOUT)
    return proc.returncode, stdout.decode(errors='replace'), stderr.decode(errors='replace')


async def _git_line(*args: str) -> str | None:
    """Run a git command and return stripped stdout, or ``None`` on failure."""
    rc, out, _ = await _git(*args)
    return out.strip() if rc == 0 else None


async def _check_update_impl() -> dict:
    """Collect all information needed to assess update readiness.

    Performs a ``git fetch origin`` (network-modifying but **working-tree
    safe**) and then inspects the local state.  Returns a dict with a
    ``status`` key that is one of:

    * ``up_to_date``   – local HEAD matches remote HEAD
    * ``available``    – remote has new commits we can fast-forward to
    * ``diverged``     – both sides have unique commits (merge / rebase needed)
    * ``dirty``        – uncommitted changes would block a safe pull
    * ``detached``     – HEAD is not on a branch
    * ``fetch_failed`` – could not contact the remote
    * ``error``        – something unexpected went wrong

    The dict also carries supporting data (branch name, commit SHAs,
    ahead/behind counts, commit subjects, error messages) so the caller
    can render a human-friendly report.
    """
    result: dict = {}

    # --- determine current branch first so we can fetch it explicitly ---
    branch = await _git_line('rev-parse', '--abbrev-ref', 'HEAD')
    if branch is None or branch == 'HEAD':
        result['status'] = 'detached'
        result['branch'] = branch or 'HEAD'
        return result
    result['branch'] = branch

    # --- fetch the current branch from origin (does NOT touch working tree) ---
    # Using an explicit refname (instead of bare `git fetch origin`) ensures
    # FETCH_HEAD is populated even when no upstream tracking branch is set.
    rc, _stdout, fetch_err = await _git('fetch', 'origin', branch)
    if rc != 0:
        result['status'] = 'fetch_failed'
        result['error'] = fetch_err.strip() or 'git fetch returned non-zero exit code'
        return result

    # --- commit SHAs ---
    local_sha = await _git_line('rev-parse', 'HEAD')
    remote_sha = await _git_line('rev-parse', 'FETCH_HEAD')
    if not local_sha or not remote_sha:
        result['status'] = 'error'
        result['error'] = 'Could not resolve local or remote HEAD'
        return result
    result['local_sha'] = local_sha[:8]
    result['remote_sha'] = remote_sha[:8]

    # --- working directory cleanliness ---
    rc, status_out, _ = await _git('status', '--porcelain')
    result['dirty'] = rc == 0 and bool(status_out.strip())

    # --- ahead / behind counts ---
    ahead_behind = await _git_line(
        'rev-list', '--left-right', '--count', 'HEAD...FETCH_HEAD',
    )
    if ahead_behind:
        parts = ahead_behind.split()
        result['ahead'] = int(parts[0])
        result['behind'] = int(parts[1])
    else:
        result['ahead'] = 0
        result['behind'] = 0

    # --- incoming commit subjects (what we'd get after pull) ---
    rc, log_out, _ = await _git(
        'log', '--oneline', '--no-merges', '--right-only',
        'HEAD...FETCH_HEAD',
    )
    result['incoming'] = (
        [line for line in log_out.strip().splitlines() if line][:10]
        if rc == 0 else []
    )

    # --- determine status (priority order) ---
    if result['dirty']:
        result['status'] = 'dirty'
    elif result['ahead'] > 0 and result['behind'] > 0:
        result['status'] = 'diverged'
    elif result['behind'] > 0:
        result['status'] = 'available'
    else:
        result['status'] = 'up_to_date'

    return result


class Maintenance(Cog):
    """
    Commands for performing maintenance on the bot.
    These commands are restricted to bot developers.
    """

    def cog_check(self, ctx: DozerContext):  # All of this cog is only available to devs
        if ctx.author.id not in ctx.bot.config['developers']:
            raise NotOwner('you are not a developer!')
        return True

    # ------------------------------------------------------------------
    # Existing commands – unchanged
    # ------------------------------------------------------------------

    @command()
    async def shutdown(self, ctx: DozerContext):
        """Force-stops the bot."""
        await ctx.send('Shutting down')
        logger.info(f'Shutting down at request of {ctx.author.name}{"#" + ctx.author.discriminator if ctx.author.discriminator != "0" else ""} '
                    f'(in {ctx.guild.name}, #{ctx.channel.name})')
        await self.bot.shutdown()

    shutdown.example_usage = """
    `{prefix}shutdown` - stop the bot
    """

    @command()
    async def restart(self, ctx: DozerContext):
        """Restarts the bot."""
        await ctx.send('Restarting')
        await self.bot.shutdown(restart=True)

    restart.example_usage = """
    `{prefix}restart` - restart the bot
    """

    @command()
    async def update(self, ctx: DozerContext):
        """
        Pulls code from GitHub and restarts.
        This pulls from whatever repository `origin` is linked to.
        If there are changes to download, and the download is successful, the bot restarts to apply changes.
        """
        res = os.popen("git pull").read()
        if res.startswith('Already up to date.') or "CONFLICT (content):" in res:
            await ctx.send('```\n' + res + '```')
        else:
            await ctx.send('```\n' + res + '```')
            await ctx.bot.get_command('restart').callback(self, ctx)

    update.example_usage = """
    `{prefix}update` - update to the latest commit and restart
    """

    # ------------------------------------------------------------------
    # New: pre-update safety check
    # ------------------------------------------------------------------

    @command()
    async def check_update(self, ctx: DozerContext):
        """Checks for available updates without pulling or restarting.

        Runs ``git fetch origin`` (safe – does not modify local files), then
        compares the local HEAD with the remote and inspects the working
        directory.  The result tells you:

        - whether new commits are available
        - whether the working tree is clean enough for a safe pull
        - how many commits behind / ahead the local branch is
        - what the incoming commits are (subjects, capped at 10)

        Use this **before** ``update`` to decide whether it is safe to
        proceed, especially during events or peak hours.
        """
        # Let the caller know something is happening while we hit the network
        status_msg = await ctx.send(
            'Checking for updates (fetching from remote)...'
        )

        try:
            info = await _check_update_impl()
        except asyncio.TimeoutError:
            embed = discord.Embed(
                title='Update Check — Timed Out',
                description=(
                    'The `git fetch` did not complete within '
                    f'{_GIT_TIMEOUT} seconds. This usually means a network '
                    'problem or an unreachable remote.'
                ),
                color=0xFFA500,  # orange
            )
            await status_msg.edit(content=None, embed=embed)
            return
        except Exception as exc:  # noqa: BLE001 – surface any unexpected error
            embed = discord.Embed(
                title='Update Check — Unexpected Error',
                description=f'```\n{exc}\n```',
                color=0xFF0000,
            )
            await status_msg.edit(content=None, embed=embed)
            return

        # ---- Build the embed based on the collected info ----
        status = info['status']

        embed = discord.Embed(title='Update Check')

        if status != 'error':
            embed.add_field(
                name='Branch',
                value=f'`{info.get("branch", "?")}`',
                inline=True,
            )

        if 'local_sha' in info:
            embed.add_field(
                name='Local',
                value=f'`{info["local_sha"]}`',
                inline=True,
            )
            embed.add_field(
                name='Remote',
                value=f'`{info["remote_sha"]}`',
                inline=True,
            )

        # -- status-specific sections --

        if status == 'dirty':
            embed.color = 0xFFA500
            embed.description = (
                'Working directory has **uncommitted changes**. '
                '`update` (which runs `git pull`) may refuse to merge or '
                'create conflicts. '
                'Commit or stash your changes before updating.'
            )
            if info.get('behind', 0) > 0:
                embed.add_field(
                    name='Pending updates',
                    value=f'**{info["behind"]}** commit(s) behind remote',
                    inline=False,
                )

        elif status == 'detached':
            embed.color = 0xFFA500
            embed.description = (
                'HEAD is **detached** — not on a named branch. '
                '`git pull` will not know which remote branch to merge. '
                'Check out a real branch before updating.'
            )

        elif status == 'up_to_date':
            embed.color = 0x00FF00
            embed.description = 'Already up to date. No action needed.'

        elif status == 'available':
            embed.color = 0x3498DB
            embed.description = (
                f'**{info["behind"]}** new commit(s) available. '
                'Run `update` when you are ready to pull and restart.'
            )
            if info.get('incoming'):
                lines = '\n'.join(f'`{c}`' for c in info['incoming'])
                if len(info['incoming']) >= 10:
                    lines += '\n_… and possibly more_'
                embed.add_field(
                    name='Incoming commits',
                    value=lines,
                    inline=False,
                )

        elif status == 'diverged':
            embed.color = 0xFF0000
            embed.description = (
                f'Local branch is **{info["ahead"]}** commit(s) ahead '
                f'and **{info["behind"]}** behind the remote. '
                'A plain `git pull` may create a merge commit or fail. '
                'Resolve the divergence manually (rebase or merge) before '
                'running `update`.'
            )

        elif status == 'fetch_failed':
            embed.color = 0xFF0000
            embed.description = (
                'Could not fetch from remote. '
                'Check network connectivity and the `origin` remote URL.'
            )
            err = info.get('error', 'Unknown error')
            # Truncate to stay well within Discord's 4096-char description
            if len(err) > 1000:
                err = err[:1000] + '…'
            embed.add_field(
                name='Error details',
                value=f'```\n{err}\n```',
                inline=False,
            )

        else:  # generic error fallback
            embed.color = 0xFF0000
            embed.description = info.get('error', 'An unexpected error occurred.')

        await status_msg.edit(content=None, embed=embed)

    check_update.example_usage = """
    `{prefix}check_update` - check for updates without pulling or restarting
    """


async def setup(bot):
    """Adds the maintenance cog to the bot process."""
    await bot.add_cog(Maintenance(bot))
