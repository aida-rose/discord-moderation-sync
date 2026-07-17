from datetime import timedelta
from typing import Optional

import discord
from discord.ext import commands

import config

JOINGUARD_MIN_ACCOUNT_AGE = timedelta(days=7)
JOINGUARD_KICK_REASON = "Joinguard triggered."


class Automation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

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
