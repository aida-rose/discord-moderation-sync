from typing import Optional

import discord
from discord.ext import commands

import config


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
    async def on_member_update(self, before: discord.Member, after: discord.Member):
        if self.bot.user is None:
            return

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
