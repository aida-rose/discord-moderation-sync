import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

import discord
from discord.ext import commands

import config
from storage import moderation_db, same_database_path
from common import (
    discord_time,
    display_user,
    send_notice,
    target_label,
    trim,
    user_id_arg,
    warn_log,
)


def warn_db() -> sqlite3.Connection:
    return moderation_db()


def migrate_legacy_warn_db(conn: sqlite3.Connection) -> None:
    legacy_path = Path(config.LEGACY_WARN_DB_PATH)

    if same_database_path(legacy_path) or not legacy_path.exists():
        return

    try:
        with sqlite3.connect(legacy_path) as legacy_conn:
            legacy_conn.row_factory = sqlite3.Row
            rows = legacy_conn.execute(
                """
                SELECT warn_id,
                       user_id,
                       moderator_id,
                       guild_id,
                       reason,
                       created_at,
                       removed_at,
                       removed_by_id,
                       removed_reason
                FROM warnings
                """
            ).fetchall()
    except sqlite3.Error:
        return

    conn.executemany(
        """
        INSERT OR IGNORE INTO warnings (
            warn_id,
            user_id,
            moderator_id,
            guild_id,
            reason,
            created_at,
            removed_at,
            removed_by_id,
            removed_reason
        )
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["warn_id"],
                row["user_id"],
                row["moderator_id"],
                row["guild_id"],
                row["reason"],
                row["created_at"],
                row["removed_at"],
                row["removed_by_id"],
                row["removed_reason"],
            )
            for row in rows
        ],
    )


def ready_warn_db() -> None:
    with warn_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS warnings (
                warn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                guild_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                removed_at TEXT,
                removed_by_id INTEGER,
                removed_reason TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_warnings_user_active
            ON warnings (user_id, removed_at)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_warnings_warn_id
            ON warnings (warn_id)
            """
        )

        migrate_legacy_warn_db(conn)


def add_warn(*, user_id: int, moderator_id: int, guild_id: int, reason: str) -> int:
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with warn_db() as conn:
        cursor = conn.execute(
            """
            INSERT INTO warnings (user_id, moderator_id, guild_id, reason, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (user_id, moderator_id, guild_id, reason, created_at),
        )

        return int(cursor.lastrowid)


def active_warns(user_id: int) -> int:
    with warn_db() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM warnings
            WHERE user_id = ?
            AND removed_at IS NULL
            """,
            (user_id,),
        ).fetchone()

        return int(row["count"])


def warns_for(user_id: int) -> list[sqlite3.Row]:
    with warn_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM warnings
            WHERE user_id = ?
            AND removed_at IS NULL
            ORDER BY warn_id ASC
            """,
            (user_id,),
        ).fetchall()

        return list(rows)


def warn_by_id(warn_id: int) -> Optional[sqlite3.Row]:
    with warn_db() as conn:
        return conn.execute(
            "SELECT * FROM warnings WHERE warn_id = ?",
            (warn_id,),
        ).fetchone()


def clear_warn(*, warn_id: int, removed_by_id: int, removed_reason: str) -> tuple[Optional[sqlite3.Row], str]:
    removed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with warn_db() as conn:
        existing = conn.execute(
            "SELECT * FROM warnings WHERE warn_id = ?",
            (warn_id,),
        ).fetchone()

        if existing is None:
            return None, "not_found"

        if existing["removed_at"] is not None:
            return existing, "already_removed"

        conn.execute(
            """
            UPDATE warnings
            SET removed_at = ?, removed_by_id = ?, removed_reason = ?
            WHERE warn_id = ?
            """,
            (removed_at, removed_by_id, removed_reason, warn_id),
        )

        updated = conn.execute(
            "SELECT * FROM warnings WHERE warn_id = ?",
            (warn_id,),
        ).fetchone()

        return updated, "removed"


class Warns(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @commands.command(name="warn")
    async def warn_member(self, ctx: commands.Context, target: str, *, reason: str = "No reason provided."):
        target_user_id = user_id_arg(target)
        target_user = await display_user(self.bot, target_user_id)

        warn_id = add_warn(
            user_id=target_user_id,
            moderator_id=ctx.author.id,
            guild_id=ctx.guild.id,
            reason=reason,
        )

        active_warn_count = active_warns(target_user_id)

        dm_result = await send_notice(
            self.bot,
            user_id=target_user_id,
            action=f"Warning #{warn_id}",
            reason=f"{reason}\n\nYou now have {active_warn_count} active warning(s).",
            moderator=ctx.author,
        )

        await warn_log(
            self.bot,
            action="Warning Added",
            moderator=ctx.author,
            target_user=target_user,
            target_user_id=target_user_id,
            warn_id=warn_id,
            reason=reason,
            active_warn_count=active_warn_count,
            dm_result=dm_result,
        )

        await ctx.reply(
            f"Warning `#{warn_id}` added for `{target_user_id}`.\n"
            f"They now have **{active_warn_count}** active warning(s).\n"
            f"{dm_result}",
            mention_author=False,
        )

    @commands.command(name="warns", aliases=["warnings"])
    async def warn_list(self, ctx: commands.Context, target: str):
        target_user_id = user_id_arg(target)
        target_user = await display_user(self.bot, target_user_id)

        warnings = warns_for(target_user_id)
        active_warn_count = len(warnings)

        if active_warn_count == 0:
            await ctx.reply(
                f"{target_label(target_user, target_user_id)} has **0** active warnings.",
                mention_author=False,
                allowed_mentions=discord.AllowedMentions.none(),
            )
            return

        lines = []

        for warning in warnings[:10]:
            lines.append(
                f"**#{warning['warn_id']}** — {discord_time(warning['created_at'])}\n"
                f"Moderator: <@{warning['moderator_id']}>\n"
                f"Reason: {trim(warning['reason'], 250)}"
            )

        description = "\n\n".join(lines)

        if active_warn_count > 10:
            description += f"\n\nShowing 10 of {active_warn_count} active warnings."

        embed = discord.Embed(title="Active Warnings", description=description, color=discord.Color.gold())
        embed.add_field(name="User", value=target_label(target_user, target_user_id), inline=False)
        embed.add_field(name="Active Warn Count", value=str(active_warn_count), inline=True)

        await ctx.reply(
            embed=embed,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="warncount")
    async def warn_total(self, ctx: commands.Context, target: str):
        target_user_id = user_id_arg(target)
        target_user = await display_user(self.bot, target_user_id)
        active_warn_count = active_warns(target_user_id)

        await ctx.reply(
            f"{target_label(target_user, target_user_id)} has **{active_warn_count}** active warning(s).",
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="warninfo")
    async def warn_lookup(self, ctx: commands.Context, warn_id: int):
        warning = warn_by_id(warn_id)

        if warning is None:
            await ctx.reply(f"No warning found with ID `#{warn_id}`.", mention_author=False)
            return

        target_user = await display_user(self.bot, int(warning["user_id"]))
        status = "Removed" if warning["removed_at"] is not None else "Active"

        embed = discord.Embed(
            title=f"Warning #{warn_id}",
            color=discord.Color.green() if status == "Removed" else discord.Color.gold(),
        )

        embed.add_field(name="Status", value=status, inline=True)
        embed.add_field(name="User", value=target_label(target_user, int(warning["user_id"])), inline=False)
        embed.add_field(name="Moderator", value=f"<@{warning['moderator_id']}> (`{warning['moderator_id']}`)", inline=False)
        embed.add_field(name="Created", value=discord_time(warning["created_at"]), inline=True)
        embed.add_field(name="Reason", value=trim(warning["reason"], 1024), inline=False)

        if warning["removed_at"] is not None:
            embed.add_field(name="Removed", value=discord_time(warning["removed_at"]), inline=True)
            embed.add_field(name="Removed By", value=f"<@{warning['removed_by_id']}> (`{warning['removed_by_id']}`)", inline=False)
            embed.add_field(name="Removal Reason", value=trim(warning["removed_reason"] or "No reason provided.", 1024), inline=False)

        await ctx.reply(
            embed=embed,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="removewarn", aliases=["delwarn", "deletewarn"])
    async def warn_remove(self, ctx: commands.Context, warn_id: int, *, removed_reason: str = "No reason provided."):
        warning, status = clear_warn(
            warn_id=warn_id,
            removed_by_id=ctx.author.id,
            removed_reason=removed_reason,
        )

        if status == "not_found" or warning is None:
            await ctx.reply(f"No warning found with ID `#{warn_id}`.", mention_author=False)
            return

        target_user_id = int(warning["user_id"])
        target_user = await display_user(self.bot, target_user_id)

        if status == "already_removed":
            await ctx.reply(f"Warning `#{warn_id}` was already removed.", mention_author=False)
            return

        active_warn_count = active_warns(target_user_id)

        dm_result = await send_notice(
            self.bot,
            user_id=target_user_id,
            action=f"Warning Removed #{warn_id}",
            reason=(
                f"A warning was removed from your record.\n\n"
                f"Original warning reason: {warning['reason']}\n\n"
                f"Removal reason: {removed_reason}\n\n"
                f"You now have {active_warn_count} active warning(s)."
            ),
            moderator=ctx.author,
        )

        await warn_log(
            self.bot,
            action="Warning Removed",
            moderator=ctx.author,
            target_user=target_user,
            target_user_id=target_user_id,
            warn_id=warn_id,
            reason=warning["reason"],
            active_warn_count=active_warn_count,
            dm_result=dm_result,
            removed_reason=removed_reason,
        )

        await ctx.reply(
            f"Warning `#{warn_id}` removed for `{target_user_id}`.\n"
            f"They now have **{active_warn_count}** active warning(s).\n"
            f"{dm_result}",
            mention_author=False,
        )


async def setup(bot: commands.Bot):
    ready_warn_db()
    await bot.add_cog(Warns(bot))
