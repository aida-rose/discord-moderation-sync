import asyncio
import io
import re
from datetime import datetime, timezone
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

import config
from common import is_staff
from storage import moderation_db


TICKET_CATEGORIES = {
    "report": {
        "label": "Report",
        "description": "Report a player, message, or server issue.",
        "ping_setting": "TICKET_REPORT_PING_ROLE_ID",
    },
    "admin": {
        "label": "Admin",
        "description": "Contact the admin team.",
        "ping_setting": "TICKET_ADMIN_PING_ROLE_ID",
    },
    "dispute": {
        "label": "Dispute",
        "description": "Dispute a moderation action or decision.",
        "ping_setting": "TICKET_DISPUTE_PING_ROLE_ID",
    },
    "other": {
        "label": "Other",
        "description": "Anything that does not fit another category.",
        "ping_setting": "TICKET_OTHER_PING_ROLE_ID",
    },
}

TICKET_STATUS_OPEN = "open"
TICKET_STATUS_CLOSED = "closed"
TICKET_STATUS_ARCHIVED = "archived"
MEDIA_EXTENSIONS = {
    ".apng",
    ".avif",
    ".gif",
    ".jpeg",
    ".jpg",
    ".mov",
    ".mp4",
    ".png",
    ".webm",
    ".webp",
}


def init_db() -> None:
    with moderation_db() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS tickets (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                guild_id INTEGER NOT NULL,
                channel_id INTEGER NOT NULL UNIQUE,
                opener_id INTEGER NOT NULL,
                ticket_number INTEGER NOT NULL,
                category_key TEXT NOT NULL,
                category_label TEXT NOT NULL,
                open_category_id INTEGER,
                closed_category_id INTEGER,
                status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                closed_at TEXT,
                archived_at TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ticket_counters (
                guild_id INTEGER PRIMARY KEY,
                next_number INTEGER NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_tickets_guild_status
            ON tickets (guild_id, status)
            """
        )


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat()


def next_ticket_number(guild_id: int) -> int:
    with moderation_db() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            "SELECT next_number FROM ticket_counters WHERE guild_id = ?",
            (guild_id,),
        ).fetchone()

        if row is None:
            conn.execute(
                "INSERT INTO ticket_counters (guild_id, next_number) VALUES (?, ?)",
                (guild_id, 2),
            )
            return 1

        number = int(row["next_number"])
        conn.execute(
            "UPDATE ticket_counters SET next_number = ? WHERE guild_id = ?",
            (number + 1, guild_id),
        )
        return number


def ticket_for_channel(channel_id: int):
    with moderation_db() as conn:
        return conn.execute(
            "SELECT * FROM tickets WHERE channel_id = ?",
            (channel_id,),
        ).fetchone()


def create_ticket_record(
    *,
    guild_id: int,
    channel_id: int,
    opener_id: int,
    ticket_number: int,
    category_key: str,
    category_label: str,
    open_category_id: Optional[int],
) -> None:
    with moderation_db() as conn:
        conn.execute(
            """
            INSERT INTO tickets (
                guild_id,
                channel_id,
                opener_id,
                ticket_number,
                category_key,
                category_label,
                open_category_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                channel_id,
                opener_id,
                ticket_number,
                category_key,
                category_label,
                open_category_id,
            ),
        )


def update_ticket_status(
    channel_id: int,
    status: str,
    *,
    closed_category_id: Optional[int] = None,
) -> None:
    timestamp_column = {
        TICKET_STATUS_CLOSED: "closed_at",
        TICKET_STATUS_ARCHIVED: "archived_at",
    }.get(status)

    with moderation_db() as conn:
        if timestamp_column is None:
            conn.execute(
                """
                UPDATE tickets
                SET status = ?, closed_at = NULL, archived_at = NULL
                WHERE channel_id = ?
                """,
                (status, channel_id),
            )
            return

        conn.execute(
            f"""
            UPDATE tickets
            SET status = ?,
                closed_category_id = COALESCE(?, closed_category_id),
                {timestamp_column} = ?
            WHERE channel_id = ?
            """,
            (status, closed_category_id, utc_now_text(), channel_id),
        )


def slug_text(value: str, *, fallback: str) -> str:
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = value.strip("-")
    return value or fallback


def ticket_channel_name(category_label: str, user: discord.abc.User, number: int) -> str:
    category_slug = slug_text(category_label, fallback="ticket")
    user_slug = slug_text(getattr(user, "display_name", str(user)), fallback="user")
    return f"{category_slug}-{user_slug}-{number:04d}"[:100]


def category_ping_role_id(category_key: str) -> int:
    category = TICKET_CATEGORIES[category_key]
    return int(getattr(config, category["ping_setting"], 0) or 0)


def is_media_attachment(attachment: discord.Attachment) -> bool:
    content_type = (attachment.content_type or "").lower()

    if content_type.startswith(("image/", "video/")):
        return True

    filename = attachment.filename.lower()
    return any(filename.endswith(extension) for extension in MEDIA_EXTENSIONS)


class TicketPanelView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.add_item(TicketCategorySelect(cog))


class TicketCategorySelect(discord.ui.Select):
    def __init__(self, cog: "Tickets"):
        self.cog = cog
        options = [
            discord.SelectOption(
                label=category["label"],
                value=key,
                description=category["description"],
            )
            for key, category in TICKET_CATEGORIES.items()
        ]
        super().__init__(
            placeholder="Choose a ticket category",
            min_values=1,
            max_values=1,
            options=options,
            custom_id="tickets:category_select",
        )

    async def callback(self, interaction: discord.Interaction):
        await self.cog.open_ticket_from_panel(interaction, self.values[0])


class TicketManageView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Close Ticket",
        style=discord.ButtonStyle.danger,
        custom_id="tickets:close",
    )
    async def close_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.cog.close_ticket_from_interaction(interaction)


class TicketClosedView(discord.ui.View):
    def __init__(self, cog: "Tickets"):
        super().__init__(timeout=None)
        self.cog = cog

    @discord.ui.button(
        label="Archive Ticket",
        style=discord.ButtonStyle.secondary,
        custom_id="tickets:archive",
    )
    async def archive_ticket(
        self,
        interaction: discord.Interaction,
        button: discord.ui.Button,
    ):
        await self.cog.archive_ticket_from_interaction(interaction)


class Tickets(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    def is_ticket_manager(self, member: discord.Member) -> bool:
        if config.is_bot_owner_id(member.id):
            return True

        permissions = member.guild_permissions
        return permissions.administrator or permissions.manage_channels or is_staff(member)

    def is_ticket_admin(self, member: discord.Member) -> bool:
        if config.is_bot_owner_id(member.id):
            return True

        permissions = member.guild_permissions
        return permissions.administrator or permissions.manage_channels

    async def send_ephemeral(
        self,
        interaction: discord.Interaction,
        message: str,
    ) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)

    async def messageable_channel(self, channel_id: int):
        if not channel_id:
            return None

        channel = self.bot.get_channel(channel_id)

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(channel_id)
            except discord.HTTPException:
                return None

        if not hasattr(channel, "send"):
            return None

        return channel

    async def configured_closed_category(self) -> Optional[discord.CategoryChannel]:
        category_id = config.TICKET_CLOSED_CATEGORY_ID

        if not category_id:
            return None

        channel = self.bot.get_channel(category_id)

        if channel is None:
            try:
                channel = await self.bot.fetch_channel(category_id)
            except discord.HTTPException:
                return None

        if isinstance(channel, discord.CategoryChannel):
            return channel

        return None

    async def open_ticket_from_panel(
        self,
        interaction: discord.Interaction,
        category_key: str,
    ) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_ephemeral(interaction, "Tickets can only be opened inside a server.")
            return

        if category_key not in TICKET_CATEGORIES:
            await self.send_ephemeral(interaction, "That ticket category is not configured.")
            return

        source_channel = interaction.channel
        if not isinstance(source_channel, discord.TextChannel):
            await self.send_ephemeral(interaction, "Ticket panels must be used in a server text channel.")
            return

        open_category = source_channel.category
        if open_category is None:
            await self.send_ephemeral(
                interaction,
                "Put the ticket panel in a Discord category first. New tickets open in that same category.",
            )
            return

        await interaction.response.defer(ephemeral=True)

        category = TICKET_CATEGORIES[category_key]
        ticket_number = next_ticket_number(interaction.guild.id)
        channel_name = ticket_channel_name(category["label"], interaction.user, ticket_number)
        ping_role = interaction.guild.get_role(category_ping_role_id(category_key))
        overwrites = {
            interaction.guild.default_role: discord.PermissionOverwrite(view_channel=False),
            interaction.user: discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            ),
        }

        bot_member = interaction.guild.me
        if bot_member is not None:
            overwrites[bot_member] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                manage_channels=True,
                manage_messages=True,
                attach_files=True,
                embed_links=True,
            )

        if ping_role is not None:
            overwrites[ping_role] = discord.PermissionOverwrite(
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
            )

        try:
            ticket_channel = await interaction.guild.create_text_channel(
                name=channel_name,
                category=open_category,
                overwrites=overwrites,
                reason=f"Ticket opened by {interaction.user} ({interaction.user.id})",
            )
        except discord.Forbidden:
            await interaction.followup.send(
                "I do not have permission to create ticket channels in this category.",
                ephemeral=True,
            )
            return
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Discord could not create the ticket channel: `{exc}`",
                ephemeral=True,
            )
            return

        create_ticket_record(
            guild_id=interaction.guild.id,
            channel_id=ticket_channel.id,
            opener_id=interaction.user.id,
            ticket_number=ticket_number,
            category_key=category_key,
            category_label=category["label"],
            open_category_id=open_category.id,
        )

        mentions = [interaction.user.mention]
        if ping_role is not None:
            mentions.append(ping_role.mention)

        embed = discord.Embed(
            title=f"{category['label']} Ticket #{ticket_number}",
            description=(
                "Please ask your question or explain what you need help with now. "
                "You do not need to wait for staff to reach out first.\n"
                "Use the button below when this ticket is ready to close."
            ),
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Opened By", value=interaction.user.mention, inline=True)
        embed.add_field(name="Category", value=category["label"], inline=True)

        await ticket_channel.send(
            " ".join(mentions),
            embed=embed,
            view=TicketManageView(self),
            allowed_mentions=discord.AllowedMentions(users=True, roles=True, everyone=False),
        )

        await interaction.followup.send(
            f"Opened {ticket_channel.mention}.",
            ephemeral=True,
        )

    async def close_ticket_from_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_ephemeral(interaction, "Tickets can only be closed inside a server.")
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self.send_ephemeral(interaction, "Use this inside a ticket channel.")
            return

        ticket = ticket_for_channel(channel.id)
        if ticket is None:
            await self.send_ephemeral(interaction, "This channel is not a tracked ticket.")
            return

        if ticket["status"] == TICKET_STATUS_ARCHIVED:
            await self.send_ephemeral(interaction, "This ticket is already archived.")
            return

        if (
            interaction.user.id != int(ticket["opener_id"])
            and not self.is_ticket_manager(interaction.user)
        ):
            await self.send_ephemeral(interaction, "Only the ticket opener or staff can close this ticket.")
            return

        await interaction.response.defer(ephemeral=True)
        result = await self.close_ticket_channel(channel, interaction.user)
        await interaction.followup.send(result, ephemeral=True)

    async def close_ticket_channel(
        self,
        channel: discord.TextChannel,
        actor: discord.Member,
    ) -> str:
        ticket = ticket_for_channel(channel.id)

        if ticket is None:
            return "This channel is not a tracked ticket."

        if ticket["status"] == TICKET_STATUS_CLOSED:
            return "This ticket is already closed."

        closed_category = await self.configured_closed_category()
        overwrites = dict(channel.overwrites)
        bot_user_id = self.bot.user.id if self.bot.user is not None else 0

        for target in list(overwrites):
            if isinstance(target, discord.Member) and target.id != bot_user_id:
                overwrites[target] = discord.PermissionOverwrite(view_channel=False)

        edit_kwargs = {
            "overwrites": overwrites,
            "reason": f"Ticket closed by {actor} ({actor.id})",
        }

        if closed_category is not None:
            edit_kwargs["category"] = closed_category

        try:
            await channel.edit(**edit_kwargs)
        except discord.HTTPException as exc:
            return f"Could not close the ticket: `{exc}`"

        update_ticket_status(
            channel.id,
            TICKET_STATUS_CLOSED,
            closed_category_id=closed_category.id if closed_category is not None else None,
        )

        embed = discord.Embed(
            title="Ticket Closed",
            description="An admin can archive this ticket when it is ready for the transcript log.",
            color=discord.Color.orange(),
        )
        embed.add_field(name="Closed By", value=actor.mention, inline=True)

        if closed_category is None:
            embed.add_field(
                name="Category Move",
                value="Skipped because `TICKET_CLOSED_CATEGORY_ID` is not configured.",
                inline=False,
            )

        try:
            await channel.send(
                embed=embed,
                view=TicketClosedView(self),
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException:
            pass

        return "Ticket closed."

    async def archive_ticket_from_interaction(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_ephemeral(interaction, "Tickets can only be archived inside a server.")
            return

        if not self.is_ticket_admin(interaction.user):
            await self.send_ephemeral(interaction, "Only admins can archive tickets.")
            return

        channel = interaction.channel
        if not isinstance(channel, discord.TextChannel):
            await self.send_ephemeral(interaction, "Use this inside a ticket channel.")
            return

        ticket = ticket_for_channel(channel.id)
        if ticket is None:
            await self.send_ephemeral(interaction, "This channel is not a tracked ticket.")
            return

        if ticket["status"] != TICKET_STATUS_CLOSED:
            await self.send_ephemeral(interaction, "Only closed tickets can be archived.")
            return

        log_channel = await self.messageable_channel(config.TICKET_LOG_CHANNEL_ID)
        if log_channel is None:
            await self.send_ephemeral(
                interaction,
                "Set `TICKET_LOG_CHANNEL_ID` before archiving tickets.",
            )
            return

        await interaction.response.defer(ephemeral=True)

        transcript = await self.build_transcript(channel, ticket)
        filename = f"{channel.name}-transcript.txt"
        file = discord.File(
            io.BytesIO(transcript.encode("utf-8")),
            filename=filename,
        )
        embed = discord.Embed(
            title="Ticket Archived",
            description=f"Transcript for `{channel.name}`.",
            color=discord.Color.dark_gray(),
        )
        embed.add_field(name="Ticket", value=f"`{channel.name}`", inline=False)
        embed.add_field(name="Archived By", value=f"{interaction.user} (`{interaction.user.id}`)", inline=False)
        embed.add_field(name="Category", value=str(ticket["category_label"]), inline=True)
        embed.add_field(name="Ticket Number", value=str(ticket["ticket_number"]), inline=True)

        try:
            await log_channel.send(
                embed=embed,
                file=file,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            await interaction.followup.send(
                f"Could not send the transcript log: `{exc}`",
                ephemeral=True,
            )
            return

        update_ticket_status(channel.id, TICKET_STATUS_ARCHIVED)
        await interaction.followup.send(
            "Ticket archived. I will delete this channel in a few seconds.",
            ephemeral=True,
        )
        await asyncio.sleep(3)

        try:
            await channel.delete(reason=f"Ticket archived by {interaction.user} ({interaction.user.id})")
        except discord.HTTPException:
            pass

    async def build_transcript(self, channel: discord.TextChannel, ticket) -> str:
        lines = [
            f"Ticket: #{channel.name}",
            f"Guild ID: {channel.guild.id}",
            f"Channel ID: {channel.id}",
            f"Ticket Number: {ticket['ticket_number']}",
            f"Category: {ticket['category_label']} ({ticket['category_key']})",
            f"Opened By: {ticket['opener_id']}",
            f"Created At: {ticket['created_at']}",
            "",
            "Messages",
            "========",
        ]

        async for message in channel.history(limit=None, oldest_first=True):
            created_at = message.created_at.astimezone(timezone.utc).isoformat()
            author = f"{message.author} ({message.author.id})"
            content = message.content or ""
            lines.append(f"[{created_at}] {author}: {content}")

            if message.attachments:
                for attachment in message.attachments:
                    lines.append(f"  Attachment: {attachment.filename} {attachment.url}")

            if message.embeds:
                for embed in message.embeds:
                    title = embed.title or "(no title)"
                    description = embed.description or ""
                    lines.append(f"  Embed: {title} {description}".rstrip())

            lines.append("")

        return "\n".join(lines).rstrip() + "\n"

    async def current_ticket_channel(self, interaction: discord.Interaction):
        channel = interaction.channel

        if not isinstance(channel, discord.TextChannel):
            await self.send_ephemeral(interaction, "Use this inside a ticket channel.")
            return None, None

        ticket = ticket_for_channel(channel.id)
        if ticket is None:
            await self.send_ephemeral(interaction, "This channel is not a tracked ticket.")
            return None, None

        return channel, ticket

    @app_commands.command(
        name="ticket_panel",
        description="Send the ticket dropdown panel.",
    )
    @app_commands.describe(
        channel="Optional channel for the ticket panel. Defaults to the current channel.",
    )
    async def ticket_panel(
        self,
        interaction: discord.Interaction,
        channel: Optional[discord.TextChannel] = None,
    ):
        if interaction.guild is None or not isinstance(interaction.user, discord.Member):
            await self.send_ephemeral(interaction, "Use this inside a server.")
            return

        if not self.is_ticket_admin(interaction.user):
            await self.send_ephemeral(interaction, "Only admins can send ticket panels.")
            return

        target = channel or interaction.channel
        if not isinstance(target, discord.TextChannel):
            await self.send_ephemeral(interaction, "Choose a server text channel.")
            return

        if target.category is None:
            await self.send_ephemeral(
                interaction,
                "The panel channel must be inside the category where open tickets should be created.",
            )
            return

        embed = discord.Embed(
            title="Open a Ticket",
            description="Choose the category that best matches what you need.",
            color=discord.Color.blurple(),
        )

        await target.send(
            embed=embed,
            view=TicketPanelView(self),
            allowed_mentions=discord.AllowedMentions.none(),
        )
        await self.send_ephemeral(interaction, f"Ticket panel sent to {target.mention}.")

    @app_commands.command(
        name="ticket_close_request",
        description="Post a close request button in this ticket.",
    )
    async def ticket_close_request(self, interaction: discord.Interaction):
        channel, ticket = await self.current_ticket_channel(interaction)
        if channel is None or ticket is None:
            return

        if not isinstance(interaction.user, discord.Member):
            await self.send_ephemeral(interaction, "Use this inside a server.")
            return

        if (
            interaction.user.id != int(ticket["opener_id"])
            and not self.is_ticket_manager(interaction.user)
        ):
            await self.send_ephemeral(interaction, "Only the ticket opener or staff can request closure.")
            return

        opener_mention = f"<@{ticket['opener_id']}>"
        await channel.send(
            f"{opener_mention}, this ticket is being requested to be closed by {interaction.user.mention}.",
            view=TicketManageView(self),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        await self.send_ephemeral(interaction, "Close request posted.")

    @app_commands.command(
        name="ticket_add_user",
        description="Add a user to this ticket.",
    )
    @app_commands.describe(
        user="User to add to the ticket.",
    )
    async def ticket_add_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        channel, ticket = await self.current_ticket_channel(interaction)
        if channel is None or ticket is None:
            return

        if not isinstance(interaction.user, discord.Member) or not self.is_ticket_manager(interaction.user):
            await self.send_ephemeral(interaction, "Only staff can add users to tickets.")
            return

        await channel.set_permissions(
            user,
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            reason=f"Added to ticket by {interaction.user} ({interaction.user.id})",
        )
        await channel.send(
            f"{user.mention} was added to this ticket by {interaction.user.mention}.",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        await self.send_ephemeral(interaction, f"Added {user.mention} to this ticket.")

    @app_commands.command(
        name="ticket_remove_user",
        description="Remove a user from this ticket.",
    )
    @app_commands.describe(
        user="User to remove from the ticket.",
    )
    async def ticket_remove_user(
        self,
        interaction: discord.Interaction,
        user: discord.Member,
    ):
        channel, ticket = await self.current_ticket_channel(interaction)
        if channel is None or ticket is None:
            return

        if not isinstance(interaction.user, discord.Member) or not self.is_ticket_manager(interaction.user):
            await self.send_ephemeral(interaction, "Only staff can remove users from tickets.")
            return

        await channel.set_permissions(
            user,
            overwrite=discord.PermissionOverwrite(view_channel=False),
            reason=f"Removed from ticket by {interaction.user} ({interaction.user.id})",
        )
        await channel.send(
            f"{user.mention} was removed from this ticket by {interaction.user.mention}.",
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        await self.send_ephemeral(interaction, f"Removed {user.mention} from this ticket.")

    @app_commands.command(
        name="ticket_reopen",
        description="Reopen this closed ticket.",
    )
    async def ticket_reopen(self, interaction: discord.Interaction):
        channel, ticket = await self.current_ticket_channel(interaction)
        if channel is None or ticket is None:
            return

        if not isinstance(interaction.user, discord.Member) or not self.is_ticket_manager(interaction.user):
            await self.send_ephemeral(interaction, "Only staff can reopen tickets.")
            return

        if ticket["status"] != TICKET_STATUS_CLOSED:
            await self.send_ephemeral(interaction, "Only closed tickets can be reopened.")
            return

        opener = interaction.guild.get_member(int(ticket["opener_id"])) if interaction.guild else None
        open_category = None

        if ticket["open_category_id"]:
            maybe_category = self.bot.get_channel(int(ticket["open_category_id"]))
            if isinstance(maybe_category, discord.CategoryChannel):
                open_category = maybe_category

        edit_kwargs = {
            "reason": f"Ticket reopened by {interaction.user} ({interaction.user.id})",
        }
        if open_category is not None:
            edit_kwargs["category"] = open_category

        await channel.edit(**edit_kwargs)

        if opener is not None:
            await channel.set_permissions(
                opener,
                view_channel=True,
                send_messages=True,
                read_message_history=True,
                attach_files=True,
                embed_links=True,
                reason=f"Ticket reopened by {interaction.user} ({interaction.user.id})",
            )

        update_ticket_status(channel.id, TICKET_STATUS_OPEN)

        await channel.send(
            f"This ticket was reopened by {interaction.user.mention}.",
            view=TicketManageView(self),
            allowed_mentions=discord.AllowedMentions(users=True, roles=False, everyone=False),
        )
        await self.send_ephemeral(interaction, "Ticket reopened.")

    @commands.Cog.listener()
    async def on_message(self, message: discord.Message):
        if message.guild is None or message.author.bot:
            return

        if not isinstance(message.channel, discord.TextChannel):
            return

        if not message.attachments:
            return

        ticket = ticket_for_channel(message.channel.id)
        if ticket is None or ticket["status"] == TICKET_STATUS_ARCHIVED:
            return

        media_attachments = [
            attachment
            for attachment in message.attachments
            if is_media_attachment(attachment)
        ]

        if not media_attachments:
            return

        log_channel = await self.messageable_channel(config.TICKET_IMAGE_LOG_CHANNEL_ID)
        if log_channel is None:
            return

        files = []
        failed_urls = []
        guild_limit = getattr(message.guild, "filesize_limit", 8 * 1024 * 1024)

        for attachment in media_attachments[:10]:
            if attachment.size > guild_limit:
                failed_urls.append(attachment.url)
                continue

            try:
                files.append(await attachment.to_file())
            except discord.HTTPException:
                failed_urls.append(attachment.url)

        caption_parts = [
            f"Ticket: `{message.channel.name}`",
            f"Author: {message.author} (`{message.author.id}`)",
            f"Category: {ticket['category_label']}",
            f"Jump: {message.jump_url}",
        ]

        if message.content:
            caption_parts.append(f"Message: {message.content[:500]}")

        if failed_urls:
            caption_parts.append("Attachment URLs:\n" + "\n".join(failed_urls))

        content = "\n".join(caption_parts)

        try:
            await log_channel.send(
                content=content[:1900],
                files=files,
                allowed_mentions=discord.AllowedMentions.none(),
            )
        except discord.HTTPException as exc:
            print(f"Failed to forward ticket media from {message.channel.id}: {exc}")


async def setup(bot: commands.Bot):
    init_db()
    cog = Tickets(bot)
    bot.add_view(TicketPanelView(cog))
    bot.add_view(TicketManageView(cog))
    bot.add_view(TicketClosedView(cog))
    await bot.add_cog(cog)
