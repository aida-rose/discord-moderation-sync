import discord
from discord import app_commands
from discord.ext import commands

import config
from affiliate_config import (
    add_affiliate,
    load_affiliates,
    remove_affiliate,
    set_affiliate_enabled,
    set_affiliate_log_channel,
)


def is_bot_owner_interaction(interaction: discord.Interaction) -> bool:
    return (
        interaction.user is not None
        and config.is_bot_owner_id(interaction.user.id)
    )


async def owner_only_check(interaction: discord.Interaction) -> bool:
    if is_bot_owner_interaction(interaction):
        return True

    raise app_commands.CheckFailure("Only the bot owner can use this command.")


def parse_discord_id(value: str, label: str) -> int:
    value = str(value or "").strip()

    if not value.isdigit():
        raise ValueError(f"{label} must be a numeric Discord ID.")

    return int(value)


class AffiliateOwner(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    async def fetch_visible_channel(self, channel_id: int):
        channel = self.bot.get_channel(channel_id)

        if channel is None:
            channel = await self.bot.fetch_channel(channel_id)

        return channel

    async def send_error(self, interaction: discord.Interaction, message: str):
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def send_success(self, interaction: discord.Interaction, message: str):
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    @app_commands.command(
        name="affiliate_add",
        description="Add an organization-approved affiliate server to the runtime config.",
    )
    @app_commands.describe(
        guild_id="Affiliate server ID.",
        log_channel_id="Optional log channel/thread ID for that affiliate.",
        confirm="Set to true to confirm adding the affiliate.",
    )
    @app_commands.check(owner_only_check)
    async def affiliate_add(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        log_channel_id: str | None = None,
        confirm: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            parsed_guild_id = parse_discord_id(guild_id, "Guild ID")
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        parsed_log_channel_id: int | None = None

        if log_channel_id:
            try:
                parsed_log_channel_id = parse_discord_id(log_channel_id, "Log channel/thread ID")
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return

        guild = self.bot.get_guild(parsed_guild_id)

        if guild is None:
            await interaction.followup.send(
                (
                    "I cannot see that server. Make sure the bot is already in the server "
                    "before adding it as an approved affiliate."
                ),
                ephemeral=True,
            )
            return

        if guild.id == config.HOME_GUILD_ID:
            await interaction.followup.send(
                "That is the home server, not an affiliate server.",
                ephemeral=True,
            )
            return

        if parsed_log_channel_id is not None:
            try:
                await self.fetch_visible_channel(parsed_log_channel_id)
            except discord.HTTPException:
                await interaction.followup.send(
                    (
                        "I could not find that log channel/thread ID. "
                        "Make sure the bot can see it, then try again."
                    ),
                    ephemeral=True,
                )
                return

        if not confirm:
            await interaction.followup.send(
                (
                    f"This will add **{guild.name}** (`{guild.id}`) as an organization-approved affiliate server.\n\n"
                    f"Log channel/thread ID: `{parsed_log_channel_id or 'Not set'}`\n\n"
                    "Run this command again with `confirm` set to `True` to continue."
                ),
                ephemeral=True,
            )
            return

        add_affiliate(
            guild_id=guild.id,
            name=guild.name,
            log_channel_id=parsed_log_channel_id,
        )

        await interaction.followup.send(
            (
                f"Added **{guild.name}** (`{guild.id}`) as an organization-approved affiliate server.\n"
                f"Log channel/thread ID: `{parsed_log_channel_id or 'Not set'}`"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="affiliate_remove",
        description="Remove an affiliate server from the runtime config.",
    )
    @app_commands.describe(
        guild_id="Affiliate server ID.",
        confirm="Set to true to confirm removing the affiliate.",
    )
    @app_commands.check(owner_only_check)
    async def affiliate_remove(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        confirm: bool = False,
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            parsed_guild_id = parse_discord_id(guild_id, "Guild ID")
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        if not confirm:
            await interaction.followup.send(
                (
                    "This will remove one affiliate server from the runtime config.\n\n"
                    "Run this command again with `confirm` set to `True` to continue."
                ),
                ephemeral=True,
            )
            return

        removed = remove_affiliate(parsed_guild_id)

        if not removed:
            await interaction.followup.send(
                "That server ID was not found in the runtime affiliate config.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Removed affiliate server `{parsed_guild_id}` from the runtime config.",
            ephemeral=True,
        )

    @app_commands.command(
        name="affiliate_enable",
        description="Enable a saved affiliate server.",
    )
    @app_commands.describe(
        guild_id="Affiliate server ID.",
    )
    @app_commands.check(owner_only_check)
    async def affiliate_enable(
        self,
        interaction: discord.Interaction,
        guild_id: str,
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            parsed_guild_id = parse_discord_id(guild_id, "Guild ID")
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        updated = set_affiliate_enabled(parsed_guild_id, True)

        if not updated:
            await interaction.followup.send(
                "That affiliate server is not in the runtime config yet.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Enabled affiliate server `{parsed_guild_id}`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="affiliate_disable",
        description="Disable a saved affiliate server without deleting its settings.",
    )
    @app_commands.describe(
        guild_id="Affiliate server ID.",
    )
    @app_commands.check(owner_only_check)
    async def affiliate_disable(
        self,
        interaction: discord.Interaction,
        guild_id: str,
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            parsed_guild_id = parse_discord_id(guild_id, "Guild ID")
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        updated = set_affiliate_enabled(parsed_guild_id, False)

        if not updated:
            await interaction.followup.send(
                "That affiliate server is not in the runtime config yet.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Disabled affiliate server `{parsed_guild_id}` without deleting its saved settings.",
            ephemeral=True,
        )

    @app_commands.command(
        name="affiliate_log",
        description="Set or clear an affiliate server's log channel/thread ID.",
    )
    @app_commands.describe(
        guild_id="Affiliate server ID.",
        log_channel_id="Log channel/thread ID. Leave blank to clear it.",
    )
    @app_commands.check(owner_only_check)
    async def affiliate_log(
        self,
        interaction: discord.Interaction,
        guild_id: str,
        log_channel_id: str | None = None,
    ):
        await interaction.response.defer(ephemeral=True)

        try:
            parsed_guild_id = parse_discord_id(guild_id, "Guild ID")
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        parsed_log_channel_id: int | None = None

        if log_channel_id:
            try:
                parsed_log_channel_id = parse_discord_id(log_channel_id, "Log channel/thread ID")
            except ValueError as exc:
                await interaction.followup.send(str(exc), ephemeral=True)
                return

            try:
                await self.fetch_visible_channel(parsed_log_channel_id)
            except discord.HTTPException:
                await interaction.followup.send(
                    "I could not find that log channel/thread ID or I cannot see it.",
                    ephemeral=True,
                )
                return

        updated = set_affiliate_log_channel(parsed_guild_id, parsed_log_channel_id)

        if not updated:
            await interaction.followup.send(
                "That affiliate server is not in the runtime config yet.",
                ephemeral=True,
            )
            return

        await interaction.followup.send(
            f"Updated affiliate `{parsed_guild_id}` log channel/thread ID to `{parsed_log_channel_id or 'Not set'}`.",
            ephemeral=True,
        )

    @app_commands.command(
        name="affiliate_list",
        description="List runtime affiliate servers.",
    )
    @app_commands.check(owner_only_check)
    async def affiliate_list(self, interaction: discord.Interaction):
        await interaction.response.defer(ephemeral=True)

        affiliates = load_affiliates()

        if not affiliates:
            await interaction.followup.send(
                "No runtime affiliate servers are configured.",
                ephemeral=True,
            )
            return

        lines = []

        for guild_id, info in sorted(affiliates.items(), key=lambda item: item[0]):
            name = info.get("name", "Unknown")
            log_channel_id = info.get("log_channel_id") or "Not set"
            enabled = info.get("enabled", True)

            lines.append(
                f"**{name}** (`{guild_id}`)\n"
                f"Log channel/thread: `{log_channel_id}`\n"
                f"Enabled: `{enabled}`"
            )

        message = "\n\n".join(lines)

        if len(message) > 1900:
            message = message[:1897] + "..."

        await interaction.followup.send(message, ephemeral=True)

    async def cog_app_command_error(
        self,
        interaction: discord.Interaction,
        error: app_commands.AppCommandError,
    ):
        if isinstance(error, app_commands.CheckFailure):
            await self.send_error(
                interaction,
                "Only the bot owner can use this command.",
            )
            return

        await self.send_error(
            interaction,
            f"Command failed: `{error}`",
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(AffiliateOwner(bot))
