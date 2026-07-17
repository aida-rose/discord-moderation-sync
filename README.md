# Discord Moderation Sync Bot

A Discord moderation bot for managing moderation actions across a primary server and approved affiliated servers.

This bot is intended for use by a controlled network of approved servers. It is not intended to be a general-purpose public moderation bot.

## Current Features

- Kick users from the primary server only
- Ban users across configured synced servers
- Temporary bans across configured synced servers
- Unban users across configured synced servers
- Timeout / mute users across configured synced servers
- Remove timeouts / unmute users across configured synced servers
- Sync current primary-server bans to one specified affiliate server
- Tempban-aware ban syncing
- Moderation action logs
- Message, role, user, invite, join/leave, VC, and server-management logs
- Warn system
- User info command
- Join guard / account age protection
- Runtime affiliate management commands for bot owners
- Role-based command permissions
- Owner-only bot management commands

## Configuration

The `.env` file only needs:

- `DISCORD_TOKEN`
- `BOT_OWNER_IDS`

All other bot settings are stored in `data/moderation.sqlite3` and managed through owner-only slash commands such as `/config_set`, `/config_id_add`, `/config_id_remove`, `/config_get`, and `/config_list`.

## Punishment Syncing

The bot applies major moderation actions across configured synced servers. This keeps bans, tempbans, unbans, and timeouts consistent between the primary server and approved affiliated servers.

The `syncbans` command copies current primary-server bans to **one specified affiliate server** at a time.
