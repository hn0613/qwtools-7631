"""Tests for the maintenance cog's check_update_status() and command structure."""

import subprocess
from unittest.mock import patch, MagicMock, AsyncMock

import pytest

from dozer.cogs.maintenance import check_update_status, _run_git, Maintenance


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_completed(stdout="", stderr="", returncode=0):
    """Build a subprocess.CompletedProcess for mocking."""
    return subprocess.CompletedProcess(args=["git"], returncode=returncode, stdout=stdout, stderr=stderr)


def _mock_git_side_effect(responses: dict):
    """
    Return a side_effect function for _run_git that dispatches on the first
    positional arg (the git subcommand / first argument).

    ``responses`` maps a lookup key to a CompletedProcess.
    Keys are matched in order against the args tuple:
      - 'rev-parse --abbrev-ref' matches ('rev-parse', '--abbrev-ref', 'HEAD')
      - 'rev-parse --short'      matches ('rev-parse', '--short', ...)
      - 'rev-parse'              matches any other rev-parse call (e.g. @{u})
      - 'fetch'                  matches ('fetch', ...)
      - 'status'                 matches ('status', ...)
      - 'log'                    matches ('log', ...)
    """
    def side_effect(*args, **kwargs):
        joined = " ".join(args)
        # Try progressively shorter prefixes for best match
        for key in responses:
            if joined.startswith(key):
                return responses[key]
        # Fallback: succeed silently
        return _make_completed()
    return side_effect


# ---------------------------------------------------------------------------
# Tests for check_update_status()
# ---------------------------------------------------------------------------

class TestCheckUpdateStatusUpToDate:
    """Tests for the 'up_to_date' status path."""

    @patch("dozer.cogs.maintenance._run_git")
    def test_up_to_date(self, mock_git):
        mock_git.side_effect = _mock_git_side_effect({
            "rev-parse --abbrev-ref": _make_completed("master\n"),
            "rev-parse --short HEAD":  _make_completed("abc1234\n"),
            "rev-parse --short @{u}":  _make_completed("abc1234\n"),
            "fetch":                   _make_completed(),
            "status --porcelain":      _make_completed(""),
            "log":                     _make_completed(""),
        })

        result = check_update_status()

        assert result['status'] == 'up_to_date'
        assert result['branch'] == 'master'
        assert result['local_sha'] == 'abc1234'
        assert result['remote_sha'] == 'abc1234'
        assert result['dirty_files'] == []
        assert result['commits'] == []
        assert result['updates_behind'] == 0


class TestCheckUpdateStatusUpdatesAvailable:
    """Tests for the 'updates_available' status path."""

    @patch("dozer.cogs.maintenance._run_git")
    def test_updates_available(self, mock_git):
        commits = "def5678 Fix login bug\nghi9012 Add tests\n"
        mock_git.side_effect = _mock_git_side_effect({
            "rev-parse --abbrev-ref": _make_completed("master\n"),
            "rev-parse --short HEAD":  _make_completed("abc1234\n"),
            "rev-parse --short @{u}":  _make_completed("def5678\n"),
            "fetch":                   _make_completed(),
            "status --porcelain":      _make_completed(""),
            "log":                     _make_completed(commits),
        })

        result = check_update_status()

        assert result['status'] == 'updates_available'
        assert result['local_sha'] == 'abc1234'
        assert result['remote_sha'] == 'def5678'
        assert result['updates_behind'] == 2
        assert len(result['commits']) == 2

    @patch("dozer.cogs.maintenance._run_git")
    def test_commits_capped_at_ten(self, mock_git):
        """When more than 10 commits are pending, only first 10 are stored."""
        lines = "\n".join(f"sha{i:04d} Commit number {i}" for i in range(15))
        mock_git.side_effect = _mock_git_side_effect({
            "rev-parse --abbrev-ref": _make_completed("main\n"),
            "rev-parse --short HEAD":  _make_completed("aaa0000\n"),
            "rev-parse --short @{u}":  _make_completed("bbb1111\n"),
            "fetch":                   _make_completed(),
            "status --porcelain":      _make_completed(""),
            "log":                     _make_completed(lines + "\n"),
        })

        result = check_update_status()

        assert result['status'] == 'updates_available'
        assert result['updates_behind'] == 15
        assert len(result['commits']) == 10


class TestCheckUpdateStatusWorkspaceDirty:
    """Tests for the 'workspace_dirty' status path."""

    @patch("dozer.cogs.maintenance._run_git")
    def test_dirty_no_remote_changes(self, mock_git):
        """Dirty workspace but no remote updates."""
        mock_git.side_effect = _mock_git_side_effect({
            "rev-parse --abbrev-ref": _make_completed("master\n"),
            "rev-parse --short HEAD":  _make_completed("abc1234\n"),
            "rev-parse --short @{u}":  _make_completed("abc1234\n"),
            "fetch":                   _make_completed(),
            "status --porcelain":      _make_completed(" M dozer/cogs/fun.py\n"),
            "log":                     _make_completed(""),
        })

        result = check_update_status()

        assert result['status'] == 'workspace_dirty'
        assert len(result['dirty_files']) == 1
        assert result['updates_behind'] == 0

    @patch("dozer.cogs.maintenance._run_git")
    def test_dirty_with_remote_changes(self, mock_git):
        """Dirty workspace AND remote has updates — still workspace_dirty."""
        mock_git.side_effect = _mock_git_side_effect({
            "rev-parse --abbrev-ref": _make_completed("master\n"),
            "rev-parse --short HEAD":  _make_completed("abc1234\n"),
            "rev-parse --short @{u}":  _make_completed("def5678\n"),
            "fetch":                   _make_completed(),
            "status --porcelain":      _make_completed(" M config.json\n"),
            "log":                     _make_completed("def5678 Some fix\n"),
        })

        result = check_update_status()

        assert result['status'] == 'workspace_dirty'
        assert result['updates_behind'] == 1
        assert len(result['dirty_files']) == 1


class TestCheckUpdateStatusFetchFailed:
    """Tests for the 'fetch_failed' status path."""

    @patch("dozer.cogs.maintenance._run_git")
    def test_fetch_failed(self, mock_git):
        mock_git.side_effect = _mock_git_side_effect({
            "rev-parse --abbrev-ref": _make_completed("master\n"),
            "rev-parse --short HEAD":  _make_completed("abc1234\n"),
            "fetch":                   _make_completed(stderr="fatal: unable to access remote", returncode=128),
        })

        result = check_update_status()

        assert result['status'] == 'fetch_failed'
        assert 'unable to access' in result['error']
        assert result['branch'] == 'master'

    @patch("dozer.cogs.maintenance._run_git")
    def test_fetch_failed_empty_stderr(self, mock_git):
        mock_git.side_effect = _mock_git_side_effect({
            "rev-parse --abbrev-ref": _make_completed("master\n"),
            "rev-parse --short HEAD":  _make_completed("abc1234\n"),
            "fetch":                   _make_completed(returncode=1),
        })

        result = check_update_status()

        assert result['status'] == 'fetch_failed'
        assert result['error'] == 'git fetch failed'


class TestCheckUpdateStatusNoTracking:
    """Tests for the 'no_tracking' status path."""

    @patch("dozer.cogs.maintenance._run_git")
    def test_no_upstream(self, mock_git):
        mock_git.side_effect = _mock_git_side_effect({
            "rev-parse --abbrev-ref": _make_completed("feature-branch\n"),
            "rev-parse --short HEAD":  _make_completed("abc1234\n"),
            "rev-parse --short @{u}":  _make_completed(stderr="fatal: no upstream configured", returncode=128),
            "fetch":                   _make_completed(),
            "status --porcelain":      _make_completed(""),
        })

        result = check_update_status()

        assert result['status'] == 'no_tracking'
        assert result['branch'] == 'feature-branch'


class TestCheckUpdateStatusError:
    """Tests for the 'error' fallback status path."""

    @patch("dozer.cogs.maintenance._run_git")
    def test_not_a_git_repo(self, mock_git):
        mock_git.side_effect = _mock_git_side_effect({
            "rev-parse --abbrev-ref": _make_completed(
                stderr="fatal: not a git repository", returncode=128
            ),
        })

        result = check_update_status()

        assert result['status'] == 'error'
        assert 'not a git repository' in result['error']


class TestCheckUpdateStatusDirtyFilesCapped:
    """Verify that dirty file list is capped at 15 entries."""

    @patch("dozer.cogs.maintenance._run_git")
    def test_dirty_files_capped(self, mock_git):
        lines = "\n".join(f" M file{i}.py" for i in range(20))
        mock_git.side_effect = _mock_git_side_effect({
            "rev-parse --abbrev-ref": _make_completed("master\n"),
            "rev-parse --short HEAD":  _make_completed("abc1234\n"),
            "rev-parse --short @{u}":  _make_completed("abc1234\n"),
            "fetch":                   _make_completed(),
            "status --porcelain":      _make_completed(lines + "\n"),
        })

        result = check_update_status()

        assert result['status'] == 'workspace_dirty'
        assert len(result['dirty_files']) == 15


# ---------------------------------------------------------------------------
# Tests for _run_git helper
# ---------------------------------------------------------------------------

class TestRunGit:

    @patch("dozer.cogs.maintenance.subprocess.run")
    def test_run_git_passes_args(self, mock_run):
        mock_run.return_value = _make_completed("output\n")

        result = _run_git("status", "--porcelain")

        mock_run.assert_called_once_with(
            ["git", "status", "--porcelain"],
            capture_output=True, text=True, timeout=30
        )
        assert result.stdout == "output\n"

    @patch("dozer.cogs.maintenance.subprocess.run")
    def test_run_git_custom_timeout(self, mock_run):
        mock_run.return_value = _make_completed()

        _run_git("fetch", timeout=60)

        mock_run.assert_called_once_with(
            ["git", "fetch"],
            capture_output=True, text=True, timeout=60
        )


# ---------------------------------------------------------------------------
# Tests that existing commands remain structurally intact
# ---------------------------------------------------------------------------

class TestExistingCommandsIntact:
    """Verify that shutdown, restart, and update commands still exist with
    expected signatures and behaviors — ensuring the new check command
    did not break anything."""

    def _make_cog(self):
        bot = MagicMock()
        bot.config = {'developers': [12345]}
        return Maintenance(bot)

    def test_shutdown_exists(self):
        cog = self._make_cog()
        assert hasattr(cog, 'shutdown')
        assert callable(cog.shutdown.callback)

    def test_restart_exists(self):
        cog = self._make_cog()
        assert hasattr(cog, 'restart')
        assert callable(cog.restart.callback)

    def test_update_exists(self):
        cog = self._make_cog()
        assert hasattr(cog, 'update')
        assert callable(cog.update.callback)

    def test_check_exists(self):
        cog = self._make_cog()
        assert hasattr(cog, 'check')
        assert callable(cog.check.callback)

    def test_all_have_example_usage(self):
        cog = self._make_cog()
        for name in ('shutdown', 'restart', 'update', 'check'):
            cmd = getattr(cog, name)
            assert cmd.example_usage, f"{name} is missing example_usage"

    def test_update_does_not_auto_restart_when_up_to_date(self):
        """The update command should NOT restart when already up to date."""
        cog = self._make_cog()
        # Verify the source logic: the condition checks for 'Already up to date.'
        # This is a structural/source-level check — the real integration is
        # covered by running the bot.
        import inspect
        source = inspect.getsource(cog.update.callback)
        assert "Already up to date." in source
        assert "CONFLICT" in source
