import os
import sys
import asyncio
from datetime import datetime, timezone

import discord
from discord import app_commands
from discord.ext import commands

import config


ID_LIST_SETTINGS = {
    "AFFILIATE_GUILD_IDS",
    "LOGGED_GUILD_IDS",
    "STAFF_ROLE_IDS",
    "BAN_STAFF_ROLE_IDS",
}

BOOL_SETTINGS = {
    "SEND_USER_NOTICES",
    "ENABLE_NATION_SELECTOR",
    "ENABLE_TICKETS",
}

INT_SETTINGS = {
    key
    for key in config.SETTING_DEFAULTS
    if (
        key.endswith("_ID")
        or key.endswith("_SECONDS")
        or key.endswith("_THREAD_ID")
    )
    and key not in ID_LIST_SETTINGS
}


def parse_csv_ids(raw: str) -> list[int]:
    ids: list[int] = []

    for item in raw.split(","):
        item = item.strip()

        if not item:
            continue

        if not item.isdigit():
            raise ValueError("Expected a comma-separated list of numeric Discord IDs.")

        parsed = int(item)
        if parsed not in ids:
            ids.append(parsed)

    return ids


def parse_bool(raw: str) -> str:
    normalized = raw.strip().lower()

    if normalized in {"true", "1", "yes", "y", "on"}:
        return "true"

    if normalized in {"false", "0", "no", "n", "off"}:
        return "false"

    raise ValueError("Expected a boolean value like true or false.")


def normalize_setting_value(key: str, value: str) -> str:
    key = key.upper()
    value = str(value).strip()

    if key not in config.SETTING_DEFAULTS:
        raise KeyError(key)

    if key in BOOL_SETTINGS:
        return parse_bool(value)

    if key in ID_LIST_SETTINGS:
        return ",".join(str(item) for item in parse_csv_ids(value))

    if key in INT_SETTINGS:
        if not value:
            return "0"

        if not value.isdigit():
            raise ValueError(f"`{key}` must be a number.")

        return str(int(value))

    if key == "AFFILIATE_LOG_ROUTES":
        if not value:
            return ""

        pairs = []
        for pair in value.split(","):
            pair = pair.strip()
            if not pair:
                continue

            if ":" not in pair:
                raise ValueError("Log routes must use `guild_id:channel_or_thread_id` pairs.")

            guild_id, channel_id = pair.split(":", 1)
            guild_id = guild_id.strip()
            channel_id = channel_id.strip()

            if not guild_id.isdigit() or not channel_id.isdigit():
                raise ValueError("Log route guild IDs and channel/thread IDs must be numeric.")

            pairs.append(f"{int(guild_id)}:{int(channel_id)}")

        return ",".join(pairs)

    return value


def setting_summary(key: str, value: str) -> str:
    description = config.SETTING_DESCRIPTIONS.get(key, "")
    suffix = f" - {description}" if description else ""
    display_value = value if value != "" else "(empty)"
    return f"`{key}` = `{display_value}`{suffix}"


async def setting_key_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    current = current.lower()
    matches = [
        key
        for key in config.setting_keys()
        if current in key.lower()
    ]

    return [
        app_commands.Choice(name=key, value=key)
        for key in matches[:25]
    ]

def format_uptime(started_at: datetime) -> str:
    delta = datetime.now(timezone.utc) - started_at
    days = delta.days
    hours, remainder = divmod(delta.seconds, 3600)
    minutes, seconds = divmod(remainder, 60)

    parts = []

    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")

    parts.append(f"{seconds}s")

    return " ".join(parts)


class Owner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.started_at = datetime.now(timezone.utc)

    async def owner_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and config.is_bot_owner_id(interaction.user.id):
            return True
    
        if interaction.response.is_done():
            await interaction.followup.send(
                "You are not allowed to use this command.",
                ephemeral=True,
            )
        else:
            await interaction.response.send_message(
                "You are not allowed to use this command.",
                ephemeral=True,
            )
    
        return False

    @app_commands.command(
        name="ping",
        description="Check the bot latency.",
    )
    async def ping(self, interaction: discord.Interaction):
        if not await self.owner_check(interaction):
            return

        latency_ms = round(self.bot.latency * 1000)

        await interaction.response.send_message(
            f"Pong. Latency: `{latency_ms}ms`",
            ephemeral=True,
        )

    @app_commands.command(
        name="botstats",
        description="Show basic bot runtime stats.",
    )
    async def botstats(self, interaction: discord.Interaction):
        if not await self.owner_check(interaction):
            return

        guild_count = len(self.bot.guilds)
        cached_user_count = len(self.bot.users)
        cached_channel_count = len(list(self.bot.get_all_channels()))
        command_count = len(self.bot.commands)
        slash_command_count = len(self.bot.tree.get_commands())

        total_members_known = 0
        for guild in self.bot.guilds:
            if guild.member_count:
                total_members_known += guild.member_count

        embed = discord.Embed(
            title="Bot Stats",
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="Latency",
            value=f"{round(self.bot.latency * 1000)}ms",
            inline=True,
        )

        embed.add_field(
            name="Uptime",
            value=format_uptime(self.started_at),
            inline=True,
        )

        embed.add_field(
            name="Guilds",
            value=str(guild_count),
            inline=True,
        )

        embed.add_field(
            name="Known Members",
            value=str(total_members_known),
            inline=True,
        )

        embed.add_field(
            name="Cached Users",
            value=str(cached_user_count),
            inline=True,
        )

        embed.add_field(
            name="Cached Channels",
            value=str(cached_channel_count),
            inline=True,
        )

        embed.add_field(
            name="Prefix Commands",
            value=str(command_count),
            inline=True,
        )

        embed.add_field(
            name="Slash Commands",
            value=str(slash_command_count),
            inline=True,
        )

        embed.add_field(
            name="Python",
            value=f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            inline=True,
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    @app_commands.command(
        name="cacheinfo",
        description="Show what the bot currently has cached.",
    )
    async def cacheinfo(self, interaction: discord.Interaction):
        if not await self.owner_check(interaction):
            return

        guild_lines = []

        for guild in self.bot.guilds:
            cached_members = len(guild.members)
            member_count = guild.member_count or 0
            text_channels = len(guild.text_channels)
            voice_channels = len(guild.voice_channels)
            roles = len(guild.roles)

            guild_lines.append(
                f"**{guild.name}** `({guild.id})`\n"
                f"Members: `{cached_members}` cached / `{member_count}` known\n"
                f"Channels: `{text_channels}` text, `{voice_channels}` voice\n"
                f"Roles: `{roles}`"
            )

        description = "\n\n".join(guild_lines)

        if not description:
            description = "No guilds cached."

        if len(description) > 3900:
            description = description[:3897] + "..."

        embed = discord.Embed(
            title="Cache Info",
            description=description,
            color=discord.Color.blurple(),
        )

        embed.add_field(
            name="Cached Users",
            value=str(len(self.bot.users)),
            inline=True,
        )

        embed.add_field(
            name="Cached Guilds",
            value=str(len(self.bot.guilds)),
            inline=True,
        )

        embed.add_field(
            name="Cached Channels",
            value=str(len(list(self.bot.get_all_channels()))),
            inline=True,
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
        )

    @app_commands.command(
        name="sync",
        description="Sync slash commands.",
    )
    @app_commands.describe(
        mode="Choose whether to sync globally or only to this server.",
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name="global", value="global"),
            app_commands.Choice(name="this_server", value="guild"),
            app_commands.Choice(name="copy_global_to_this_server", value="copy"),
        ]
    )
    async def sync(
        self,
        interaction: discord.Interaction,
        mode: app_commands.Choice[str],
    ):
        if not await self.owner_check(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        try:
            if mode.value == "global":
                synced = await self.bot.tree.sync()
                await interaction.followup.send(
                    f"Synced `{len(synced)}` global slash command(s). "
                    "Global commands may take a while to appear everywhere.",
                    ephemeral=True,
                )

            elif mode.value == "guild":
                if interaction.guild is None:
                    await interaction.followup.send(
                        "This option can only be used inside a server.",
                        ephemeral=True,
                    )
                    return

                synced = await self.bot.tree.sync(guild=interaction.guild)
                await interaction.followup.send(
                    f"Synced `{len(synced)}` slash command(s) to this server.",
                    ephemeral=True,
                )

            elif mode.value == "copy":
                if interaction.guild is None:
                    await interaction.followup.send(
                        "This option can only be used inside a server.",
                        ephemeral=True,
                    )
                    return

                self.bot.tree.copy_global_to(guild=interaction.guild)
                synced = await self.bot.tree.sync(guild=interaction.guild)

                await interaction.followup.send(
                    f"Copied global commands and synced `{len(synced)}` command(s) to this server.",
                    ephemeral=True,
                )

        except Exception as error:
            await interaction.followup.send(
                f"Slash command sync failed: `{type(error).__name__}: {error}`",
                ephemeral=True,
            )

    @app_commands.command(
        name="config_list",
        description="List SQLite-backed bot settings.",
    )
    @app_commands.describe(
        configured_only="Only show settings currently saved in SQLite.",
    )
    async def config_list(
        self,
        interaction: discord.Interaction,
        configured_only: bool = False,
    ):
        if not await self.owner_check(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        settings = config.all_settings()

        if configured_only:
            settings = {
                key: value
                for key, value in settings.items()
                if value != config.SETTING_DEFAULTS.get(key, "")
            }

        if not settings:
            await interaction.followup.send(
                "No SQLite-backed settings are configured yet.",
                ephemeral=True,
            )
            return

        lines = [
            setting_summary(key, settings[key])
            for key in sorted(settings)
        ]

        chunks: list[str] = []
        current = ""

        for line in lines:
            candidate = f"{current}\n{line}" if current else line

            if len(candidate) > 1800:
                chunks.append(current)
                current = line
            else:
                current = candidate

        if current:
            chunks.append(current)

        await interaction.followup.send(chunks[0], ephemeral=True)

        for chunk in chunks[1:]:
            await interaction.followup.send(chunk, ephemeral=True)

    @app_commands.command(
        name="config_get",
        description="Show one SQLite-backed bot setting.",
    )
    @app_commands.describe(
        key="Setting key.",
    )
    @app_commands.autocomplete(key=setting_key_autocomplete)
    async def config_get(
        self,
        interaction: discord.Interaction,
        key: str,
    ):
        if not await self.owner_check(interaction):
            return

        key = key.upper()

        if key not in config.SETTING_DEFAULTS:
            await interaction.response.send_message(
                f"Unknown setting `{key}`.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            setting_summary(key, config.get_setting(key)),
            ephemeral=True,
        )

    @app_commands.command(
        name="config_set",
        description="Set one SQLite-backed bot setting.",
    )
    @app_commands.describe(
        key="Setting key.",
        value="New value. Use comma-separated IDs for list settings.",
    )
    @app_commands.autocomplete(key=setting_key_autocomplete)
    async def config_set(
        self,
        interaction: discord.Interaction,
        key: str,
        value: str,
    ):
        if not await self.owner_check(interaction):
            return

        key = key.upper()

        try:
            normalized = normalize_setting_value(key, value)
        except KeyError:
            await interaction.response.send_message(
                f"Unknown setting `{key}`.",
                ephemeral=True,
            )
            return
        except ValueError as exc:
            await interaction.response.send_message(str(exc), ephemeral=True)
            return

        config.set_setting(key, normalized)

        if key == "COMMAND_PREFIX":
            self.bot.command_prefix = normalized

        restart_note = ""
        if key in {"ENABLE_NATION_SELECTOR", "ENABLE_TICKETS"}:
            restart_note = "\nRestart the bot for cog loading changes to apply."

        await interaction.response.send_message(
            f"Updated {setting_summary(key, config.get_setting(key))}.{restart_note}",
            ephemeral=True,
        )

    @app_commands.command(
        name="config_clear",
        description="Clear a SQLite-backed bot setting back to its default.",
    )
    @app_commands.describe(
        key="Setting key.",
    )
    @app_commands.autocomplete(key=setting_key_autocomplete)
    async def config_clear(
        self,
        interaction: discord.Interaction,
        key: str,
    ):
        if not await self.owner_check(interaction):
            return

        key = key.upper()

        try:
            config.clear_setting(key)
        except KeyError:
            await interaction.response.send_message(
                f"Unknown setting `{key}`.",
                ephemeral=True,
            )
            return

        if key == "COMMAND_PREFIX":
            self.bot.command_prefix = config.COMMAND_PREFIX

        await interaction.response.send_message(
            f"Cleared `{key}` back to default `{config.get_setting(key) or '(empty)'}`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="config_id_add",
        description="Add one ID to a comma-separated ID setting.",
    )
    @app_commands.describe(
        key="ID-list setting key.",
        discord_id="Discord ID to add.",
    )
    @app_commands.autocomplete(key=setting_key_autocomplete)
    async def config_id_add(
        self,
        interaction: discord.Interaction,
        key: str,
        discord_id: str,
    ):
        if not await self.owner_check(interaction):
            return

        key = key.upper()

        if key not in ID_LIST_SETTINGS:
            await interaction.response.send_message(
                f"`{key}` is not an ID-list setting.",
                ephemeral=True,
            )
            return

        if not discord_id.isdigit():
            await interaction.response.send_message(
                "Discord ID must be numeric.",
                ephemeral=True,
            )
            return

        ids = parse_csv_ids(config.get_setting(key))
        parsed_id = int(discord_id)

        if parsed_id not in ids:
            ids.append(parsed_id)

        config.set_setting(key, ",".join(str(item) for item in ids))

        await interaction.response.send_message(
            f"Added `{parsed_id}` to `{key}`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="config_id_remove",
        description="Remove one ID from a comma-separated ID setting.",
    )
    @app_commands.describe(
        key="ID-list setting key.",
        discord_id="Discord ID to remove.",
    )
    @app_commands.autocomplete(key=setting_key_autocomplete)
    async def config_id_remove(
        self,
        interaction: discord.Interaction,
        key: str,
        discord_id: str,
    ):
        if not await self.owner_check(interaction):
            return

        key = key.upper()

        if key not in ID_LIST_SETTINGS:
            await interaction.response.send_message(
                f"`{key}` is not an ID-list setting.",
                ephemeral=True,
            )
            return

        if not discord_id.isdigit():
            await interaction.response.send_message(
                "Discord ID must be numeric.",
                ephemeral=True,
            )
            return

        parsed_id = int(discord_id)
        ids = [
            item
            for item in parse_csv_ids(config.get_setting(key))
            if item != parsed_id
        ]

        config.set_setting(key, ",".join(str(item) for item in ids))

        await interaction.response.send_message(
            f"Removed `{parsed_id}` from `{key}`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="shutdown",
        description="Shut down the bot.",
    )
    async def shutdown(self, interaction: discord.Interaction):
        if not await self.owner_check(interaction):
            return

        await interaction.response.send_message(
            "Shutting down the bot.",
            ephemeral=True,
        )

        await self.bot.close()

    @app_commands.command(
        name="restart",
        description="Restart the bot process.",
    )
    async def restart(self, interaction: discord.Interaction):
        if not await self.owner_check(interaction):
            return

        await interaction.response.send_message(
            "Restarting the bot.",
            ephemeral=True,
        )

        await asyncio.sleep(1)

        os.execv(
            sys.executable,
            [sys.executable, *sys.argv],
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Owner(bot))
