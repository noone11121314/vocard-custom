"""Patch 46 (vocard-custom issue #1): /cantus - play your own songs and
playlists from Cantus Flaccus. See voicelink/cantus.py for how it talks to
Cantus, how queued songs get their play links and why they're private.

Slash commands only (no prefix forms), and every account reply is
ephemeral. Connecting happens in the dashboard (a personal token), so a
token is never typed in Discord: `/cantus link` just explains the steps."""

import discord
import voicelink

from discord import app_commands
from discord.ext import commands

from voicelink import cantus

STEPS = (
    "In Cantus, create a token under **Account -> Connect other apps**, copy the **connect code** "
    "(`cantus+https://cfa_…@…`, shown once), and paste it in the dashboard under **Settings -> Cantus**.\n"
    "Then `/cantus play` and `/cantus playlist` use your own library. Your Cantus songs stay yours: "
    "others only see them while they're in the queue. Revoking the token in Cantus cuts Vocard off; "
    "`/cantus unlink` forgets it here."
)
NOT_LINKED = "Your Discord account isn't connected to Cantus yet - see `/cantus link`."


def _steps() -> str:
    """The token steps, with a link to the dashboard when settings.json has one.
    Tokens are never typed in Discord."""
    url = cantus.dashboard_url()
    return STEPS + (f"\nDashboard: {url}" if url else "")


def _error_text(e: cantus.CantusError) -> str:
    if e.not_linked:
        return NOT_LINKED
    if e.code == "token_revoked":
        return "Your Cantus token isn't valid any more (revoked?) - paste a new one in the dashboard (Settings -> Cantus)."
    if e.code in ("private_address", "bad_url", "foreign_link"):
        return "Your Cantus server address isn't allowed - reconnect in the dashboard."
    if e.status in (401, 403):
        return "Cantus refused that."
    if e.status == 404:
        return "Not found in your Cantus library."
    return "Cantus didn't answer - try again in a moment."


class Cantus(commands.Cog, name="cantus"):
    group = app_commands.Group(name="cantus", description="Play your songs and playlists from Cantus Flaccus.")

    def __init__(self, bot: commands.Bot) -> None:
        self.bot = bot
        self.description = "Play your own songs and playlists from Cantus Flaccus."

    async def cog_load(self) -> None:
        await cantus.load_gains()
        await cantus.load_default_server()

    # --- autocomplete ------------------------------------------------------

    async def song_autocomplete(self, interaction: discord.Interaction, current: str) -> list:
        try:
            songs = (await cantus.search_songs(interaction.user.id, current, limit=25)).get("songs", [])
        except cantus.CantusError:
            return []
        return [app_commands.Choice(name=cantus.song_label(s), value=str(s["id"])[:100]) for s in songs if s.get("id")]

    async def playlist_autocomplete(self, interaction: discord.Interaction, current: str) -> list:
        try:
            lists = await cantus.playlists(interaction.user.id)
        except cantus.CantusError:
            return []
        words = current.lower().split()
        matches = [p for p in lists if p.get("id") and all(w in str(p.get("name", "")).lower() for w in words)]
        return [app_commands.Choice(name=cantus.choice_label(f"{p.get('name')} ({p.get('songs', 0)} songs)"), value=str(p["id"])[:100]) for p in matches[:25]]

    # --- account -----------------------------------------------------------

    @group.command(name="help", description="How to connect Cantus and play from it.")
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("**Cantus Flaccus in Vocard**\n" + _steps(), ephemeral=True)

    @group.command(name="link", description="How to connect your Cantus account (in the dashboard).")
    async def link(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message("**Connect your Cantus account**\n" + _steps(), ephemeral=True)

    @group.command(name="unlink", description="Disconnect your Cantus account.")
    async def unlink(self, interaction: discord.Interaction) -> None:
        await interaction.response.defer(ephemeral=True)
        if await cantus.disconnect(interaction.user.id):
            await interaction.followup.send("Your Cantus token is forgotten - Vocard can't reach your library any more.", ephemeral=True)
        else:
            await interaction.followup.send("You weren't connected to Cantus.", ephemeral=True)

    # --- playing -----------------------------------------------------------

    async def _player_for(self, interaction: discord.Interaction):
        player: voicelink.Player = interaction.guild.voice_client
        if not player:
            player = await voicelink.connect_channel(interaction)
        if not player.is_user_join(interaction.user):
            raise voicelink.VoicelinkException(f"Join {player.channel.mention} first.")
        return player

    async def _find_song(self, user_id: int, value: str) -> dict:
        """A picked autocomplete entry is a song id; anything else typed is
        searched and the best match taken."""
        song = await cantus.song_by_id(user_id, value)
        if song:
            return song
        songs = (await cantus.search_songs(user_id, value, limit=1)).get("songs", [])
        if not songs:
            raise cantus.CantusError(404, "not_found")
        return songs[0]

    async def _queue(self, interaction: discord.Interaction, player, tracks: list, text: str) -> None:
        try:
            await player.add_track(tracks if len(tracks) > 1 else tracks[0])
            await interaction.followup.send(text)
        finally:
            if not player.is_playing:
                await player.do_next()

    @group.command(name="play", description="Play a song from your Cantus library.")
    @app_commands.describe(song="Start typing to search your library")
    @app_commands.autocomplete(song=song_autocomplete)
    async def play(self, interaction: discord.Interaction, song: str) -> None:
        await interaction.response.defer()
        try:
            found = await self._find_song(interaction.user.id, song)
            server = await cantus.server_of(interaction.user.id)
            player = await self._player_for(interaction)
        except cantus.CantusError as e:
            return await interaction.followup.send(_error_text(e), ephemeral=True)
        except voicelink.VoicelinkException as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        track = cantus.build_track(found, interaction.user.id, interaction.user, server)
        await self._queue(interaction, player, [track], f"Added **{track.title}** - {track.author} from Cantus.")

    @group.command(name="playlist", description="Queue one of your Cantus playlists.")
    @app_commands.describe(name="Start typing a playlist name")
    @app_commands.autocomplete(name=playlist_autocomplete)
    async def playlist(self, interaction: discord.Interaction, name: str) -> None:
        await interaction.response.defer()
        user_id = interaction.user.id
        try:
            try:
                data = await cantus.playlist(user_id, name)
            except cantus.CantusError as e:
                if e.status != 404:
                    raise
                # Typed a name instead of picking: look it up by name.
                match = next((p for p in await cantus.playlists(user_id) if str(p.get("name", "")).lower() == name.lower()), None)
                if not match:
                    raise
                data = await cantus.playlist(user_id, match["id"])
            server = await cantus.server_of(user_id)
            player = await self._player_for(interaction)
        except cantus.CantusError as e:
            return await interaction.followup.send(_error_text(e), ephemeral=True)
        except voicelink.VoicelinkException as e:
            return await interaction.followup.send(str(e), ephemeral=True)
        songs = [s for s in data.get("songs", []) if s.get("id")]
        if not songs:
            return await interaction.followup.send(f"**{data.get('name', name)}** has no playable songs yet.", ephemeral=True)
        tracks = [cantus.build_track(s, user_id, interaction.user, server) for s in songs]
        await self._queue(interaction, player, tracks, f"Added **{len(tracks)}** songs from your Cantus playlist **{data.get('name', name)}**.")


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Cantus(bot))
