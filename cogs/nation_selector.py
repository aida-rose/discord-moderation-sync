from __future__ import annotations

import base64
import hashlib
import html
import json
import os
import re
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional
from urllib.parse import urlencode, urlparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

import config
from storage import moderation_db


NATION_SETTINGS = [
    ("Plains", "PLAINS_ROLE_ID"),
    ("Forest", "FOREST_ROLE_ID"),
    ("Desert", "DESERT_ROLE_ID"),
    ("Taiga", "TAIGA_ROLE_ID"),
    ("Jungle", "JUNGLE_ROLE_ID"),
    ("Dark Forest", "DARK_FOREST_ROLE_ID"),
    ("Mesa", "MESA_ROLE_ID"),
    ("Snow", "SNOW_ROLE_ID"),
    ("Mushroom Island", "MUSHROOM_ISLAND_ROLE_ID"),
    ("Savanna", "SAVANNA_ROLE_ID"),
    ("Swamp", "SWAMP_ROLE_ID"),
    ("Cherry", "CHERRY_ROLE_ID"),
]

NATION_CHOICES = [
    app_commands.Choice(name=name, value=name)
    for name, _setting in NATION_SETTINGS
]

USER_AGENT = "discord-moderation-sync/nation-selector"
DEFAULT_OAUTH_CALLBACK_PATH = "/oauth/microsoft/callback"
DEFAULT_OAUTH_STATE_TTL_SECONDS = 600
MICROSOFT_AUTHORIZE_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/authorize"
MICROSOFT_TOKEN_URL = "https://login.microsoftonline.com/consumers/oauth2/v2.0/token"
MICROSOFT_OAUTH_SCOPE = "XboxLive.signin"
XBOX_USER_AUTH_URL = "https://user.auth.xboxlive.com/user/authenticate"
XBOX_XSTS_URL = "https://xsts.auth.xboxlive.com/xsts/authorize"
MINECRAFT_LOGIN_URL = "https://api.minecraftservices.com/authentication/login_with_xbox"
MINECRAFT_PROFILE_URL = "https://api.minecraftservices.com/minecraft/profile"
MINECRAFT_RELYING_PARTY = "rp://api.minecraftservices.com/"
XBOX_AUTH_RELYING_PARTY = "http://auth.xboxlive.com"
XBOX_RELYING_PARTY = "http://xboxlive.com"


class MinecraftLookupError(Exception):
    pass


class OAuthConfigError(Exception):
    pass


class OAuthFlowError(Exception):
    pass


class NoJavaMinecraftProfile(Exception):
    pass


class AlreadyRegisteredError(Exception):
    pass


class MinecraftAlreadyRegisteredError(Exception):
    def __init__(self, discord_id: int):
        super().__init__("Minecraft account is already registered.")
        self.discord_id = discord_id


@dataclass(frozen=True)
class MinecraftProfile:
    account_type: str
    username: str
    uuid: str
    xuid: Optional[str] = None


@dataclass(frozen=True)
class OAuthConfig:
    client_id: str
    client_secret: str
    redirect_uri: str
    callback_path: str
    host: str
    port: int
    state_ttl_seconds: int


@dataclass(frozen=True)
class OAuthState:
    state: str
    code_verifier: str
    discord_id: int
    nation_name: str
    account_type: str
    expires_at: str


@dataclass(frozen=True)
class NationRole:
    name: str
    setting: str
    role: discord.Role


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def dashed_uuid(raw_uuid: str) -> str:
    cleaned = re.sub(r"[^0-9a-fA-F]", "", raw_uuid).lower()

    if len(cleaned) != 32:
        raise MinecraftLookupError("The Minecraft API returned an invalid UUID.")

    return (
        f"{cleaned[:8]}-{cleaned[8:12]}-{cleaned[12:16]}-"
        f"{cleaned[16:20]}-{cleaned[20:]}"
    )


def floodgate_uuid_from_xuid(xuid: str | int) -> str:
    try:
        xuid_int = int(str(xuid))
    except ValueError as exc:
        raise MinecraftLookupError("The Geyser API returned an invalid XUID.") from exc

    if xuid_int < 0 or xuid_int > 0xFFFFFFFFFFFFFFFF:
        raise MinecraftLookupError("The Geyser API returned an out-of-range XUID.")

    xuid_hex = f"{xuid_int:016x}"
    return f"00000000-0000-0000-{xuid_hex[:4]}-{xuid_hex[4:]}"


def env_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()

    if not raw_value:
        return default

    try:
        return int(raw_value)
    except ValueError as exc:
        raise OAuthConfigError(f"`{name}` must be a number.") from exc


def oauth_config() -> OAuthConfig:
    client_id = os.getenv("MS_CLIENT_ID", "").strip()
    client_secret = os.getenv("MS_CLIENT_SECRET", "").strip()
    redirect_uri = os.getenv("MS_REDIRECT_URI", "").strip()

    if not client_id or not client_secret or not redirect_uri:
        raise OAuthConfigError("Set `MS_CLIENT_ID`, `MS_CLIENT_SECRET`, and `MS_REDIRECT_URI` in `.env`.")

    parsed_redirect = urlparse(redirect_uri)
    callback_path = parsed_redirect.path or DEFAULT_OAUTH_CALLBACK_PATH

    if not callback_path.startswith("/"):
        callback_path = f"/{callback_path}"

    state_ttl_seconds = env_int("NATION_OAUTH_STATE_TTL_SECONDS", DEFAULT_OAUTH_STATE_TTL_SECONDS)
    if state_ttl_seconds <= 0:
        raise OAuthConfigError("`NATION_OAUTH_STATE_TTL_SECONDS` must be greater than 0.")

    return OAuthConfig(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        callback_path=callback_path,
        host=os.getenv("NATION_OAUTH_HOST", "0.0.0.0").strip() or "0.0.0.0",
        port=env_int("NATION_OAUTH_PORT", 8080),
        state_ttl_seconds=state_ttl_seconds,
    )


def code_challenge(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def authorization_url(state: OAuthState, oauth: OAuthConfig) -> str:
    query = urlencode(
        {
            "client_id": oauth.client_id,
            "response_type": "code",
            "redirect_uri": oauth.redirect_uri,
            "response_mode": "query",
            "scope": MICROSOFT_OAUTH_SCOPE,
            "state": state.state,
            "code_challenge": code_challenge(state.code_verifier),
            "code_challenge_method": "S256",
        }
    )
    return f"{MICROSOFT_AUTHORIZE_URL}?{query}"


async def response_json(response: aiohttp.ClientResponse) -> dict:
    raw_text = await response.text()

    if not raw_text:
        return {}

    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise OAuthFlowError(f"OAuth provider returned invalid JSON from `{response.url}`.") from exc

    if not isinstance(data, dict):
        raise OAuthFlowError(f"OAuth provider returned an unexpected response from `{response.url}`.")

    return data


def oauth_error_message(prefix: str, status: int, data: dict) -> str:
    message = (
        data.get("error_description")
        or data.get("errorMessage")
        or data.get("message")
        or data.get("error")
        or f"HTTP {status}"
    )
    return f"{prefix}: {message}"


async def exchange_microsoft_code(
    session: aiohttp.ClientSession,
    code: str,
    oauth_state: OAuthState,
    oauth: OAuthConfig,
) -> str:
    form = {
        "client_id": oauth.client_id,
        "client_secret": oauth.client_secret,
        "code": code,
        "redirect_uri": oauth.redirect_uri,
        "grant_type": "authorization_code",
        "scope": MICROSOFT_OAUTH_SCOPE,
        "code_verifier": oauth_state.code_verifier,
    }

    async with session.post(MICROSOFT_TOKEN_URL, data=form) as response:
        data = await response_json(response)

        if response.status != 200:
            raise OAuthFlowError(oauth_error_message("Microsoft token exchange failed", response.status, data))

    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise OAuthFlowError("Microsoft token exchange did not return an access token.")

    return access_token


async def xbox_user_token(session: aiohttp.ClientSession, microsoft_access_token: str) -> str:
    payload = {
        "Properties": {
            "AuthMethod": "RPS",
            "SiteName": "user.auth.xboxlive.com",
            "RpsTicket": f"d={microsoft_access_token}",
        },
        "RelyingParty": XBOX_AUTH_RELYING_PARTY,
        "TokenType": "JWT",
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-xbl-contract-version": "1",
    }

    async with session.post(XBOX_USER_AUTH_URL, json=payload, headers=headers) as response:
        data = await response_json(response)

        if response.status != 200:
            raise OAuthFlowError(oauth_error_message("Xbox user-token exchange failed", response.status, data))

    token = str(data.get("Token") or "").strip()
    if not token:
        raise OAuthFlowError("Xbox user-token exchange did not return a token.")

    return token


async def xbox_xsts_token(
    session: aiohttp.ClientSession,
    user_token: str,
    relying_party: str,
) -> dict:
    payload = {
        "Properties": {
            "SandboxId": "RETAIL",
            "UserTokens": [user_token],
        },
        "RelyingParty": relying_party,
        "TokenType": "JWT",
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "x-xbl-contract-version": "1",
    }

    async with session.post(XBOX_XSTS_URL, json=payload, headers=headers) as response:
        data = await response_json(response)

        if response.status != 200:
            raise OAuthFlowError(oauth_error_message("Xbox XSTS exchange failed", response.status, data))

    if not data.get("Token"):
        raise OAuthFlowError("Xbox XSTS exchange did not return a token.")

    return data


def xsts_claims(xsts_data: dict) -> dict:
    try:
        claims = xsts_data["DisplayClaims"]["xui"][0]
    except (KeyError, IndexError, TypeError) as exc:
        raise OAuthFlowError("Xbox XSTS response did not include account claims.") from exc

    if not isinstance(claims, dict):
        raise OAuthFlowError("Xbox XSTS response included invalid account claims.")

    return claims


async def minecraft_access_token(session: aiohttp.ClientSession, xsts_data: dict) -> str:
    claims = xsts_claims(xsts_data)
    user_hash = str(claims.get("uhs") or "").strip()
    xsts_token = str(xsts_data.get("Token") or "").strip()

    if not user_hash or not xsts_token:
        raise OAuthFlowError("Xbox XSTS response was missing the user hash or token.")

    payload = {
        "identityToken": f"XBL3.0 x={user_hash};{xsts_token}",
        "ensureLegacyEnabled": True,
    }

    async with session.post(MINECRAFT_LOGIN_URL, json=payload) as response:
        data = await response_json(response)

        if response.status != 200:
            raise OAuthFlowError(oauth_error_message("Minecraft Services login failed", response.status, data))

    access_token = str(data.get("access_token") or "").strip()
    if not access_token:
        raise OAuthFlowError("Minecraft Services login did not return an access token.")

    return access_token


async def java_profile_from_minecraft_token(
    session: aiohttp.ClientSession,
    access_token: str,
) -> MinecraftProfile:
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    async with session.get(MINECRAFT_PROFILE_URL, headers=headers) as response:
        data = await response_json(response)

        if response.status in {204, 404}:
            raise NoJavaMinecraftProfile()

        if response.status != 200:
            raise OAuthFlowError(oauth_error_message("Minecraft profile lookup failed", response.status, data))

    profile_name = str(data.get("name") or "").strip()
    profile_uuid = str(data.get("id") or "").strip()

    if not profile_name or not profile_uuid:
        raise OAuthFlowError("Minecraft profile lookup did not return a username and UUID.")

    return MinecraftProfile(
        account_type="java",
        username=profile_name,
        uuid=dashed_uuid(profile_uuid),
    )


async def bedrock_profile_from_user_token(
    session: aiohttp.ClientSession,
    user_token: str,
) -> MinecraftProfile:
    xsts_data = await xbox_xsts_token(session, user_token, XBOX_RELYING_PARTY)
    claims = xsts_claims(xsts_data)
    xuid = str(claims.get("xid") or claims.get("xuid") or "").strip()
    gamertag = str(
        claims.get("gtg")
        or claims.get("gamertag")
        or claims.get("usr")
        or "Bedrock Player"
    ).strip()

    if not xuid:
        raise OAuthFlowError("Xbox account claims did not include an XUID for Bedrock/Geyser registration.")

    return MinecraftProfile(
        account_type="bedrock",
        username=gamertag,
        uuid=floodgate_uuid_from_xuid(xuid),
        xuid=xuid,
    )


async def minecraft_profile_from_oauth_code(
    *,
    code: str,
    oauth_state: OAuthState,
    oauth: OAuthConfig,
) -> MinecraftProfile:
    timeout = aiohttp.ClientTimeout(total=30)

    async with aiohttp.ClientSession(timeout=timeout, headers={"User-Agent": USER_AGENT}) as session:
        microsoft_access_token = await exchange_microsoft_code(session, code, oauth_state, oauth)
        user_token = await xbox_user_token(session, microsoft_access_token)

        if oauth_state.account_type == "bedrock":
            return await bedrock_profile_from_user_token(session, user_token)

        minecraft_xsts = await xbox_xsts_token(session, user_token, MINECRAFT_RELYING_PARTY)
        minecraft_token = await minecraft_access_token(session, minecraft_xsts)

        try:
            return await java_profile_from_minecraft_token(session, minecraft_token)
        except NoJavaMinecraftProfile:
            raise OAuthFlowError(
                "That Microsoft account does not have a Java Minecraft profile. "
                "Return to Discord and choose Bedrock if you meant to link a Bedrock/Geyser account."
            ) from None


def html_page(title: str, body: str) -> web.Response:
    escaped_title = html.escape(title)
    return web.Response(
        text=(
            "<!doctype html>"
            "<html><head>"
            "<meta charset='utf-8'>"
            "<meta name='viewport' content='width=device-width, initial-scale=1'>"
            f"<title>{escaped_title}</title>"
            "<style>"
            "body{font-family:system-ui,-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;"
            "line-height:1.5;margin:0;min-height:100vh;display:grid;place-items:center;"
            "background:#101820;color:#f8fbff}"
            "main{max-width:34rem;padding:2rem}"
            "h1{font-size:1.8rem;margin:0 0 1rem}"
            "p{font-size:1rem;color:#d6dee8}"
            "</style></head><body><main>"
            f"<h1>{escaped_title}</h1>"
            f"<p>{body}</p>"
            "</main></body></html>"
        ),
        content_type="text/html",
    )


def init_db() -> None:
    with moderation_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nation_members (
                discord_id INTEGER PRIMARY KEY,
                minecraft_uuid TEXT NOT NULL COLLATE NOCASE UNIQUE,
                nation_name TEXT NOT NULL,
                minecraft_username TEXT NOT NULL DEFAULT '',
                minecraft_account_type TEXT NOT NULL DEFAULT 'java',
                minecraft_xuid TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )

        columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(nation_members)")
        }

        migrations = {
            "minecraft_username": "ALTER TABLE nation_members ADD COLUMN minecraft_username TEXT NOT NULL DEFAULT ''",
            "minecraft_account_type": "ALTER TABLE nation_members ADD COLUMN minecraft_account_type TEXT NOT NULL DEFAULT 'java'",
            "minecraft_xuid": "ALTER TABLE nation_members ADD COLUMN minecraft_xuid TEXT",
            "created_at": "ALTER TABLE nation_members ADD COLUMN created_at TEXT NOT NULL DEFAULT ''",
            "updated_at": "ALTER TABLE nation_members ADD COLUMN updated_at TEXT NOT NULL DEFAULT ''",
        }

        for column, sql in migrations.items():
            if column not in columns:
                conn.execute(sql)

        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_nation_members_minecraft_uuid
            ON nation_members (minecraft_uuid COLLATE NOCASE)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS nation_oauth_states (
                state TEXT PRIMARY KEY,
                code_verifier TEXT NOT NULL,
                discord_id INTEGER NOT NULL UNIQUE,
                nation_name TEXT NOT NULL,
                account_type TEXT NOT NULL DEFAULT 'java',
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL
            )
            """
        )

        oauth_columns = {
            row["name"]
            for row in conn.execute("PRAGMA table_info(nation_oauth_states)")
        }

        if "account_type" not in oauth_columns:
            conn.execute(
                """
                ALTER TABLE nation_oauth_states
                ADD COLUMN account_type TEXT NOT NULL DEFAULT 'java'
                """
            )


def cleanup_oauth_states(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        DELETE FROM nation_oauth_states
        WHERE expires_at <= ?
        """,
        (utc_now_iso(),),
    )


def create_oauth_state(
    *,
    discord_id: int,
    nation_name: str,
    account_type: str,
    ttl_seconds: int,
) -> OAuthState:
    state = secrets.token_urlsafe(32)
    code_verifier = secrets.token_urlsafe(64)[:128]
    now = datetime.now(timezone.utc)
    created_at = now.isoformat()
    expires_at = datetime.fromtimestamp(now.timestamp() + ttl_seconds, timezone.utc).isoformat()

    with moderation_db() as conn:
        cleanup_oauth_states(conn)
        conn.execute(
            """
            DELETE FROM nation_oauth_states
            WHERE discord_id = ?
            """,
            (discord_id,),
        )
        conn.execute(
            """
            INSERT INTO nation_oauth_states (
                state,
                code_verifier,
                discord_id,
                nation_name,
                account_type,
                created_at,
                expires_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (state, code_verifier, discord_id, nation_name, account_type, created_at, expires_at),
        )

    return OAuthState(
        state=state,
        code_verifier=code_verifier,
        discord_id=discord_id,
        nation_name=nation_name,
        account_type=account_type,
        expires_at=expires_at,
    )


def consume_oauth_state(state: str) -> Optional[OAuthState]:
    with moderation_db() as conn:
        cleanup_oauth_states(conn)
        row = conn.execute(
            """
            SELECT *
            FROM nation_oauth_states
            WHERE state = ?
            """,
            (state,),
        ).fetchone()

        if row is None:
            return None

        conn.execute(
            """
            DELETE FROM nation_oauth_states
            WHERE state = ?
            """,
            (state,),
        )

    return OAuthState(
        state=str(row["state"]),
        code_verifier=str(row["code_verifier"]),
        discord_id=int(row["discord_id"]),
        nation_name=str(row["nation_name"]),
        account_type=str(row["account_type"]),
        expires_at=str(row["expires_at"]),
    )


def registration_for_discord(discord_id: int) -> Optional[sqlite3.Row]:
    with moderation_db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM nation_members
            WHERE discord_id = ?
            """,
            (discord_id,),
        ).fetchone()


def registration_for_minecraft(minecraft_uuid: str) -> Optional[sqlite3.Row]:
    with moderation_db() as conn:
        return conn.execute(
            """
            SELECT *
            FROM nation_members
            WHERE minecraft_uuid = ? COLLATE NOCASE
            """,
            (minecraft_uuid,),
        ).fetchone()


def create_registration(
    *,
    discord_id: int,
    profile: MinecraftProfile,
    nation_name: str,
) -> None:
    now = utc_now_iso()

    try:
        with moderation_db() as conn:
            conn.execute(
                """
                INSERT INTO nation_members (
                    discord_id,
                    minecraft_uuid,
                    nation_name,
                    minecraft_username,
                    minecraft_account_type,
                    minecraft_xuid,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    discord_id,
                    profile.uuid,
                    nation_name,
                    profile.username,
                    profile.account_type,
                    profile.xuid,
                    now,
                    now,
                ),
            )
    except sqlite3.IntegrityError as exc:
        if registration_for_discord(discord_id) is not None:
            raise AlreadyRegisteredError() from exc

        existing = registration_for_minecraft(profile.uuid)
        if existing is not None:
            raise MinecraftAlreadyRegisteredError(int(existing["discord_id"])) from exc

        raise


def update_registration_nation(discord_id: int, nation_name: str) -> bool:
    with moderation_db() as conn:
        cursor = conn.execute(
            """
            UPDATE nation_members
            SET nation_name = ?,
                updated_at = ?
            WHERE discord_id = ?
            """,
            (nation_name, utc_now_iso(), discord_id),
        )
        return cursor.rowcount > 0


def delete_registration(discord_id: int) -> Optional[sqlite3.Row]:
    with moderation_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM nation_members
            WHERE discord_id = ?
            """,
            (discord_id,),
        ).fetchone()

        if row is None:
            return None

        conn.execute(
            """
            DELETE FROM nation_members
            WHERE discord_id = ?
            """,
            (discord_id,),
        )
        return row


class OAuthLoginView(discord.ui.View):
    def __init__(self, url: str):
        super().__init__(timeout=300)
        self.add_item(
            discord.ui.Button(
                label="Sign in with Microsoft",
                style=discord.ButtonStyle.link,
                url=url,
            )
        )


class EditionChoiceView(discord.ui.View):
    def __init__(self, cog: "NationSelector", nation_name: str):
        super().__init__(timeout=300)
        self.cog = cog
        self.nation_name = nation_name

    @discord.ui.button(
        label="Java",
        style=discord.ButtonStyle.primary,
    )
    async def java(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.cog.start_oauth_login(interaction, self.nation_name, "java")

    @discord.ui.button(
        label="Bedrock / Geyser",
        style=discord.ButtonStyle.secondary,
    )
    async def bedrock(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.cog.start_oauth_login(interaction, self.nation_name, "bedrock")


class NationPanelView(discord.ui.View):
    def __init__(self, cog: "NationSelector"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Join a Nation",
        style=discord.ButtonStyle.primary,
        custom_id="nation_selector:join",
    )
    async def join_nation(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.cog.start_registration(interaction)


class NationSelector(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.oauth_runner: Optional[web.AppRunner] = None
        self.oauth_site: Optional[web.TCPSite] = None

    async def start_oauth_server(self) -> None:
        if self.oauth_runner is not None:
            return

        try:
            oauth = oauth_config()
        except OAuthConfigError as exc:
            print(f"[nation_selector.py] OAuth callback server disabled: {exc}")
            return

        app = web.Application()
        app.router.add_get(oauth.callback_path, self.handle_oauth_callback)

        if oauth.callback_path != DEFAULT_OAUTH_CALLBACK_PATH:
            app.router.add_get(DEFAULT_OAUTH_CALLBACK_PATH, self.handle_oauth_callback)

        self.oauth_runner = web.AppRunner(app)
        await self.oauth_runner.setup()
        self.oauth_site = web.TCPSite(self.oauth_runner, oauth.host, oauth.port)
        await self.oauth_site.start()
        print(f"[nation_selector.py] OAuth callback listening on {oauth.host}:{oauth.port}{oauth.callback_path}")

    async def stop_oauth_server(self) -> None:
        if self.oauth_runner is None:
            return

        await self.oauth_runner.cleanup()
        self.oauth_runner = None
        self.oauth_site = None

    def cog_unload(self):
        if self.oauth_runner is not None:
            self.bot.loop.create_task(self.stop_oauth_server())

    async def send_ephemeral(self, interaction: discord.Interaction, message: str) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def admin_or_owner_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and config.is_bot_owner_id(interaction.user.id):
            return True

        if (
            interaction.guild is not None
            and interaction.guild.id == config.HOME_GUILD_ID
            and isinstance(interaction.user, discord.Member)
            and interaction.user.guild_permissions.administrator
        ):
            return True

        await self.send_ephemeral(
            interaction,
            "Only a primary-server administrator or bot owner can use this command.",
        )
        return False

    def home_guild_ready(self, interaction: discord.Interaction) -> bool:
        return (
            interaction.guild is not None
            and config.HOME_GUILD_ID != 0
            and interaction.guild.id == config.HOME_GUILD_ID
        )

    def nation_roles(self, guild: discord.Guild) -> tuple[list[NationRole], list[str]]:
        roles: list[NationRole] = []
        missing: list[str] = []

        for nation_name, setting in NATION_SETTINGS:
            role_id = getattr(config, setting, 0)

            if role_id == 0:
                missing.append(f"`{setting}` is not configured")
                continue

            role = guild.get_role(role_id)

            if role is None:
                missing.append(f"`{setting}` points to missing role `{role_id}`")
                continue

            roles.append(NationRole(nation_name, setting, role))

        return roles, missing

    def nation_by_name(self, guild: discord.Guild, nation_name: str) -> Optional[NationRole]:
        roles, _missing = self.nation_roles(guild)
        return next((info for info in roles if info.name == nation_name), None)

    def current_nations(
        self,
        member: discord.Member,
        nation_roles: list[NationRole],
    ) -> list[NationRole]:
        member_role_ids = {role.id for role in member.roles}
        return [
            info
            for info in nation_roles
            if info.role.id in member_role_ids
        ]

    def is_admin_no_nation_member(self, member: discord.Member) -> bool:
        return member.guild_permissions.administrator

    def least_populated_nation(self, nation_roles: list[NationRole]) -> NationRole:
        return min(
            nation_roles,
            key=lambda info: (
                sum(1 for member in info.role.members if not member.bot),
                [name for name, _setting in NATION_SETTINGS].index(info.name),
            ),
        )

    async def apply_nation_role(
        self,
        member: discord.Member,
        selected: NationRole,
        nation_roles: list[NationRole],
        *,
        reason: str,
    ) -> None:
        selected_role_ids = {selected.role.id}
        extra_roles = [
            info.role
            for info in nation_roles
            if info.role.id in {role.id for role in member.roles}
            and info.role.id not in selected_role_ids
        ]

        if extra_roles:
            await member.remove_roles(*extra_roles, reason=reason)

        if selected.role not in member.roles:
            await member.add_roles(selected.role, reason=reason)

    async def remove_selector_roles(
        self,
        member: discord.Member,
        nation_roles: list[NationRole],
        *,
        reason: str,
    ) -> None:
        roles_to_remove = [
            info.role
            for info in nation_roles
            if info.role in member.roles
        ]

        whitelisted_role = self.whitelisted_role(member.guild)
        if whitelisted_role is not None and whitelisted_role in member.roles:
            roles_to_remove.append(whitelisted_role)

        if roles_to_remove:
            await member.remove_roles(*roles_to_remove, reason=reason)

    async def remove_nation_roles(
        self,
        member: discord.Member,
        *,
        reason: str,
    ) -> list[str]:
        nation_roles, _missing = self.nation_roles(member.guild)
        notes: list[str] = []

        nation_roles_to_remove = [
            info.role
            for info in nation_roles
            if info.role in member.roles
        ]

        if nation_roles_to_remove:
            try:
                await member.remove_roles(*nation_roles_to_remove, reason=reason)
                notes.append("Removed nation role(s).")
            except (discord.Forbidden, discord.HTTPException) as exc:
                notes.append(f"Could not remove nation role(s): `{type(exc).__name__}: {exc}`")

        return notes

    def whitelisted_role(self, guild: discord.Guild) -> Optional[discord.Role]:
        if config.WHITELISTED_ROLE_ID == 0:
            return None

        return guild.get_role(config.WHITELISTED_ROLE_ID)

    async def apply_whitelisted_role(self, member: discord.Member, *, reason: str) -> str:
        role_id = config.WHITELISTED_ROLE_ID

        if role_id == 0:
            return "Whitelisted role skipped because `WHITELISTED_ROLE_ID` is not configured."

        role = member.guild.get_role(role_id)

        if role is None:
            return f"Whitelisted role skipped because role `{role_id}` was not found."

        if role in member.roles:
            return f"Whitelisted role already present: {role.mention}"

        await member.add_roles(role, reason=reason)
        return f"Whitelisted role added: {role.mention}"

    async def log(self, guild: Optional[discord.Guild], message: str) -> None:
        logging_cog = self.bot.get_cog("Logging")

        if logging_cog is not None and hasattr(logging_cog, "nation_selector_log"):
            await logging_cog.nation_selector_log(guild=guild, message=message)
            return

        print(f"[nation_selector.py] {message}")

    async def start_registration(self, interaction: discord.Interaction) -> None:
        if not self.home_guild_ready(interaction):
            await self.send_ephemeral(interaction, "Use this in the configured primary server.")
            return

        if not isinstance(interaction.user, discord.Member):
            await self.send_ephemeral(interaction, "Use this from a server member account.")
            return

        existing = registration_for_discord(interaction.user.id)
        if existing is not None:
            await self.restore_registered_roles(interaction.user, existing, notify_interaction=interaction)
            return

        if self.is_admin_no_nation_member(interaction.user):
            await self.remove_nation_roles(
                interaction.user,
                reason="Nation selector admin no-nation registration.",
            )
            await interaction.response.send_message(
                (
                    "Admins are not assigned to nations.\n"
                    "Choose which Minecraft edition you want to link for whitelist verification."
                ),
                view=EditionChoiceView(self, ""),
                ephemeral=True,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        nation_roles, missing = self.nation_roles(interaction.guild)
        if missing or len(nation_roles) != len(NATION_SETTINGS):
            details = "\n".join(missing[:12])
            await self.send_ephemeral(
                interaction,
                "The nation roles are not fully configured yet.\n" + details,
            )
            return

        current_nations = self.current_nations(interaction.user, nation_roles)
        selected = current_nations[0] if current_nations else self.least_populated_nation(nation_roles)

        if not current_nations:
            try:
                await self.apply_nation_role(
                    interaction.user,
                    selected,
                    nation_roles,
                    reason="Nation selector pending OAuth registration.",
                )
            except (discord.Forbidden, discord.HTTPException) as exc:
                await self.send_ephemeral(
                    interaction,
                    f"Could not assign a nation role: `{type(exc).__name__}: {exc}`",
                )
                return

        await interaction.response.send_message(
            (
                f"Your pending nation is **{selected.name}**.\n"
                "Choose which Minecraft edition you want to link."
            ),
            view=EditionChoiceView(self, selected.name),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    async def start_oauth_login(
        self,
        interaction: discord.Interaction,
        nation_name: str,
        account_type: str,
    ) -> None:
        if not self.home_guild_ready(interaction):
            await self.send_ephemeral(interaction, "Use this in the configured primary server.")
            return

        if not isinstance(interaction.user, discord.Member):
            await self.send_ephemeral(interaction, "Use this from a server member account.")
            return

        if account_type not in {"java", "bedrock"}:
            await self.send_ephemeral(interaction, "Choose Java or Bedrock.")
            return

        if registration_for_discord(interaction.user.id) is not None:
            await self.send_ephemeral(
                interaction,
                "You are already registered. Ask an administrator to run `/nation_reset` if you need to start over.",
            )
            return

        admin_without_nation = self.is_admin_no_nation_member(interaction.user) and not nation_name

        if not admin_without_nation and self.nation_by_name(interaction.guild, nation_name) is None:
            await self.send_ephemeral(interaction, "That pending nation is no longer configured. Click the panel again.")
            return

        try:
            oauth = oauth_config()
        except OAuthConfigError as exc:
            await self.send_ephemeral(interaction, f"Microsoft OAuth is not configured: {exc}")
            return

        oauth_state = create_oauth_state(
            discord_id=interaction.user.id,
            nation_name=nation_name,
            account_type=account_type,
            ttl_seconds=oauth.state_ttl_seconds,
        )
        login_url = authorization_url(oauth_state, oauth)
        edition_label = "Bedrock/Geyser" if account_type == "bedrock" else "Java"
        target_label = "No nation (admin)" if admin_without_nation else nation_name

        await interaction.response.send_message(
            (
                f"Your pending nation is **{target_label}**.\n"
                f"Selected edition: **{edition_label}**.\n"
                "Use the Microsoft sign-in button to verify the Microsoft/Xbox account "
                "that owns your Minecraft account. This link expires in "
                f"{oauth.state_ttl_seconds // 60 or 1} minute(s)."
            ),
            view=OAuthLoginView(login_url),
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        await self.log(
            interaction.guild,
            (
                f"{interaction.user} (`{interaction.user.id}`) started "
                f"{edition_label} OAuth registration for {target_label}."
            ),
        )

    async def home_member(self, discord_id: int) -> tuple[Optional[discord.Guild], Optional[discord.Member]]:
        guild = self.bot.get_guild(config.HOME_GUILD_ID)

        if guild is None:
            return None, None

        member = guild.get_member(discord_id)

        if member is not None:
            return guild, member

        try:
            return guild, await guild.fetch_member(discord_id)
        except discord.NotFound:
            return guild, None
        except discord.HTTPException:
            return guild, None

    async def apply_registered_roles(
        self,
        member: discord.Member,
        nation_name: str,
        *,
        reason: str,
    ) -> list[str]:
        notes: list[str] = []

        if not nation_name:
            notes.extend(await self.remove_nation_roles(member, reason=reason))

            try:
                notes.append(await self.apply_whitelisted_role(member, reason=reason))
            except (discord.Forbidden, discord.HTTPException) as exc:
                notes.append(f"Could not apply whitelisted role: {type(exc).__name__}: {exc}")

            return notes

        nation_roles, missing = self.nation_roles(member.guild)

        if missing:
            notes.append("Nation roles are not fully configured.")
            return notes

        selected = next((info for info in nation_roles if info.name == nation_name), None)

        if selected is None:
            notes.append(f"Database nation `{nation_name}` does not match a configured nation.")
            return notes

        try:
            await self.apply_nation_role(member, selected, nation_roles, reason=reason)
            notes.append(f"Nation role applied: {selected.role.name}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            notes.append(f"Could not apply nation role: {type(exc).__name__}: {exc}")

        try:
            notes.append(await self.apply_whitelisted_role(member, reason=reason))
        except (discord.Forbidden, discord.HTTPException) as exc:
            notes.append(f"Could not apply whitelisted role: {type(exc).__name__}: {exc}")

        return notes

    async def handle_oauth_callback(self, request: web.Request) -> web.Response:
        if request.query.get("error"):
            description = html.escape(
                request.query.get("error_description")
                or request.query.get("error")
                or "Microsoft sign-in was cancelled or failed."
            )
            return html_page("Registration Cancelled", description)

        state = request.query.get("state", "").strip()
        code = request.query.get("code", "").strip()

        if not state or not code:
            return html_page("Registration Failed", "The OAuth callback was missing its state or code.")

        oauth_state = consume_oauth_state(state)

        if oauth_state is None:
            return html_page(
                "Registration Link Expired",
                "That Microsoft sign-in link is expired or was already used. Return to Discord and click the nation button again.",
            )

        try:
            oauth = oauth_config()
            profile = await minecraft_profile_from_oauth_code(
                code=code,
                oauth_state=oauth_state,
                oauth=oauth,
            )
        except (OAuthConfigError, OAuthFlowError, MinecraftLookupError) as exc:
            await self.log(
                None,
                f"OAuth registration failed for `{oauth_state.discord_id}`: {type(exc).__name__}: {exc}",
            )
            return html_page("Registration Failed", html.escape(str(exc)))

        existing = registration_for_discord(oauth_state.discord_id)
        if existing is not None:
            guild, member = await self.home_member(oauth_state.discord_id)
            if member is not None:
                await self.restore_registered_roles(member, existing)
            return html_page(
                "Already Registered",
                "That Discord account is already registered. You can close this page.",
            )

        try:
            create_registration(
                discord_id=oauth_state.discord_id,
                profile=profile,
                nation_name=oauth_state.nation_name,
            )
        except AlreadyRegisteredError:
            return html_page(
                "Already Registered",
                "That Discord account is already registered. You can close this page.",
            )
        except MinecraftAlreadyRegisteredError as exc:
            return html_page(
                "Minecraft Account Already Registered",
                f"That Minecraft account is already registered to Discord ID {exc.discord_id}.",
            )

        guild, member = await self.home_member(oauth_state.discord_id)
        role_notes = ["Member is not currently in the primary server; roles will be restored on rejoin."]

        if member is not None:
            role_notes = await self.apply_registered_roles(
                member,
                oauth_state.nation_name,
                reason="Nation selector Microsoft OAuth registration.",
            )

        account_label = "Bedrock/Geyser" if profile.account_type == "bedrock" else "Java"
        nation_label = oauth_state.nation_name or "No nation (admin)"
        await self.log(
            guild,
            (
                f"`{oauth_state.discord_id}` completed OAuth registration for "
                f"{account_label} `{profile.username}` (`{profile.uuid}`) in {nation_label}."
            ),
        )

        body = (
            f"Registered {html.escape(account_label)} account "
            f"<strong>{html.escape(profile.username)}</strong> to "
            f"<strong>{html.escape(nation_label)}</strong>. "
            "You can close this page and return to Discord."
        )

        if role_notes:
            body += "<br><br>" + "<br>".join(html.escape(note) for note in role_notes)

        return html_page("Registration Complete", body)

    async def restore_registered_roles(
        self,
        member: discord.Member,
        registration: sqlite3.Row,
        *,
        notify_interaction: Optional[discord.Interaction] = None,
    ) -> None:
        if self.is_admin_no_nation_member(member) and registration["nation_name"]:
            update_registration_nation(member.id, "")
            notes = await self.apply_registered_roles(
                member,
                "",
                reason="Nation selector admin no-nation whitelist restore.",
            )

            if notify_interaction is not None:
                await self.send_ephemeral(
                    notify_interaction,
                    "You are registered as an admin with no nation assignment.\n" + "\n".join(notes),
                )

            await self.log(
                member.guild,
                f"Converted admin registration for {member} (`{member.id}`) to no nation.",
            )
            return

        if not registration["nation_name"]:
            notes = await self.apply_registered_roles(
                member,
                "",
                reason="Nation selector no-nation whitelist restore.",
            )

            if notify_interaction is not None:
                await self.send_ephemeral(
                    notify_interaction,
                    "You are already registered with no nation assignment.\n" + "\n".join(notes),
                )

            return

        nation_roles, missing = self.nation_roles(member.guild)

        if missing:
            message = "The nation roles are not fully configured yet."
            if notify_interaction is not None:
                await self.send_ephemeral(notify_interaction, message)
            await self.log(member.guild, f"Could not restore nation roles for `{member.id}`: {message}")
            return

        selected = next(
            (info for info in nation_roles if info.name == registration["nation_name"]),
            None,
        )

        if selected is None:
            message = f"Database nation `{registration['nation_name']}` does not match a configured nation."
            if notify_interaction is not None:
                await self.send_ephemeral(notify_interaction, message)
            await self.log(member.guild, f"Could not restore nation roles for `{member.id}`: {message}")
            return

        notes: list[str] = []

        try:
            await self.apply_nation_role(
                member,
                selected,
                nation_roles,
                reason="Nation selector role restore.",
            )
            notes.append(f"Nation role restored: {selected.role.mention}")
        except (discord.Forbidden, discord.HTTPException) as exc:
            notes.append(f"Could not restore nation role: `{type(exc).__name__}: {exc}`")

        try:
            notes.append(
                await self.apply_whitelisted_role(
                    member,
                    reason="Nation selector whitelisted role restore.",
                )
            )
        except (discord.Forbidden, discord.HTTPException) as exc:
            notes.append(f"Could not restore whitelisted role: `{type(exc).__name__}: {exc}`")

        if notify_interaction is not None:
            await self.send_ephemeral(
                notify_interaction,
                (
                    "You are already registered.\n"
                    f"Nation: **{registration['nation_name']}**\n"
                    + "\n".join(notes)
                ),
            )

    @app_commands.command(
        name="nation_panel",
        description="Send the nation selector panel.",
    )
    @app_commands.describe(
        channel="Optional channel for the panel. Defaults to the current channel.",
    )
    async def nation_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        if not await self.admin_or_owner_check(interaction):
            return

        if not self.home_guild_ready(interaction):
            await self.send_ephemeral(interaction, "Use this in the configured primary server.")
            return

        target = channel or interaction.channel

        if not isinstance(target, discord.TextChannel):
            await self.send_ephemeral(interaction, "Choose a server text channel.")
            return

        embed = discord.Embed(
            title="Nation Selector",
            description="Register your Minecraft account and receive a nation assignment.",
            color=discord.Color.green(),
        )

        await target.send(embed=embed, view=NationPanelView(self))
        await self.send_ephemeral(interaction, f"Nation selector panel sent to {target.mention}.")

    @app_commands.command(
        name="nation_change",
        description="Manually change a registered user's nation.",
    )
    @app_commands.describe(
        user="Registered Discord user.",
        nation="New nation.",
    )
    @app_commands.choices(nation=NATION_CHOICES)
    async def nation_change(
        self,
        interaction: discord.Interaction,
        user: discord.User,
        nation: app_commands.Choice[str],
    ):
        if not await self.admin_or_owner_check(interaction):
            return

        if not self.home_guild_ready(interaction):
            await self.send_ephemeral(interaction, "Use this in the configured primary server.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        if registration_for_discord(user.id) is None:
            await interaction.followup.send(
                "That user is not in the nation database. They need to register first.",
                ephemeral=True,
            )
            return

        if not update_registration_nation(user.id, nation.value):
            await interaction.followup.send("That user is not in the nation database.", ephemeral=True)
            return

        member = interaction.guild.get_member(user.id)
        role_note = "User is not currently in the server; roles will be restored when they rejoin."

        if member is not None:
            nation_roles, missing = self.nation_roles(interaction.guild)
            selected = self.nation_by_name(interaction.guild, nation.value)

            if missing or selected is None:
                role_note = "Database updated, but nation roles are not fully configured."
            else:
                try:
                    await self.apply_nation_role(
                        member,
                        selected,
                        nation_roles,
                        reason=f"Nation manually changed by {interaction.user}.",
                    )
                    role_note = f"Role updated to {selected.role.mention}."
                except (discord.Forbidden, discord.HTTPException) as exc:
                    role_note = f"Database updated, but role update failed: `{type(exc).__name__}: {exc}`"

        await interaction.followup.send(
            f"Updated <@{user.id}> to **{nation.value}**. {role_note}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        await self.log(
            interaction.guild,
            f"{interaction.user} changed `{user.id}` to {nation.value}.",
        )

    @app_commands.command(
        name="nation_reset",
        description="Remove a user from the nation database so they can register again.",
    )
    @app_commands.describe(
        user="Discord user to reset.",
    )
    async def nation_reset(
        self,
        interaction: discord.Interaction,
        user: discord.User,
    ):
        if not await self.admin_or_owner_check(interaction):
            return

        if not self.home_guild_ready(interaction):
            await self.send_ephemeral(interaction, "Use this in the configured primary server.")
            return

        await interaction.response.defer(ephemeral=True, thinking=True)

        deleted = delete_registration(user.id)
        if deleted is None:
            await interaction.followup.send("That user was not in the nation database.", ephemeral=True)
            return

        member = interaction.guild.get_member(user.id)
        role_note = "User is not currently in the server; no roles were changed."

        if member is not None:
            nation_roles, _missing = self.nation_roles(interaction.guild)
            try:
                await self.remove_selector_roles(
                    member,
                    nation_roles,
                    reason=f"Nation registration reset by {interaction.user}.",
                )
                role_note = "Nation and whitelisted roles were removed."
            except (discord.Forbidden, discord.HTTPException) as exc:
                role_note = f"Database row removed, but role cleanup failed: `{type(exc).__name__}: {exc}`"

        await interaction.followup.send(
            f"Reset <@{user.id}>. {role_note}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

        await self.log(
            interaction.guild,
            (
                f"{interaction.user} reset `{user.id}` "
                f"from {deleted['nation_name'] or 'No nation'} / `{deleted['minecraft_uuid']}`."
            ),
        )

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if member.guild.id != config.HOME_GUILD_ID:
            return

        registration = registration_for_discord(member.id)
        if registration is None:
            return

        await self.restore_registered_roles(member, registration)
        await self.log(
            member.guild,
            f"Restored nation roles for {member} (`{member.id}`) on join.",
        )


async def setup(bot: commands.Bot):
    init_db()
    cog = NationSelector(bot)
    bot.add_view(NationPanelView(cog))
    await bot.add_cog(cog)
    await cog.start_oauth_server()
