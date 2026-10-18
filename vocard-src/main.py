"""MIT License

Copyright (c) 2023 - present Vocard Development

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

import discord
import sys
import os
import signal
import asyncio
import aiohttp
import update
import logging
import function as func

from discord.ext import commands
from logging.handlers import TimedRotatingFileHandler
from voicelink import Config, LangHandler, MongoDBHandler, IPCClient, VoicelinkException
from voicelink.session import save_last_session
from voicelink.utils import dispatch_message

class Translator(discord.app_commands.Translator):
    MISSING_TRANSLATOR: dict[str, list[str]] = {}

    async def load(self):
        func.logger.info("Loaded Translator")
        
    async def unload(self):
        func.logger.info("Unload Translator")
        
    async def translate(
        self,
        string: discord.app_commands.locale_str,
        locale: discord.Locale,
        context: discord.app_commands.TranslationContext
    ) -> str | None:
        locale_key = str(locale).upper()
        local_translations = LangHandler._local_langs.get(locale_key)
        if not local_translations:
            return None

        translated_text = local_translations.get(string.message)
        if translated_text is not None:
            return translated_text

        missing = self.MISSING_TRANSLATOR.setdefault(locale_key, [])
        if string.message not in missing:
            missing.append(string.message)

        return None

class Vocard(commands.Bot):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)

        self.ipc_client: IPCClient
        self._shutting_down: bool = False

    def _handle_shutdown_signal(self) -> None:
        """Patch 18: Docker's `docker compose restart`/`stop` sends SIGTERM
        to this process, and by default neither Python nor discord.py do
        anything graceful with that signal - the process just dies
        immediately, with no chance to save anything. Registered (see
        setup_hook below) for both SIGTERM and SIGINT so every normal way
        this container stops - a deploy restart, `docker stop`, or a local
        Ctrl+C - saves the current queue/playback state first via
        save_last_session(), which cogs/listeners.py's
        restore_last_session_players() automatically reads back and
        resumes from on the next startup.

        `_shutting_down` guards against this firing twice (e.g. SIGTERM
        then SIGKILL-adjacent SIGTERM retransmits, or SIGTERM racing a
        manual Stop-Bot click) - save_last_session() is cheap/idempotent
        either way, but there's no reason to run it or schedule close()
        more than once.
        """
        if self._shutting_down:
            return
        self._shutting_down = True

        func.logger.info("Received a shutdown signal - saving current playback state before exiting.")
        try:
            save_last_session()
        except Exception as e:
            func.logger.error("Failed to save last-session file on shutdown.", exc_info=e)

        asyncio.get_running_loop().create_task(self.close())

    async def on_message(self, message: discord.Message, /) -> None:
        # Ignore messages from bots or DMs
        if message.author.bot or not message.guild:
            return False

        # Check if the bot is directly mentioned
        if message.content.strip() == self.user.mention and not message.mention_everyone:
            prefix = await self.command_prefix(self, message)
            if not prefix:
                return await message.channel.send("I don't have a bot prefix set.")
            return await message.channel.send(f"My prefix is `{prefix}`")

        # Fetch guild settings and check if the mesage is in the music request channel
        settings = await MongoDBHandler.get_settings(message.guild.id)
        if settings and (request_channel := settings.get("music_request_channel")):
            if message.channel.id == request_channel.get("text_channel_id"):
                ctx = await self.get_context(message)    
                try:
                    if not ctx.prefix:
                        cmd = self.get_command("play")
                        if message.content:
                            await cmd(ctx, query=message.content)

                        elif message.attachments:
                            for attachment in message.attachments:
                                await cmd(ctx, query=attachment.url)

                except Exception as e:
                    await dispatch_message(ctx, str(e), ephemeral=True)

                finally:
                    await message.delete()

        await self.process_commands(message)

    async def setup_hook(self) -> None:
        # Patch 18: register BEFORE anything else - a graceful shutdown
        # (and the save it triggers) should still be possible even if
        # something below fails to set up. add_signal_handler is Unix-only
        # (fine - this always runs in the Linux container), so this is
        # best-effort: on an unsupported platform it just logs and the
        # bot behaves exactly as it did before this patch.
        try:
            loop = asyncio.get_running_loop()
            loop.add_signal_handler(signal.SIGTERM, self._handle_shutdown_signal)
            loop.add_signal_handler(signal.SIGINT, self._handle_shutdown_signal)
        except (NotImplementedError, RuntimeError) as e:
            func.logger.warning(f"Could not register graceful-shutdown signal handlers: {e}")

        # Connecting to MongoDB
        await MongoDBHandler.init(bot_config.mongodb_url, bot_config.mongodb_name)

        # Set translator
        await self.tree.set_translator(Translator())

        # Loading all the module in `cogs` folder
        for module in os.listdir(func.ROOT_DIR + '/cogs'):
            if module.endswith('.py'):
                try:
                    await self.load_extension(f"cogs.{module[:-3]}")
                    func.logger.info(f"Loaded {module[:-3]}")
                except Exception as e:
                    func.logger.error(f"Something went wrong while loading {module[:-3]} cog.", exc_info=e)

        self.ipc_client: IPCClient = IPCClient(self, **bot_config.ipc_client)
        if bot_config.ipc_client.get("enable", False):
            try:
                await self.ipc_client.connect()
            except Exception as e:
                func.logger.error(f"Cannot connected to dashboard! - Reason: {e}")

        # Update version tracking
        if not bot_config.version or bot_config.version != update.__version__:
            await self.tree.sync()
            func.update_json("settings.json", new_data={"version": update.__version__})
            
            for locale_key, values in self.tree.translator.MISSING_TRANSLATOR.items():
                func.logger.warning(f'Missing translation for "{", ".join(values)}" in "{locale_key}"')
            self.tree.translator.MISSING_TRANSLATOR.clear()

    async def on_ready(self):
        func.logger.info("------------------")
        func.logger.info(f"Logging As {self.user}")
        func.logger.info(f"Bot ID: {self.user.id}")
        func.logger.info("------------------")
        func.logger.info(f"Vocard Version: {update.__version__}")
        func.logger.info(f"Discord Version: {discord.__version__}")
        func.logger.info(f"Python Version: {sys.version}")
        func.logger.info("------------------")

        bot_config.client_id = self.user.id
        LangHandler._local_langs.clear()

    async def on_command_error(self, ctx: commands.Context, exception, /) -> None:
        error = getattr(exception, 'original', exception)
        if ctx.interaction:
            error = getattr(error, 'original', error)

        if isinstance(error, (commands.CommandNotFound, aiohttp.client_exceptions.ClientOSError, discord.errors.NotFound)):
            return

        elif isinstance(error, (commands.CommandOnCooldown, commands.MissingPermissions, commands.RangeError, commands.BadArgument)):
            pass

        elif isinstance(error, (commands.MissingRequiredArgument, commands.MissingRequiredAttachment)):
            command = f"{ctx.prefix}" + (f"{ctx.command.parent.qualified_name} " if ctx.command.parent else "") + f"{ctx.command.name} {ctx.command.signature}"
            position = command.find(f"<{ctx.current_parameter.name}>") + 1
            description = f"**Correct Usage:**\n```{command}\n" + " " * position + "^" * len(ctx.current_parameter.name) + "```\n"
            if ctx.command.aliases:
                description += f"**Aliases:**\n`{', '.join([f'{ctx.prefix}{alias}' for alias in ctx.command.aliases])}`\n\n"
            description += f"**Description:**\n{ctx.command.help}\n\u200b"

            embed = discord.Embed(description=description, color=bot_config.embed_color)
            embed.set_footer(icon_url=ctx.me.display_avatar.url, text=f"More Help: {bot_config.invite_link}")
            return await ctx.reply(embed=embed)

        elif not issubclass(error.__class__, VoicelinkException):
            error = await Lang_handler.get_lang(ctx.guild.id, "common.errors.unknown") + bot_config.invite_link
            func.logger.error(f"An unexpected error occurred in the {ctx.command.name} command on the {ctx.guild.name}({ctx.guild.id}).", exc_info=exception)

        try:
            return await ctx.reply(error, ephemeral=True)
        except:
            pass

class CommandCheck(discord.app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction, /) -> bool:
        if interaction.type == discord.InteractionType.application_command:
            if not interaction.guild:
                await interaction.response.send_message("This command can only be used in guilds!")
                return False

            channel_perm = interaction.channel.permissions_for(interaction.guild.me)
            if not channel_perm.read_messages or not channel_perm.send_messages:
                await interaction.response.send_message("I don't have permission to read or send messages in this channel.", ephemeral=True)
                return False
            
        return True

async def get_prefix(bot: commands.Bot, message: discord.Message) -> str:
    settings = await MongoDBHandler.get_settings(message.guild.id)
    prefix = settings.get("prefix", bot_config.bot_prefix)
    return prefix if prefix is not None else ""

# Loading settings and logger
bot_config = Config(func.open_json("settings.json"))
Lang_handler = LangHandler.init()

LOG_SETTINGS = bot_config.logging
if (LOG_FILE := LOG_SETTINGS.get("file", {})).get("enable", True):
    log_path = os.path.abspath(LOG_FILE.get("path", "./logs"))
    if not os.path.exists(log_path):
        os.makedirs(log_path)

    file_handler = TimedRotatingFileHandler(filename=f'{log_path}/vocard.log', encoding="utf-8", backupCount=LOG_SETTINGS.get("max_history", 30), when="d")
    file_handler.namer = lambda name: name.replace(".log", "") + ".log"
    file_handler.setFormatter(logging.Formatter('{asctime} [{levelname:<8}] {name}: {message}', '%Y-%m-%d %H:%M:%S', style='{'))
    logging.getLogger().addHandler(file_handler)

for log_name, log_level in LOG_SETTINGS.get("level", {}).items():
    _logger = logging.getLogger(log_name)
    _logger.setLevel(log_level)

# Setup the bot object
intents = discord.Intents.default()
intents.message_content = False if bot_config.bot_prefix is None else True
intents.members = bot_config.ipc_client.get("enable", False)
intents.voice_states = True
intents.presences = False

bot = Vocard(
    command_prefix=get_prefix,
    help_command=None,
    tree_cls=CommandCheck,
    chunk_guilds_at_startup=False,
    activity=discord.Activity(type=discord.ActivityType.listening, name="Starting..."),
    case_insensitive=True,
    intents=intents
)

if __name__ == "__main__":
    update.check_version(with_msg=True)
    bot.run(bot_config.token, root_logger=True)
