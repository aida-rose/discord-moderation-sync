import sqlite3
import asyncio
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable

import discord
from discord.ext import commands, tasks

import config
from altcheck import (
    AltReport,
    best_match_text,
    concise_report_text,
    evaluate_alt_risk,
    mark_known_ban,
    record_user_profile,
    report_assessment,
    warm_cached_profiles,
)
from storage import moderation_db, same_database_path
from protected_actions import (
    clear_protected_action,
    protect_action,
)
from common import (
    BackfillResult,
    NothingToDo,
    banned_ids,
    backfill_log,
    case_log,
    display_user,
    duration_arg,
    home_ban_entries,
    member_in,
    send_notice,
    user_id_arg,
    user_or_snowflake,
)


try:
    from affiliate_config import get_runtime_affiliate_ids
except ImportError:
    def get_runtime_affiliate_ids() -> set[int]:
        return set()


def is_bot_owner(ctx: commands.Context) -> bool:
    return config.is_bot_owner_id(ctx.author.id)


def current_sync_guild_ids() -> set[int]:
    return set(config.SYNC_GUILD_IDS) | get_runtime_affiliate_ids()


def member_status_text(member: discord.Member) -> str:
    status_labels = {
        discord.Status.online: "Online",
        discord.Status.idle: "Idle",
        discord.Status.dnd: "Do Not Disturb",
    }

    status_parts = []
    status_label = status_labels.get(member.status)

    if status_label:
        status_parts.append(status_label)

    custom_status = next(
        (
            activity
            for activity in member.activities
            if isinstance(activity, discord.CustomActivity)
            and getattr(activity, "name", None)
        ),
        None,
    )

    if custom_status is not None:
        status_parts.append(str(custom_status.name))

    if not status_parts:
        return "cant display status or no status to be displayed"

    return "\n".join(status_parts)


def member_role_chunks(member: discord.Member) -> list[str]:
    roles = [
        role.mention
        for role in sorted(member.roles, key=lambda role: role.position, reverse=True)
        if not role.is_default()
    ]

    if not roles:
        return ["No roles."]

    chunks: list[str] = []
    current = ""

    for role in roles:
        next_value = role if not current else f"{current}, {role}"

        if len(next_value) <= 1024:
            current = next_value
            continue

        if current:
            chunks.append(current)
        current = role

    if current:
        chunks.append(current)

    return chunks


class GuildActionResult:
    def __init__(self, guild_name: str, guild_id: int, status: str, detail: str):
        self.guild_name = guild_name
        self.guild_id = guild_id
        self.status = status
        self.detail = detail


async def for_each_current_guild(
    bot: commands.Bot,
    action: Callable[[discord.Guild], Awaitable[str]],
) -> list[GuildActionResult]:

    results: list[GuildActionResult] = []

    for guild_id in current_sync_guild_ids():
        guild = bot.get_guild(guild_id)

        if guild is None:
            results.append(
                GuildActionResult(
                    guild_name="Unknown / Not Cached",
                    guild_id=guild_id,
                    status="Needs review",
                    detail="Bot could not find this server. Check the guild ID and bot invite.",
                )
            )
            continue

        try:
            detail = await action(guild)

            results.append(
                GuildActionResult(
                    guild_name=guild.name,
                    guild_id=guild.id,
                    status="Done",
                    detail=detail,
                )
            )

        except NothingToDo as exc:
            results.append(
                GuildActionResult(
                    guild_name=guild.name,
                    guild_id=guild.id,
                    status="Skipped",
                    detail=str(exc),
                )
            )

        except discord.Forbidden:
            results.append(
                GuildActionResult(
                    guild_name=guild.name,
                    guild_id=guild.id,
                    status="Failed",
                    detail="Missing permission or bot role hierarchy is too low.",
                )
            )

        except discord.HTTPException as exc:
            results.append(
                GuildActionResult(
                    guild_name=guild.name,
                    guild_id=guild.id,
                    status="Failed",
                    detail=f"Discord API error: `{exc}`",
                )
            )

        except Exception as exc:
            results.append(
                GuildActionResult(
                    guild_name=guild.name,
                    guild_id=guild.id,
                    status="Failed",
                    detail=f"Unexpected error: `{type(exc).__name__}: {exc}`",
                )
            )

        await asyncio.sleep(0.25)

    return results


MAX_CASE_LOG_REASON_LENGTH = 900
MAX_CASE_LOG_DM_RESULT_LENGTH = 500
MAX_CASE_LOG_RESULT_DETAIL_LENGTH = 180


def shorten_text(value: object, limit: int) -> str:
    text = str(value) if value is not None else ""

    if len(text) <= limit:
        return text

    return text[: limit - 3] + "..."


def shorten_result_details(results: list) -> list:
    for result in results:
        if hasattr(result, "detail"):
            result.detail = shorten_text(
                getattr(result, "detail", ""),
                MAX_CASE_LOG_RESULT_DETAIL_LENGTH,
            )

    return results


async def safe_case_log(
    bot: commands.Bot,
    *,
    action: str,
    moderator: discord.abc.User,
    target_user,
    target_user_id: int,
    reason: str,
    results: list,
    dm_result: str,
    duration=None,
):
    safe_results = shorten_result_details(results)
    kwargs = {
        "action": shorten_text(action, 160),
        "moderator": moderator,
        "target_user": target_user,
        "target_user_id": target_user_id,
        "reason": shorten_text(reason, MAX_CASE_LOG_REASON_LENGTH),
        "results": safe_results,
        "dm_result": shorten_text(dm_result, MAX_CASE_LOG_DM_RESULT_LENGTH),
    }

    if duration is not None:
        kwargs["duration"] = duration

    try:
        await case_log(bot, **kwargs)
    except discord.HTTPException:
        kwargs["reason"] = shorten_text(reason, 500)
        kwargs["dm_result"] = shorten_text(dm_result, 250)
        await case_log(bot, **kwargs)


async def current_ban_map(bot: commands.Bot) -> dict[int, list[int]]:
    ban_map: dict[int, list[int]] = {}

    for guild_id in current_sync_guild_ids():
        guild = bot.get_guild(guild_id)

        if guild is None:
            continue

        try:
            async for ban_entry in guild.bans(limit=None):
                user_id = int(ban_entry.user.id)
                guilds = ban_map.setdefault(user_id, [])

                if guild.id not in guilds:
                    guilds.append(guild.id)

                record_user_profile(ban_entry.user, guild_id=guild.id)
                mark_known_ban(user_id, guild.id, active=True)
        except discord.Forbidden:
            continue
        except discord.HTTPException:
            continue

        await asyncio.sleep(0.25)

    return ban_map


def altcheck_color(report: AltReport) -> discord.Color:
    if report.level == "high":
        return discord.Color.red()

    if report.level == "medium":
        return discord.Color.orange()

    if report.level == "low":
        return discord.Color.gold()

    return discord.Color.green()


def tempban_db() -> sqlite3.Connection:
    return moderation_db()


def migrate_legacy_tempban_db(conn: sqlite3.Connection) -> None:
    legacy_path = Path(config.LEGACY_TEMPBAN_DB_PATH)

    if same_database_path(legacy_path) or not legacy_path.exists():
        return

    try:
        with sqlite3.connect(legacy_path) as legacy_conn:
            legacy_conn.row_factory = sqlite3.Row
            rows = legacy_conn.execute(
                """
                SELECT tempban_id,
                       user_id,
                       moderator_id,
                       reason,
                       created_at,
                       expires_at,
                       completed_at
                FROM tempbans
                """
            ).fetchall()
    except sqlite3.Error:
        return

    conn.executemany(
        """
        INSERT OR IGNORE INTO tempbans (
            tempban_id,
            user_id,
            moderator_id,
            reason,
            created_at,
            expires_at,
            completed_at
        )
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                row["tempban_id"],
                row["user_id"],
                row["moderator_id"],
                row["reason"],
                row["created_at"],
                row["expires_at"],
                row["completed_at"],
            )
            for row in rows
        ],
    )


def init_tempban_db() -> None:
    with tempban_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tempbans (
                tempban_id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                moderator_id INTEGER NOT NULL,
                reason TEXT NOT NULL,
                created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                completed_at TEXT
            )
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tempbans_active
            ON tempbans (completed_at, expires_at)
            """
        )

        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tempbans_user
            ON tempbans (user_id, completed_at)
            """
        )

        migrate_legacy_tempban_db(conn)


def save_tempban(user_id: int, moderator_id: int, reason: str, expires_at: datetime) -> int:
    created_at = datetime.now(timezone.utc).isoformat(timespec="seconds")
    expires_at_text = expires_at.isoformat(timespec="seconds")

    with tempban_db() as conn:
        existing = conn.execute(
            """
            SELECT tempban_id
            FROM tempbans
            WHERE user_id = ?
            AND completed_at IS NULL
            ORDER BY tempban_id DESC
            LIMIT 1
            """,
            (user_id,),
        ).fetchone()

        if existing:
            conn.execute(
                """
                UPDATE tempbans
                SET moderator_id = ?,
                    reason = ?,
                    created_at = ?,
                    expires_at = ?
                WHERE tempban_id = ?
                """,
                (
                    moderator_id,
                    reason,
                    created_at,
                    expires_at_text,
                    int(existing["tempban_id"]),
                ),
            )
            return int(existing["tempban_id"])

        cursor = conn.execute(
            """
            INSERT INTO tempbans (
                user_id,
                moderator_id,
                reason,
                created_at,
                expires_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                user_id,
                moderator_id,
                reason,
                created_at,
                expires_at_text,
            ),
        )

        return int(cursor.lastrowid)


def due_tempbans() -> list[sqlite3.Row]:
    now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with tempban_db() as conn:
        rows = conn.execute(
            """
            SELECT *
            FROM tempbans
            WHERE completed_at IS NULL
            AND expires_at <= ?
            ORDER BY expires_at ASC
            """,
            (now_text,),
        ).fetchall()

        return list(rows)

def active_tempban_for_user(user_id: int) -> sqlite3.Row | None:
    now_text = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with tempban_db() as conn:
        row = conn.execute(
            """
            SELECT *
            FROM tempbans
            WHERE user_id = ?
            AND completed_at IS NULL
            AND expires_at > ?
            ORDER BY tempban_id DESC
            LIMIT 1
            """,
            (user_id, now_text),
        ).fetchone()

        return row


def finish_tempban(tempban_id: int) -> None:
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with tempban_db() as conn:
        conn.execute(
            """
            UPDATE tempbans
            SET completed_at = ?
            WHERE tempban_id = ?
            """,
            (completed_at, tempban_id),
        )


def clear_active_tempban(user_id: int) -> None:
    completed_at = datetime.now(timezone.utc).isoformat(timespec="seconds")

    with tempban_db() as conn:
        conn.execute(
            """
            UPDATE tempbans
            SET completed_at = ?
            WHERE user_id = ?
            AND completed_at IS NULL
            """,
            (completed_at, user_id),
        )


class Moderation(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        init_tempban_db()
        self.tempban_watcher.start()

    def cog_unload(self):
        self.tempban_watcher.cancel()

    @tasks.loop(seconds=60)
    async def tempban_watcher(self):
        expired = due_tempbans()

        if not expired:
            return

        home_guild = self.bot.get_guild(config.HOME_GUILD_ID)
        bot_member = home_guild.me if home_guild else None

        for tempban in expired:
            user_id = int(tempban["user_id"])
            tempban_id = int(tempban["tempban_id"])
            original_reason = tempban["reason"]

            unban_target = discord.Object(id=user_id)

            async def unban_in_guild(guild: discord.Guild) -> str:
                try:
                    await guild.unban(
                        unban_target,
                        reason=f"[{config.CASE_TAG}] tempban expired. Tempban ID: {tempban_id}",
                    )
                    clear_protected_action(
                        action_type="ban",
                        guild_id=guild.id,
                        user_id=user_id,
                    )
                    return "Tempban expired; ban removed."
                except discord.NotFound:
                    clear_protected_action(
                        action_type="ban",
                        guild_id=guild.id,
                        user_id=user_id,
                    )
                    raise NothingToDo("User was not banned in this server.")

            results = await for_each_current_guild(self.bot, unban_in_guild)
            finish_tempban(tempban_id)

            if bot_member is not None:
                target_user = await display_user(self.bot, user_id)

                dm_result = await send_notice(
                    self.bot,
                    user_id=user_id,
                    action="Tempban Expired / Unbanned",
                    reason=(
                        "Your temporary ban has expired.\n\n"
                        f"Original reason: {original_reason}"
                    ),
                    moderator=bot_member,
                )

                await safe_case_log(
                    self.bot,
                    action="Tempban Expired / Auto Unban",
                    moderator=bot_member,
                    target_user=target_user,
                    target_user_id=user_id,
                    reason=f"Temporary ban expired. Original reason: {original_reason}",
                    results=results,
                    dm_result=dm_result,
                )

            await asyncio.sleep(0.25)

    @tempban_watcher.before_loop
    async def before_tempban_watcher(self):
        await self.bot.wait_until_ready()

    @commands.command(name="ban")
    async def ban_member(self, ctx: commands.Context, target: str, *, reason: str = "No reason provided."):
        target_user_id = user_id_arg(target)
        target_user = await display_user(self.bot, target_user_id)
        ban_target = await user_or_snowflake(self.bot, target_user_id)

        dm_result = await send_notice(
            self.bot,
            user_id=target_user_id,
            action="Ban",
            reason=reason,
            moderator=ctx.author,
        )

        audit_reason = f"[{config.CASE_TAG}] {reason} | ban issued by {ctx.author} ({ctx.author.id}) from home server"

        async def ban_in_guild(guild: discord.Guild) -> str:
            await guild.ban(
                ban_target,
                reason=audit_reason,
                delete_message_seconds=config.BAN_PRUNE_SECONDS,
            )
            protect_action(
                action_type="ban",
                guild_id=guild.id,
                user_id=target_user_id,
                reason=audit_reason,
            )
            return "Ban applied."

        results = await for_each_current_guild(self.bot, ban_in_guild)

        await safe_case_log(
            self.bot,
            action="Ban",
            moderator=ctx.author,
            target_user=target_user,
            target_user_id=target_user_id,
            reason=reason,
            results=results,
            dm_result=dm_result,
        )

        await ctx.reply(f"Ban finished for `{target_user_id}`.\n{dm_result}", mention_author=False)


    @commands.command(name="tempban")
    async def tempban_member(
        self,
        ctx: commands.Context,
        target: str,
        duration: str,
        *,
        reason: str = "No reason provided.",
    ):
        target_user_id = user_id_arg(target)
        target_user = await display_user(self.bot, target_user_id)
        ban_target = await user_or_snowflake(self.bot, target_user_id)
        ban_duration = duration_arg(duration, max_duration=None)
        expires_at = datetime.now(timezone.utc) + ban_duration

        tempban_id = save_tempban(
            user_id=target_user_id,
            moderator_id=ctx.author.id,
            reason=reason,
            expires_at=expires_at,
        )

        dm_result = await send_notice(
            self.bot,
            user_id=target_user_id,
            action=f"Tempban #{tempban_id}",
            reason=reason,
            duration=ban_duration,
            moderator=ctx.author,
        )

        audit_reason = (
            f"[{config.CASE_TAG}] {reason} | tempban issued by "
            f"{ctx.author} ({ctx.author.id}) from home server | "
            f"expires at {expires_at.isoformat(timespec='seconds')}"
        )

        async def tempban_in_guild(guild: discord.Guild) -> str:
            await guild.ban(
                ban_target,
                reason=audit_reason,
                delete_message_seconds=config.BAN_PRUNE_SECONDS,
            )
            protect_action(
                action_type="ban",
                guild_id=guild.id,
                user_id=target_user_id,
                reason=audit_reason,
                expires_at=expires_at,
            )
            return f"Tempban applied until {discord.utils.format_dt(expires_at, 'F')}."

        results = await for_each_current_guild(self.bot, tempban_in_guild)

        await safe_case_log(
            self.bot,
            action=f"Tempban #{tempban_id}",
            moderator=ctx.author,
            target_user=target_user,
            target_user_id=target_user_id,
            reason=reason,
            duration=ban_duration,
            results=results,
            dm_result=dm_result,
        )

        await ctx.reply(
            (
                f"Tempban `#{tempban_id}` finished for `{target_user_id}`.\n"
                f"Expires: {discord.utils.format_dt(expires_at, 'F')}\n"
                f"{dm_result}"
            ),
            mention_author=False,
        )
    

    @commands.command(name="unban")
    async def unban_member(self, ctx: commands.Context, target: str, *, reason: str = "No reason provided."):
        target_user_id = user_id_arg(target)
        target_user = await display_user(self.bot, target_user_id)
        unban_target = await user_or_snowflake(self.bot, target_user_id)

        audit_reason = f"[{config.CASE_TAG}] {reason} | unban issued by {ctx.author} ({ctx.author.id}) from home server"

        async def unban_in_guild(guild: discord.Guild) -> str:
            await guild.unban(unban_target, reason=audit_reason)
            clear_protected_action(
                action_type="ban",
                guild_id=guild.id,
                user_id=target_user_id,
            )
            return "Ban removed."

        results = await for_each_current_guild(self.bot, unban_in_guild)

        if any(result.status == "Done" for result in results):
            clear_active_tempban(target_user_id)

        if any(result.status == "Done" for result in results):
            dm_result = await send_notice(
                self.bot,
                user_id=target_user_id,
                action="Unban",
                reason=reason,
                moderator=ctx.author,
            )
        else:
            dm_result = "No notice sent because no server reported a successful unban."

        await safe_case_log(
            self.bot,
            action="Unban",
            moderator=ctx.author,
            target_user=target_user,
            target_user_id=target_user_id,
            reason=reason,
            results=results,
            dm_result=dm_result,
        )

        await ctx.reply(f"Unban finished for `{target_user_id}`.\n{dm_result}", mention_author=False)

    @commands.command(name="kick")
    async def kick_member(self, ctx: commands.Context, target: str, *, reason: str = "No reason provided."):
        target_user_id = user_id_arg(target)
        target_user = await display_user(self.bot, target_user_id)

        home_guild = self.bot.get_guild(config.HOME_GUILD_ID)

        if home_guild is None:
            await ctx.reply(
                "I could not find the home server. Check `HOME_GUILD_ID` with `/config_get` or update it with `/config_set`.",
                mention_author=False,
            )
            return

        member = await member_in(home_guild, target_user_id)

        if member is None:
            await ctx.reply(
                f"`{target_user_id}` is not currently in the home server, so I cannot kick them.",
                mention_author=False,
            )
            return

        dm_result = await send_notice(
            self.bot,
            user_id=target_user_id,
            action="Kick",
            reason=reason,
            moderator=ctx.author,
        )

        audit_reason = (
            f"[{config.CASE_TAG}] {reason} | kick issued by "
            f"{ctx.author} ({ctx.author.id}) from home server"
        )

        try:
            await member.kick(reason=audit_reason)

            class KickResult:
                guild_name = home_guild.name
                guild_id = home_guild.id
                status = "Done"
                detail = "Member kicked from the home server only."

            results = [KickResult()]

        except discord.Forbidden:
            class KickResult:
                guild_name = home_guild.name
                guild_id = home_guild.id
                status = "Failed"
                detail = "Missing permission to kick this member, or the bot role is too low."

            results = [KickResult()]

        except discord.HTTPException as exc:
            class KickResult:
                guild_name = home_guild.name
                guild_id = home_guild.id
                status = "Failed"
                detail = f"Discord API error: `{exc}`"

            results = [KickResult()]

        await safe_case_log(
            self.bot,
            action="Kick",
            moderator=ctx.author,
            target_user=target_user,
            target_user_id=target_user_id,
            reason=reason,
            results=results,
            dm_result=dm_result,
        )

        await ctx.reply(
            f"Kick finished for `{target_user_id}` from the home server only.\n{dm_result}",
            mention_author=False,
        )

    @commands.command(name="mute", aliases=["timeout"])
    async def timeout_member(
        self,
        ctx: commands.Context,
        target: str,
        duration: str,
        *,
        reason: str = "No reason provided.",
    ):
        target_user_id = user_id_arg(target)
        target_user = await display_user(self.bot, target_user_id)
        timeout_duration = duration_arg(duration)

        dm_result = await send_notice(
            self.bot,
            user_id=target_user_id,
            action="Mute / Timeout",
            reason=reason,
            duration=timeout_duration,
            moderator=ctx.author,
        )

        audit_reason = f"[{config.CASE_TAG}] {reason} | timeout issued by {ctx.author} ({ctx.author.id}) from home server"

        async def mute_in_guild(guild: discord.Guild) -> str:
            member = await member_in(guild, target_user_id)

            if member is None:
                raise NothingToDo("Member is not in this server right now.")

            await member.timeout(timeout_duration, reason=audit_reason)
            protect_action(
                action_type="timeout",
                guild_id=guild.id,
                user_id=target_user_id,
                reason=audit_reason,
                expires_at=datetime.now(timezone.utc) + timeout_duration,
            )
            return f"Timeout set for {timeout_duration}."

        results = await for_each_current_guild(self.bot, mute_in_guild)

        await safe_case_log(
            self.bot,
            action="Mute / Timeout",
            moderator=ctx.author,
            target_user=target_user,
            target_user_id=target_user_id,
            reason=reason,
            duration=timeout_duration,
            results=results,
            dm_result=dm_result,
        )

        await ctx.reply(f"Timeout finished for `{target_user_id}`.\n{dm_result}", mention_author=False)

    @commands.command(name="unmute", aliases=["untimeout"])
    async def clear_timeout(self, ctx: commands.Context, target: str, *, reason: str = "No reason provided."):
        target_user_id = user_id_arg(target)
        target_user = await display_user(self.bot, target_user_id)

        audit_reason = f"[{config.CASE_TAG}] {reason} | timeout removed by {ctx.author} ({ctx.author.id}) from home server"

        async def unmute_in_guild(guild: discord.Guild) -> str:
            member = await member_in(guild, target_user_id)

            if member is None:
                raise NothingToDo("Member is not in this server right now.")

            await member.timeout(None, reason=audit_reason)
            clear_protected_action(
                action_type="timeout",
                guild_id=guild.id,
                user_id=target_user_id,
            )
            return "Timeout cleared."

        results = await for_each_current_guild(self.bot, unmute_in_guild)

        if any(result.status == "Done" for result in results):
            dm_result = await send_notice(
                self.bot,
                user_id=target_user_id,
                action="Unmute / Timeout Removed",
                reason=reason,
                moderator=ctx.author,
            )
        else:
            dm_result = "No notice sent because no server reported a successful unmute."

        await safe_case_log(
            self.bot,
            action="Unmute / Remove Timeout",
            moderator=ctx.author,
            target_user=target_user,
            target_user_id=target_user_id,
            reason=reason,
            results=results,
            dm_result=dm_result,
        )

        await ctx.reply(f"Unmute finished for `{target_user_id}`.\n{dm_result}", mention_author=False)

    @commands.command(name="syncbans", aliases=["synchomebans", "backfillbans"])
    @commands.check(is_bot_owner)
    async def backfill_bans(
        self,
        ctx: commands.Context,
        affiliate_guild_id: str = "",
        confirm: str = "",
    ):

        if not affiliate_guild_id:
            await ctx.reply(
                (
                    "You must specify one affiliate server ID to sync bans to.\n\n"
                    f"Usage: `{config.COMMAND_PREFIX}syncbans affiliate_guild_id confirm`"
                ),
                mention_author=False,
            )
            return

        try:
            target_affiliate_guild_id = int(affiliate_guild_id)
        except ValueError:
            await ctx.reply(
                "Invalid affiliate server ID. Please provide a numeric Discord server ID.",
                mention_author=False,
            )
            return

        if target_affiliate_guild_id == config.HOME_GUILD_ID:
            await ctx.reply(
                "You cannot sync home-server bans to the home server. Please choose an affiliate server ID.",
                mention_author=False,
            )
            return

        if target_affiliate_guild_id not in current_sync_guild_ids():
            await ctx.reply(
                (
                    "That server ID is not in your configured synced guild list.\n\n"
                    "Set it as `BASE_GUILD_ID` with `/config_set`, or add it with the owner affiliate command first."
                ),
                mention_author=False,
            )
            return

        home_guild = self.bot.get_guild(config.HOME_GUILD_ID)

        if home_guild is None:
            await ctx.reply(
                "I could not find the home server. Check `HOME_GUILD_ID` with `/config_get` or update it with `/config_set`.",
                mention_author=False,
            )
            return

        affiliate_guild = self.bot.get_guild(target_affiliate_guild_id)

        if affiliate_guild is None:
            await ctx.reply(
                (
                    "I could not find that affiliate server.\n\n"
                    "Make sure the bot is in that server and the guild ID is correct."
                ),
                mention_author=False,
            )
            return

        if confirm.lower() != "confirm":
            await ctx.reply(
                (
                    f"This will copy every current home-server ban to **{affiliate_guild.name}** "
                    f"(`{affiliate_guild.id}`).\n\n"
                    f"Run `{config.COMMAND_PREFIX}syncbans {affiliate_guild.id} confirm` to continue."
                ),
                mention_author=False,
            )
            return

        await ctx.reply(
            (
                f"Starting home-server ban backfill to **{affiliate_guild.name}** "
                f"(`{affiliate_guild.id}`). This may take a moment if the ban list is large."
            ),
            mention_author=False,
        )

        async with ctx.typing():
            try:
                home_bans = await home_ban_entries(home_guild)
            except discord.Forbidden:
                await ctx.reply(
                    "I do not have permission to view bans in the home server.",
                    mention_author=False,
                )
                return
            except discord.HTTPException as exc:
                await ctx.reply(
                    f"Failed to fetch home-server bans: `{exc}`",
                    mention_author=False,
                )
                return

            summary = BackfillResult(
                guild_name=affiliate_guild.name,
                guild_id=affiliate_guild.id,
                status="Done",
                detail="Backfill finished for this affiliate.",
            )

            try:
                existing_affiliate_bans = await banned_ids(affiliate_guild)
            except discord.Forbidden:
                summary.status = "Failed"
                summary.detail = "Missing permission to view bans in this affiliate server."

                await backfill_log(
                    self.bot,
                    moderator=ctx.author,
                    home_guild=home_guild,
                    home_ban_count=len(home_bans),
                    summaries=[summary],
                )

                await ctx.reply(
                    "Failed: I am missing permission to view bans in that affiliate server.",
                    mention_author=False,
                )
                return
            except discord.HTTPException as exc:
                summary.status = "Failed"
                summary.detail = f"Failed to fetch affiliate bans: `{exc}`"

                await backfill_log(
                    self.bot,
                    moderator=ctx.author,
                    home_guild=home_guild,
                    home_ban_count=len(home_bans),
                    summaries=[summary],
                )

                await ctx.reply(
                    f"Failed to fetch affiliate bans: `{exc}`",
                    mention_author=False,
                )
                return

            for ban_entry in home_bans:
                user = ban_entry.user

                if user.id in existing_affiliate_bans:
                    summary.already_banned += 1
                    continue

                active_tempban = active_tempban_for_user(user.id)

                if active_tempban is not None:
                    tempban_id = int(active_tempban["tempban_id"])
                    expires_at_text = str(active_tempban["expires_at"])
                    tempban_reason = str(active_tempban["reason"])

                    reason_parts = [
                        f"[{config.CASE_TAG}] copied active tempban from the home-server ban list.",
                        f"Tempban ID: {tempban_id}",
                        f"Expires at: {expires_at_text}",
                        f"Home server: {home_guild.name} ({home_guild.id})",
                        f"Target affiliate: {affiliate_guild.name} ({affiliate_guild.id})",
                    ]

                    if tempban_reason:
                        reason_parts.append(f"Tempban reason: {tempban_reason}")

                    if ban_entry.reason:
                        reason_parts.append(f"Home ban audit reason: {ban_entry.reason}")

                else:
                    reason_parts = [
                        f"[{config.CASE_TAG}] copied permanent ban from the home-server ban list.",
                        f"Home server: {home_guild.name} ({home_guild.id})",
                        f"Target affiliate: {affiliate_guild.name} ({affiliate_guild.id})",
                    ]

                    if ban_entry.reason:
                        reason_parts.append(f"Original reason: {ban_entry.reason}")

                reason_parts.append(f"Backfilled by {ctx.author} ({ctx.author.id})")
                audit_reason = " | ".join(reason_parts)

                if len(audit_reason) > 512:
                    audit_reason = audit_reason[:509] + "..."

                try:
                    await affiliate_guild.ban(
                        discord.Object(id=user.id),
                        reason=audit_reason,
                        delete_message_seconds=0,
                    )
                    protect_action(
                        action_type="ban",
                        guild_id=affiliate_guild.id,
                        user_id=user.id,
                        reason=audit_reason,
                        expires_at=expires_at_text if active_tempban is not None else None,
                    )
                    summary.newly_banned += 1
                    existing_affiliate_bans.add(user.id)
                except discord.Forbidden:
                    summary.failed += 1
                except discord.HTTPException:
                    summary.failed += 1

                await asyncio.sleep(0.35)

            if summary.failed > 0:
                summary.status = "Needs review"
                summary.detail = "Completed, but some bans failed. Check bot permissions and role hierarchy."

            summaries = [summary]

            await backfill_log(
                self.bot,
                moderator=ctx.author,
                home_guild=home_guild,
                home_ban_count=len(home_bans),
                summaries=summaries,
            )

        await ctx.reply(
            (
                f"**Home-server ban backfill complete for {affiliate_guild.name}.**\n"
                f"Newly banned: **{summary.newly_banned}**\n"
                f"Already banned: **{summary.already_banned}**\n"
                f"Failed: **{summary.failed}**"
            ),
            mention_author=False,
        )

    @commands.command(name="userinfo", aliases=["whois", "user"])
    async def user_info(self, ctx: commands.Context, target: str):
        """
        Usage:
        e!userinfo user_id
        """
        target_user_id = user_id_arg(target)
        target_user = await display_user(self.bot, target_user_id)

        embed = discord.Embed(
            title="User Info",
            color=discord.Color.blurple(),
        )

        if target_user is not None:
            embed.add_field(
                name="User",
                value=f"<@{target_user.id}> {target_user} (`{target_user.id}`)",
                inline=False,
            )

            embed.add_field(
                name="Account Created",
                value=discord.utils.format_dt(target_user.created_at, "F"),
                inline=False,
            )

            embed.add_field(
                name="Bot Account",
                value="Yes" if target_user.bot else "No",
                inline=True,
            )

            embed.set_thumbnail(url=target_user.display_avatar.url)
        else:
            embed.add_field(
                name="User",
                value=f"<@{target_user_id}> (`{target_user_id}`)",
                inline=False,
            )

            embed.add_field(
                name="Account Created",
                value="Could not fetch user profile.",
                inline=False,
            )

        if ctx.guild is None:
            embed.add_field(
                name="Server Membership",
                value="This command must be used in a server to show server-specific info.",
                inline=False,
            )
        else:
            member = await member_in(ctx.guild, target_user_id)

            if member is None:
                embed.add_field(
                    name="Server Membership",
                    value=f"Not currently in **{ctx.guild.name}**.",
                    inline=False,
                )
            else:
                joined_text = (
                    discord.utils.format_dt(member.joined_at, "F")
                    if member.joined_at
                    else "Unknown"
                )

                embed.add_field(
                    name="Server Membership",
                    value=(
                        f"**{ctx.guild.name}** `({ctx.guild.id})`\n"
                        f"Joined: {joined_text}\n"
                        f"Top Role: {member.top_role.mention}"
                    ),
                    inline=False,
                )
                embed.add_field(
                    name="Status",
                    value=member_status_text(member),
                    inline=False,
                )

                for index, role_chunk in enumerate(member_role_chunks(member), start=1):
                    field_name = "Roles" if index == 1 else f"Roles {index}"
                    embed.add_field(name=field_name, value=role_chunk, inline=False)

        await ctx.reply(
            embed=embed,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="altcheck")
    async def alt_check(self, ctx: commands.Context, target: str):
        """
        Usage:
        e!altcheck user_id
        """
        target_user_id = user_id_arg(target)

        async with ctx.typing():
            target_user = await display_user(self.bot, target_user_id)
            target_member = await member_in(ctx.guild, target_user_id) if ctx.guild else None
            target_profile = target_member or target_user

            for guild_id in current_sync_guild_ids():
                guild = self.bot.get_guild(guild_id)

                if guild is not None:
                    warm_cached_profiles(guild.members)

            if target_profile is not None:
                record_user_profile(
                    target_profile,
                    guild_id=ctx.guild.id if ctx.guild is not None else None,
                )

            ban_map = await current_ban_map(self.bot)
            report = evaluate_alt_risk(
                target_user_id,
                account_created_at=getattr(target_profile, "created_at", None),
                banned_guilds_by_user=ban_map,
            )

            target_text = (
                f"<@{target_profile.id}> {target_profile} (`{target_profile.id}`)"
                if target_profile is not None
                else f"<@{target_user_id}> (`{target_user_id}`)"
            )
            evidence_text = "\n".join(f"- {reason}" for reason in report.reasons[:6])

            embed = discord.Embed(
                title="Altcheck Report",
                description=shorten_text(concise_report_text(report), 4096),
                color=altcheck_color(report),
            )
            embed.add_field(name="User", value=target_text, inline=False)
            embed.add_field(name="Assessment", value=report_assessment(report), inline=False)
            embed.add_field(
                name="Score",
                value=f"`{report.score}/100` - likelihood `{report.likelihood_percent}%`",
                inline=True,
            )
            embed.add_field(name="Level", value=report.level.title(), inline=True)
            embed.add_field(name="Best Match", value=best_match_text(report), inline=False)
            embed.add_field(
                name="Point Breakdown",
                value=(
                    f"Profile `{report.profile_points}` | "
                    f"Language `{report.language_points}` | "
                    f"Account `{report.account_points}`"
                ),
                inline=False,
            )
            embed.add_field(
                name="Language Samples",
                value=f"Target `{report.target_message_count}` | Match `{report.matched_message_count}`",
                inline=True,
            )
            embed.add_field(name="Evidence", value=shorten_text(evidence_text, 1024), inline=False)

            embed.set_footer(text="Altcheck uses public Discord profile and message-language patterns only.")

            if target_profile is not None:
                embed.set_thumbnail(url=target_profile.display_avatar.url)

            logged_alt_flag = False
            logging_cog = self.bot.get_cog("Logging")
            if report.is_medium_or_high and logging_cog is not None and ctx.guild is not None:
                send_alt_flag_log = getattr(logging_cog, "send_alt_flag_log", None)

                if send_alt_flag_log is not None:
                    logged_alt_flag = await send_alt_flag_log(
                        report,
                        guild=ctx.guild,
                        user=target_profile,
                        source=f"Manual altcheck by {ctx.author} ({ctx.author.id})",
                        force=True,
                    )

            if report.matched_banned_user_id is not None:
                if config.ALT_ALERT_ROLE_ID and logged_alt_flag:
                    ping_note = f"<@&{config.ALT_ALERT_ROLE_ID}> was pinged in the other log thread."
                elif config.ALT_ALERT_ROLE_ID:
                    ping_note = "The alert role is configured, but I could not send the other-thread alert."
                else:
                    ping_note = "`ALT_ALERT_ROLE_ID` is not configured, so no role was pinged."

                embed.add_field(name="Banned Match Alert", value=ping_note, inline=False)

        await ctx.reply(
            embed=embed,
            mention_author=False,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @commands.command(name="modhelp")
    async def help_msg(self, ctx: commands.Context):
        prefix = config.COMMAND_PREFIX

        message = (
            f"**{config.NETWORK_NAME} staff commands**\n\n"
            f"`{prefix}ban user_id reason`\n"
            f"`{prefix}tempban user_id duration reason`\n"
            f"`{prefix}unban user_id reason`\n\n"
            f"`{prefix}kick user_id reason`\n\n"
            f"`{prefix}mute user_id duration reason`\n"
            f"`{prefix}unmute user_id reason`\n\n"
            f"`{prefix}altcheck user_id`\n\n"
            f"`{prefix}warn user_id reason`\n"
            f"`{prefix}warns user_id`\n"
            f"`{prefix}warncount user_id`\n"
            f"`{prefix}warninfo warn_id`\n"
            f"`{prefix}removewarn warn_id reason`\n\n"
            f"`{prefix}userinfo user_id`\n\n"
            f"`{prefix}syncbans affiliate_guild_id confirm` - copy current home-server bans to one specified affiliate server\n\n"
            f"Durations support `s`, `m`, `h`, `d`, and `w`."
        )

        await ctx.reply(message, mention_author=False)


async def setup(bot: commands.Bot):
    await bot.add_cog(Moderation(bot))
