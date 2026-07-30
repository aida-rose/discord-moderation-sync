# LinkVerify Plugin

Small Paper plugin for the Minecraft verification server.

Players run:

```text
/link CODE
```

The plugin sends their real in-game identity to the Discord bot. The bot checks the pending Discord whitelist link, marks it verified, adds the user to whitelist sync, optionally RCON-whitelists them on the main server, applies the Discord whitelist role, and tells this plugin to kick the player from the verification server.

## Verification Server

Recommended server setup:

```text
Paper
Geyser
Floodgate
LinkVerify
```

The verification server can be lightweight; it does not need the main modpack.

## Build

```bash
cd mc-link-verify-plugin
gradle build
```

The plugin jar will be at:

```text
build/libs/LinkVerify-1.0.0.jar
```

Copy it into:

```text
verification-server/plugins/
```

Then restart the verification server.

## Plugin Config

After the first server start, edit:

```text
plugins/LinkVerify/config.yml
```

Set:

```yaml
bot:
  verify-url: "http://BOT_HOST_OR_IP:8765/minecraft/verify"
  token: "same-secret-as-MC_VERIFY_API_TOKEN"
```

If the plugin and bot are on the same machine, `127.0.0.1` may work. If the bot is in Docker or on another host, use the reachable host/IP.

## Bot Config

In the bot `.env`:

```env
MC_VERIFY_API_HOST=0.0.0.0
MC_VERIFY_API_PORT=8765
MC_VERIFY_API_TOKEN=use-a-long-random-secret
```

In Discord:

```text
/config_set ENABLE_WHITELIST_SYSTEM true
/config_set ENABLE_WHITELIST true
/config_set ENABLE_MC_VERIFY_API true
```

Restart the bot after enabling `ENABLE_WHITELIST_SYSTEM`.

If the main Minecraft server is ready, also configure RCON and run:

```text
/config_set ENABLE_MC_WHITELIST true
```

If the main server is not ready, leave `ENABLE_MC_WHITELIST` false. Verified users will still be stored and included later by:

```text
/whitelist_sync
```
