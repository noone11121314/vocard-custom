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

import os
import asyncio
import discord
import voicelink
import function as func

from discord.ext import commands

from voicelink import MongoDBHandler, Config
from voicelink.utils import TempCtx

class Listeners(commands.Cog):
    """Music Cog."""

    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self.voicelink = voicelink.NodePool()

        bot.loop.create_task(self.start_nodes())
        bot.loop.create_task(self.restore_last_session_players())
        
    async def start_nodes(self) -> None:
        """Connect and intiate nodes."""
        for n in Config().nodes.values():
            try:
                await self.voicelink.create_node(bot=self.bot, **n)
            except Exception as e:
                func.logger.error(f'Node {n["identifier"]} is not able to connect! - Reason: {e}')

    async def restore_last_session_players(self) -> None:
        """Re-establish connections for players from the last session."""
        await self.bot.wait_until_ready()
        players = func.open_json(Config.LAST_SESSION_FILE_DIR)
        if not players:
            return

        for data in players:
            try:
                channel_id = data.get("channel_id")
                if not channel_id:
                    continue

                channel = self.bot.get_channel(channel_id)
                if not channel:
                    continue
                elif not any(False if member.bot or member.voice.self_deaf else True for member in channel.members):
                    continue
                    
                # Patch 18: fall back to the bot's own member instead of
                # giving up on restoring this guild entirely just because
                # the original DJ happened to have left since the last
                # session - TempCtx doesn't actually require a real human
                # Member, it's just a duck-typed context stand-in (author/
                # channel/guild attributes only, see voicelink/utils.py).
                dj_member = channel.guild.get_member(data.get("dj")) or channel.guild.me
                if not dj_member:
                    continue

                # Get the guild settings
                settings = await MongoDBHandler.get_settings(channel.guild.id)

                # Connect to the channel and initialize the player.
                player: voicelink.Player = await channel.connect(
                    cls=voicelink.Player(self.bot, channel, TempCtx(dj_member, channel), settings)
                )

                # Restore the queue.
                queue_data = data.get("queue", {})
                for track_data in queue_data.get("tracks", []):
                    track_id = track_data.get("track_id")
                    if not track_id:
                        continue

                    decoded_track = voicelink.Track.decode(track_id)
                    # Patch 18: same requester-left-the-guild fallback as
                    # dj_member above - Track.requester is accessed
                    # unconditionally elsewhere (e.g. `.bot`, `.id`), so a
                    # bare None here would crash the first thing that touches it.
                    requester = channel.guild.get_member(track_data.get("requester_id")) or channel.guild.me
                    track = voicelink.Track(track_id=track_id, info=decoded_track, requester=requester)
                    player.queue._queue.append(track)

                # Restore queue settings. No adjustment here - Player.data
                # (Patch 20) already saves the position do_next()'s own
                # Queue.get() needs to land back on the track that was
                # actually interrupted (one less than the live, post-get()
                # `queue._position`, when there was a current track; the
                # raw value otherwise). Re-subtracting here would double it.
                player.queue._position = queue_data.get("position", 0)
                repeat_mode = queue_data.get("repeat_mode", "OFF")
                try:
                    loop_mode = voicelink.LoopType[repeat_mode]
                except KeyError:
                    loop_mode = voicelink.LoopType.OFF
                player.queue._repeat.set_mode(loop_mode)
                player.queue._repeat_position = queue_data.get("repeat_position")

                # Restore player settings
                player.dj = dj_member
                player.settings['autoplay'] = data.get('autoplay', False)

                # Resume playback or invoke the controller based on the player's state.
                if not player.is_playing:
                    # Patch 25: start the resumed track *at* the saved position
                    # rather than seeking right after do_next(). Lavalink drops
                    # a seek that arrives before the track has actually started
                    # (confirmed live: paused was restored, position stayed 0).
                    player._resume_start_ms = int(data.get("position") or 0)
                    await player.do_next()

                    if is_paused := data.get("is_paused"):
                        await player.set_pause(is_paused, self.bot.user)

                await asyncio.sleep(5)

            except Exception as e:
                func.logger.error(f"Error encountered while restoring a player for channel ID {channel_id}.", exc_info=e)

        # Clear the last session file so a later crash (no graceful save)
        # can't restore this same stale session again. Patch 25: truncated
        # rather than deleted - since Patch 18 it's a bind-mounted file, and
        # a mount point can't be removed (EBUSY).
        try:
            if os.path.exists(Config.LAST_SESSION_FILE_DIR):
                with open(Config.LAST_SESSION_FILE_DIR, "w", encoding="utf-8") as f:
                    f.write("[]")

        except Exception as del_error:
            func.logger.error("Failed to clear session file: %s", Config.LAST_SESSION_FILE_DIR, exc_info=del_error)

    @commands.Cog.listener()
    async def on_voicelink_track_end(self, player: voicelink.Player, track, reason: str, position: float):
        if reason == "loadFailed" and self._is_retryable_direct_resolve(player, track):
            player._direct_resolve_retry_counts[track.identifier] = player._direct_resolve_retry_counts.get(track.identifier, 0) + 1
            return await player._retry_direct_resolve(track, position)

        await player.do_next()

    @commands.Cog.listener()
    async def on_voicelink_track_stuck(self, player: voicelink.Player, track, _):
        await asyncio.sleep(10)
        await player.do_next()

    @commands.Cog.listener()
    async def on_voicelink_track_exception(self, player: voicelink.Player, track, error: dict):
        try:
            if self._is_retryable_direct_resolve(player, track):
                # on_voicelink_track_end (fired right after this, same
                # loadFailed) will resume the same track - don't tell the
                # user we're moving to the next song, and don't set
                # _track_is_stuck, which would otherwise add a stale extra
                # 10s delay the next time do_next() actually runs.
                await player.context.send(f"{error['message']} Reconnecting...", delete_after=10)
            else:
                player._track_is_stuck = True
                await player.context.send(f"{error['message']} The next song will begin in the next 5 seconds.", delete_after=10)
        except:
            pass

    @staticmethod
    def _is_retryable_direct_resolve(player: voicelink.Player, track) -> bool:
        """Whether a loadFailed/exception for `track` is eligible for
        Player._retry_direct_resolve instead of the normal do_next() advance:
        only direct-resolved YouTube tracks, and only within their retry
        budget (see Player.MAX_DIRECT_RESOLVE_RETRIES).
        """
        return bool(
            track and track.source == "youtube" and track.identifier and
            player._direct_resolve_retry_counts.get(track.identifier, 0) < player.MAX_DIRECT_RESOLVE_RETRIES
        )

    @commands.Cog.listener()
    async def on_voice_state_update(self, member: discord.Member, before: discord.VoiceState, after: discord.VoiceState):
        if member.bot:
            return
        
        if before.channel == after.channel:
            return

        player: voicelink.Player = member.guild.voice_client
        if not player:
            return

        is_joined = True
        
        if not before.channel and after.channel:
            if after.channel.id != player.channel.id:
                return

        elif before.channel and not after.channel:
            is_joined = False
        
        elif before.channel and after.channel:
            if after.channel.id != player.channel.id:
                is_joined = False
                
        if is_joined and player.settings.get("24/7", False):
            if player.is_paused and len([m for m in player.channel.members if not m.bot or not m.voice.self_deaf]) == 1:
                await player.set_pause(False, member)
                    
        if not is_joined:
            if not player.is_paused and len([m for m in player.channel.members if not m.bot or not m.voice.self_deaf]) == 0:
                player._schedule_inactive_cleanup_timer()

        # if dj is not in the channel, find a new DJ
        if player.dj not in player.channel.members:
            for m in player.channel.members:
                if not m.bot or not m.voice.self_deaf:
                    player.dj = m
                    break
                    
        if player.is_ipc_connected:
            await player._ipc_client.send({
                "op": "updateGuild",
                "user": {
                    "userId": str(member.id),
                    "avatarUrl": member.display_avatar.url,
                    "name": member.name,
                },
                "channelName": member.voice.channel.name if is_joined else "",
                "guildId": str(member.guild.id),
                "isJoined": is_joined
            })

async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Listeners(bot))
