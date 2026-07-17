from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Optional

from dotenv import load_dotenv

load_dotenv()


MODERATION_DB_PATH = "data/moderation.sqlite3"


def _csv_ids(raw: str) -> list[int]:
    if not raw:
        return []

    ids: list[int] = []

    for item in raw.split(","):
        item = item.strip()

        if not item:
            continue

        guild_id = int(item)

        if guild_id not in ids:
            ids.append(guild_id)

    return ids


def _log_routes(raw: str) -> dict[int, int]:
    routes: dict[int, int] = {}

    if not raw:
        return routes

    for pair in raw.split(","):
        pair = pair.strip()
        if not pair:
            continue

        if ":" not in pair:
            continue

        guild_id, channel_id = pair.split(":", 1)
        routes[int(guild_id.strip())] = int(channel_id.strip())

    return routes


def _bool(raw: str) -> bool:
    return raw.strip().lower() in {"true", "1", "yes", "y", "on"}


SETTING_DEFAULTS: dict[str, str] = {
    "COMMAND_PREFIX": "!",
    "NETWORK_NAME": "ESMP affiliate servers",
    "CASE_TAG": "ESMP-MOD",
    "HOME_GUILD_ID": "0",
    "BASE_GUILD_ID": "0",
    "HOME_LOG_CHANNEL_ID": "0",
    "AFFILIATE_LOG_ROUTES": "",
    "LOGGED_GUILD_IDS": "",
    "STAFF_ROLE_IDS": "",
    "BAN_STAFF_ROLE_IDS": "",
    "PRIMARY_JOIN_ROLE_ID": "0",
    "BAN_PRUNE_SECONDS": "0",
    "SEND_USER_NOTICES": "true",
    "APPEAL_URL": "",
    "ENABLE_NATION_SELECTOR": "false",
    "ENABLE_TICKETS": "false",
    "LOG_MODERATION_THREAD_ID": "0",
    "LOG_SERVER_MANAGEMENT_THREAD_ID": "0",
    "LOG_INVITE_THREAD_ID": "0",
    "LOG_USER_THREAD_ID": "0",
    "LOG_REACTION_THREAD_ID": "0",
    "LOG_FLAGGED_MESSAGE_THREAD_ID": "0",
    "LOG_MESSAGE_THREAD_ID": "0",
    "LOG_VC_THREAD_ID": "0",
    "LOG_JOINS_THREAD_ID": "0",
    "LOG_OTHER_THREAD_ID": "0",
    "LOG_ROLE_MANAGEMENT_THREAD_ID": "0",
    "SELF_LOG_THREAD_ID": "0",
    "NATION_SELECTOR_LOG_THREAD_ID": "0",
    "SWEARS_FILE": "cogs/swears.txt",
    "FLAGGED_MESSAGE_REGEX": "",
    "NATION_ASSIGNMENTS_CSV": "data/nation_assignments.csv",
    "PLAINS_ROLE_ID": "0",
    "FOREST_ROLE_ID": "0",
    "DESERT_ROLE_ID": "0",
    "TAIGA_ROLE_ID": "0",
    "JUNGLE_ROLE_ID": "0",
    "DARK_FOREST_ROLE_ID": "0",
    "MESA_ROLE_ID": "0",
    "SNOW_ROLE_ID": "0",
    "MUSHROOM_ISLAND_ROLE_ID": "0",
    "SAVANNA_ROLE_ID": "0",
    "SWAMP_ROLE_ID": "0",
    "CHERRY_ROLE_ID": "0",
}

SETTING_DESCRIPTIONS: dict[str, str] = {
    "COMMAND_PREFIX": "Prefix for legacy text commands.",
    "NETWORK_NAME": "Network/server name shown in notices.",
    "CASE_TAG": "Short tag used in audit-log reasons.",
    "HOME_GUILD_ID": "Primary/home Discord server ID.",
    "BASE_GUILD_ID": "Base/server hub Discord server ID.",
    "HOME_LOG_CHANNEL_ID": "Shared moderation log channel/thread ID.",
    "AFFILIATE_LOG_ROUTES": "Comma-separated guild_id:channel_id log routes.",
    "LOGGED_GUILD_IDS": "Comma-separated extra guild IDs to log.",
    "STAFF_ROLE_IDS": "Comma-separated regular moderation role IDs.",
    "BAN_STAFF_ROLE_IDS": "Comma-separated ban-permission role IDs.",
    "PRIMARY_JOIN_ROLE_ID": "Role ID assigned to new members.",
    "BAN_PRUNE_SECONDS": "Seconds of messages to delete when banning.",
    "SEND_USER_NOTICES": "Whether punishment notices are DM'd to users.",
    "APPEAL_URL": "Appeal/questions URL shown in user notices.",
    "ENABLE_NATION_SELECTOR": "Whether to load the nation selector cog on startup.",
    "ENABLE_TICKETS": "Whether to load the tickets cog on startup.",
    "SWEARS_FILE": "File path for flagged message terms.",
    "FLAGGED_MESSAGE_REGEX": "Optional regex for flagged-message logging.",
}

ENV_MIGRATIONS: dict[str, tuple[str, ...]] = {
    "COMMAND_PREFIX": ("ESMP_COMMAND_PREFIX", "COMMAND_PREFIX"),
    "NETWORK_NAME": ("ESMP_NETWORK_NAME", "SERVER_NETWORK_NAME", "NETWORK_NAME"),
    "CASE_TAG": ("ESMP_CASE_TAG", "CASE_TAG"),
    "HOME_GUILD_ID": ("ESMP_HOME_GUILD_ID", "PRIMARY_GUILD_ID"),
    "BASE_GUILD_ID": ("BASE_GUILD_ID",),
    "HOME_LOG_CHANNEL_ID": ("ESMP_HOME_LOG", "PRIMARY_LOG_CHANNEL_ID"),
    "AFFILIATE_LOG_ROUTES": ("ESMP_AFFILIATE_LOGS", "AFFILIATE_LOG_CHANNELS"),
    "STAFF_ROLE_IDS": ("ESMP_STAFF_ROLES", "MOD_ROLE_IDS"),
    "BAN_STAFF_ROLE_IDS": ("ESMP_BAN_ROLES", "BAN_MOD_ROLE_IDS", "BAN_ROLE_IDS"),
    "BAN_PRUNE_SECONDS": ("ESMP_BAN_PRUNE_SECONDS", "BAN_DELETE_MESSAGE_SECONDS", "BAN_PRUNE_SECONDS"),
    "SEND_USER_NOTICES": ("ESMP_SEND_USER_NOTICES", "DM_ON_PUNISHMENTS", "SEND_USER_NOTICES"),
    "APPEAL_URL": ("ESMP_APPEAL_URL", "APPEAL_URL"),
    "ENABLE_NATION_SELECTOR": ("ENABLE_NATION_SELECTOR",),
    "ENABLE_TICKETS": ("ENABLE_TICKETS",),
    "LOGGED_GUILD_IDS": ("LOGGED_GUILD_IDS",),
    "PRIMARY_JOIN_ROLE_ID": ("PRIMARY_JOIN_ROLE_ID",),
    "LOG_MODERATION_THREAD_ID": ("LOG_MODERATION_THREAD_ID",),
    "LOG_SERVER_MANAGEMENT_THREAD_ID": ("LOG_SERVER_MANAGEMENT_THREAD_ID",),
    "LOG_INVITE_THREAD_ID": ("LOG_INVITE_THREAD_ID",),
    "LOG_USER_THREAD_ID": ("LOG_USER_THREAD_ID",),
    "LOG_REACTION_THREAD_ID": ("LOG_REACTION_THREAD_ID",),
    "LOG_FLAGGED_MESSAGE_THREAD_ID": ("LOG_FLAGGED_MESSAGE_THREAD_ID",),
    "LOG_MESSAGE_THREAD_ID": ("LOG_MESSAGE_THREAD_ID",),
    "LOG_VC_THREAD_ID": ("LOG_VC_THREAD_ID",),
    "LOG_JOINS_THREAD_ID": ("LOG_JOINS_THREAD_ID",),
    "LOG_OTHER_THREAD_ID": ("LOG_OTHER_THREAD_ID",),
    "LOG_ROLE_MANAGEMENT_THREAD_ID": ("LOG_ROLE_MANAGEMENT_THREAD_ID",),
    "SELF_LOG_THREAD_ID": ("SELF_LOG_THREAD_ID",),
    "NATION_SELECTOR_LOG_THREAD_ID": ("NATION_SELECTOR_LOG_THREAD_ID",),
    "SWEARS_FILE": ("SWEARS_FILE",),
    "FLAGGED_MESSAGE_REGEX": ("FLAGGED_MESSAGE_REGEX",),
    "NATION_ASSIGNMENTS_CSV": ("NATION_ASSIGNMENTS_CSV",),
    "PLAINS_ROLE_ID": ("PLAINS_ROLE_ID",),
    "FOREST_ROLE_ID": ("FOREST_ROLE_ID",),
    "DESERT_ROLE_ID": ("DESERT_ROLE_ID",),
    "TAIGA_ROLE_ID": ("TAIGA_ROLE_ID",),
    "JUNGLE_ROLE_ID": ("JUNGLE_ROLE_ID",),
    "DARK_FOREST_ROLE_ID": ("DARK_FOREST_ROLE_ID",),
    "MESA_ROLE_ID": ("MESA_ROLE_ID",),
    "SNOW_ROLE_ID": ("SNOW_ROLE_ID",),
    "MUSHROOM_ISLAND_ROLE_ID": ("MUSHROOM_ISLAND_ROLE_ID",),
    "SAVANNA_ROLE_ID": ("SAVANNA_ROLE_ID",),
    "SWAMP_ROLE_ID": ("SWAMP_ROLE_ID",),
    "CHERRY_ROLE_ID": ("CHERRY_ROLE_ID",),
}


DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
if not DISCORD_TOKEN:
    raise RuntimeError("Missing DISCORD_TOKEN in .env")

BOT_OWNER_IDS = _csv_ids(os.getenv("BOT_OWNER_IDS", ""))


def _settings_db() -> sqlite3.Connection:
    path = Path(MODERATION_DB_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS bot_settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    return conn


def _env_value(names: tuple[str, ...]) -> Optional[str]:
    for name in names:
        value = os.getenv(name)
        if value not in (None, ""):
            return value
    return None


def migrate_env_settings() -> None:
    with _settings_db() as conn:
        for key, names in ENV_MIGRATIONS.items():
            if conn.execute("SELECT 1 FROM bot_settings WHERE key = ?", (key,)).fetchone():
                continue

            value = _env_value(names)
            if value is None:
                continue

            conn.execute(
                """
                INSERT INTO bot_settings (key, value)
                VALUES (?, ?)
                """,
                (key, value),
            )

        base_row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = ?",
            ("BASE_GUILD_ID",),
        ).fetchone()

        if base_row is None or str(base_row["value"]) in {"", "0"}:
            legacy_row = conn.execute(
                "SELECT value FROM bot_settings WHERE key = ?",
                ("AFFILIATE_GUILD_IDS",),
            ).fetchone()
            legacy_value = str(legacy_row["value"]) if legacy_row is not None else None

            if legacy_value is None:
                legacy_value = _env_value(("ESMP_AFFILIATE_GUILDS", "AFFILIATE_GUILD_IDS"))

            legacy_ids = _csv_ids(legacy_value or "")

            if legacy_ids:
                conn.execute(
                    """
                    INSERT INTO bot_settings (key, value)
                    VALUES (?, ?)
                    ON CONFLICT(key) DO UPDATE SET
                        value = excluded.value,
                        updated_at = CURRENT_TIMESTAMP
                    """,
                    ("BASE_GUILD_ID", str(legacy_ids[0])),
                )

        conn.execute(
            "DELETE FROM bot_settings WHERE key = ?",
            ("AFFILIATE_GUILD_IDS",),
        )


def setting_keys() -> list[str]:
    return sorted(SETTING_DEFAULTS)


def get_setting(key: str, default: Optional[str] = None) -> str:
    key = key.upper()
    fallback = SETTING_DEFAULTS.get(key, default if default is not None else "")

    with _settings_db() as conn:
        row = conn.execute(
            "SELECT value FROM bot_settings WHERE key = ?",
            (key,),
        ).fetchone()

    if row is None:
        return fallback

    return str(row["value"])


def set_setting(key: str, value: Any) -> None:
    key = key.upper()

    if key not in SETTING_DEFAULTS:
        raise KeyError(key)

    with _settings_db() as conn:
        conn.execute(
            """
            INSERT INTO bot_settings (key, value, updated_at)
            VALUES (?, ?, CURRENT_TIMESTAMP)
            ON CONFLICT(key) DO UPDATE SET
                value = excluded.value,
                updated_at = CURRENT_TIMESTAMP
            """,
            (key, str(value)),
        )

    reload_settings()


def clear_setting(key: str) -> None:
    key = key.upper()

    if key not in SETTING_DEFAULTS:
        raise KeyError(key)

    with _settings_db() as conn:
        conn.execute(
            "DELETE FROM bot_settings WHERE key = ?",
            (key,),
        )

    reload_settings()


def all_settings() -> dict[str, str]:
    return {key: get_setting(key) for key in setting_keys()}


def _int_setting(key: str) -> int:
    try:
        return int(get_setting(key))
    except (TypeError, ValueError):
        return int(SETTING_DEFAULTS.get(key, "0") or 0)


def _bool_setting(key: str) -> bool:
    return _bool(get_setting(key))


def reload_settings() -> None:
    global COMMAND_PREFIX
    global NETWORK_NAME
    global CASE_TAG
    global HOME_GUILD_ID
    global BASE_GUILD_ID
    global HOME_LOG_CHANNEL_ID
    global AFFILIATE_LOG_ROUTES
    global LOGGED_GUILD_IDS
    global STAFF_ROLE_IDS
    global BAN_STAFF_ROLE_IDS
    global PRIMARY_JOIN_ROLE_ID
    global BAN_PRUNE_SECONDS
    global SEND_USER_NOTICES
    global APPEAL_URL
    global ENABLE_NATION_SELECTOR
    global ENABLE_TICKETS
    global LOG_MODERATION_THREAD_ID
    global LOG_SERVER_MANAGEMENT_THREAD_ID
    global LOG_INVITE_THREAD_ID
    global LOG_USER_THREAD_ID
    global LOG_REACTION_THREAD_ID
    global LOG_FLAGGED_MESSAGE_THREAD_ID
    global LOG_MESSAGE_THREAD_ID
    global LOG_VC_THREAD_ID
    global LOG_JOINS_THREAD_ID
    global LOG_OTHER_THREAD_ID
    global LOG_ROLE_MANAGEMENT_THREAD_ID
    global SELF_LOG_THREAD_ID
    global NATION_SELECTOR_LOG_THREAD_ID
    global SWEARS_FILE
    global FLAGGED_MESSAGE_REGEX
    global NATION_ASSIGNMENTS_CSV
    global SYNC_GUILD_IDS
    global MODLOG_ROUTES

    COMMAND_PREFIX = get_setting("COMMAND_PREFIX")
    NETWORK_NAME = get_setting("NETWORK_NAME")
    CASE_TAG = get_setting("CASE_TAG")
    HOME_GUILD_ID = _int_setting("HOME_GUILD_ID")
    BASE_GUILD_ID = _int_setting("BASE_GUILD_ID")
    HOME_LOG_CHANNEL_ID = _int_setting("HOME_LOG_CHANNEL_ID")
    AFFILIATE_LOG_ROUTES = _log_routes(get_setting("AFFILIATE_LOG_ROUTES"))
    LOGGED_GUILD_IDS = _csv_ids(get_setting("LOGGED_GUILD_IDS"))
    STAFF_ROLE_IDS = set(_csv_ids(get_setting("STAFF_ROLE_IDS")))
    BAN_STAFF_ROLE_IDS = set(_csv_ids(get_setting("BAN_STAFF_ROLE_IDS")))
    PRIMARY_JOIN_ROLE_ID = _int_setting("PRIMARY_JOIN_ROLE_ID")
    BAN_PRUNE_SECONDS = _int_setting("BAN_PRUNE_SECONDS")
    SEND_USER_NOTICES = _bool_setting("SEND_USER_NOTICES")
    APPEAL_URL = get_setting("APPEAL_URL").strip()
    ENABLE_NATION_SELECTOR = _bool_setting("ENABLE_NATION_SELECTOR")
    ENABLE_TICKETS = _bool_setting("ENABLE_TICKETS")
    LOG_MODERATION_THREAD_ID = _int_setting("LOG_MODERATION_THREAD_ID")
    LOG_SERVER_MANAGEMENT_THREAD_ID = _int_setting("LOG_SERVER_MANAGEMENT_THREAD_ID")
    LOG_INVITE_THREAD_ID = _int_setting("LOG_INVITE_THREAD_ID")
    LOG_USER_THREAD_ID = _int_setting("LOG_USER_THREAD_ID")
    LOG_REACTION_THREAD_ID = _int_setting("LOG_REACTION_THREAD_ID")
    LOG_FLAGGED_MESSAGE_THREAD_ID = _int_setting("LOG_FLAGGED_MESSAGE_THREAD_ID")
    LOG_MESSAGE_THREAD_ID = _int_setting("LOG_MESSAGE_THREAD_ID")
    LOG_VC_THREAD_ID = _int_setting("LOG_VC_THREAD_ID")
    LOG_JOINS_THREAD_ID = _int_setting("LOG_JOINS_THREAD_ID")
    LOG_OTHER_THREAD_ID = _int_setting("LOG_OTHER_THREAD_ID")
    LOG_ROLE_MANAGEMENT_THREAD_ID = _int_setting("LOG_ROLE_MANAGEMENT_THREAD_ID")
    SELF_LOG_THREAD_ID = _int_setting("SELF_LOG_THREAD_ID")
    NATION_SELECTOR_LOG_THREAD_ID = _int_setting("NATION_SELECTOR_LOG_THREAD_ID")
    SWEARS_FILE = get_setting("SWEARS_FILE")
    FLAGGED_MESSAGE_REGEX = get_setting("FLAGGED_MESSAGE_REGEX").strip()
    NATION_ASSIGNMENTS_CSV = get_setting("NATION_ASSIGNMENTS_CSV")

    SYNC_GUILD_IDS = []
    for guild_id in [HOME_GUILD_ID, BASE_GUILD_ID]:
        if guild_id and guild_id not in SYNC_GUILD_IDS:
            SYNC_GUILD_IDS.append(guild_id)

    MODLOG_ROUTES = {}
    if HOME_GUILD_ID and HOME_LOG_CHANNEL_ID:
        MODLOG_ROUTES[HOME_GUILD_ID] = HOME_LOG_CHANNEL_ID
    MODLOG_ROUTES.update(AFFILIATE_LOG_ROUTES)


def is_bot_owner_id(user_id: int) -> bool:
    return user_id in BOT_OWNER_IDS


LEGACY_WARN_DB_PATH = "data/warnings.sqlite3"
LEGACY_TEMPBAN_DB_PATH = "data/tempbans.sqlite3"
WARN_DB_PATH = MODERATION_DB_PATH
TEMPBAN_DB_PATH = MODERATION_DB_PATH

migrate_env_settings()
reload_settings()
