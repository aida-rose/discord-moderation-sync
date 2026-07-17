import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from storage import moderation_db


AFFILIATE_CONFIG_PATH = Path("data/affiliates.json")


def clean_affiliates(data: dict[Any, Any]) -> dict[str, dict[str, Any]]:
    cleaned: dict[str, dict[str, Any]] = {}

    for guild_id, info in data.items():
        try:
            key = str(int(guild_id))
        except (TypeError, ValueError):
            continue

        if not isinstance(info, dict):
            continue

        log_channel_id = info.get("log_channel_id")
        if log_channel_id in (None, ""):
            log_channel_id = None
        else:
            try:
                log_channel_id = int(log_channel_id)
            except (TypeError, ValueError):
                log_channel_id = None

        cleaned[key] = {
            "name": str(info.get("name", "Unknown")),
            "log_channel_id": log_channel_id,
            "enabled": bool(info.get("enabled", True)),
        }

    return cleaned


def load_legacy_affiliates() -> dict[str, dict[str, Any]]:
    if not AFFILIATE_CONFIG_PATH.exists():
        return {}

    try:
        with AFFILIATE_CONFIG_PATH.open("r", encoding="utf-8") as file:
            data = json.load(file)
    except json.JSONDecodeError:
        backup_path = AFFILIATE_CONFIG_PATH.with_suffix(".broken.json")
        AFFILIATE_CONFIG_PATH.replace(backup_path)
        return {}

    if not isinstance(data, dict):
        return {}

    return clean_affiliates(data)


def migrate_legacy_affiliates(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS storage_migrations (
            name TEXT PRIMARY KEY,
            completed_at TEXT NOT NULL
        )
        """
    )

    migrated = conn.execute(
        """
        SELECT 1
        FROM storage_migrations
        WHERE name = ?
        """,
        ("legacy_affiliates_json",),
    ).fetchone()

    if migrated is not None:
        return

    legacy_affiliates = load_legacy_affiliates()

    conn.executemany(
        """
        INSERT OR IGNORE INTO affiliates (
            guild_id,
            name,
            log_channel_id,
            enabled
        )
        VALUES (?, ?, ?, ?)
        """,
        [
            (
                int(guild_id),
                info["name"],
                info["log_channel_id"],
                1 if info["enabled"] else 0,
            )
            for guild_id, info in legacy_affiliates.items()
        ],
    )

    conn.execute(
        """
        INSERT INTO storage_migrations (name, completed_at)
        VALUES (?, ?)
        """,
        (
            "legacy_affiliates_json",
            datetime.now(timezone.utc).isoformat(timespec="seconds"),
        ),
    )


def ensure_affiliate_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS affiliates (
            guild_id INTEGER PRIMARY KEY,
            name TEXT NOT NULL,
            log_channel_id INTEGER,
            enabled INTEGER NOT NULL DEFAULT 1
        )
        """
    )

    migrate_legacy_affiliates(conn)


def load_affiliates() -> dict[str, dict[str, Any]]:
    with moderation_db() as conn:
        ensure_affiliate_db(conn)

        rows = conn.execute(
            """
            SELECT guild_id, name, log_channel_id, enabled
            FROM affiliates
            ORDER BY guild_id ASC
            """
        ).fetchall()

    return {
        str(row["guild_id"]): {
            "name": row["name"],
            "log_channel_id": row["log_channel_id"],
            "enabled": bool(row["enabled"]),
        }
        for row in rows
    }


def save_affiliates(data: dict[str, dict[str, Any]]) -> None:
    cleaned = clean_affiliates(data)

    with moderation_db() as conn:
        ensure_affiliate_db(conn)
        conn.execute("DELETE FROM affiliates")
        conn.executemany(
            """
            INSERT INTO affiliates (
                guild_id,
                name,
                log_channel_id,
                enabled
            )
            VALUES (?, ?, ?, ?)
            """,
            [
                (
                    int(guild_id),
                    info["name"],
                    info["log_channel_id"],
                    1 if info["enabled"] else 0,
                )
                for guild_id, info in cleaned.items()
            ],
        )


def get_runtime_affiliate_ids() -> set[int]:
    data = load_affiliates()

    return {
        int(guild_id)
        for guild_id, info in data.items()
        if isinstance(info, dict) and info.get("enabled", True)
    }


def get_affiliate_log_channel_id(guild_id: int) -> int | None:
    data = load_affiliates()
    info = data.get(str(guild_id), {})

    if not isinstance(info, dict):
        return None

    value = info.get("log_channel_id")

    if value in (None, ""):
        return None

    return int(value)


def add_affiliate(guild_id: int, name: str, log_channel_id: int | None = None) -> None:
    with moderation_db() as conn:
        ensure_affiliate_db(conn)
        conn.execute(
            """
            INSERT INTO affiliates (
                guild_id,
                name,
                log_channel_id,
                enabled
            )
            VALUES (?, ?, ?, 1)
            ON CONFLICT(guild_id) DO UPDATE SET
                name = excluded.name,
                log_channel_id = excluded.log_channel_id,
                enabled = 1
            """,
            (guild_id, name, log_channel_id),
        )


def remove_affiliate(guild_id: int) -> bool:
    with moderation_db() as conn:
        ensure_affiliate_db(conn)
        cursor = conn.execute(
            "DELETE FROM affiliates WHERE guild_id = ?",
            (guild_id,),
        )

        return cursor.rowcount > 0


def set_affiliate_enabled(guild_id: int, enabled: bool) -> bool:
    with moderation_db() as conn:
        ensure_affiliate_db(conn)
        cursor = conn.execute(
            """
            UPDATE affiliates
            SET enabled = ?
            WHERE guild_id = ?
            """,
            (1 if enabled else 0, guild_id),
        )

        return cursor.rowcount > 0


def set_affiliate_log_channel(guild_id: int, log_channel_id: int | None) -> bool:
    with moderation_db() as conn:
        ensure_affiliate_db(conn)
        cursor = conn.execute(
            """
            UPDATE affiliates
            SET log_channel_id = ?
            WHERE guild_id = ?
            """,
            (log_channel_id, guild_id),
        )

        return cursor.rowcount > 0
