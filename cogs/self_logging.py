import asyncio
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

import config
from protected_actions import get_active_protected_action, parse_datetime


REQUIRED_AUDIT_PERMISSIONS = {
    "attach_files": "Attach Files",
    "ban_members": "Ban Members",
    "bypass_slowmode": "Bypass Slowmode",
    "embed_links": "Embed Links",
    "kick_members": "Kick Members",
    "manage_roles": "Manage Roles",
    "manage_threads": "Manage Threads",
    "moderate_members": "Moderate Members",
    "read_message_history": "Read Message History",
    "send_messages": "Send Messages",
    "send_messages_in_threads": "Send Messages In Threads",
    "use_application_commands": "Use Slash Commands",
    "view_audit_log": "View Audit Logs",
    "view_channel": "View Channels",
}


def format_guild(guild: Optional[discord.Guild]) -> str:
    if guild is None:
        return "Unknown server"

    return f"{guild.name} (`{guild.id}`)"


def format_role(role: Optional[discord.Role]) -> str:
    if role is None:
        return "None"

    return f"{role.mention} `{role.name}` (`{role.id}`)"


def format_permissions(perms: discord.Permissions) -> str:
    enabled = [
        name.replace("_", " ").title()
        for name, value in perms
        if value
    ]

    if not enabled:
        return "No permissions"

    text = ", ".join(enabled)

    if len(text) > 1000:
        text = text[:997] + "..."

    return text


def diff_permissions(before: discord.Permissions, after: discord.Permissions) -> tuple[list[str], list[str]]:
    added = []
    removed = []

    before_dict = dict(before)
    after_dict = dict(after)

    for name, after_value in after_dict.items():
        before_value = before_dict.get(name, False)

        if before_value == after_value:
            continue

        pretty_name = name.replace("_", " ").title()

        if after_value:
            added.append(pretty_name)
        else:
            removed.append(pretty_name)

    return added, removed


def role_id_set(roles: list[discord.Role]) -> set[int]:
    return {role.id for role in roles}


def role_list(roles: list[discord.Role]) -> str:
    if not roles:
        return "None"

    text = "\n".join(format_role(role) for role in roles)

    if len(text) > 1000:
        text = text[:997] + "..."

    return text


class SelfLogging(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.permission_audit.start()

    def cog_unload(self):
        self.permission_audit.cancel()

    async def owner_only_interaction(self, interaction: discord.Interaction) -> bool:
        if interaction.user is not None and config.is_bot_owner_id(interaction.user.id):
            return True

        await interaction.response.send_message(
            "Only the bot owner can use this command.",
            ephemeral=True,
        )
        return False

    async def get_self_log_thread(self):
        thread_id = config.SELF_LOG_THREAD_ID or config.LOG_OTHER_THREAD_ID

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

    async def send_self_log(
        self,
        *,
        guild: Optional[discord.Guild],
        title: str,
        description: Optional[str] = None,
        fields: Optional[list[tuple[str, str, bool]]] = None,
        color: discord.Color = discord.Color.blurple(),
    ):
        thread = await self.get_self_log_thread()

        if thread is None:
            return

        embed = discord.Embed(
            title=title,
            description=description,
            color=color,
            timestamp=discord.utils.utcnow(),
        )

        embed.add_field(
            name="Server",
            value=format_guild(guild),
            inline=False,
        )

        if fields:
            for name, value, inline in fields:
                embed.add_field(
                    name=name[:256],
                    value=value[:1024] if value else "None",
                    inline=inline,
                )

        embed.set_footer(text="Self logging")

        try:
            await thread.send(
                embed=embed,
                allowed_mentions=discord.AllowedMentions.none(),
            )

        except discord.HTTPException:
            pass

    def missing_audit_permissions(self, member: discord.Member) -> list[str]:
        permissions = member.guild_permissions

        return [
            label
            for permission_name, label in REQUIRED_AUDIT_PERMISSIONS.items()
            if not getattr(permissions, permission_name, False)
        ]

    def channel_access_counts(self, guild: discord.Guild, member: discord.Member) -> tuple[int, int, int]:
        existing_channels = len(guild.channels)
        view_access = 0
        read_history = 0

        for channel in guild.channels:
            permissions = channel.permissions_for(member)

            if getattr(permissions, "view_channel", False):
                view_access += 1

            if getattr(permissions, "read_message_history", False):
                read_history += 1

        return view_access, read_history, existing_channels

    def can_view_all_threads(self, guild: discord.Guild, member: discord.Member) -> bool:
        for thread in guild.threads:
            permissions = thread.permissions_for(member)

            if not getattr(permissions, "view_channel", False):
                return False

        return True

    def hierarchy_counts(self, guild: discord.Guild, member: discord.Member) -> tuple[int, int]:
        bot_top_role = member.top_role
        roles_above = [
            role
            for role in guild.roles
            if not role.is_default() and role.position > bot_top_role.position
        ]

        users_above = [
            guild_member
            for guild_member in guild.members
            if (
                (
                    guild_member.top_role.position > bot_top_role.position
                    or guild_member.id == guild.owner_id
                )
                and guild_member.id != member.id
            )
        ]

        return len(users_above), len(roles_above)

    async def audit_guild_permissions(self, guild: discord.Guild) -> None:
        member = guild.me

        if member is None and self.bot.user is not None:
            member = guild.get_member(self.bot.user.id)

        if member is None:
            await self.send_self_log(
                guild=guild,
                title="Automated Permission Audit",
                description=(
                    f"Server: {guild.name}\n"
                    "Audit failed: bot member is not cached for this server."
                ),
                color=discord.Color.red(),
            )
            return

        view_access, read_history, existing_channels = self.channel_access_counts(guild, member)
        users_above, roles_above = self.hierarchy_counts(guild, member)
        view_all_threads = self.can_view_all_threads(guild, member)
        missing_permissions = self.missing_audit_permissions(member)

        description = (
            f"Server: {guild.name}\n"
            f"Users Above: {users_above}\n"
            f"Roles above: {roles_above}\n"
            f"View Access: {view_access}\n"
            f"Read History: {read_history}\n"
            f"Existing channels: {existing_channels}\n"
            f"View all threads?: {view_all_threads}"
        )

        fields: list[tuple[str, str, bool]] = [
            (
                "Missing Permissions",
                ", ".join(missing_permissions) if missing_permissions else "None",
                False,
            )
        ]

        await self.send_self_log(
            guild=guild,
            title="Automated Permission Audit",
            description=description,
            fields=fields,
            color=discord.Color.green() if not missing_permissions and view_all_threads else discord.Color.orange(),
        )

    async def run_permission_audit(self) -> int:
        audited = 0

        for guild in list(self.bot.guilds):
            await self.audit_guild_permissions(guild)
            audited += 1

        return audited

    @tasks.loop(hours=24)
    async def permission_audit(self):
        await self.run_permission_audit()

    @permission_audit.before_loop
    async def before_permission_audit(self):
        await self.bot.wait_until_ready()

    @app_commands.command(
        name="permission_audit",
        description="Run the bot permission audit now.",
    )
    async def permission_audit_command(self, interaction: discord.Interaction):
        if not await self.owner_only_interaction(interaction):
            return

        await interaction.response.defer(ephemeral=True)

        audited = await self.run_permission_audit()

        await interaction.followup.send(
            f"Permission audit complete. Audited `{audited}` server(s).",
            ephemeral=True,
        )

    async def find_audit_entry(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: Optional[int] = None,
    ) -> Optional[discord.AuditLogEntry]:
        try:
            async for entry in guild.audit_logs(limit=8, action=action):
                age = abs((discord.utils.utcnow() - entry.created_at).total_seconds())

                if age > 20:
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

    async def audit_actor_field(
        self,
        guild: discord.Guild,
        action: discord.AuditLogAction,
        target_id: Optional[int] = None,
    ) -> list[tuple[str, str, bool]]:
        entry = await self.find_audit_entry(guild, action, target_id)

        if entry is None:
            return [
                (
                    "Actor",
                    "Unknown or missing View Audit Log permission.",
                    False,
                )
            ]

        actor = entry.user
        actor_text = f"{actor} (`{actor.id}`)" if actor else "Unknown"

        fields = [
            ("Actor", actor_text, False),
        ]

        if entry.reason:
            fields.append(("Audit Log Reason", entry.reason, False))

        return fields

    def audit_entry_is_bot_action(self, entry: Optional[discord.AuditLogEntry]) -> bool:
        if entry is None or self.bot.user is None:
            return False

        actor = entry.user
        return actor is not None and actor.id == self.bot.user.id

    def is_timed_out(self, member: discord.Member) -> bool:
        until = member.communication_disabled_until

        if until is None:
            return False

        if until.tzinfo is None:
            until = until.replace(tzinfo=timezone.utc)

        return until > datetime.now(timezone.utc)

    async def log_reapplied_protected_action(
        self,
        *,
        guild: discord.Guild,
        undone_by: Optional[discord.abc.User],
        action_undone: str,
        target_user: discord.abc.User,
        reapply_result: str,
        color: discord.Color = discord.Color.orange(),
    ) -> None:
        actor_text = format_user(undone_by) if undone_by is not None else "Unknown or missing View Audit Log permission."
        target_text = format_user(target_user)

        await self.send_self_log(
            guild=guild,
            title="Protected Moderation Action Reapplied",
            fields=[
                ("Undone By", actor_text, False),
                ("Action Undone", action_undone, False),
                ("Target User", target_text, False),
                ("Reapply Result", reapply_result, False),
            ],
            color=color,
        )

    async def on_protected_ban_removed(
        self,
        guild: discord.Guild,
        user: discord.User,
    ) -> None:
        protected_action = get_active_protected_action(
            action_type="ban",
            guild_id=guild.id,
            user_id=user.id,
        )

        if protected_action is None:
            return

        await asyncio.sleep(1)
        audit_entry = await self.find_audit_entry(
            guild,
            discord.AuditLogAction.unban,
            user.id,
            within_seconds=60,
        )

        if self.audit_entry_is_bot_action(audit_entry):
            return

        actor = audit_entry.user if audit_entry is not None else None
        actor_text = format_user(actor) if actor is not None else "Unknown"
        reason = (
            f"[{config.CASE_TAG}] Reapplied protected ban after it was removed by "
            f"{actor_text}."
        )

        try:
            await guild.ban(
                user,
                reason=reason[:512],
                delete_message_seconds=0,
            )
            result = "Ban reapplied."
            color = discord.Color.orange()
        except discord.HTTPException as error:
            result = f"Failed to reapply ban: `{type(error).__name__}: {error}`"
            color = discord.Color.red()

        await self.log_reapplied_protected_action(
            guild=guild,
            undone_by=actor,
            action_undone="Unbanned protected banned member",
            target_user=user,
            reapply_result=result,
            color=color,
        )

    async def on_protected_timeout_removed(
        self,
        before: discord.Member,
        after: discord.Member,
    ) -> None:
        if not self.is_timed_out(before) or self.is_timed_out(after):
            return

        if self.bot.user is not None and after.id == self.bot.user.id:
            return

        protected_action = get_active_protected_action(
            action_type="timeout",
            guild_id=after.guild.id,
            user_id=after.id,
        )

        if protected_action is None:
            return

        expires_at = parse_datetime(protected_action["expires_at"])

        if expires_at is None:
            return

        remaining = expires_at - datetime.now(timezone.utc)

        if remaining.total_seconds() <= 0:
            return

        await asyncio.sleep(1)
        audit_entry = await self.find_audit_entry(
            after.guild,
            discord.AuditLogAction.member_update,
            after.id,
            within_seconds=60,
        )

        if self.audit_entry_is_bot_action(audit_entry):
            return

        actor = audit_entry.user if audit_entry is not None else None
        actor_text = format_user(actor) if actor is not None else "Unknown"
        reason = (
            f"[{config.CASE_TAG}] Reapplied protected timeout after it was removed by "
            f"{actor_text}."
        )

        try:
            await after.timeout(remaining, reason=reason[:512])
            result = "Timeout reapplied."
            color = discord.Color.orange()
        except discord.HTTPException as error:
            result = f"Failed to reapply timeout: `{type(error).__name__}: {error}`"
            color = discord.Color.red()

        await self.log_reapplied_protected_action(
            guild=after.guild,
            undone_by=actor,
            action_undone="Removed protected timeout",
            target_user=after,
            reapply_result=result,
            color=color,
        )

    @commands.Cog.listener()
    async def on_guild_join(self, guild: discord.Guild):
        await self.send_self_log(
            guild=guild,
            title="Bot Added To Server",
            fields=[
                ("Server Name", guild.name, True),
                ("Server ID", f"`{guild.id}`", True),
                ("Member Count", str(guild.member_count), True),
                ("Owner ID", f"`{guild.owner_id}`", True),
            ],
            color=discord.Color.green(),
        )

    @commands.Cog.listener()
    async def on_guild_remove(self, guild: discord.Guild):
        await self.send_self_log(
            guild=guild,
            title="Bot Removed From Server",
            fields=[
                ("Server Name", guild.name, True),
                ("Server ID", f"`{guild.id}`", True),
                ("Member Count", str(guild.member_count), True),
                ("Owner ID", f"`{guild.owner_id}`", True),
            ],
            color=discord.Color.red(),
        )

    @commands.Cog.listener()
    async def on_member_unban(self, guild: discord.Guild, user: discord.User):
        await self.on_protected_ban_removed(guild, user)

    @commands.Cog.listener()
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if self.bot.user is None:
            return

        await self.on_protected_timeout_removed(before, after)

        if after.id != self.bot.user.id:
            return

        before_roles = role_id_set(before.roles)
        after_roles = role_id_set(after.roles)

        roles_added = [
            role for role in after.roles
            if role.id not in before_roles and not role.is_default()
        ]

        roles_removed = [
            role for role in before.roles
            if role.id not in after_roles and not role.is_default()
        ]

        before_top = before.top_role
        after_top = after.top_role

        before_permissions = before.guild_permissions
        after_permissions = after.guild_permissions
        perms_added, perms_removed = diff_permissions(before_permissions, after_permissions)

        fields: list[tuple[str, str, bool]] = []

        if roles_added:
            fields.append(("Roles Added To Bot", role_list(roles_added), False))

        if roles_removed:
            fields.append(("Roles Removed From Bot", role_list(roles_removed), False))

        if before_top.id != after_top.id or before_top.position != after_top.position:
            fields.append(("Old Top Role", format_role(before_top), False))
            fields.append(("New Top Role", format_role(after_top), False))

        if perms_added:
            fields.append(("Permissions Added", ", ".join(perms_added)[:1024], False))

        if perms_removed:
            fields.append(("Permissions Removed", ", ".join(perms_removed)[:1024], False))

        if not fields:
            return

        fields.extend(
            await self.audit_actor_field(
                after.guild,
                discord.AuditLogAction.member_role_update,
                after.id,
            )
        )

        await self.send_self_log(
            guild=after.guild,
            title="Bot Roles / Permissions / Hierarchy Changed",
            fields=fields,
            color=discord.Color.orange(),
        )

    @commands.Cog.listener()
    async def on_guild_role_update(self, before: discord.Role, after: discord.Role):
        if self.bot.user is None:
            return

        bot_member = after.guild.me

        if bot_member is None:
            return

        bot_role_ids = {role.id for role in bot_member.roles}

        if after.id not in bot_role_ids:
            return

        fields: list[tuple[str, str, bool]] = [
            ("Role", format_role(after), False),
        ]

        if before.name != after.name:
            fields.append(("Name Changed", f"`{before.name}` → `{after.name}`", False))

        if before.position != after.position:
            fields.append(
                "Hierarchy Position Changed",
                f"`{before.position}` → `{after.position}`",
                True,
            )

        if before.color != after.color:
            fields.append(("Color Changed", f"`{before.color}` → `{after.color}`", True))

        if before.hoist != after.hoist:
            fields.append(("Displayed Separately Changed", f"`{before.hoist}` → `{after.hoist}`", True))

        if before.mentionable != after.mentionable:
            fields.append(("Mentionable Changed", f"`{before.mentionable}` → `{after.mentionable}`", True))

        perms_added, perms_removed = diff_permissions(before.permissions, after.permissions)

        if perms_added:
            fields.append(("Role Permissions Added", ", ".join(perms_added)[:1024], False))

        if perms_removed:
            fields.append(("Role Permissions Removed", ", ".join(perms_removed)[:1024], False))

        if len(fields) == 1:
            return

        fields.extend(
            await self.audit_actor_field(
                after.guild,
                discord.AuditLogAction.role_update,
                after.id,
            )
        )

        await self.send_self_log(
            guild=after.guild,
            title="Bot Role Updated",
            fields=fields,
            color=discord.Color.orange(),
        )

    @commands.Cog.listener()
    async def on_guild_role_delete(self, role: discord.Role):
        if self.bot.user is None:
            return

        bot_member = role.guild.me

        if bot_member is None:
            return

        if role.position >= bot_member.top_role.position:
            return

        fields = [
            ("Deleted Role", f"`{role.name}` (`{role.id}`)", False),
            ("Bot Top Role", format_role(bot_member.top_role), False),
        ]

        fields.extend(
            await self.audit_actor_field(
                role.guild,
                discord.AuditLogAction.role_delete,
                role.id,
            )
        )

        await self.send_self_log(
            guild=role.guild,
            title="Role Deleted Below Bot Hierarchy",
            description=(
                "A role below the bot's top role was deleted. "
                "If this role was assigned to the bot, the bot's permissions may have changed."
            ),
            fields=fields,
            color=discord.Color.red(),
        )


async def setup(bot: commands.Bot):
    await bot.add_cog(SelfLogging(bot))
