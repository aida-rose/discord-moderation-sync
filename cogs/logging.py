import csv
import io
import os
import re
from datetime import datetime, timezone
from typing import Optional, Iterable

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


def load_flagged_terms_from_file(path: str) -> list[str]:
    """
    Loads flagged words/phrases from swears.txt.

    Supports:
    - one word/phrase per line
    - blank lines
    - comments starting with #
    """

    terms: list[str] = []

    if not os.path.exists(path):
        print(f"[logging.py] Flagged words file not found: {path}")
        return terms

    with open(path, "r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()

            if not line:
                continue

            if line.startswith("#"):
                continue

            terms.append(line.lower())

    return terms


def flagged_term_pattern(term: str) -> re.Pattern[str]:
    normalized = " ".join(term.split())
    escaped_parts = [re.escape(part) for part in normalized.split()]
    phrase_pattern = r"\s+".join(escaped_parts)

    return re.compile(rf"(?<!\w){phrase_pattern}(?!\w)", re.IGNORECASE)


def matched_flagged_terms(content: str, terms: list[str]) -> list[str]:
    matches: list[str] = []

    for term in terms:
        if not term:
            continue

        if flagged_term_pattern(term).search(content):
            matches.append(term)

    return matches


# ============================================================
# Config
# ============================================================

def current_logged_guild_ids() -> set[int]:
    return (
        ({config.HOME_GUILD_ID} if config.HOME_GUILD_ID else set())
        | set(config.AFFILIATE_GUILD_IDS)
        | set(config.LOGGED_GUILD_IDS)
        | get_runtime_affiliate_ids()
    )

THREADS = {
    "moderation": "LOG_MODERATION_THREAD_ID",
    "server_management": "LOG_SERVER_MANAGEMENT_THREAD_ID",
    "invite": "LOG_INVITE_THREAD_ID",
    "user": "LOG_USER_THREAD_ID",
    "reaction": "LOG_REACTION_THREAD_ID",
    "flagged_message": "LOG_FLAGGED_MESSAGE_THREAD_ID",
    "message": "LOG_MESSAGE_THREAD_ID",
    "vc": "LOG_VC_THREAD_ID",
    "joins": "LOG_JOINS_THREAD_ID",
    "other": "LOG_OTHER_THREAD_ID",
    "role_management": "LOG_ROLE_MANAGEMENT_THREAD_ID",
}


def current_thread_id(category: str) -> int:
    setting_name = THREADS.get(category, "LOG_OTHER_THREAD_ID")
    thread_id = getattr(config, setting_name, 0)
    return thread_id or config.LOG_OTHER_THREAD_ID


def current_flagged_regex() -> re.Pattern[str] | None:
    if not config.FLAGGED_MESSAGE_REGEX:
        return None

    try:
        return re.compile(config.FLAGGED_MESSAGE_REGEX, re.IGNORECASE)
    except re.error as exc:
        print(f"[logging.py] Invalid FLAGGED_MESSAGE_REGEX: {exc}")
        return None


# ============================================================
# Formatting helpers
# ============================================================

def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def truncate(value: object, limit: int = 1024) -> str:
    text = str(value) if value is not None else ""

    if not text:
        return "None"

    if len(text) <= limit:
        return text

    return text[: limit - 3] + "..."


def format_user(user: Optional[discord.abc.User]) -> str:
    if user is None:
        return "Unknown"

    return f"{user} (`{user.id}`)"


def format_member(member: Optional[discord.Member]) -> str:
    if member is None:
        return "Unknown"

    return f"{member} (`{member.id}`)"


def format_channel(channel: Optional[discord.abc.GuildChannel | discord.Thread]) -> str:
    if channel is None:
        return "Unknown"

    mention = getattr(channel, "mention", None)

    if mention:
        return f"{mention} (`{channel.id}`)"

    return f"{channel.name} (`{channel.id}`)"


def format_role(role: Optional[discord.Role]) -> str:
    if role is None:
        return "Unknown"

    return f"{role.mention} `{role.name}` (`{role.id}`)"


def format_guild(guild: Optional[discord.Guild]) -> str:
    if guild is None:
        return "Unknown"

    return f"{guild.name} (`{guild.id}`)"


def format_dt(value: Optional[datetime]) -> str:
    if value is None:
        return "None"

    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)

    unix = int(value.timestamp())
    return f"<t:{unix}:F> (`{value.isoformat()}`)"


def role_diff(before_roles: Iterable[discord.Role], after_roles: Iterable[discord.Role]):
    before_ids = {role.id: role for role in before_roles}
    after_ids = {role.id: role for role in after_roles}

    added = [
        after_ids[role_id]
        for role_id in after_ids
        if role_id not in before_ids
    ]

    removed = [
        before_ids[role_id]
        for role_id in before_ids
        if role_id not in after_ids
    ]

    return added, removed


def role_list_text(roles: list[discord.Role]) -> str:
    if not roles:
        return "None"

    return "\n".join(format_role(role) for role in roles)[:1024]


def channel_type_name(channel) -> str:
    if isinstance(channel, discord.CategoryChannel):
        return "Category"

    if isinstance(channel, discord.TextChannel):
        return "Text Channel"

    if isinstance(channel, discord.VoiceChannel):
        return "Voice Channel"

    if isinstance(channel, discord.StageChannel):
        return "Stage Channel"

    if isinstance(channel, discord.ForumChannel):
        return "Forum Channel"

    if isinstance(channel, discord.Thread):
        return "Thread"

    return type(channel).__name__


def get_changed_attrs(before, after, attrs: list[str]) -> list[str]:
    changes = []

    for attr in attrs:
        before_value = getattr(before, attr, None)
        after_value = getattr(after, attr, None)

        if before_value != after_value:
            changes.append(
                f"**{attr}**\nBefore: `{truncate(before_value, 400)}`\nAfter: `{truncate(after_value, 400)}`"
            )

    return changes


def message_link(guild_id: int, channel_id: int, message_id: int) -> str:
    return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"


# ============================================================
# Logging cog
# ============================================================

class Logging(commands.Cog):
    """
    Thread-based logging cog.

    Thread routing:
    - moderation: bans, kicks, timeouts, mutes, unbans, warning logs
    - server_management: thread, channel, category, emoji, sticker, soundboard, events, server changes
    - invite: invite creations
    - user: role updates, username changes, member role additions/removals
    - reaction: reactions
    - flagged_message: flagged possible inappropriate messages
    - message: message edits/deletions/bulk deletions
    - vc: voice channel and stage activity
    - joins: server joins/leaves
    - other: anything else
    """

    def __init__(self, bot: commands.Bot):
        self.bot = bot

    # ------------------------------------------------------------
    # Core log sending
    # ------------------------------------------------------------

    def should_log_guild(self, guild: Optional[discord.Guild]) -> bool:
        if guild is None:
            return False

        return guild.id in current_logged_guild_ids()

    async def get_log_thread(
        self,
        category: str,
        guild: Optional[discord.Guild] = None,
    ):
        thread_id = current_thread_id(category)

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

    async def send_log(
        self,
        *,
        category: str,
        guild: Optional[discord.Guild],
        title: str,
        description: Optional[str] = None,
        fields: Optional[list[tuple[str, str, bool]]] = None,
        color: discord.Color = discord.Color.blurple(),
        file: Optional[discord.File] = None,
    ):
        if guild is not None and not self.should_log_guild(guild):
            return

        embed = discord.Embed(
            title=title,
            description=truncate(description, 4096) if description else None,
            color=color,
            timestamp=utc_now(),
        )

        embed.add_field(
            name="Server",
            value=format_guild(guild),
            inline=False,
        )

        if fields:
            for name, value, inline in fields:
                embed.add_field(
                    name=truncate(name, 256),
                    value=truncate(value, 1024),
                    inline=inline,
                )

        embed.set_footer(text=f"Log category: {category}")

        try:
            thread = await self.get_log_thread(category, guild)

            if thread is None:
                return

            await thread.send(
                embed=embed,
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except Exception as exc:
            print(f"[logging.py] Failed to send {category} log: {exc}")

    async def send_simple(
        self,
        category: str,
        guild: Optional[discord.Guild],
        title: str,
        description: str,
        color: discord.Color = discord.Color.blurple(),
    ):
        await self.send_log(
            category=category,
            guild=guild,
            title=title,
            description=description,
            color=color,
        )

    async def find_audit_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: Optional[int] = None,
        within_seconds: int = 15,
    ) -> Optional[discord.AuditLogEntry]:
        try:
            async for entry in guild.audit_logs(limit=8, action=action):
                age = abs((utc_now() - entry.created_at).total_seconds())

                if age > within_seconds:
                    continue

                if target_id is None:
                    return entry

                target = entry.target
                entry_target_id = getattr(target, "id", None)

                if entry_target_id == target_id:
                    return entry

        except discord.Forbidden:
            return None

        except discord.HTTPException:
            return None

        return None

    async def audit_fields(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: Optional[int] = None,
    ) -> list[tuple[str, str, bool]]:
        entry = await self.find_audit_entry(guild, action, target_id)

        if entry is None:
            return [
                ("Moderator / Actor", "Unknown or missing View Audit Log permission.", False),
            ]

        fields = [
            ("Moderator / Actor", format_user(entry.user), False),
        ]

        if entry.reason:
            fields.append(("Audit Log Reason", entry.reason, False))

        return fields

    # ------------------------------------------------------------
    # Public helper methods for other cogs
    # ------------------------------------------------------------

    async def moderation_log(
        self,
        *,
        guild: discord.Guild,
        title: str,
        description: Optional[str] = None,
        fields: Optional[list[tuple[str, str, bool]]] = None,
        color: discord.Color = discord.Color.orange(),
    ):
        await self.send_log(
            category="moderation",
            guild=guild,
            title=title,
            description=description,
            fields=fields,
            color=color,
        )

    async def nation_selector_log(
        self,
        *,
        guild: Optional[discord.Guild],
        message: str,
    ):
    
        if guild is not None and guild.id != config.HOME_GUILD_ID:
            return

        thread_id = config.NATION_SELECTOR_LOG_THREAD_ID or config.LOG_OTHER_THREAD_ID

        if thread_id == 0:
            return

        try:
            channel = self.bot.get_channel(thread_id)

            if channel is None:
                channel = await self.bot.fetch_channel(thread_id)

            if isinstance(channel, discord.Thread) and channel.archived:
                try:
                    await channel.edit(archived=False)
                except discord.HTTPException:
                    pass

            await channel.send(
                message,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except Exception as exc:
            print(f"[logging.py] Failed to send nation selector log: {exc}")

    async def warn_log(
        self,
        *,
        guild: Optional[discord.Guild] = None,
        action: str = "Warning",
        moderator: Optional[discord.abc.User] = None,
        target_user: Optional[discord.abc.User] = None,
        target_user_id: Optional[int] = None,
        warn_id: Optional[int] = None,
        reason: str = "No reason provided.",
        active_warn_count: Optional[int] = None,
        dm_result: Optional[str] = None,
        removed_reason: Optional[str] = None,
    ):
     
        fields: list[tuple[str, str, bool]] = []

        if warn_id is not None:
            fields.append(("Warn ID", f"`{warn_id}`", True))

        if target_user_id is not None:
            target_value = format_user(target_user) if target_user else f"`{target_user_id}`"
            fields.append(("Target", target_value, False))

        if moderator is not None:
            fields.append(("Moderator", format_user(moderator), False))

        fields.append(("Reason", reason or "No reason provided.", False))

        if active_warn_count is not None:
            fields.append(("Active Warn Count", str(active_warn_count), True))

        if dm_result is not None:
            fields.append(("User DM", dm_result, False))

        if removed_reason is not None:
            fields.append(("Removal Reason", removed_reason, False))

        await self.send_log(
            category="moderation",
            guild=guild,
            title=action,
            fields=fields,
            color=discord.Color.gold(),
        )

    async def log_warn(self, **kwargs):
        await self.warn_log(**kwargs)

    async def log_warning(self, **kwargs):
        await self.warn_log(**kwargs)

    # ------------------------------------------------------------
    # Message logs
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None:
            return

        if message.author.bot:
            return

        if not self.should_log_guild(message.guild):
            return

        if not message.content:
            return

        flagged_terms = load_flagged_terms_from_file(config.SWEARS_FILE)
        flagged_re = current_flagged_regex()
        matched_terms = matched_flagged_terms(message.content, flagged_terms)

        regex_match = flagged_re.search(message.content) if flagged_re else None

        if not matched_terms and not regex_match:
            return

        reason_parts = []

        if matched_terms:
            reason_parts.append("Matched term(s): " + ", ".join(f"`{term}`" for term in matched_terms[:10]))

        if regex_match:
            reason_parts.append(f"Matched regex: `{truncate(regex_match.group(0), 100)}`")

        await self.send_log(
            category="flagged_message",
            guild=message.guild,
            title="Flagged Possible Inappropriate Message",
            fields=[
                ("Author", format_user(message.author), False),
                ("Channel", format_channel(message.channel), False),
                ("Reason", "\n".join(reason_parts), False),
                ("Message", truncate(message.content, 1024), False),
                ("Jump Link", message.jump_url, False),
            ],
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_message_delete(self, message: discord.Message):
        if message.guild is None:
            return

        if not self.should_log_guild(message.guild):
            return

        attachments = "\n".join(attachment.url for attachment in message.attachments) or "None"

        await self.send_log(
            category="message",
            guild=message.guild,
            title="Message Deleted",
            fields=[
                ("Author", format_user(message.author), False),
                ("Channel", format_channel(message.channel), False),
                ("Message ID", f"`{message.id}`", True),
                ("Content", truncate(message.content or "[No cached content]", 1024), False),
                ("Attachments", truncate(attachments, 1024), False),
            ],
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_bulk_message_delete(self, messages: list[discord.Message]):
        if not messages:
            return

        guild = messages[0].guild

        if guild is None:
            return

        if not self.should_log_guild(guild):
            return

        channel = messages[0].channel

        output = io.StringIO()
        writer = csv.writer(output)

        writer.writerow([
            "Message ID",
            "Author",
            "Author ID",
            "Created At UTC",
            "Channel",
            "Channel ID",
            "Content",
            "Attachments",
        ])

        for message in sorted(messages, key=lambda item: item.created_at):
            writer.writerow([
                message.id,
                str(message.author),
                getattr(message.author, "id", ""),
                message.created_at.isoformat(),
                getattr(channel, "name", "Unknown"),
                getattr(channel, "id", "Unknown"),
                message.content or "",
                " | ".join(attachment.url for attachment in message.attachments),
            ])

        data = output.getvalue().encode("utf-8")
        file = discord.File(
            io.BytesIO(data),
            filename=f"bulk-delete-{guild.id}-{int(utc_now().timestamp())}.txt",
        )

        await self.send_log(
            category="message",
            guild=guild,
            title="Bulk Message Deletion",
            fields=[
                ("Channel", format_channel(channel), False),
                ("Messages Deleted", str(len(messages)), True),
            ],
            color=discord.Color.red(),
            file=file,
        )

    @commands.Cog.listener()
    async def on_message_edit(self, before: discord.Message, after: discord.Message):
        if before.guild is None:
            return

        if before.author.bot:
            return

        if before.content == after.content:
            return

        if not self.should_log_guild(before.guild):
            return

        await self.send_log(
            category="message",
            guild=before.guild,
            title="Message Edited",
            fields=[
                ("Author", format_user(before.author), False),
                ("Channel", format_channel(before.channel), False),
                ("Before", truncate(before.content or "[No cached content]", 1024), False),
                ("After", truncate(after.content or "[No cached content]", 1024), False),
                ("Jump Link", after.jump_url, False),
            ],
            color=discord.Color.orange(),
        )

    # ------------------------------------------------------------
    # Moderation logs
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_ban(self, guild: discord.Guild, user: discord.User):
        if not self.should_log_guild(guild):
            return

        fields = [
            ("Target", format_user(user), False),
        ]

        fields.extend(await self.audit_fields(guild, discord.AuditLogAction.ban, user.id))

        await self.send_log(
            category="moderation",
            guild=guild,
            title="User Banned",
            fields=fields,
            color=discord.Color.dark_red(),
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        if not self.should_log_guild(guild):
            return

        fields = [
            ("Target", format_user(user), False),
        ]

        fields.extend(await self.audit_fields(guild, discord.AuditLogAction.unban, user.id))

        await self.send_log(
            category="moderation",
            guild=guild,
            title="User Unbanned",
            fields=fields,
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_member_remove(self, member: discord.Member):
        if not self.should_log_guild(member.guild):
            return

        kick_entry = await self.find_audit_entry(
            member.guild,
            discord.AuditLogAction.kick,
            member.id,
        )

        if kick_entry is not None:
            fields = [
                ("Target", format_member(member), False),
                ("Moderator / Actor", format_user(kick_entry.user), False),
            ]

            if kick_entry.reason:
                fields.append(("Audit Log Reason", kick_entry.reason, False))

            await self.send_log(
                category="moderation",
                guild=member.guild,
                title="User Kicked",
                fields=fields,
                color=discord.Color.dark_orange(),
            )
            return

        await self.send_log(
            category="joins",
            guild=member.guild,
            title="Server Leave",
            fields=[
                ("User", format_member(member), False),
                ("Joined Server", format_dt(member.joined_at), False),
                ("Account Created", format_dt(member.created_at), False),
            ],
            color=discord.Color.dark_gray(),
        )

    # ------------------------------------------------------------
    # Joins
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_join(self, member: discord.Member):
        if not self.should_log_guild(member.guild):
            return

        await self.send_log(
            category="joins",
            guild=member.guild,
            title="Server Join",
            fields=[
                ("User", format_member(member), False),
                ("Account Created", format_dt(member.created_at), False),
                ("Bot Account", str(member.bot), True),
            ],
            color=discord.Color.green(),
        )

    # ------------------------------------------------------------
    # Member updates: roles, timeout/mute, nicknames
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if not self.should_log_guild(after.guild):
            return

        if before.timed_out_until != after.timed_out_until:
            if after.timed_out_until is not None and after.timed_out_until > utc_now():
                title = "User Timed Out / Muted"
                color = discord.Color.dark_orange()
            else:
                title = "User Timeout / Mute Removed"
                color = discord.Color.green()

            fields = [
                ("Target", format_member(after), False),
                ("Before", format_dt(before.timed_out_until), True),
                ("After", format_dt(after.timed_out_until), True),
            ]

            fields.extend(
                await self.audit_fields(
                    after.guild,
                    discord.AuditLogAction.member_update,
                    after.id,
                )
            )

            await self.send_log(
                category="moderation",
                guild=after.guild,
                title=title,
                fields=fields,
                color=color,
            )

        if before.nick != after.nick:
            await self.send_log(
                category="user",
                guild=after.guild,
                title="Nickname Changed",
                fields=[
                    ("User", format_member(after), False),
                    ("Before", before.nick or "None", True),
                    ("After", after.nick or "None", True),
                ],
                color=discord.Color.blurple(),
            )

        added_roles, removed_roles = role_diff(before.roles, after.roles)

        if added_roles or removed_roles:
            fields = [
                ("User", format_member(after), False),
            ]

            if added_roles:
                fields.append(("Roles Added", role_list_text(added_roles), False))

            if removed_roles:
                fields.append(("Roles Removed", role_list_text(removed_roles), False))

            fields.extend(
                await self.audit_fields(
                    after.guild,
                    discord.AuditLogAction.member_role_update,
                    after.id,
                )
            )

            await self.send_log(
                category="user",
                guild=after.guild,
                title="User Role Update",
                fields=fields,
                color=discord.Color.blue(),
            )

            mute_added = [
                role for role in added_roles
                if "mute" in role.name.lower() or "timeout" in role.name.lower()
            ]

            mute_removed = [
                role for role in removed_roles
                if "mute" in role.name.lower() or "timeout" in role.name.lower()
            ]

            if mute_added:
                await self.send_log(
                    category="moderation",
                    guild=after.guild,
                    title="Mute Role Added",
                    fields=[
                        ("Target", format_member(after), False),
                        ("Role(s)", role_list_text(mute_added), False),
                    ],
                    color=discord.Color.dark_orange(),
                )

            if mute_removed:
                await self.send_log(
                    category="moderation",
                    guild=after.guild,
                    title="Mute Role Removed",
                    fields=[
                        ("Target", format_member(after), False),
                        ("Role(s)", role_list_text(mute_removed), False),
                    ],
                    color=discord.Color.green(),
                )

    # ------------------------------------------------------------
    # Global username / profile changes
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_user_update(self, before: discord.User, after: discord.User):
        changed = []

        if before.name != after.name:
            changed.append(f"**Username**\nBefore: `{before.name}`\nAfter: `{after.name}`")

        if before.global_name != after.global_name:
            changed.append(f"**Display Name**\nBefore: `{before.global_name}`\nAfter: `{after.global_name}`")

        if before.discriminator != after.discriminator:
            changed.append(f"**Discriminator**\nBefore: `{before.discriminator}`\nAfter: `{after.discriminator}`")

        if not changed:
            return

        for guild in self.bot.guilds:
            if guild.get_member(after.id) and self.should_log_guild(guild):
                await self.send_log(
                    category="user",
                    guild=guild,
                    title="Username / Profile Changed",
                    fields=[
                        ("User", format_user(after), False),
                        ("Changes", "\n\n".join(changed), False),
                    ],
                    color=discord.Color.blurple(),
                )

    # ------------------------------------------------------------
    # Role create/update/delete
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_role_create(self, role: discord.Role):
        if not self.should_log_guild(role.guild):
            return

        fields = [
            ("Role", format_role(role), False),
        ]

        fields.extend(await self.audit_fields(role.guild, discord.AuditLogAction.role_create, role.id))

        await self.send_log(
            category="role_management",
            guild=role.guild,
            title="Role Created",
            fields=fields,
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if not self.should_log_guild(role.guild):
            return

        fields = [
            ("Role", f"`{role.name}` (`{role.id}`)", False),
        ]

        fields.extend(await self.audit_fields(role.guild, discord.AuditLogAction.role_delete, role.id))

        await self.send_log(
            category="role_management",
            guild=role.guild,
            title="Role Deleted",
            fields=fields,
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if not self.should_log_guild(after.guild):
            return

        changes = get_changed_attrs(
            before,
            after,
            [
                "name",
                "color",
                "hoist",
                "mentionable",
                "permissions",
            ],
        )

        if not changes:
            return

        fields = [
            ("Role", format_role(after), False),
            ("Changes", "\n\n".join(changes), False),
        ]

        fields.extend(await self.audit_fields(after.guild, discord.AuditLogAction.role_update, after.id))

        await self.send_log(
            category="role_management",
            guild=after.guild,
            title="Role Updated",
            fields=fields,
            color=discord.Color.orange(),
        )

    # ------------------------------------------------------------
    # Channel / category changes
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_channel_create(self, channel: discord.abc.GuildChannel):
        if not self.should_log_guild(channel.guild):
            return

        title = "Category Created" if isinstance(channel, discord.CategoryChannel) else "Channel Created"

        fields = [
            ("Channel", format_channel(channel), False),
            ("Type", channel_type_name(channel), True),
        ]

        fields.extend(await self.audit_fields(channel.guild, discord.AuditLogAction.channel_create, channel.id))

        await self.send_log(
            category="server_management",
            guild=channel.guild,
            title=title,
            fields=fields,
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_guild_channel_delete(self, channel: discord.abc.GuildChannel):
        if not self.should_log_guild(channel.guild):
            return

        title = "Category Deleted" if isinstance(channel, discord.CategoryChannel) else "Channel Deleted"

        fields = [
            ("Channel", f"`{channel.name}` (`{channel.id}`)", False),
            ("Type", channel_type_name(channel), True),
        ]

        fields.extend(await self.audit_fields(channel.guild, discord.AuditLogAction.channel_delete, channel.id))

        await self.send_log(
            category="server_management",
            guild=channel.guild,
            title=title,
            fields=fields,
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_guild_channel_update(self, before: discord.abc.GuildChannel, after: discord.abc.GuildChannel):
        if not self.should_log_guild(after.guild):
            return

        changes = get_changed_attrs(
            before,
            after,
            [
                "name",
                "topic",
                "nsfw",
                "slowmode_delay",
                "bitrate",
                "user_limit",
                "rtc_region",
                "video_quality_mode",
            ],
        )

        before_category = getattr(before, "category", None)
        after_category = getattr(after, "category", None)

        if before_category != after_category:
            changes.append(
                f"**category**\nBefore: `{before_category}`\nAfter: `{after_category}`"
            )

        if not changes:
            return

        title = "Category Updated" if isinstance(after, discord.CategoryChannel) else "Channel Updated"

        fields = [
            ("Channel", format_channel(after), False),
            ("Type", channel_type_name(after), True),
            ("Changes", "\n\n".join(changes), False),
        ]

        fields.extend(await self.audit_fields(after.guild, discord.AuditLogAction.channel_update, after.id))

        await self.send_log(
            category="server_management",
            guild=after.guild,
            title=title,
            fields=fields,
            color=discord.Color.orange(),
        )

    # ------------------------------------------------------------
    # Thread changes
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_thread_create(self, thread: discord.Thread):
        if not self.should_log_guild(thread.guild):
            return

        fields = [
            ("Thread", format_channel(thread), False),
            ("Parent", format_channel(thread.parent), False),
            ("Owner ID", f"`{thread.owner_id}`", True),
        ]

        fields.extend(await self.audit_fields(thread.guild, discord.AuditLogAction.thread_create, thread.id))

        await self.send_log(
            category="server_management",
            guild=thread.guild,
            title="Thread Created",
            fields=fields,
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_thread_delete(self, thread: discord.Thread):
        if not self.should_log_guild(thread.guild):
            return

        fields = [
            ("Thread", f"`{thread.name}` (`{thread.id}`)", False),
            ("Parent", format_channel(thread.parent), False),
        ]

        fields.extend(await self.audit_fields(thread.guild, discord.AuditLogAction.thread_delete, thread.id))

        await self.send_log(
            category="server_management",
            guild=thread.guild,
            title="Thread Deleted",
            fields=fields,
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_thread_update(self, before: discord.Thread, after: discord.Thread):
        if not self.should_log_guild(after.guild):
            return

        changes = get_changed_attrs(
            before,
            after,
            [
                "name",
                "archived",
                "locked",
                "slowmode_delay",
                "auto_archive_duration",
                "invitable",
            ],
        )

        if not changes:
            return

        fields = [
            ("Thread", format_channel(after), False),
            ("Parent", format_channel(after.parent), False),
            ("Changes", "\n\n".join(changes), False),
        ]

        fields.extend(await self.audit_fields(after.guild, discord.AuditLogAction.thread_update, after.id))

        await self.send_log(
            category="server_management",
            guild=after.guild,
            title="Thread Updated",
            fields=fields,
            color=discord.Color.orange(),
        )

    # ------------------------------------------------------------
    # Server management
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_update(self, before: discord.Guild, after: discord.Guild):
        if not self.should_log_guild(after):
            return

        changes = get_changed_attrs(
            before,
            after,
            [
                "name",
                "description",
                "verification_level",
                "explicit_content_filter",
                "default_notifications",
                "afk_timeout",
                "premium_tier",
                "preferred_locale",
                "vanity_url_code",
            ],
        )

        if not changes:
            return

        fields = [
            ("Changes", "\n\n".join(changes), False),
        ]

        fields.extend(await self.audit_fields(after, discord.AuditLogAction.guild_update, after.id))

        await self.send_log(
            category="server_management",
            guild=after,
            title="Server Settings Updated",
            fields=fields,
            color=discord.Color.orange(),
        )

    @commands.Cog.listener()
    async def on_webhooks_update(self, channel):
        guild = getattr(channel, "guild", None)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="server_management",
            guild=guild,
            title="Webhook Update",
            fields=[
                ("Channel", format_channel(channel), False),
            ],
            color=discord.Color.orange(),
        )

    @commands.Cog.listener()
    async def on_guild_integrations_update(self, guild: discord.Guild):
        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="server_management",
            guild=guild,
            title="Server Integrations Updated",
            description="A server integration was created, updated, or removed.",
            color=discord.Color.orange(),
        )

    # ------------------------------------------------------------
    # Emojis / stickers / soundboard
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_emojis_update(self, guild: discord.Guild, before: list[discord.Emoji], after: list[discord.Emoji]):
        if not self.should_log_guild(guild):
            return

        before_ids = {emoji.id: emoji for emoji in before}
        after_ids = {emoji.id: emoji for emoji in after}

        added = [after_ids[item] for item in after_ids if item not in before_ids]
        removed = [before_ids[item] for item in before_ids if item not in after_ids]
        updated = [
            after_ids[item]
            for item in after_ids
            if item in before_ids and after_ids[item].name != before_ids[item].name
        ]

        if not added and not removed and not updated:
            return

        fields = []

        if added:
            fields.append(("Added", "\n".join(f"{emoji} `{emoji.name}` (`{emoji.id}`)" for emoji in added), False))

        if removed:
            fields.append(("Removed", "\n".join(f"`{emoji.name}` (`{emoji.id}`)" for emoji in removed), False))

        if updated:
            lines = []
            for emoji in updated:
                lines.append(f"`{before_ids[emoji.id].name}` → `{emoji.name}` (`{emoji.id}`)")
            fields.append(("Updated", "\n".join(lines), False))

        await self.send_log(
            category="server_management",
            guild=guild,
            title="Emoji Changes",
            fields=fields,
            color=discord.Color.orange(),
        )

    @commands.Cog.listener()
    async def on_guild_stickers_update(self, guild: discord.Guild, before, after):
        if not self.should_log_guild(guild):
            return

        before_ids = {sticker.id: sticker for sticker in before}
        after_ids = {sticker.id: sticker for sticker in after}

        added = [after_ids[item] for item in after_ids if item not in before_ids]
        removed = [before_ids[item] for item in before_ids if item not in after_ids]
        updated = [
            after_ids[item]
            for item in after_ids
            if item in before_ids and after_ids[item].name != before_ids[item].name
        ]

        if not added and not removed and not updated:
            return

        fields = []

        if added:
            fields.append(("Added", "\n".join(f"`{sticker.name}` (`{sticker.id}`)" for sticker in added), False))

        if removed:
            fields.append(("Removed", "\n".join(f"`{sticker.name}` (`{sticker.id}`)" for sticker in removed), False))

        if updated:
            lines = []
            for sticker in updated:
                lines.append(f"`{before_ids[sticker.id].name}` → `{sticker.name}` (`{sticker.id}`)")
            fields.append(("Updated", "\n".join(lines), False))

        await self.send_log(
            category="server_management",
            guild=guild,
            title="Sticker Changes",
            fields=fields,
            color=discord.Color.orange(),
        )

    @commands.Cog.listener("on_soundboard_sound_create")
    async def soundboard_sound_create(self, sound):
        guild = getattr(sound, "guild", None)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="server_management",
            guild=guild,
            title="Soundboard Sound Created",
            fields=[
                ("Sound", f"`{getattr(sound, 'name', 'Unknown')}` (`{getattr(sound, 'id', 'Unknown')}`)", False),
            ],
            color=discord.Color.green(),
        )

    @commands.Cog.listener("on_soundboard_sound_update")
    async def soundboard_sound_update(self, before, after):
        guild = getattr(after, "guild", None)

        if not self.should_log_guild(guild):
            return

        changes = get_changed_attrs(before, after, ["name", "volume", "emoji"])

        await self.send_log(
            category="server_management",
            guild=guild,
            title="Soundboard Sound Updated",
            fields=[
                ("Sound", f"`{getattr(after, 'name', 'Unknown')}` (`{getattr(after, 'id', 'Unknown')}`)", False),
                ("Changes", "\n\n".join(changes) if changes else "Unknown changes", False),
            ],
            color=discord.Color.orange(),
        )

    @commands.Cog.listener("on_soundboard_sound_delete")
    async def soundboard_sound_delete(self, sound):
        guild = getattr(sound, "guild", None)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="server_management",
            guild=guild,
            title="Soundboard Sound Deleted",
            fields=[
                ("Sound", f"`{getattr(sound, 'name', 'Unknown')}` (`{getattr(sound, 'id', 'Unknown')}`)", False),
            ],
            color=discord.Color.red(),
        )

    # ------------------------------------------------------------
    # Invites
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_invite_create(self, invite: discord.Invite):
        guild = invite.guild

        if not isinstance(guild, discord.Guild):
            return

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="invite",
            guild=guild,
            title="Invite Created",
            fields=[
                ("Code", f"`{invite.code}`", True),
                ("URL", invite.url, False),
                ("Channel", format_channel(invite.channel), False),
                ("Created By", format_user(invite.inviter), False),
                ("Max Uses", str(invite.max_uses or "Unlimited"), True),
                ("Temporary", str(invite.temporary), True),
                ("Expires At", format_dt(invite.expires_at), False),
            ],
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_invite_delete(self, invite: discord.Invite):
        guild = invite.guild

        if not isinstance(guild, discord.Guild):
            return

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="invite",
            guild=guild,
            title="Invite Deleted",
            fields=[
                ("Code", f"`{invite.code}`", True),
                ("Channel", format_channel(invite.channel), False),
            ],
            color=discord.Color.red(),
        )

    # ------------------------------------------------------------
    # Reactions
    # ------------------------------------------------------------

    async def guild_from_payload(self, payload) -> Optional[discord.Guild]:
        guild_id = getattr(payload, "guild_id", None)

        if guild_id is None:
            return None

        return self.bot.get_guild(guild_id)

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent):
        guild = await self.guild_from_payload(payload)

        if not self.should_log_guild(guild):
            return

        if self.bot.user and payload.user_id == self.bot.user.id:
            return

        await self.send_log(
            category="reaction",
            guild=guild,
            title="Reaction Added",
            fields=[
                ("User ID", f"`{payload.user_id}`", True),
                ("Emoji", str(payload.emoji), True),
                ("Channel ID", f"`{payload.channel_id}`", True),
                ("Message ID", f"`{payload.message_id}`", True),
                ("Message Link", message_link(payload.guild_id, payload.channel_id, payload.message_id), False),
            ],
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent):
        guild = await self.guild_from_payload(payload)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="reaction",
            guild=guild,
            title="Reaction Removed",
            fields=[
                ("User ID", f"`{payload.user_id}`", True),
                ("Emoji", str(payload.emoji), True),
                ("Channel ID", f"`{payload.channel_id}`", True),
                ("Message ID", f"`{payload.message_id}`", True),
                ("Message Link", message_link(payload.guild_id, payload.channel_id, payload.message_id), False),
            ],
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_clear(self, payload: discord.RawReactionClearEvent):
        guild = await self.guild_from_payload(payload)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="reaction",
            guild=guild,
            title="All Reactions Cleared",
            fields=[
                ("Channel ID", f"`{payload.channel_id}`", True),
                ("Message ID", f"`{payload.message_id}`", True),
                ("Message Link", message_link(payload.guild_id, payload.channel_id, payload.message_id), False),
            ],
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_raw_reaction_clear_emoji(self, payload: discord.RawReactionClearEmojiEvent):
        guild = await self.guild_from_payload(payload)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="reaction",
            guild=guild,
            title="Reaction Emoji Cleared",
            fields=[
                ("Emoji", str(payload.emoji), True),
                ("Channel ID", f"`{payload.channel_id}`", True),
                ("Message ID", f"`{payload.message_id}`", True),
                ("Message Link", message_link(payload.guild_id, payload.channel_id, payload.message_id), False),
            ],
            color=discord.Color.red(),
        )

    # ------------------------------------------------------------
    # Voice channel / stage actions
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ):
        if not self.should_log_guild(member.guild):
            return

        if before.channel is None and after.channel is not None:
            await self.send_log(
                category="vc",
                guild=member.guild,
                title="Joined Voice Channel",
                fields=[
                    ("User", format_member(member), False),
                    ("Channel", format_channel(after.channel), False),
                ],
                color=discord.Color.green(),
            )
            return

        if before.channel is not None and after.channel is None:
            await self.send_log(
                category="vc",
                guild=member.guild,
                title="Left Voice Channel",
                fields=[
                    ("User", format_member(member), False),
                    ("Channel", format_channel(before.channel), False),
                ],
                color=discord.Color.red(),
            )
            return

        if before.channel != after.channel:
            await self.send_log(
                category="vc",
                guild=member.guild,
                title="Moved Voice Channel",
                fields=[
                    ("User", format_member(member), False),
                    ("Before", format_channel(before.channel), False),
                    ("After", format_channel(after.channel), False),
                ],
                color=discord.Color.orange(),
            )
            return

        changes = []

        voice_attrs = [
            "self_mute",
            "self_deaf",
            "self_stream",
            "self_video",
            "mute",
            "deaf",
            "suppress",
            "requested_to_speak_at",
        ]

        for attr in voice_attrs:
            before_value = getattr(before, attr, None)
            after_value = getattr(after, attr, None)

            if before_value != after_value:
                changes.append(f"**{attr}**: `{before_value}` → `{after_value}`")

        if changes:
            await self.send_log(
                category="vc",
                guild=member.guild,
                title="Voice Status Changed",
                fields=[
                    ("User", format_member(member), False),
                    ("Channel", format_channel(after.channel), False),
                    ("Changes", "\n".join(changes), False),
                ],
                color=discord.Color.blurple(),
            )

    @commands.Cog.listener()
    async def on_stage_instance_create(self, stage_instance):
        guild = getattr(stage_instance, "guild", None)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="vc",
            guild=guild,
            title="Stage Started",
            fields=[
                ("Topic", getattr(stage_instance, "topic", "Unknown"), False),
                ("Channel", format_channel(getattr(stage_instance, "channel", None)), False),
            ],
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_stage_instance_update(self, before, after):
        guild = getattr(after, "guild", None)

        if not self.should_log_guild(guild):
            return

        changes = get_changed_attrs(before, after, ["topic", "privacy_level"])

        await self.send_log(
            category="vc",
            guild=guild,
            title="Stage Updated",
            fields=[
                ("Channel", format_channel(getattr(after, "channel", None)), False),
                ("Changes", "\n\n".join(changes) if changes else "Unknown changes", False),
            ],
            color=discord.Color.orange(),
        )

    @commands.Cog.listener()
    async def on_stage_instance_delete(self, stage_instance):
        guild = getattr(stage_instance, "guild", None)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="vc",
            guild=guild,
            title="Stage Ended",
            fields=[
                ("Topic", getattr(stage_instance, "topic", "Unknown"), False),
                ("Channel", format_channel(getattr(stage_instance, "channel", None)), False),
            ],
            color=discord.Color.red(),
        )

    # ------------------------------------------------------------
    # Scheduled events
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_scheduled_event_create(self, event):
        guild = getattr(event, "guild", None)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="server_management",
            guild=guild,
            title="Scheduled Event Created",
            fields=[
                ("Event", f"`{getattr(event, 'name', 'Unknown')}` (`{getattr(event, 'id', 'Unknown')}`)", False),
                ("Start", format_dt(getattr(event, "start_time", None)), False),
                ("End", format_dt(getattr(event, "end_time", None)), False),
            ],
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_scheduled_event_update(self, before, after):
        guild = getattr(after, "guild", None)

        if not self.should_log_guild(guild):
            return

        changes = get_changed_attrs(
            before,
            after,
            [
                "name",
                "description",
                "start_time",
                "end_time",
                "status",
                "location",
            ],
        )

        await self.send_log(
            category="server_management",
            guild=guild,
            title="Scheduled Event Updated",
            fields=[
                ("Event", f"`{getattr(after, 'name', 'Unknown')}` (`{getattr(after, 'id', 'Unknown')}`)", False),
                ("Changes", "\n\n".join(changes) if changes else "Unknown changes", False),
            ],
            color=discord.Color.orange(),
        )

    @commands.Cog.listener()
    async def on_scheduled_event_delete(self, event):
        guild = getattr(event, "guild", None)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="server_management",
            guild=guild,
            title="Scheduled Event Deleted",
            fields=[
                ("Event", f"`{getattr(event, 'name', 'Unknown')}` (`{getattr(event, 'id', 'Unknown')}`)", False),
            ],
            color=discord.Color.red(),
        )

    # ------------------------------------------------------------
    # Other / fallback-ish events
    # ------------------------------------------------------------

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.send_log(
            category="other",
            guild=guild,
            title="Bot Added To Server",
            fields=[
                ("Server", format_guild(guild), False),
                ("Members", str(guild.member_count), True),
            ],
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        await self.send_log(
            category="other",
            guild=guild,
            title="Bot Removed From Server",
            fields=[
                ("Server", format_guild(guild), False),
            ],
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_automod_rule_create(self, rule):
        guild = getattr(rule, "guild", None)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="server_management",
            guild=guild,
            title="AutoMod Rule Created",
            fields=[
                ("Rule", f"`{getattr(rule, 'name', 'Unknown')}` (`{getattr(rule, 'id', 'Unknown')}`)", False),
            ],
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_automod_rule_update(self, rule):
        guild = getattr(rule, "guild", None)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="server_management",
            guild=guild,
            title="AutoMod Rule Updated",
            fields=[
                ("Rule", f"`{getattr(rule, 'name', 'Unknown')}` (`{getattr(rule, 'id', 'Unknown')}`)", False),
            ],
            color=discord.Color.orange(),
        )

    @commands.Cog.listener()
    async def on_automod_rule_delete(self, rule):
        guild = getattr(rule, "guild", None)

        if not self.should_log_guild(guild):
            return

        await self.send_log(
            category="server_management",
            guild=guild,
            title="AutoMod Rule Deleted",
            fields=[
                ("Rule", f"`{getattr(rule, 'name', 'Unknown')}` (`{getattr(rule, 'id', 'Unknown')}`)", False),
            ],
            color=discord.Color.red(),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(Logging(bot))
