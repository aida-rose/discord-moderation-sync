import sqlite3
from datetime import datetime, timezone
from typing import Optional

from storage import moderation_db


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_datetime(value: str | None) -> datetime | None:
    if not value:
        return None

    parsed = datetime.fromisoformat(value)

    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)

    return parsed


def ensure_protected_actions_db(conn: sqlite3.Connection) -> None:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS protected_actions (
            action_type TEXT NOT NULL,
            guild_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            reason TEXT NOT NULL,
            expires_at TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (action_type, guild_id, user_id)
        )
        """
    )

    conn.execute(
        """
        CREATE INDEX IF NOT EXISTS idx_protected_actions_lookup
        ON protected_actions (action_type, guild_id, user_id, active)
        """
    )


def protect_action(
    *,
    action_type: str,
    guild_id: int,
    user_id: int,
    reason: str,
    expires_at: datetime | str | None = None,
) -> None:
    if isinstance(expires_at, datetime):
        expires_at_text = expires_at.isoformat(timespec="seconds")
    else:
        expires_at_text = expires_at

    with moderation_db() as conn:
        ensure_protected_actions_db(conn)
        conn.execute(
            """
            INSERT INTO protected_actions (
                action_type,
                guild_id,
                user_id,
                reason,
                expires_at,
                active,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(action_type, guild_id, user_id) DO UPDATE SET
                reason = excluded.reason,
                expires_at = excluded.expires_at,
                active = 1,
                updated_at = excluded.updated_at
            """,
            (
                action_type,
                guild_id,
                user_id,
                reason,
                expires_at_text,
                utc_now_text(),
            ),
        )


def clear_protected_action(*, action_type: str, guild_id: int, user_id: int) -> None:
    with moderation_db() as conn:
        ensure_protected_actions_db(conn)
        conn.execute(
            """
            UPDATE protected_actions
            SET active = 0,
                updated_at = ?
            WHERE action_type = ?
            AND guild_id = ?
            AND user_id = ?
            """,
            (utc_now_text(), action_type, guild_id, user_id),
        )


def clear_protected_actions_for_user(*, action_type: str, user_id: int) -> None:
    with moderation_db() as conn:
        ensure_protected_actions_db(conn)
        conn.execute(
            """
            UPDATE protected_actions
            SET active = 0,
                updated_at = ?
            WHERE action_type = ?
            AND user_id = ?
            """,
            (utc_now_text(), action_type, user_id),
        )


def get_active_protected_action(*, action_type: str, guild_id: int, user_id: int) -> Optional[sqlite3.Row]:
    with moderation_db() as conn:
        ensure_protected_actions_db(conn)
        row = conn.execute(
            """
            SELECT *
            FROM protected_actions
            WHERE action_type = ?
            AND guild_id = ?
            AND user_id = ?
            AND active = 1
            """,
            (action_type, guild_id, user_id),
        ).fetchone()

    if row is None:
        return None

    expires_at = parse_datetime(row["expires_at"])

    if expires_at is not None and expires_at <= datetime.now(timezone.utc):
        clear_protected_action(
            action_type=action_type,
            guild_id=guild_id,
            user_id=user_id,
        )
        return None

    return row
