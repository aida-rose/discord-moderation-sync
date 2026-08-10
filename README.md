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
- Privacy-preserving altcheck scoring from public profile and language patterns
- Join guard / account age protection
- Runtime affiliate management commands for bot owners
- Optional ticket system with category dropdowns, close/archive flow, transcripts, and media logs
- Optional nation selector with Minecraft account registration, Bedrock/Geyser support, and rejoin role restore
- Role-based command permissions
- Owner-only bot management commands

## Configuration

The `.env` file only needs:

- `DISCORD_TOKEN`
- `BOT_OWNER_IDS`

All other bot settings are stored in `data/moderation.sqlite3` and managed through owner-only slash commands such as `/config_set`, `/config_id_add`, `/config_id_remove`, `/config_get`, and `/config_list`.

Set `ALT_ALERT_ROLE_ID` with `/config_set` to ping a role when altcheck links a medium/high-risk account to a banned user.

To enable tickets, set `ENABLE_TICKETS` to `true` and restart the bot. Then configure:

- `TICKET_LOG_CHANNEL_ID` for archived `.txt` transcripts
- `TICKET_IMAGE_LOG_CHANNEL_ID` for forwarded ticket media
- `TICKET_CLOSED_CATEGORY_ID` for closed ticket channels
- `TICKET_REPORT_PING_ROLE_ID`, `TICKET_ADMIN_PING_ROLE_ID`, `TICKET_DISPUTE_PING_ROLE_ID`, and `TICKET_OTHER_PING_ROLE_ID` for category-specific pings

Use `/ticket_panel` in a text channel inside the Discord category where open tickets should be created.

To enable the nation selector, set `ENABLE_NATION_SELECTOR` to `true` and restart the bot. Then configure:

- `WHITELISTED_ROLE_ID` for the role granted after Minecraft account registration
- `PLAINS_ROLE_ID`, `FOREST_ROLE_ID`, `DESERT_ROLE_ID`, `TAIGA_ROLE_ID`, `JUNGLE_ROLE_ID`, `DARK_FOREST_ROLE_ID`, `MESA_ROLE_ID`, `SNOW_ROLE_ID`, `MUSHROOM_ISLAND_ROLE_ID`, `SAVANNA_ROLE_ID`, `SWAMP_ROLE_ID`, and `CHERRY_ROLE_ID` for the 12 nation roles
- `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, and `MS_REDIRECT_URI` in `.env` for Microsoft OAuth registration
- `NATION_SELECTOR_LOG_THREAD_ID` for optional selector logs

Register the Microsoft OAuth app for personal Microsoft accounts, add a web redirect URI that exactly matches `MS_REDIRECT_URI`, and make that URL route to this bot's `NATION_OAUTH_PORT`.

Java profile verification uses Minecraft Java game service APIs; new third-party integrations may need Mojang review/allowlist access.

Use `/nation_panel` to send the registration button. Users choose Java or Bedrock/Geyser before Microsoft sign-in, and the callback links only the selected account type. Primary-server administrators can use `/nation_change` to change a registered user's nation and `/nation_reset` to remove a user from the nation database.

## Docker Compose Deployment

Create a `.env` file with `DISCORD_TOKEN` and `BOT_OWNER_IDS`, then run:

```sh
docker compose up -d --build
```

The Compose service mounts `./data` into the container, so `data/moderation.sqlite3` persists across rebuilds and restarts.

Useful deployment commands:

```sh
docker compose logs -f
docker compose restart
docker compose down
```

## Punishment Syncing

The bot applies major moderation actions across configured synced servers. This keeps bans, tempbans, unbans, and timeouts consistent between the primary server and approved affiliated servers.

The `syncbans` command copies current primary-server bans to **one specified affiliate server** at a time.
