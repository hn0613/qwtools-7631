===========
Maintenance
===========
shutdown
++++++++
Force-stops the bot.
::
   `{prefix}shutdown` - stop the bot
restart
+++++++
Restarts the bot.
::
   `{prefix}restart` - restart the bot
update
++++++
Pulls code from GitHub and restarts. This pulls from whatever repository
`origin` is linked to. If there are changes to download, and the
download is successful, the bot restarts to apply changes.
::
   `{prefix}update` - update to the latest commit and restart
check
+++++
Checks for available updates without pulling or restarting. Shows the
current branch, local and remote commit status, pending commits, and
whether it is safe to run ``update``. Use this before updating to see
what will change and whether the workspace is in a clean state.

Possible statuses:

- **Already Up to Date** — local matches remote, no action needed.
- **Updates Available** — new commits on remote, safe to update.
- **Workspace Has Local Changes** — uncommitted changes detected, not safe to auto-update.
- **Fetch Failed** — could not reach the remote repository.
- **No Upstream Branch** — current branch has no tracking branch configured.
::
   `{prefix}check` - check if updates are available without pulling or restarting
