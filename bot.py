import discord
from discord.ext import commands

import config
from common import (
    WrongServer,
    NotModStaff,
    NotBanStaff,
    current_sync_guild_ids,
    is_staff,
    is_ban_staff,
)


CORE_COGS = [
    "cogs.moderation",
    "cogs.warns",
    "cogs.logging",
    "cogs.owner",
    "cogs.automation",
    "cogs.self_logging",
    "cogs.affiliate_owner",
]

OPTIONAL_COGS = []

if config.ENABLE_TICKETS:
    OPTIONAL_COGS.append("cogs.tickets")

if config.ENABLE_NATION_SELECTOR:
    OPTIONAL_COGS.append("cogs.nation_selector")

COGS = [*CORE_COGS, *OPTIONAL_COGS]

BAN_LOCKED_COMMANDS = {
    "altcheck",
    "ban",
    "tempban",
    "syncbans",
    "unban",
}


intents = discord.Intents.default()
intents.guilds = True
intents.members = True
intents.message_content = True
intents.reactions = True
intents.voice_states = True
intents.invites = True
intents.emojis_and_stickers = True

if hasattr(intents, "moderation"):
    intents.moderation = True

bot = commands.Bot(
    command_prefix=config.COMMAND_PREFIX,
    intents=intents,
    case_insensitive=True,
    help_command=None,
)


@bot.check
async def mod_gate(ctx: commands.Context) -> bool:
    if ctx.guild is None:
        raise commands.NoPrivateMessage()

    if not config.HOME_GUILD_ID or ctx.guild.id != config.HOME_GUILD_ID:
        raise WrongServer()

    if config.is_bot_owner_id(ctx.author.id):
        return True

    if not isinstance(ctx.author, discord.Member):
        raise NotModStaff()

    command_name = ctx.command.name if ctx.command else ""

    if command_name in BAN_LOCKED_COMMANDS:
        if not is_ban_staff(ctx.author):
            raise NotBanStaff()
        return True

    if not is_staff(ctx.author):
        raise NotModStaff()

    return True


@bot.event
async def on_ready():
    user_id = bot.user.id if bot.user else "unknown"

    print(f"Logged in as {bot.user} ({user_id})")
    print(f"Home guild: {config.HOME_GUILD_ID or 'not configured'}")
    print(f"Base guild: {config.BASE_GUILD_ID or 'not configured'}")
    print(f"Current action guilds: {sorted(current_sync_guild_ids())}")
    print(f"Command prefix: {config.COMMAND_PREFIX}")
    print(f"Nation selector enabled: {config.ENABLE_NATION_SELECTOR}")
    print(f"Tickets enabled: {config.ENABLE_TICKETS}")

    print("Guilds the bot is currently in:")
    for guild in bot.guilds:
        print(f"- {guild.name}: {guild.id}")

    try:
        synced = await bot.tree.sync()
        print(f"Synced {len(synced)} slash command(s).")
    except Exception as error:
        print(f"Failed to sync slash commands: {error}")


@bot.event
async def on_command_error(ctx: commands.Context, error: commands.CommandError):
    if isinstance(error, commands.CommandNotFound):
        return

    if isinstance(error, WrongServer):
        await ctx.reply(
            "Moderation commands can only be used in the configured home server. "
            "If this is a fresh setup, use the owner slash config commands first.",
            mention_author=False,
        )
        return

    if isinstance(error, NotModStaff):
        await ctx.reply(
            "You do not have the required moderator role to use this command.",
            mention_author=False,
        )
        return

    if isinstance(error, NotBanStaff):
        await ctx.reply(
            "You do not have the required ban-permission role to use this command.",
            mention_author=False,
        )
        return

    if isinstance(error, commands.NoPrivateMessage):
        await ctx.reply("These commands cannot be used in DMs.", mention_author=False)
        return

    if isinstance(error, commands.MissingRequiredArgument):
        await ctx.reply(
            f"Missing argument: `{error.param.name}`. Try `{config.COMMAND_PREFIX}modhelp`.",
            mention_author=False,
        )
        return

    if isinstance(error, commands.BadArgument):
        await ctx.reply(str(error), mention_author=False)
        return

    if isinstance(error, commands.CheckFailure):
        await ctx.reply(
            "You do not have permission to use this command.",
            mention_author=False,
        )
        return

    if isinstance(error, commands.CommandInvokeError):
        original = error.original
        await ctx.reply(
            f"Command failed: `{type(original).__name__}: {original}`",
            mention_author=False,
        )
        raise original

    await ctx.reply(f"Unexpected error: `{type(error).__name__}: {error}`", mention_author=False)
    raise error


async def load_cogs():
    for cog in COGS:
        await bot.load_extension(cog)
        print(f"Loaded {cog}")


async def main():
    async with bot:
        await load_cogs()
        await bot.start(config.DISCORD_TOKEN)


if __name__ == "__main__":
    import asyncio

    asyncio.run(main())
