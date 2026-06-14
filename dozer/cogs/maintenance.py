"""Maintenance commands for bot developers"""

import os
import subprocess

import discord
from discord.ext.commands import NotOwner
from loguru import logger

from dozer.context import DozerContext
from ._utils import *


def _run_git(*args, timeout=30):
    """Run a git command and return the CompletedProcess result."""
    return subprocess.run(
        ["git"] + list(args),
        capture_output=True, text=True, timeout=timeout
    )


def check_update_status():
    """
    Check the current git update status without modifying the working tree.

    Returns a dict with:
        status: one of 'up_to_date', 'updates_available', 'workspace_dirty',
                'fetch_failed', 'no_tracking', 'error'
        branch: current branch name
        local_sha: local HEAD short sha
        remote_sha: upstream short sha (if available)
        dirty_files: list of modified file entries (if dirty)
        commits: list of pending commit one-line summaries (if updates available)
        error: error message (if failed)
        updates_behind: number of commits behind remote

    Assumptions:
        - The process working directory is the git repository root.
        - The current branch tracks a remote branch (typically origin/<branch>).
        - The remote is reachable over the network for git fetch.
    """
    result = {
        'status': 'error',
        'branch': None,
        'local_sha': None,
        'remote_sha': None,
        'dirty_files': [],
        'commits': [],
        'error': None,
        'updates_behind': 0,
    }

    # Get current branch name
    branch_result = _run_git('rev-parse', '--abbrev-ref', 'HEAD')
    if branch_result.returncode != 0:
        result['error'] = branch_result.stderr.strip() or 'Not a git repository'
        return result
    result['branch'] = branch_result.stdout.strip()

    # Get local HEAD short sha
    sha_result = _run_git('rev-parse', '--short', 'HEAD')
    if sha_result.returncode == 0:
        result['local_sha'] = sha_result.stdout.strip()

    # Fetch from remote (does not modify working tree)
    fetch_result = _run_git('fetch', '--quiet', timeout=30)
    if fetch_result.returncode != 0:
        result['status'] = 'fetch_failed'
        result['error'] = fetch_result.stderr.strip() or 'git fetch failed'
        return result

    # Check for uncommitted changes
    status_result = _run_git('status', '--porcelain')
    if status_result.returncode == 0 and status_result.stdout.strip():
        dirty_files = [line.strip() for line in status_result.stdout.strip().splitlines()]
        result['dirty_files'] = dirty_files[:15]

    # Get upstream tracking ref sha
    upstream_result = _run_git('rev-parse', '--short', '@{u}')
    if upstream_result.returncode != 0:
        result['status'] = 'no_tracking'
        result['error'] = 'No upstream tracking branch configured'
        return result
    result['remote_sha'] = upstream_result.stdout.strip()

    # Compare local vs upstream
    if result['local_sha'] == result['remote_sha']:
        if result['dirty_files']:
            result['status'] = 'workspace_dirty'
        else:
            result['status'] = 'up_to_date'
        return result

    # Upstream is ahead — list pending commits
    log_result = _run_git('log', 'HEAD..@{u}', '--oneline', '--no-decorate')
    if log_result.returncode == 0 and log_result.stdout.strip():
        commits = log_result.stdout.strip().splitlines()
        result['updates_behind'] = len(commits)
        result['commits'] = commits[:10]

    if result['dirty_files']:
        result['status'] = 'workspace_dirty'
    else:
        result['status'] = 'updates_available'

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

    @command()
    async def check(self, ctx: DozerContext):
        """
        Checks for available updates without pulling or restarting.
        Shows current branch, commit status, and whether it is safe to update.
        Use this before running update to see what will change.
        """
        await ctx.defer()

        try:
            info = check_update_status()
        except subprocess.TimeoutExpired:
            embed = discord.Embed(
                title='\u274c Fetch Timed Out',
                description='Git operation did not complete within 30 seconds.',
                color=discord.Color.red()
            )
            embed.set_footer(text='Remote may be unreachable. Try again later.')
            await ctx.send(embed=embed)
            return

        prefix = ctx.prefix or '&'

        if info['status'] == 'up_to_date':
            embed = discord.Embed(
                title='\u2705 Already Up to Date',
                description='Local code matches the remote — no update needed.',
                color=discord.Color.green()
            )
            embed.add_field(name='Branch', value=f"`{info['branch']}`", inline=True)
            embed.add_field(name='Commit', value=f"`{info['local_sha']}`", inline=True)
            embed.set_footer(text='No action needed.')

        elif info['status'] == 'updates_available':
            commit_list = '\n'.join(f'`{c}`' for c in info['commits'])
            if info['updates_behind'] > len(info['commits']):
                commit_list += f"\n... and {info['updates_behind'] - len(info['commits'])} more"

            embed = discord.Embed(
                title='\U0001f4e6 Updates Available',
                description=f"**{info['updates_behind']}** new commit(s) ready to pull.",
                color=discord.Color.blue()
            )
            embed.add_field(name='Branch', value=f"`{info['branch']}`", inline=True)
            embed.add_field(name='Local', value=f"`{info['local_sha']}`", inline=True)
            embed.add_field(name='Remote', value=f"`{info['remote_sha']}`", inline=True)
            embed.add_field(name='Pending Commits', value=commit_list, inline=False)
            embed.set_footer(text=f'Run {prefix}update to pull and restart.')

        elif info['status'] == 'workspace_dirty':
            file_list = '\n'.join(f'`{f}`' for f in info['dirty_files'])
            if len(info['dirty_files']) >= 15:
                file_list += '\n... (truncated)'

            embed = discord.Embed(
                title='\u26a0\ufe0f Workspace Has Local Changes',
                description='Uncommitted changes detected — auto-update is not safe.',
                color=discord.Color.orange()
            )
            embed.add_field(name='Branch', value=f"`{info['branch']}`", inline=True)
            embed.add_field(name='Commit', value=f"`{info['local_sha']}`", inline=True)
            embed.add_field(name='Modified Files', value=file_list, inline=False)
            if info['updates_behind'] > 0:
                embed.add_field(
                    name='Updates Behind',
                    value=f"{info['updates_behind']} commit(s) available after resolving local changes",
                    inline=False
                )
            embed.set_footer(text='Commit or stash local changes before updating.')

        elif info['status'] == 'fetch_failed':
            embed = discord.Embed(
                title='\u274c Fetch Failed',
                description='Could not retrieve updates from the remote repository.',
                color=discord.Color.red()
            )
            if info['branch']:
                embed.add_field(name='Branch', value=f"`{info['branch']}`", inline=True)
            if info['error']:
                error_text = info['error'][:500]
                embed.add_field(name='Error', value=f"```\n{error_text}\n```", inline=False)
            embed.set_footer(text='Check network and remote configuration.')

        elif info['status'] == 'no_tracking':
            embed = discord.Embed(
                title='\u26a0\ufe0f No Upstream Branch',
                description='Current branch has no remote tracking branch configured.',
                color=discord.Color.orange()
            )
            embed.add_field(name='Branch', value=f"`{info['branch']}`", inline=True)
            if info['local_sha']:
                embed.add_field(name='Commit', value=f"`{info['local_sha']}`", inline=True)
            embed.set_footer(text='Set upstream with: git branch --set-upstream-to=origin/<branch>')

        else:
            embed = discord.Embed(
                title='\u274c Check Failed',
                description='An unexpected error occurred while checking update status.',
                color=discord.Color.red()
            )
            if info['error']:
                embed.add_field(name='Error', value=f"```\n{info['error'][:500]}\n```", inline=False)
            embed.set_footer(text='Check bot logs for details.')

        await ctx.send(embed=embed)

    check.example_usage = """
    `{prefix}check` - check if updates are available without pulling or restarting
    """


async def setup(bot):
    """Adds the maintenance cog to the bot process."""
    await bot.add_cog(Maintenance(bot))
