from datetime import timedelta
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config

try:
    from affiliate_config import get_runtime_affiliate_ids
except ImportError:
    def get_runtime_affiliate_ids() -> set[int]:
        return set()

JOINGUARD_MIN_ACCOUNT_AGE = timedelta(days=7)
JOINGUARD_KICK_REASON = "Joinguard triggered."


class Automation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def allowed_guild_ids(self) -> set[int]:
        guild_ids = {
            config.HOME_GUILD_ID,
            config.BASE_GUILD_ID,
            *get_runtime_affiliate_ids(),
        }

        return {guild_id for guild_id in guild_ids if guild_id}

    async def leave_unapproved_guild(self, guild: discord.Guild, *, source: str) -> str:
        allowed_guild_ids = self.allowed_guild_ids()

        if not allowed_guild_ids:
            print(
                "[automation.py] Skipping auto-leave because no allowed guild IDs are configured."
            )
            return "skipped"

        if guild.id in allowed_guild_ids:
            return "allowed"

        print(
            f"[automation.py] Leaving unapproved guild from {source}: {guild.name} ({guild.id})"
        )

        await self.send_join_log(
            guild,
            "Leaving Unapproved Server",
            (
                f"The bot is leaving **{guild.name}** (`{guild.id}`) because it is not "
                "the configured home server, base server, or an affiliate server."
            ),
            color=discord.Color.orange(),
        )

        try:
            await guild.leave()
            return "left"
        except discord.HTTPException as error:
            print(
                f"[automation.py] Failed to leave unapproved guild {guild.name} ({guild.id}): {error}"
            )
            return "failed"

    async def enforce_allowed_guilds(self, *, source: str) -> dict[str, int]:
        results = {
            "allowed": 0,
            "left": 0,
            "failed": 0,
            "skipped": 0,
        }

        for guild in list(self.bot.guilds):
            status = await self.leave_unapproved_guild(guild, source=source)
            results[status] = results.get(status, 0) + 1

        return results

    async def owner_only_interaction(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and config.is_bot_owner_id(interaction.user.id):
            return True

        await interaction.response.send_message(
            "Only the bot owner can use this command.",
            ephemeral=True,
        )
        return False

    async def send_join_log(
        self,
        guild: Optional[discord.Guild],
        title: str,
        description: str,
        color: discord.Color = discord.Color.red(),
    ):
        if config.LOG_JOINS_THREAD_ID == 0:
            return

        try:
            channel = self.bot.get_channel(config.LOG_JOINS_THREAD_ID)

            if channel is None:
                channel = await self.bot.fetch_channel(config.LOG_JOINS_THREAD_ID)

            if isinstance(channel, discord.Thread) and channel.archived:
                try:
                    await channel.edit(archived=False)
                except discord.HTTPException:
                    pass

            embed = discord.Embed(
                title=title,
                description=description,
                color=color,
                timestamp=discord.utils.utcnow(),
            )

            if guild is not None:
                embed.add_field(
                    name="Server",
                    value=f"{guild.name} (`{guild.id}`)",
                    inline=False,
                )

            await channel.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except Exception:
            pass

    @commands.Cog.listener()
    async def on_ready(self):
        await self.enforce_allowed_guilds(source="startup")

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.leave_unapproved_guild(guild, source="guild join")

    @app_commands.command(
        name="scan_servers",
        description="Scan servers and leave any that are not home, base, or affiliates.",
    )
    async def scan_servers(self, interaction: discord.Interaction):
        if not await self.owner_only_interaction(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        results = await self.enforce_allowed_guilds(
            source=f"manual scan by {interaction.user} ({interaction.user.id})"
        )

        await interaction.followup.send(
            (
                "Server scan complete.\n"
                f"Allowed: `{results['allowed']}`\n"
                f"Left: `{results['left']}`\n"
                f"Failed to leave: `{results['failed']}`\n"
                f"Skipped: `{results['skipped']}`"
            ),
            ephemeral=True,
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != config.HOME_GUILD_ID:
            return

        # -----------------------------
        # Joinguard
        # -----------------------------
        account_age = discord.utils.utcnow() - member.created_at

        if account_age < JOINGUARD_MIN_ACCOUNT_AGE:
            try:
                await member.kick(reason=JOINGUARD_KICK_REASON)

            except discord.Forbidden:
                await self.send_join_log(
                    member.guild,
                    "Joinguard Kick Failed",
                    (
                        f"Could not kick `{member}` (`{member.id}`).\n\n"
                        f"Reason: missing **Kick Members** permission or role hierarchy issue.\n"
                        f"Account age: `{account_age}`"
                    ),
                )

            except discord.HTTPException as error:
                await self.send_join_log(
                    member.guild,
                    "Joinguard Kick Failed",
                    (
                        f"Could not kick `{member}` (`{member.id}`).\n\n"
                        f"Discord HTTP error: `{error}`\n"
                        f"Account age: `{account_age}`"
                    ),
                )

            return

        # -----------------------------
        # Auto-role
        # -----------------------------
        if config.PRIMARY_JOIN_ROLE_ID == 0:
            await self.send_join_log(
                member.guild,
                "Join Role Failed",
                "`PRIMARY_JOIN_ROLE_ID` is not configured.",
            )
            return

        role = member.guild.get_role(config.PRIMARY_JOIN_ROLE_ID)

        if role is None:
            await self.send_join_log(
                member.guild,
                "Join Role Failed",
                f"Could not find join role with ID `{config.PRIMARY_JOIN_ROLE_ID}`.",
            )
            return

        try:
            await member.add_roles(
                role,
                reason="Automatic role given on join.",
            )

        except discord.Forbidden:
            await self.send_join_log(
                member.guild,
                "Join Role Failed",
                (
                    f"Could not add {role.mention} to `{member}` (`{member.id}`).\n\n"
                    f"Reason: missing **Manage Roles** permission or role hierarchy issue."
                ),
            )

        except discord.HTTPException as error:
            await self.send_join_log(
                member.guild,
                "Join Role Failed",
                (
                    f"Could not add {role.mention} to `{member}` (`{member.id}`).\n\n"
                    f"Discord HTTP error: `{error}`"
                ),
            )


async def setup(bot: commands.Bot):
    await bot.add_cog(Automation(bot))
