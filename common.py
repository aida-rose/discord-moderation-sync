import re
import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Optional, Callable, Awaitable

import discord
from discord.ext import commands

import config

try:
    from affiliate_config import (
        get_runtime_affiliate_ids,
        get_affiliate_log_channel_id,
    )
except ImportError:
    def get_runtime_affiliate_ids() -> set[int]:
        return set()

    def get_affiliate_log_channel_id(guild_id: int) -> int | None:
        return None


class WrongServer(commands.CheckFailure):
    pass


class NotModStaff(commands.CheckFailure):
    pass


class NotBanStaff(commands.CheckFailure):
    pass


class NothingToDo(Exception):
    pass


def current_sync_guild_id_list() -> list[int]:
    """
    Returns synced guild IDs in a stable order.

    Includes:
    - home/base guild IDs loaded from SQLite-backed config.py
    - runtime affiliate guild IDs from the shared moderation database
    """

    ids: list[int] = []

    for guild_id in config.SYNC_GUILD_IDS:
        if guild_id not in ids:
            ids.append(guild_id)

    for guild_id in sorted(get_runtime_affiliate_ids()):
        if guild_id not in ids:
            ids.append(guild_id)

    return ids


def current_sync_guild_ids() -> set[int]:
    return set(current_sync_guild_id_list())



def current_modlog_routes() -> dict[int, int]:
    """
    Returns mod-log routes from config plus runtime affiliate log channels.

    Runtime affiliate routes only apply when the shared moderation database has
    a log_channel_id for that affiliate.
    """

    routes = dict(config.MODLOG_ROUTES)

    for guild_id in get_runtime_affiliate_ids():
        log_channel_id = get_affiliate_log_channel_id(guild_id)

        if log_channel_id is not None:
            routes[guild_id] = log_channel_id

    return routes


@dataclass
class GuildResult:
    guild_name: str
    guild_id: int
    status: str
    detail: str


@dataclass
class BackfillResult:
    guild_name: str
    guild_id: int
    status: str
    already_banned: int = 0
    newly_banned: int = 0
    failed: int = 0
    detail: str = ""


_RAW_USER_ID = re.compile(r"^\d{15,25}$")


def user_id_arg(target: str) -> int:
    target = target.strip()

    if not _RAW_USER_ID.fullmatch(target):
        raise commands.BadArgument(
            "Use a raw Discord user ID, not a mention or username. "
            f"Example: `{config.COMMAND_PREFIX}ban 123456789012345678 reason`"
        )

    return int(target)


async def user_or_snowflake(bot: commands.Bot, user_id: int) -> discord.abc.Snowflake:
    try:
        return await bot.fetch_user(user_id)
    except discord.NotFound:
        raise commands.BadArgument("That Discord user ID does not appear to exist.")
    except discord.HTTPException:
        return discord.Object(id=user_id)


async def display_user(bot: commands.Bot, user_id: int) -> Optional[discord.User]:
    try:
        return await bot.fetch_user(user_id)
    except discord.HTTPException:
        return None


async def member_in(guild: discord.Guild, user_id: int) -> Optional[discord.Member]:
    member = guild.get_member(user_id)
    if member is not None:
        return member

    try:
        return await guild.fetch_member(user_id)
    except discord.NotFound:
        return None


def target_label(user: Optional[discord.User], user_id: int) -> str:
    if user is None:
        return f"`{user_id}`"

    return f"{user} (`{user_id}`)"


def duration_arg(duration_text: str, *, max_duration: Optional[timedelta] = timedelta(days=28)) -> timedelta:
    match = re.fullmatch(r"(\d+)([smhdw])", duration_text.lower().strip())

    if not match:
        raise commands.BadArgument("Invalid duration. Use `30s`, `10m`, `2h`, `7d`, or `1w`.")

    amount = int(match.group(1))
    unit = match.group(2)

    seconds_per_unit = {
        "s": 1,
        "m": 60,
        "h": 60 * 60,
        "d": 60 * 60 * 24,
        "w": 60 * 60 * 24 * 7,
    }

    total_seconds = amount * seconds_per_unit[unit]

    if total_seconds <= 0:
        raise commands.BadArgument("Duration must be longer than 0 seconds.")

    duration = timedelta(seconds=total_seconds)

    if max_duration is not None and duration > max_duration:
        raise commands.BadArgument(f"Duration cannot be longer than {max_duration}.")

    return duration


def is_staff(member: discord.Member) -> bool:
    return any(role.id in config.STAFF_ROLE_IDS for role in member.roles)


def is_ban_staff(member: discord.Member) -> bool:
    return any(role.id in config.BAN_STAFF_ROLE_IDS for role in member.roles)


def trim(text: str, limit: int = 900) -> str:
    if len(text) <= limit:
        return text

    return text[: limit - 3] + "..."


def discord_time(iso_text: str) -> str:
    try:
        dt = datetime.fromisoformat(iso_text)
        return f"<t:{int(dt.timestamp())}:f>"
    except Exception:
        return iso_text

def user_avatar_url(user: discord.User | discord.Member | None) -> str | None:
    if user is None:
        return None

    return user.display_avatar.url


async def post_modlog(bot: commands.Bot, embed: discord.Embed) -> None:
    for guild_id, channel_id in current_modlog_routes().items():
        channel = bot.get_channel(channel_id)

        if channel is None:
            try:
                channel = await bot.fetch_channel(channel_id)
            except discord.HTTPException:
                print(f"Could not fetch log channel {channel_id} for guild {guild_id}")
                continue

        try:
            await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
        except discord.HTTPException as exc:
            print(f"Failed to send log to channel {channel_id}: {exc}")


def is_user_not_in_server_result(result: GuildResult) -> bool:
    detail = str(getattr(result, "detail", "")).lower()

    return (
        getattr(result, "status", "") == "Skipped"
        and (
            "not in this server" in detail
            or "not in this server right now" in detail
            or "not currently in" in detail
        )
    )


def is_permission_or_hierarchy_result(result: GuildResult) -> bool:
    detail = str(getattr(result, "detail", "")).lower()

    return (
        getattr(result, "status", "") == "Failed"
        and (
            "missing permission" in detail
            or "role hierarchy" in detail
            or "role is too low" in detail
            or "above the bot" in detail
            or "hierarchy is too low" in detail
        )
    )


def server_list_text(results: list[GuildResult]) -> str:
    lines = [
        f"- **{result.guild_name}** `({result.guild_id})`"
        for result in results
    ]

    return "\n".join(lines) if lines else "None"


def guild_result_text(results: list[GuildResult]) -> str:
    successful = [
        result
        for result in results
        if getattr(result, "status", "") == "Done"
    ]
    user_not_in_server = [
        result
        for result in results
        if is_user_not_in_server_result(result)
    ]
    permission_or_hierarchy = [
        result
        for result in results
        if is_permission_or_hierarchy_result(result)
    ]
    other_failures = [
        result
        for result in results
        if getattr(result, "status", "") in {"Failed", "Needs review"}
        and result not in permission_or_hierarchy
    ]

    lines = [
        f"Succeeded: **{len(successful)}**",
        f"User not in server: **{len(user_not_in_server)}**",
        f"Missing permissions / hierarchy: **{len(permission_or_hierarchy)}**",
        "Missing permissions / hierarchy servers:",
        server_list_text(permission_or_hierarchy),
    ]

    if other_failures:
        lines.extend(
            [
                f"Other failures / needs review: **{len(other_failures)}**",
                server_list_text(other_failures),
            ]
        )

    text = "\n".join(lines)
    return trim(text, 1000)


async def case_log(
    bot: commands.Bot,
    *,
    action: str,
    moderator: discord.Member,
    target_user: Optional[discord.User],
    target_user_id: int,
    reason: str,
    results: list[GuildResult],
    duration: Optional[timedelta] = None,
    dm_result: Optional[str] = None,
) -> None:
    embed = discord.Embed(title=f"{config.CASE_TAG} {action}", color=discord.Color.orange())

    avatar_url = user_avatar_url(target_user)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    embed.add_field(name="Target", value=target_label(target_user, target_user_id), inline=False)
    embed.add_field(name="Moderator", value=f"{moderator} (`{moderator.id}`)", inline=False)

    if duration is not None:
        embed.add_field(name="Duration", value=str(duration), inline=True)

    embed.add_field(name="Reason", value=reason[:1024] if reason else "No reason provided.", inline=False)

    if dm_result is not None:
        embed.add_field(name="User DM", value=dm_result, inline=False)

    embed.add_field(name="Server Results", value=guild_result_text(results), inline=False)

    await post_modlog(bot, embed)


async def warn_log(
    bot: commands.Bot,
    *,
    action: str,
    moderator: discord.Member,
    target_user: Optional[discord.User],
    target_user_id: int,
    warn_id: int,
    reason: str,
    active_warn_count: Optional[int] = None,
    dm_result: Optional[str] = None,
    removed_reason: Optional[str] = None,
) -> None:
    color = discord.Color.green() if action.lower().startswith("warning removed") else discord.Color.gold()
    embed = discord.Embed(title=action, color=color)

    avatar_url = user_avatar_url(target_user)
    if avatar_url:
        embed.set_thumbnail(url=avatar_url)

    embed.add_field(name="Warn ID", value=f"`{warn_id}`", inline=True)

    if active_warn_count is not None:
        embed.add_field(name="Active Warn Count", value=str(active_warn_count), inline=True)

    

    embed.add_field(name="Target", value=target_label(target_user, target_user_id), inline=False)
    embed.add_field(name="Moderator", value=f"{moderator} (`{moderator.id}`)", inline=False)
    embed.add_field(name="Warning Reason", value=trim(reason, 1024), inline=False)

    if removed_reason is not None:
        embed.add_field(name="Removal Reason", value=trim(removed_reason, 1024), inline=False)

    if dm_result is not None:
        embed.add_field(name="User DM", value=dm_result, inline=False)

    await post_modlog(bot, embed)


def backfill_text(summaries: list[BackfillResult]) -> str:
    lines = []

    for summary in summaries:
        lines.append(
            f"{summary.status} **{summary.guild_name}** `({summary.guild_id})`\n"
            f"> Newly banned: **{summary.newly_banned}** | "
            f"Already banned: **{summary.already_banned}** | "
            f"Failed: **{summary.failed}**\n"
            f"> {summary.detail}"
        )

    return trim("\n".join(lines), 1000)


async def backfill_log(
    bot: commands.Bot,
    *,
    moderator: discord.Member,
    home_guild: discord.Guild,
    home_ban_count: int,
    summaries: list[BackfillResult],
) -> None:
    embed = discord.Embed(title=f"{config.CASE_TAG} Primary Ban Backfill", color=discord.Color.red())

    embed.add_field(name="Moderator", value=f"{moderator} (`{moderator.id}`)", inline=False)
    embed.add_field(name="Primary Server", value=f"{home_guild.name} `({home_guild.id})`", inline=False)
    embed.add_field(name="Primary Ban Count", value=str(home_ban_count), inline=True)
    embed.add_field(
        name="Affiliate Results",
        value=backfill_text(summaries) or "No affiliate servers configured.",
        inline=False,
    )

    await post_modlog(bot, embed)


async def for_each_guild(
    bot: commands.Bot,
    action_callback: Callable[[discord.Guild], Awaitable[str]],
) -> list[GuildResult]:
    results: list[GuildResult] = []

    for guild_id in current_sync_guild_id_list():
        guild = bot.get_guild(guild_id)

        if guild is None:
            results.append(
                GuildResult(
                    guild_name="Unknown / Not Cached",
                    guild_id=guild_id,
                    status="Needs review",
                    detail="Guild was not available to the bot. Check the affiliate guild list and bot invite.",
                )
            )
            continue

        try:
            detail = await action_callback(guild)
            results.append(GuildResult(guild.name, guild.id, "Done", detail))
        except NothingToDo as exc:
            results.append(GuildResult(guild.name, guild.id, "Skipped", str(exc)))
        except discord.Forbidden:
            results.append(
                GuildResult(
                    guild.name,
                    guild.id,
                    "Failed",
                    "Discord denied the action. Check bot role position and moderation permissions.",
                )
            )
        except discord.HTTPException as exc:
            results.append(GuildResult(guild.name, guild.id, "Failed", f"Discord API error: {exc}"))

        await asyncio.sleep(0.25)

    return results


async def send_notice(
    bot: commands.Bot,
    *,
    user_id: int,
    action: str,
    reason: str,
    moderator: discord.Member,
    duration: Optional[timedelta] = None,
) -> str:
    if not config.SEND_USER_NOTICES:
        return "Skipped: member notice DMs are disabled in config."

    try:
        user = await bot.fetch_user(user_id)
    except discord.NotFound:
        return "Notice not sent: Discord could not find that user ID."
    except discord.HTTPException as exc:
        return f"Notice not sent: user lookup failed. `{exc}`"

    red_notice_keywords = ("ban", "kick", "mute", "timeout", "warning")
    notice_color = (
        discord.Color.red()
        if any(keyword in action.lower() for keyword in red_notice_keywords)
        else discord.Color.green()
    )

    embed = discord.Embed(
        title=f"{action} Notice",
        description=f"A moderation action was recorded by **{config.NETWORK_NAME}** staff.",
        color=notice_color,
    )

    embed.add_field(name="Action", value=action, inline=False)

    if duration is not None:
        embed.add_field(name="Duration", value=str(duration), inline=False)

    embed.add_field(name="Reason", value=reason[:1024] if reason else "No reason provided.", inline=False)
    # embed.add_field(name="Moderator", value=str(moderator), inline=False)

    if config.APPEAL_URL:
        embed.add_field(name="Appeal / Questions", value=config.APPEAL_URL, inline=False)

    embed.set_footer(text=f"{config.NETWORK_NAME} moderation notice")

    try:
        await user.send(embed=embed)
        return "Member notice sent."
    except discord.Forbidden:
        return "Notice not sent: user has DMs closed or blocked the bot."
    except discord.HTTPException as exc:
        return f"Notice not sent: Discord API error. `{exc}`"


async def banned_ids(guild: discord.Guild) -> set[int]:
    ids: set[int] = set()

    async for ban_entry in guild.bans(limit=None):
        ids.add(ban_entry.user.id)

    return ids


async def home_ban_entries(home_guild: discord.Guild) -> list[discord.BanEntry]:
    entries: list[discord.BanEntry] = []

    async for ban_entry in home_guild.bans(limit=None):
        entries.append(ban_entry)

    return entries
