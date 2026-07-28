import asyncio
import hmac
import json
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.request
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from storage import moderation_db

try:
    from aiohttp import web
except ImportError:
    web = None

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
PENDING_STATUS = "pending_verification"
ABSENT_STATUS = "discord_absent"
BANNED_STATUS = "discord_banned"
VERIFICATION_CODE_LENGTH = 8


class LinkExists(Exception):
    pass


class AccountLookupFailed(Exception):
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
                verification_code TEXT UNIQUE,
                minecraft_uuid TEXT,
                minecraft_xuid TEXT,
                verified_at TEXT,
                verification_method TEXT,
                verification_note TEXT,
                last_rcon_action TEXT,
                last_rcon_result TEXT,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(minecraft_account_links)")
        }
        migrations = {
            "verification_code": "ALTER TABLE minecraft_account_links ADD COLUMN verification_code TEXT",
            "minecraft_uuid": "ALTER TABLE minecraft_account_links ADD COLUMN minecraft_uuid TEXT",
            "minecraft_xuid": "ALTER TABLE minecraft_account_links ADD COLUMN minecraft_xuid TEXT",
            "verified_at": "ALTER TABLE minecraft_account_links ADD COLUMN verified_at TEXT",
            "verification_method": "ALTER TABLE minecraft_account_links ADD COLUMN verification_method TEXT",
            "verification_note": "ALTER TABLE minecraft_account_links ADD COLUMN verification_note TEXT",
        }

        for column, statement in migrations.items():
            if column not in columns:
                conn.execute(statement)

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_minecraft_account_links_verification_code
            ON minecraft_account_links (verification_code)
            WHERE verification_code IS NOT NULL
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_minecraft_account_links_minecraft_uuid
            ON minecraft_account_links (minecraft_uuid)
            WHERE minecraft_uuid IS NOT NULL
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


def lookup_java_profile(username: str) -> dict[str, str] | None:
    url = f"https://api.mojang.com/users/profiles/minecraft/{username}"
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "discord-moderation-sync/whitelist"},
    )

    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            if response.status == 204:
                return None

            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        if exc.code in {204, 404}:
            return None
        raise AccountLookupFailed(f"Mojang lookup failed with HTTP {exc.code}.") from exc
    except Exception as exc:
        raise AccountLookupFailed(f"Mojang lookup failed: {type(exc).__name__}: {exc}") from exc

    profile_id = str(data.get("id", "")).strip()
    profile_name = str(data.get("name", "")).strip()

    if not profile_id or not profile_name:
        raise AccountLookupFailed("Mojang returned an incomplete profile response.")

    return {
        "id": profile_id,
        "name": profile_name,
    }


async def lookup_java_profile_async(username: str) -> dict[str, str] | None:
    return await asyncio.to_thread(lookup_java_profile, username)


def server_whitelist_name(platform: str, entered_name: str) -> str:
    if platform == "bedrock":
        replacement = config.BEDROCK_SPACE_REPLACEMENT
        bedrock_name = entered_name.replace(" ", replacement)
        return f"{config.BEDROCK_USERNAME_PREFIX}{bedrock_name}"

    return entered_name


def current_link_server_name(link) -> str:
    return server_whitelist_name(link["platform"], link["entered_name"])


def normalize_server_name(name: str) -> str:
    return " ".join(name.strip().split()).casefold()


def normalize_uuid(value: object) -> str:
    return str(value or "").replace("-", "").strip().casefold()


def normalize_bedrock_name(value: object) -> str:
    name = str(value or "").strip()
    prefix = config.BEDROCK_USERNAME_PREFIX

    if prefix and name.startswith(prefix):
        name = name[len(prefix):]

    replacement = config.BEDROCK_SPACE_REPLACEMENT
    if replacement:
        name = name.replace(replacement, " ")

    return " ".join(name.split()).casefold()


def quote_command_arg(value: str) -> str:
    escaped = value.replace("\\", "\\\\").replace('"', '\\"')
    return f'"{escaped}"'


def generate_verification_code() -> str:
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"
    return "".join(secrets.choice(alphabet) for _ in range(VERIFICATION_CODE_LENGTH))


def truncate(text: str, limit: int = 700) -> str:
    if len(text) <= limit:
        return text

    return text[: limit - 3] + "..."


def format_link(row) -> str:
    platform = PLATFORM_LABELS.get(row["platform"], row["platform"])
    current_server_name = current_link_server_name(row)
    return (
        f"Discord: <@{row['discord_user_id']}> (`{row['discord_user_id']}`)\n"
        f"Platform: `{platform}`\n"
        f"Entered name: `{row['entered_name']}`\n"
        f"Server whitelist name: `{current_server_name}`\n"
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


def link_is_verified(link) -> bool:
    return bool(link["verified_at"])


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


def get_link_for_code(verification_code: str):
    normalized_code = str(verification_code).strip().upper()

    with moderation_db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM minecraft_account_links
            WHERE UPPER(verification_code) = ?
            """,
            (normalized_code,),
        ).fetchone()


def create_link(
    discord_user_id: int,
    platform: str,
    raw_name: str,
    *,
    minecraft_uuid: Optional[str] = None,
    canonical_name: Optional[str] = None,
):
    entered_name = canonical_name or clean_player_name(platform, raw_name)
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

        if minecraft_uuid:
            existing_uuid = conn.execute(
                """
                SELECT *
                FROM minecraft_account_links
                WHERE minecraft_uuid = ?
                """,
                (minecraft_uuid,),
            ).fetchone()

            if existing_uuid is not None:
                raise LinkExists("That Minecraft account is already linked to a Discord account.")

        entered_name_normalized = normalize_server_name(entered_name)
        same_platform_links = conn.execute(
            """
            SELECT entered_name
            FROM minecraft_account_links
            WHERE platform = ?
            """,
            (platform,),
        ).fetchall()

        if any(
            normalize_server_name(row["entered_name"]) == entered_name_normalized
            for row in same_platform_links
        ):
            raise LinkExists("That Minecraft account is already linked to a Discord account.")

        for _ in range(10):
            verification_code = generate_verification_code()
            try:
                conn.execute(
                    """
                    INSERT INTO minecraft_account_links (
                        discord_user_id,
                        platform,
                        entered_name,
                        server_name,
                        server_name_normalized,
                        status,
                        verification_code,
                        minecraft_uuid
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        discord_user_id,
                        platform,
                        entered_name,
                        server_name,
                        normalized,
                        PENDING_STATUS,
                        verification_code,
                        minecraft_uuid,
                    ),
                )
                break
            except sqlite3.IntegrityError as exc:
                if "verification_code" in str(exc).lower():
                    continue
                raise LinkExists("That Discord or Minecraft account is already linked.") from exc
        else:
            raise LinkExists("Could not create a unique verification code. Please try again.")

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


def verify_link(
    discord_user_id: int,
    verification_code: str,
    *,
    method: str,
    note: str,
):
    normalized_code = str(verification_code).strip().upper()

    with moderation_db() as conn:
        link = conn.execute(
            """
            SELECT *
            FROM minecraft_account_links
            WHERE discord_user_id = ?
            """,
            (discord_user_id,),
        ).fetchone()

        if link is None:
            raise ValueError("That user does not have an account link.")

        if link["verified_at"]:
            raise ValueError("That account link is already verified.")

        expected_code = str(link["verification_code"] or "").strip().upper()
        if not expected_code or normalized_code != expected_code:
            raise ValueError("That verification code does not match the pending account link.")

        conn.execute(
            """
            UPDATE minecraft_account_links
            SET status = ?,
                verification_code = NULL,
                verified_at = CURRENT_TIMESTAMP,
                verification_method = ?,
                verification_note = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE discord_user_id = ?
            """,
            (ACTIVE_STATUS, method, note, discord_user_id),
        )

    return get_link_for_discord(discord_user_id)


def verify_link_from_minecraft(payload: dict):
    code = str(payload.get("code") or "").strip().upper()
    platform = str(payload.get("platform") or "").strip().lower()
    player_name = str(payload.get("player_name") or "").strip()
    player_uuid = str(payload.get("player_uuid") or "").strip()
    bedrock_username = str(payload.get("bedrock_username") or "").strip()
    xuid = str(payload.get("xuid") or "").strip()
    is_bedrock = bool(payload.get("is_bedrock"))

    if not code:
        raise ValueError("Missing verification code.")

    if platform not in PLATFORM_LABELS:
        raise ValueError("Invalid or missing platform.")

    if not player_name or not player_uuid:
        raise ValueError("Missing player name or UUID.")

    link = get_link_for_code(code)

    if link is None:
        raise ValueError("That verification code was not found.")

    if link["verified_at"]:
        raise ValueError("That account link is already verified.")

    if link["platform"] != platform:
        raise ValueError(f"That code is for {PLATFORM_LABELS[link['platform']]}, not {PLATFORM_LABELS[platform]}.")

    if platform == "java":
        if is_bedrock:
            raise ValueError("That code is for a Java account, but the player joined through Floodgate/Bedrock.")

        expected_uuid = normalize_uuid(link["minecraft_uuid"])
        actual_uuid = normalize_uuid(player_uuid)

        if expected_uuid and expected_uuid != actual_uuid:
            raise ValueError("The in-game Java UUID does not match the submitted Java account.")

    else:
        if not is_bedrock:
            raise ValueError("That code is for a Bedrock account, but the player did not join through Floodgate.")

        actual_name = bedrock_username or player_name
        if normalize_bedrock_name(actual_name) != normalize_bedrock_name(link["entered_name"]):
            raise ValueError("The in-game Bedrock gamertag does not match the submitted Bedrock account.")

    with moderation_db() as conn:
        existing_uuid = conn.execute(
            """
            SELECT discord_user_id
            FROM minecraft_account_links
            WHERE minecraft_uuid = ?
              AND discord_user_id != ?
            """,
            (normalize_uuid(player_uuid), link["discord_user_id"]),
        ).fetchone()

        if existing_uuid is not None:
            raise ValueError("That Minecraft UUID is already linked to another Discord account.")

        conn.execute(
            """
            UPDATE minecraft_account_links
            SET status = ?,
                verification_code = NULL,
                minecraft_uuid = ?,
                minecraft_xuid = ?,
                verified_at = CURRENT_TIMESTAMP,
                verification_method = ?,
                verification_note = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE discord_user_id = ?
            """,
            (
                ACTIVE_STATUS,
                normalize_uuid(player_uuid),
                xuid or None,
                "verification_server",
                f"Verified by {player_name} on the Minecraft verification server.",
                link["discord_user_id"],
            ),
        )

    return get_link_for_discord(link["discord_user_id"])


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
            entered_name = clean_player_name(self.platform, str(self.player_name.value))
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        java_profile = None
        if self.platform == "java":
            try:
                java_profile = await lookup_java_profile_async(entered_name)
            except AccountLookupFailed as exc:
                await interaction.followup.send(
                    (
                        "I could not verify that Java account with Mojang right now. "
                        f"Please try again later.\n\n`{exc}`"
                    ),
                    ephemeral=True,
                )
                return

            if java_profile is None:
                await interaction.followup.send(
                    f"`{entered_name}` does not appear to be an existing Java account.",
                    ephemeral=True,
                )
                return

        try:
            link = create_link(
                interaction.user.id,
                self.platform,
                entered_name,
                minecraft_uuid=java_profile["id"] if java_profile else None,
                canonical_name=java_profile["name"] if java_profile else None,
            )
        except LinkExists as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        await self.cog.send_whitelist_log(
            guild=interaction.guild,
            title="Whitelist Verification Started",
            user=interaction.user,
            fields=[
                ("Link", format_link(link), False),
                ("Java UUID", link["minecraft_uuid"] or "Pending in-game verification", False),
            ],
            color=discord.Color.orange(),
        )

        details = [
            "Your account link was saved, but it is not verified yet.",
            "",
            format_link(link),
            "",
            f"Verification code: `{link['verification_code']}`",
            "",
            "When the Minecraft server is ready, join with that account and give this code to staff or run the server link command.",
            "You will get the Discord whitelist role after the account is verified.",
        ]

        await interaction.followup.send("\n".join(details), ephemeral=True)


class Whitelist(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.verify_api_runner = None
        self.verify_api_site = None

    async def cog_load(self):
        await self.start_verify_api()

    def cog_unload(self):
        if self.verify_api_runner is not None:
            self.bot.loop.create_task(self.stop_verify_api())

    async def start_verify_api(self) -> None:
        if not config.ENABLE_MC_VERIFY_API:
            return

        if web is None:
            print("[whitelist.py] MC verification API disabled because aiohttp is not installed.")
            return

        token = os.getenv("MC_VERIFY_API_TOKEN", "")
        if not token:
            print("[whitelist.py] MC verification API disabled because MC_VERIFY_API_TOKEN is missing.")
            return

        host = os.getenv("MC_VERIFY_API_HOST", "127.0.0.1")
        try:
            port = int(os.getenv("MC_VERIFY_API_PORT", "8765"))
        except ValueError:
            print("[whitelist.py] MC verification API disabled because MC_VERIFY_API_PORT is invalid.")
            return

        app = web.Application()
        app.router.add_post("/minecraft/verify", self.handle_minecraft_verify)

        self.verify_api_runner = web.AppRunner(app)
        await self.verify_api_runner.setup()
        self.verify_api_site = web.TCPSite(self.verify_api_runner, host, port)
        await self.verify_api_site.start()

        print(f"[whitelist.py] MC verification API listening on {host}:{port}")

    async def stop_verify_api(self) -> None:
        if self.verify_api_runner is None:
            return

        await self.verify_api_runner.cleanup()
        self.verify_api_runner = None
        self.verify_api_site = None

    def verify_api_authorized(self, request) -> bool:
        expected = os.getenv("MC_VERIFY_API_TOKEN", "")
        provided = request.headers.get("X-MC-Verify-Token", "")

        auth_header = request.headers.get("Authorization", "")
        if auth_header.lower().startswith("bearer "):
            provided = auth_header[7:].strip()

        return bool(expected) and hmac.compare_digest(provided, expected)

    async def handle_minecraft_verify(self, request):
        if not self.verify_api_authorized(request):
            return web.json_response(
                {
                    "ok": False,
                    "message": "Unauthorized.",
                    "kick": False,
                },
                status=401,
            )

        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {
                    "ok": False,
                    "message": "Invalid JSON payload.",
                    "kick": False,
                },
                status=400,
            )

        try:
            link = verify_link_from_minecraft(payload)
        except ValueError as exc:
            return web.json_response(
                {
                    "ok": False,
                    "message": str(exc),
                    "kick": False,
                },
                status=400,
            )

        guild = self.bot.get_guild(config.HOME_GUILD_ID) if config.HOME_GUILD_ID else None
        user = None
        member = None

        if guild is not None:
            member = guild.get_member(link["discord_user_id"])
            if member is None:
                try:
                    member = await guild.fetch_member(link["discord_user_id"])
                except discord.NotFound:
                    member = None
                except discord.HTTPException:
                    member = None

        if member is not None:
            user = member
        else:
            try:
                user = await self.bot.fetch_user(link["discord_user_id"])
            except discord.HTTPException:
                user = None

        if member is None:
            set_link_status(link["discord_user_id"], ABSENT_STATUS)
            role_result = "Discord role: skipped because the user is not in the Discord server."
            rcon_result = "Minecraft whitelist: skipped because the user is not in the Discord server."
        else:
            role_result, rcon_result = await self.finalize_verified_link(guild, member, link)

        await self.send_whitelist_log(
            guild=guild,
            title="Whitelist Account Verified By Minecraft",
            user=user,
            user_id=link["discord_user_id"],
            fields=[
                ("Link", format_link(get_link_for_discord(link["discord_user_id"]) or link), False),
                ("Minecraft Player", str(payload.get("player_name") or "Unknown"), True),
                ("Floodgate Bedrock", str(bool(payload.get("is_bedrock"))), True),
                ("Discord Role", role_result, False),
                ("Minecraft RCON", rcon_result, False),
            ],
            color=discord.Color.green(),
        )

        return web.json_response(
            {
                "ok": True,
                "message": "Your Minecraft account is linked. You can leave this verification server now.",
                "kick": True,
                "discord_user_id": link["discord_user_id"],
                "role_result": role_result,
                "rcon_result": rcon_result,
            }
        )

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
        command = f"whitelist add {quote_command_arg(current_link_server_name(link))}"
        result = await self.rcon_command(command)
        set_link_status(
            link["discord_user_id"],
            ACTIVE_STATUS,
            rcon_action=command,
            rcon_result=result,
        )
        return result

    async def remove_mc_whitelist(self, link, *, status: str) -> str:
        command = f"whitelist remove {quote_command_arg(current_link_server_name(link))}"
        result = await self.rcon_command(command)
        set_link_status(
            link["discord_user_id"],
            status,
            rcon_action=command,
            rcon_result=result,
        )
        return result

    async def finalize_verified_link(
        self,
        guild: Optional[discord.Guild],
        user: discord.User | discord.Member,
        link,
    ) -> tuple[str, str]:
        rcon_result = await self.add_mc_whitelist(link)

        if config.ENABLE_MC_WHITELIST and rcon_failed(rcon_result):
            role_result = (
                "Discord role: skipped because Minecraft RCON whitelist failed. "
                "Run `/whitelist_sync` after fixing RCON."
            )
            return role_result, rcon_result

        role_result = await self.apply_whitelist_role(guild, user)
        return role_result, rcon_result

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

        if link_is_verified(link):
            rcon_result = await self.remove_mc_whitelist(link, status="admin_unlinked")
        else:
            rcon_result = "Minecraft whitelist: skipped because the account link was not verified."

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
        name="whitelist_verify",
        description="Verify a pending Minecraft account link after the user proves the code in-game.",
    )
    @app_commands.describe(
        user="Discord user whose pending link should be verified.",
        code="Verification code the user proved in-game.",
        note="Optional note about how ownership was verified.",
    )
    async def whitelist_verify(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        code: str,
        note: str = "Verified manually by staff.",
    ):
        if not await self.admin_check(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        try:
            link = verify_link(
                user.id,
                code,
                method="manual_staff",
                note=note,
            )
        except ValueError as exc:
            await interaction.followup.send(str(exc), ephemeral=True)
            return

        role_result, rcon_result = await self.finalize_verified_link(
            interaction.guild,
            user,
            link,
        )

        await self.send_whitelist_log(
            guild=interaction.guild,
            title="Whitelist Account Verified",
            user=user,
            fields=[
                ("Admin", format_user(interaction.user), False),
                ("Link", format_link(link), False),
                ("Discord Role", role_result, False),
                ("Minecraft RCON", rcon_result, False),
                ("Verification Note", note, False),
            ],
            color=discord.Color.green(),
        )

        await interaction.followup.send(
            (
                "Verified account link.\n\n"
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

        details = [format_link(link)]

        if link["verification_code"]:
            details.append(f"Verification code: `{link['verification_code']}`")

        if link["minecraft_uuid"]:
            details.append(f"Minecraft UUID: `{link['minecraft_uuid']}`")

        if link["minecraft_xuid"]:
            details.append(f"Bedrock XUID: `{link['minecraft_xuid']}`")

        if link["verified_at"]:
            details.append(f"Verified at: `{link['verified_at']}`")
            details.append(f"Verification method: `{link['verification_method'] or 'unknown'}`")

        if link["verification_note"]:
            details.append(f"Verification note: `{link['verification_note']}`")

        await interaction.response.send_message(
            "\n".join(details),
            ephemeral=True,
        )

    @app_commands.command(
        name="whitelist_dryrun",
        description="Preview validation and RCON commands without saving an account link.",
    )
    @app_commands.describe(
        platform="Minecraft edition to test.",
        name="Minecraft username or Bedrock gamertag to test.",
    )
    @app_commands.choices(
        platform=[
            app_commands.Choice(name="Java", value="java"),
            app_commands.Choice(name="Bedrock", value="bedrock"),
        ]
    )
    async def whitelist_dryrun(
        self,
        interaction: discord.Interaction,
        platform: app_commands.Choice[str],
        name: str,
    ):
        if not await self.admin_check(interaction):
            return

        try:
            entered_name = clean_player_name(platform.value, name)
        except ValueError as exc:
            await interaction.response.send_message(
                f"Validation failed: {exc}",
                ephemeral=True,
            )
            return

        server_name = server_whitelist_name(platform.value, entered_name)
        add_command = f"whitelist add {quote_command_arg(server_name)}"
        remove_command = f"whitelist remove {quote_command_arg(server_name)}"

        await interaction.response.send_message(
            (
                "Dry run only. No link was saved and no RCON command was sent.\n\n"
                f"Platform: `{PLATFORM_LABELS[platform.value]}`\n"
                f"Entered name: `{entered_name}`\n"
                f"Server whitelist name: `{server_name}`\n"
                f"Add command: `{add_command}`\n"
                f"Remove command: `{remove_command}`\n"
                f"RCON enabled: `{config.ENABLE_MC_WHITELIST}`"
            ),
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
        pending = 0
        role_failures = 0
        rcon_failures = 0

        for link in links:
            if not link_is_verified(link):
                pending += 1
                continue

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
                f"Pending verification: `{pending}`\n"
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
                ("Pending Verification", str(pending), True),
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

        if not link_is_verified(link):
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

        if not link_is_verified(link):
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

        if not link_is_verified(link):
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
