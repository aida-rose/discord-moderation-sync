import asyncio
import os
import re
import sqlite3
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from storage import moderation_db

try:
    from mcrcon import MCRcon
except ImportError:
    MCRcon = None


JAVA_USERNAME_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
PLATFORM_LABELS = {
    "java": "Java",
    "bedrock": "Bedrock",
}
ACTIVE_STATUS = "linked"
ABSENT_STATUS = "discord_absent"
BANNED_STATUS = "discord_banned"


class LinkExists(Exception):
    pass


def init_db() -> None:
    with moderation_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS minecraft_account_links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                discord_user_id INTEGER NOT NULL UNIQUE,
                platform TEXT NOT NULL CHECK(platform IN ('java', 'bedrock')),
                entered_name TEXT NOT NULL,
                server_name TEXT NOT NULL,
                server_name_normalized TEXT NOT NULL UNIQUE,
                status TEXT NOT NULL,
                last_rcon_action TEXT,
                last_rcon_result TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def clean_player_name(platform: str, raw_name: str) -> str:
    name = " ".join(str(raw_name).strip().split())

    if not name:
        raise ValueError("Minecraft name cannot be empty.")

    if platform == "java":
        if not JAVA_USERNAME_RE.fullmatch(name):
            raise ValueError("Java usernames must be 3-16 characters and only use letters, numbers, or underscores.")
        return name

    prefix = config.BEDROCK_USERNAME_PREFIX
    if prefix and name.startswith(prefix):
        name = " ".join(name[len(prefix):].strip().split())

    if not 3 <= len(name) <= 32:
        raise ValueError("Bedrock gamertags must be 3-32 characters.")

    blocked_chars = {'"', "\\", "\n", "\r", "\t", "`"}
    if any(char in blocked_chars for char in name):
        raise ValueError("Bedrock gamertags cannot include quotes, backslashes, tabs, or line breaks.")

    return name


def server_whitelist_name(platform: str, entered_name: str) -> str:
    if platform == "bedrock":
        return f"{config.BEDROCK_USERNAME_PREFIX}{entered_name}"

    return entered_name


def normalize_server_name(name: str) -> str:
    return " ".join(name.strip().split()).casefold()


def quote_command_arg(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def truncate(text: str, limit: int = 700) -> str:
    if len(text) <= limit:
        return text

    return text[: limit - 3] + "..."


def format_link(row) -> str:
    platform = PLATFORM_LABELS.get(row["platform"], row["platform"])
    return (
        f"Discord: <@{row['discord_user_id']}> (`{row['discord_user_id']}`)\n"
        f"Platform: `{platform}`\n"
        f"Entered name: `{row['entered_name']}`\n"
        f"Server whitelist name: `{row['server_name']}`\n"
        f"Status: `{row['status']}`"
    )


def format_user(user: Optional[discord.abc.User]) -> str:
    if user is None:
        return "Unknown"

    return f"<@{user.id}> {user} (`{user.id}`)"


def format_user_id(user_id: int | str | None) -> str:
    if user_id in (None, ""):
        return "Unknown"

    return f"<@{user_id}> (`{user_id}`)"


def avatar_url(user: Optional[discord.abc.User]) -> str | None:
    if user is None:
        return None

    return user.display_avatar.url


def rcon_failed(result: str) -> bool:
    return result.lower().startswith("minecraft whitelist: failed")


def get_link_for_discord(discord_user_id: int):
    with moderation_db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM minecraft_account_links
            WHERE discord_user_id = ?
            """,
            (discord_user_id,),
        ).fetchone()


def create_link(discord_user_id: int, platform: str, raw_name: str):
    entered_name = clean_player_name(platform, raw_name)
    server_name = server_whitelist_name(platform, entered_name)
    normalized = normalize_server_name(server_name)

    with moderation_db() as conn:
        existing_discord = conn.execute(
            """
            SELECT *
            FROM minecraft_account_links
            WHERE discord_user_id = ?
            """,
            (discord_user_id,),
        ).fetchone()

        if existing_discord is not None:
            raise LinkExists("Your Discord account is already linked. Ask an admin to remove the link if you need to start over.")

        existing_mc = conn.execute(
            """
            SELECT *
            FROM minecraft_account_links
            WHERE server_name_normalized = ?
            """,
            (normalized,),
        ).fetchone()

        if existing_mc is not None:
            raise LinkExists("That Minecraft account is already linked to a Discord account.")

        try:
            conn.execute(
                """
                INSERT INTO minecraft_account_links (
                    discord_user_id,
                    platform,
                    entered_name,
                    server_name,
                    server_name_normalized,
                    status
                )
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    discord_user_id,
                    platform,
                    entered_name,
                    server_name,
                    normalized,
                    ACTIVE_STATUS,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise LinkExists("That Discord or Minecraft account is already linked.") from exc

    return get_link_for_discord(discord_user_id)


def set_link_status(
    discord_user_id: int,
    status: str,
    *,
    rcon_action: Optional[str] = None,
    rcon_result: Optional[str] = None,
) -> None:
    with moderation_db() as conn:
        conn.execute(
            """
            UPDATE minecraft_account_links
            SET status = ?,
                last_rcon_action = COALESCE(?, last_rcon_action),
                last_rcon_result = COALESCE(?, last_rcon_result),
                updated_at = CURRENT_TIMESTAMP
            WHERE discord_user_id = ?
            """,
            (status, rcon_action, rcon_result, discord_user_id),
        )


def delete_link(discord_user_id: int) -> None:
    with moderation_db() as conn:
        conn.execute(
            """
            DELETE FROM minecraft_account_links
            WHERE discord_user_id = ?
            """,
            (discord_user_id,),
        )


def all_links():
    with moderation_db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM minecraft_account_links
            ORDER BY created_at ASC
            """
        ).fetchall()


class WhitelistPanelView(discord.ui.View):
    def __init__(self, cog: "Whitelist"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Start Whitelist",
        style=discord.ButtonStyle.primary,
        custom_id="minecraft_whitelist:start",
    )
    async def start_whitelist(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        if not self.cog.whitelist_available(interaction):
            await interaction.response.send_message(
                "Whitelist linking is not enabled yet.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            "Which edition do you play on?",
            view=PlatformSelectView(self.cog),
            ephemeral=True,
        )


class PlatformSelectView(discord.ui.View):
    def __init__(self, cog: "Whitelist"):
        super().__init__(timeout=180)
        self.add_item(PlatformSelect(cog))


class PlatformSelect(discord.ui.Select):
    def __init__(self, cog: "Whitelist"):
        self.cog = cog
        options = [
            discord.SelectOption(
                label="Java",
                value="java",
                description="Use this for a normal Java Edition account.",
            ),
            discord.SelectOption(
                label="Bedrock",
                value="bedrock",
                description="Use this for Bedrock joining through Geyser.",
            ),
        ]
        super().__init__(
            placeholder="Choose Java or Bedrock",
            min_values=1,
            max_values=1,
            options=options,
        )

    async def callback(self, interaction: discord.Interaction):
        platform = self.values[0]
        await interaction.response.send_modal(MinecraftNameModal(self.cog, platform))


class MinecraftNameModal(discord.ui.Modal):
    def __init__(self, cog: "Whitelist", platform: str):
        self.cog = cog
        self.platform = platform
        label = "Java username" if platform == "java" else "Bedrock gamertag"
        super().__init__(title=f"Link {PLATFORM_LABELS[platform]} Account")

        self.player_name = discord.ui.TextInput(
            label=label,
            placeholder="Cool Steve" if platform == "bedrock" else "Steve",
            min_length=3,
            max_length=32,
            required=True,
        )
        self.add_item(self.player_name)

    async def on_submit(self, interaction: discord.Interaction):
        if not self.cog.whitelist_available(interaction):
            await interaction.response.send_message(
                "Whitelist linking is not enabled yet.",
                ephemeral=True,
            )
            return

        await interaction.response.defer(ephemeral=True)

        try:
            link = create_link(interaction.user.id, self.platform, str(self.player_name.value))
        except (ValueError, LinkExists) as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        role_result = await self.cog.apply_whitelist_role(interaction.guild, interaction.user)
        rcon_result = await self.cog.add_mc_whitelist(link)

        await self.cog.send_whitelist_log(
            guild=interaction.guild,
            title="Whitelist Account Linked",
            user=interaction.user,
            fields=[
                ("Link", format_link(link), False),
                ("Discord Role", role_result, False),
                ("Minecraft RCON", rcon_result, False),
            ],
            color=discord.Color.green(),
        )

        details = [
            "Your account link was saved.",
            "",
            format_link(link),
            "",
            role_result,
            rcon_result,
        ]

        await interaction.followup.send("\n".join(details), ephemeral=True)


class Whitelist(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def whitelist_available(self, interaction: discord.Interaction) -> bool:
        if not config.ENABLE_WHITELIST:
            return False

        if interaction.guild is None:
            return False

        if config.HOME_GUILD_ID and interaction.guild.id != config.HOME_GUILD_ID:
            return False

        return True

    async def get_whitelist_log_thread(self):
        thread_id = config.LOG_WHITELIST_THREAD_ID or config.LOG_OTHER_THREAD_ID

        if thread_id == 0:
            return None

        channel = self.bot.get_channel(thread_id)

        if channel is None:
            channel = await self.bot.fetch_channel(thread_id)

        if isinstance(channel, discord.Thread) and channel.archived:
            try:
                await channel.edit(archived=False)
            except discord.HTTPException:
                pass

        return channel

    async def send_whitelist_log(
        self,
        *,
        guild: Optional[discord.Guild],
        title: str,
        user: Optional[discord.abc.User] = None,
        user_id: int | None = None,
        fields: Optional[list[tuple[str, str, bool]]] = None,
        description: Optional[str] = None,
        color: discord.Color = discord.Color.blurple(),
    ) -> None:
        logging_cog = self.bot.get_cog("Logging")

        user_field = format_user(user) if user is not None else format_user_id(user_id)
        all_fields = [("User", user_field, False)]

        if fields:
            all_fields.extend(fields)

        if logging_cog is not None and hasattr(logging_cog, "send_log"):
            await logging_cog.send_log(
                category="whitelist",
                guild=guild,
                title=title,
                description=description,
                fields=all_fields,
                color=color,
                thumbnail_url=avatar_url(user),
            )
            return

        thread = await self.get_whitelist_log_thread()
        if thread is None:
            return

        embed = discord.Embed(
            title=title,
            description=truncate(description, 4096) if description else None,
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        if user is not None:
            embed.set_thumbnail(url=user.display_avatar.url)

        if guild is not None:
            embed.add_field(
                name="Server",
                value=f"{guild.name} (`{guild.id}`)",
                inline=False,
            )

        for name, value, inline in all_fields:
            embed.add_field(
                name=truncate(name, 256),
                value=truncate(value, 1024),
                inline=inline,
            )

        embed.set_footer(text="Log category: whitelist")

        try:
            await thread.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            print(f"[whitelist.py] Failed to send whitelist log: {exc}")

    async def admin_check(self, interaction: discord.Interaction) -> bool:
        if interaction.guild is None:
            await interaction.response.send_message(
                "This command can only be used inside the home server.",
                ephemeral=True,
            )
            return False

        if config.HOME_GUILD_ID and interaction.guild.id != config.HOME_GUILD_ID:
            await interaction.response.send_message(
                "Whitelist commands can only be used inside the configured home server.",
                ephemeral=True,
            )
            return False

        if interaction.user is not None and config.is_bot_owner_id(interaction.user.id):
            return True

        if isinstance(interaction.user, discord.Member) and interaction.user.guild_permissions.administrator:
            return True

        await interaction.response.send_message(
            "Only a server administrator or bot owner can use this command.",
            ephemeral=True,
        )
        return False

    async def apply_whitelist_role(
        self,
        guild: Optional[discord.Guild],
        user: discord.User | discord.Member,
    ) -> str:
        if guild is None:
            return "Discord role: skipped because this was not used in a server."

        if config.WHITELIST_ROLE_ID == 0:
            return "Discord role: skipped because `WHITELIST_ROLE_ID` is not configured."

        role = guild.get_role(config.WHITELIST_ROLE_ID)
        if role is None:
            return f"Discord role: failed because role `{config.WHITELIST_ROLE_ID}` was not found."

        member = user if isinstance(user, discord.Member) else guild.get_member(user.id)
        if member is None:
            try:
                member = await guild.fetch_member(user.id)
            except discord.NotFound:
                return "Discord role: skipped because the user is not in the server."
            except discord.HTTPException as exc:
                return f"Discord role: failed to fetch member: `{exc}`"

        if role in member.roles:
            return f"Discord role: {role.mention} was already applied."

        try:
            await member.add_roles(
                role,
                reason="Minecraft whitelist account linked.",
            )
            return f"Discord role: applied {role.mention}."
        except discord.Forbidden:
            return "Discord role: failed because the bot is missing Manage Roles or has a lower role."
        except discord.HTTPException as exc:
            return f"Discord role: failed with Discord HTTP error: `{exc}`"

    async def remove_whitelist_role(
        self,
        guild: Optional[discord.Guild],
        user_id: int,
        *,
        reason: str,
    ) -> str:
        if guild is None or config.WHITELIST_ROLE_ID == 0:
            return "Discord role removal: skipped."

        role = guild.get_role(config.WHITELIST_ROLE_ID)
        if role is None:
            return f"Discord role removal: failed because role `{config.WHITELIST_ROLE_ID}` was not found."

        member = guild.get_member(user_id)
        if member is None:
            try:
                member = await guild.fetch_member(user_id)
            except discord.NotFound:
                return "Discord role removal: skipped because the user is not in the server."
            except discord.HTTPException as exc:
                return f"Discord role removal: failed to fetch member: `{exc}`"

        if role not in member.roles:
            return f"Discord role removal: {role.mention} was not present."

        try:
            await member.remove_roles(role, reason=reason)
            return f"Discord role removal: removed {role.mention}."
        except discord.Forbidden:
            return "Discord role removal: failed because the bot is missing Manage Roles or has a lower role."
        except discord.HTTPException as exc:
            return f"Discord role removal: failed with Discord HTTP error: `{exc}`"

    def rcon_enabled_message(self) -> Optional[str]:
        if not config.ENABLE_MC_WHITELIST:
            return "Minecraft whitelist: skipped because `ENABLE_MC_WHITELIST` is false."

        if MCRcon is None:
            return "Minecraft whitelist: failed because the `mcrcon` package is not installed."

        if not os.getenv("MC_RCON_HOST") or not os.getenv("MC_RCON_PASSWORD"):
            return "Minecraft whitelist: failed because `MC_RCON_HOST` or `MC_RCON_PASSWORD` is missing from `.env`."

        return None

    async def rcon_command(self, command: str) -> str:
        skipped = self.rcon_enabled_message()
        if skipped is not None:
            return skipped

        host = os.getenv("MC_RCON_HOST", "")
        password = os.getenv("MC_RCON_PASSWORD", "")
        try:
            port = int(os.getenv("MC_RCON_PORT", "25575"))

            def run_command() -> str:
                with MCRcon(host, password, port=port) as rcon:
                    return str(rcon.command(command))

            result = await asyncio.to_thread(run_command)
        except Exception as exc:
            return f"Minecraft whitelist: failed `{type(exc).__name__}: {exc}`"

        return f"Minecraft whitelist: `{command}` -> `{truncate(result)}`"

    async def add_mc_whitelist(self, link) -> str:
        command = f"whitelist add {quote_command_arg(link['server_name'])}"
        result = await self.rcon_command(command)
        set_link_status(
            link["discord_user_id"],
            ACTIVE_STATUS,
            rcon_action=command,
            rcon_result=result,
        )
        return result

    async def remove_mc_whitelist(self, link, *, status: str) -> str:
        command = f"whitelist remove {quote_command_arg(link['server_name'])}"
        result = await self.rcon_command(command)
        set_link_status(
            link["discord_user_id"],
            status,
            rcon_action=command,
            rcon_result=result,
        )
        return result

    @app_commands.command(
        name="whitelist_panel",
        description="Send the Minecraft whitelist linking panel.",
    )
    @app_commands.describe(
        channel="Where to send the panel. Defaults to the current channel.",
    )
    async def whitelist_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not await self.admin_check(interaction):
            return

        target = channel or interaction.channel

        if target is None or not hasattr(target, "send"):
            await interaction.response.send_message(
                "I could not find a text channel to send the panel to.",
                ephemeral=True,
            )
            return

        embed = discord.Embed(
            title="Minecraft Whitelist",
            description="Click the button below to link your Java or Bedrock account.",
            color=discord.Color.green(),
        )

        await target.send(embed=embed, view=WhitelistPanelView(self))
        await self.send_whitelist_log(
            guild=interaction.guild,
            title="Whitelist Panel Sent",
            user=interaction.user,
            fields=[
                ("Channel", target.mention, False),
            ],
            color=discord.Color.green(),
        )
        await interaction.response.send_message(
            f"Whitelist panel sent to {target.mention}.",
            ephemeral=True,
        )

    @app_commands.command(
        name="whitelist_unlink",
        description="Remove a user's Minecraft account link so they can link again.",
    )
    @app_commands.describe(
        user="Discord user whose link should be removed.",
        reason="Reason shown in audit logs.",
    )
    async def whitelist_unlink(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        reason: str = "Whitelist link removed by an admin.",
    ):
        if not await self.admin_check(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        link = get_link_for_discord(user.id)
        if link is None:
            await interaction.followup.send(
                f"<@{user.id}> does not have a Minecraft account link.",
                ephemeral=True,
            )
            return

        rcon_result = await self.remove_mc_whitelist(link, status="admin_unlinked")
        role_result = await self.remove_whitelist_role(
            interaction.guild,
            user.id,
            reason=reason,
        )
        delete_link(user.id)

        await self.send_whitelist_log(
            guild=interaction.guild,
            title="Whitelist Link Removed",
            user=user,
            fields=[
                ("Admin", format_user(interaction.user), False),
                ("Removed Link", format_link(link), False),
                ("Discord Role", role_result, False),
                ("Minecraft RCON", rcon_result, False),
                ("Reason", reason, False),
            ],
            color=discord.Color.orange(),
        )

        await interaction.followup.send(
            (
                "Removed account link.\n\n"
                f"{format_link(link)}\n\n"
                f"{role_result}\n"
                f"{rcon_result}"
            ),
            ephemeral=True,
        )

    @app_commands.command(
        name="whitelist_status",
        description="Show a user's Minecraft account link status.",
    )
    @app_commands.describe(
        user="Discord user to inspect.",
    )
    async def whitelist_status(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ):
        if not await self.admin_check(interaction):
            return

        link = get_link_for_discord(user.id)
        if link is None:
            await interaction.response.send_message(
                f"<@{user.id}> does not have a Minecraft account link.",
                ephemeral=True,
            )
            return

        await interaction.response.send_message(
            format_link(link),
            ephemeral=True,
        )

    @app_commands.command(
        name="whitelist_sync",
        description="Sync saved account links against Discord membership and Minecraft RCON.",
    )
    async def whitelist_sync(self, interaction: discord.Interaction):
        if not await self.admin_check(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        guild = interaction.guild
        links = all_links()
        present = 0
        absent = 0
        role_failures = 0
        rcon_failures = 0

        for link in links:
            member = guild.get_member(link["discord_user_id"]) if guild is not None else None

            if member is None and guild is not None:
                try:
                    member = await guild.fetch_member(link["discord_user_id"])
                except discord.NotFound:
                    member = None
                except discord.HTTPException:
                    member = None

            if member is None:
                absent += 1
                rcon_result = await self.remove_mc_whitelist(link, status=ABSENT_STATUS)
                if rcon_failed(rcon_result):
                    rcon_failures += 1
                continue

            present += 1
            role_result = await self.apply_whitelist_role(guild, member)
            rcon_result = await self.add_mc_whitelist(link)

            if "failed" in role_result.lower():
                role_failures += 1
            if rcon_failed(rcon_result):
                rcon_failures += 1

        await interaction.followup.send(
            (
                "Whitelist sync complete.\n"
                f"Linked members present: `{present}`\n"
                f"Linked users absent: `{absent}`\n"
                f"Role failures: `{role_failures}`\n"
                f"RCON failures: `{rcon_failures}`\n"
                f"Minecraft RCON enabled: `{config.ENABLE_MC_WHITELIST}`"
            ),
            ephemeral=True,
        )

        await self.send_whitelist_log(
            guild=interaction.guild,
            title="Whitelist Sync Complete",
            user=interaction.user,
            fields=[
                ("Linked Members Present", str(present), True),
                ("Linked Users Absent", str(absent), True),
                ("Role Failures", str(role_failures), True),
                ("RCON Failures", str(rcon_failures), True),
                ("Minecraft RCON Enabled", str(config.ENABLE_MC_WHITELIST), True),
            ],
            color=discord.Color.blurple(),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if member.guild.id != config.HOME_GUILD_ID:
            return

        link = get_link_for_discord(member.id)
        if link is None:
            return

        result = await self.remove_mc_whitelist(link, status=ABSENT_STATUS)
        await self.send_whitelist_log(
            guild=member.guild,
            title="Linked Member Left Discord",
            user=member,
            fields=[
                ("Link", format_link(link), False),
                ("Minecraft RCON", result, False),
            ],
            color=discord.Color.orange(),
        )
        print(f"[whitelist.py] Member left; removed whitelist for {member.id}: {result}")

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if guild.id != config.HOME_GUILD_ID:
            return

        link = get_link_for_discord(user.id)
        if link is None:
            return

        result = await self.remove_mc_whitelist(link, status=BANNED_STATUS)
        await self.send_whitelist_log(
            guild=guild,
            title="Linked Member Banned From Discord",
            user=user,
            fields=[
                ("Link", format_link(link), False),
                ("Minecraft RCON", result, False),
            ],
            color=discord.Color.red(),
        )
        print(f"[whitelist.py] Member banned; removed whitelist for {user.id}: {result}")

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != config.HOME_GUILD_ID:
            return

        link = get_link_for_discord(member.id)
        if link is None:
            return

        role_result = await self.apply_whitelist_role(member.guild, member)
        rcon_result = await self.add_mc_whitelist(link)
        await self.send_whitelist_log(
            guild=member.guild,
            title="Linked Member Rejoined Discord",
            user=member,
            fields=[
                ("Link", format_link(link), False),
                ("Discord Role", role_result, False),
                ("Minecraft RCON", rcon_result, False),
            ],
            color=discord.Color.green(),
        )
        print(
            f"[whitelist.py] Member rejoined; restored whitelist for {member.id}: "
            f"{role_result} {rcon_result}"
        )


async def setup(bot: commands.Bot):
    init_db()
    cog = Whitelist(bot)
    bot.add_view(WhitelistPanelView(cog))
    await bot.add_cog(cog)
