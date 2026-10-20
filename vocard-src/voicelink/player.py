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

from __future__ import annotations

import time, logging, asyncio, hashlib, datetime

from math import ceil
from random import shuffle, choice
from typing import Any, Dict, List, Optional, Union, Tuple, TYPE_CHECKING

from discord import (
    Client,
    Guild,
    VoiceChannel,
    VoiceProtocol,
    Member,
    Message,
    PartialMessage,
    Interaction,
    errors,
    ChannelType
)

from discord.ext import commands

from . import events
from .config import Config
from .pool import Node, NodePool
from .objects import Track, Playlist
from .filters import Filter, Filters
from .enums import SearchType, LoopType, RequestMethod
from .events import VoicelinkEvent, TrackEndEvent, TrackStartEvent, TrackExceptionEvent
from .exceptions import VoicelinkException, FilterInvalidArgument, TrackInvalidPosition, FilterTagAlreadyInUse, DuplicateTrack
from .placeholders import PlayerPlaceholder
from .queue import Queue, QUEUE_TYPES
from .mongodb import MongoDBHandler
from . import cantus
from .language import LangHandler
from .views import InteractiveController
from .utils import format_ms, dispatch_message

if TYPE_CHECKING:
    from .ipc import IPCClient

async def connect_channel(ctx: Union[commands.Context, Interaction], channel: VoiceChannel = None):
    texts = await LangHandler.get_lang(ctx.guild.id, "voice.connection.noChannel", "voice.connection.noPermission")
    try:
        channel = channel or ctx.author.voice.channel if isinstance(ctx, commands.Context) else ctx.user.voice.channel
    except:
        raise VoicelinkException(texts[0])

    check = channel.permissions_for(ctx.guild.me)
    if check.connect == False or check.speak == False:
        raise VoicelinkException(texts[1])

    settings = await MongoDBHandler.get_settings(channel.guild.id)
    player: Player = await channel.connect(
        cls=Player(
            ctx.bot if isinstance(ctx, commands.Context) else ctx.client,
            channel, ctx, settings
        ))

    if player.volume != 100:
        await player.set_volume(player.volume)

    if player.is_ipc_connected:
        await player.send_ws({"op": "createPlayer", "memberIds": [str(member.id) for member in channel.members]})

    return player

class Player(VoiceProtocol):
    """The base player class for Voicelink.
       In order to initiate a player, you must pass it in as a cls when you connect to a channel.
       i.e: ```py
       await ctx.author.voice.channel.connect(cls=voicelink.Player)
       ```
    """

    # How many consecutive loadFailed retries a single direct-resolved video
    # gets (see _retry_direct_resolve) before giving up and moving on like a
    # normal track end. Caps retry storms if a video is persistently broken
    # (e.g. invidious-companion down) rather than just mid-stream-redirected.
    MAX_DIRECT_RESOLVE_RETRIES = 2

    # How often _prefetch_and_notify() polls the yt-dlp download's live
    # progress to push a dashboard update while it's in flight.
    PREFETCH_PROGRESS_POLL_SECONDS = 1.0

    def __call__(self, client: Client, channel: VoiceChannel):
        self.client: Client = client
        self.channel: VoiceChannel = channel

        return self

    def __init__(
        self, 
        client: Optional[Client] = None, 
        channel: Optional[VoiceChannel] = None, 
        ctx: Union[commands.Context, Interaction] = None,
        settings: dict[str, Any] = None
    ):
        self.client: Client = client
        self._bot: Client = client
        self._ipc_client: IPCClient = self._bot.ipc_client
        self._ipc_connection: bool = False
        
        self.context = ctx
        self.dj: Member = ctx.user if isinstance(ctx, Interaction) else ctx.author
        self.channel: VoiceChannel = channel
        self._guild = channel.guild if channel else None

        self.settings: dict = settings
        self.joinTime: float = round(time.time())
        # Patch 28: clamp - a guild may have a >100 volume saved from before the cap.
        self._volume: int = max(0, min(100, int(self.settings.get('volume', 100) or 0)))
        self.queue: Queue = QUEUE_TYPES.get(self.settings.get("queue_type", "queue").lower())(
            self.settings.get("max_queue", Config().max_queue),
            self.settings.get("duplicate_track", True), self.get_msg
        )

        self._node = NodePool.get_node()
        self._current: Optional[Track] = None
        # Set while do_next() is resolving/caching a track it has already
        # pulled off the queue (Queue.get() advanced _position) but before
        # play() sets _current - with Patch 11's blocking pre-cache this
        # window can last a long time. See has_dequeued_track.
        self._loading_track: Optional[Track] = None
        # One-shot start offset for the next do_next() (restart restore).
        self._resume_start_ms: int = 0
        self._filters: Filters = Filters()
        # Patch 10: per-track loudness-normalization gain (a plain Lavalink
        # "volume" filter multiplier, 1.0 = unchanged) - kept separate from
        # self._filters (the user-togglable Effects list) since this is
        # system-managed and must never show up in/be clearable from the
        # dashboard's Effects panel. See _filters_payload_with_gain().
        self._loudness_gain: float = 1.0
        self._paused: bool = False
        self._is_connected: bool = False
        self._ping: float = 0.0
        self._track_is_stuck = False

        self._position: int = 0
        self._last_position: int = 0
        self._last_update: int = 0
        self._ending_track: Optional[Track] = None

        # video_id -> consecutive loadFailed retry count for the direct URL
        # resolver (see _retry_direct_resolve). Reset once that video starts
        # playing successfully again.
        self._direct_resolve_retry_counts: Dict[str, int] = {}

        # Background task sequentially pre-fetching the queue's upcoming
        # YouTube tracks - see _prefetch_next_direct_track/_prefetch_worker.
        self._prefetch_worker_task: Optional[asyncio.Task] = None

        self._voice_state: dict = {}

        self.controller: Union[Message, PartialMessage] = None
        self._updating: bool = False

        self.pause_votes = set()
        self.resume_votes = set()
        self.skip_votes = set()
        self.previous_votes = set()
        self.shuffle_votes = set()
        self.stop_votes = set()

        self._ph = PlayerPlaceholder(client, self)
        self._logger: Optional[logging.Logger] = self._node._logger
        self._inactive_cleanup_task: Optional[asyncio.Task[None]] = None
        self._alone_timer_pending: bool = False  # Patch 44

    def __repr__(self):
        return (
            f"<Voicelink.player bot={self.bot} guildId={self.guild.id} "
            f"is_connected={self.is_connected} is_playing={self.is_playing}>"
        )

    @property
    def position(self) -> float:
        """Property which returns the player's position in a track in milliseconds"""
        if not self.is_playing or not self._current:
            return 0

        if self.is_paused:
            return min(self._last_position, self._current.length)

        difference = (time.time() * 1000) - self._last_update
        position = self._last_position + difference

        if position > self._current.length:
            return 0

        return min(position, self._current.length)

    @property
    def is_playing(self) -> bool:
        """Property which returns whether or not the player is actively playing a track."""
        return self._is_connected and self._current is not None

    @property
    def is_connected(self) -> bool:
        """Property which returns whether or not the player is connected"""
        return self._is_connected

    @property
    def is_paused(self) -> bool:
        """Property which returns whether or not the player has a track which is paused or not."""
        return self._is_connected and self._paused

    @property
    def current(self) -> Optional[Track]:
        """Property which returns the currently playing track"""
        return self._current

    @property
    def node(self) -> Node:
        """Property which returns the node the player is connected to"""
        return self._node

    @property
    def guild(self) -> Guild:
        """Property which returns the guild associated with the player"""
        return self._guild

    @property
    def volume(self) -> int:
        """Property which returns the players current volume"""
        return self._volume

    @property
    def filters(self) -> Filters:
        """Property which returns the helper class for interacting with filters"""
        return self._filters

    @property
    def bot(self) -> Client:
        """Property which returns the bot associated with this player instance"""
        return self._bot

    @property
    def is_dead(self) -> bool:
        """Returns a bool representing whether the player is dead or not.
           A player is considered dead if it has been destroyed and removed from stored players.
        """
        return self.guild.id not in self._node._players

    @property
    def ping(self) -> float:
        """Calculates and returns the player's current ping in seconds."""
        return round(self._ping / 1000, 2)
    
    @property
    def autoplay(self) -> bool:
        """Indicates whether the player is set to autoplay."""
        return self.settings.get("autoplay", False)
    
    @property
    def data(self) -> dict:
        """Returns a dictionary containing the player's data."""
        return {
            "guild_id": self._guild.id,
            "channel_id": self.channel.id,
            "queue": {
                "tracks": [track.data for track in self.queue._queue],
                # Patch 20: queue._position always sits one past whatever
                # track is actually current (Queue.get() reads _queue[pos]
                # then increments) - restoring this raw value and handing it
                # to do_next() (which also calls Queue.get()) would skip
                # straight past the interrupted track to the *next* one,
                # silently dropping it into history despite the is_paused/
                # position fields below clearly intending to resume it.
                # Save the pre-get() position instead (what do_next()'s own
                # Queue.get() needs to land back on self._current) whenever
                # there is a current track; otherwise nothing was playing
                # yet and the raw position (0, pointing at the first
                # upcoming track) is already correct as-is.
                "position": self.queue._position - 1 if self.has_dequeued_track else self.queue._position,
                "repeat_mode": self.queue._repeat.current.name,
                "repeat_position": self.queue._repeat_position
            },
            "dj": self.dj.id,
            "is_paused": self.is_paused,
            "position": self.position,
            "autoplay": self.autoplay
        }
    
    @property
    def loading_track(self) -> Optional[Track]:
        """The track do_next() is currently resolving/caching, if any."""
        return getattr(self, "_loading_track", None)

    @property
    def has_dequeued_track(self) -> bool:
        """Whether queue._position already sits one past a "current" track -
        either one that's playing (_current) or one still being resolved/
        cached before play() hands it to Lavalink (loading_track)."""
        return self._current is not None or self.loading_track is not None

    @property
    def is_ipc_connected(self) -> bool:
        """Indicates whether the Inter-Process Communication (IPC) connection is active."""
        return self._ipc_client._is_connected and self._ipc_connection
        
    def get_msg(self, *keys) -> Union[list[str], str]:
        """Retrieves a localized message or list of messages based on the given keys
           for the guild associated with this player.
        """
        return LangHandler._get_lang(self.settings.get("lang"), *keys)

    def required(self, leave: bool = False):
        """
        Calculates the number of votes required for a specific action in the voice channel.

        If `leave` is True and the channel has three members, the requirement adjusts to 2 votes.
        """
        if self.settings.get('disabled_vote'):
            return 0

        required = ceil((len(self.channel.members) - 1) / 2.5)
        if leave:
            required += 1
        
        return required
    
    def is_user_join(self, user: Member):
        """Checks if a user is present in the voice channel or has 'Manage Server' permission."""
        if user not in self.channel.members:
            if not user.guild_permissions.manage_guild:
                return False        
        return True
    
    def is_privileged(self, user: Member, check_user_join: bool = True) -> bool:
        """
        Determines if a user has privileged access.

        Privileged access is granted if the user is in the bot access list, 
        has 'Manage Server' permission, or meets the DJ role criteria in the settings.
        Raises an exception if `check_user_join` is True and the user is not in the channel.
        """
        if user.id in Config().bot_access_user:
            return True
        
        manage_perm = user.guild_permissions.manage_guild
        if check_user_join and not self.is_user_join(user):
            raise VoicelinkException(self.get_msg('voice.connection.notInChannel').format(user.mention, self.channel.mention))
            
        if 'dj' in self.settings and self.settings['dj']:
            return manage_perm or (self.settings['dj'] in [role.id for role in user.roles])
        return self.dj.id == user.id or manage_perm
    
    def build_embed(self, current_track: Track = None):
        """Builds an embed based on the current track state."""
        controller = self.settings.get("default_controller", Config().controller).get("embeds", {})
        embed_form = controller.get("active" if current_track else "inactive", {})
        
        return PlayerPlaceholder.build_embed(embed_form, self._ph)

    async def send(self, method: RequestMethod, query: str = None, data: Union[Dict, str] = {}) -> Dict:
        """Sends an HTTP request to the node with the given method, query, and data."""
        uri: str = f"sessions/{self._node._session_id}/players/{self._guild.id}" + (f"?{query}" if query else "")
        return await self._node.send(method, query=uri, data=data)
        
    async def _update_state(self, data: dict) -> None:
        """Updates the player's state based on the provided data."""
        state: dict = data.get("state")
        self._last_update = time.time() * 1000
        self._is_connected = state.get("connected")
        self._last_position = state.get("position")
        self._ping = state.get("ping")
        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) update state with data {data}")

        if self.is_ipc_connected:
            await self.send_ws({
                "op": "playerUpdate",
                "lastUpdate": self._last_update,
                "isConnected": self._is_connected,
                "lastPosition": self._last_position
            })

    async def _dispatch_voice_update(self, voice_data: Dict[str, Any] = None):
        """Dispatches a voice update to the node."""
        if {"sessionId", "event"} != self._voice_state.keys():
            self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) dispatched voice update failed {voice_data}")
            return

        state = voice_data or self._voice_state

        data = {
            "token": state['event']['token'],
            "endpoint": state['event']['endpoint'],
            "sessionId": state['sessionId'],
            "channelId": str(self.channel.id),
        }
        
        await self.send(method=RequestMethod.PATCH, data={"voice": data})
        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) dispatched voice update to {state['event']['endpoint']} with data {data}")

    async def on_voice_server_update(self, data: dict):
        """Handles a voice server update event."""
        self._voice_state.update({"event": data})
        await self._dispatch_voice_update(self._voice_state)

    async def on_voice_state_update(self, data: dict):
        """Handles a voice state update event."""
        self._voice_state.update({"sessionId": data.get("session_id")})

        if not (channel_id := data.get("channel_id")):
            await self.teardown()
            self._voice_state.clear()
            return

        self.channel = self.guild.get_channel(int(channel_id))

        if not data.get("token"):
            return

        await self._dispatch_voice_update({**self._voice_state, "event": data})

    async def _dispatch_event(self, data: dict):
        """Dispatches an event based on the type of event data received."""
        event_type = data.get("type")
        event: VoicelinkEvent = getattr(events, event_type)(data, self)

        if isinstance(event, TrackEndEvent) and event.reason != "replaced":
            self._current = None
        
        if isinstance(event, TrackExceptionEvent) and event.exception["message"] == "This content isn’t available.":
            if self._node.yt_ratelimit:
                await self._node.yt_ratelimit.flag_active_token()

        event.dispatch(self._bot)

        if isinstance(event, TrackStartEvent):
            self._ending_track = self._current
            self._prefetch_next_direct_track()

            # A fresh successful start clears any retry budget this video
            # used up from a previous loadFailed - see _retry_direct_resolve.
            if self._current and self._current.source == "youtube" and self._current.identifier:
                self._direct_resolve_retry_counts.pop(self._current.identifier, None)

        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) dispatched event {event_type}.")

    def _prefetch_next_direct_track(self) -> None:
        """(Re)starts the background worker that sequentially pre-fetches
        every upcoming YouTube track in the queue, one at a time, so play()
        finds each one already cached instead of paying the companion/yt-dlp
        round-trip at track-start time.

        Deliberately one-at-a-time rather than firing a download per queued
        track at once: invidious-companion and the yt-dlp prefetch path both
        egress to YouTube from the same host, so a burst of concurrent
        downloads for an entire playlist would look like a scraping spike
        from a single IP - a real risk of getting that IP rate-limited or
        blocked, which would degrade invidious-companion too, not just
        yt-dlp. A strictly sequential worker never has more than one
        download in flight, however long the queue is, which reads as
        ordinary one-track-at-a-time usage no matter how far ahead it gets.

        Safe to call repeatedly (from TrackStartEvent and from add_track()):
        a no-op while a worker is already running (it re-reads the queue
        itself - see _prefetch_worker - so it'll pick up anything new on its
        own); once that run finishes, the *next* call (e.g. the next
        add_track()) starts a fresh pass over the whole queue again, but
        _prefetch_worker skips anything Node.is_direct_resolve_cached()
        already - not just to avoid a redundant download (which
        prefetch_direct_track's own cache check already made cheap), but so
        the dashboard doesn't flash "loading" for the whole queue's worth of
        already-ready tracks every single time one more song gets queued.
        """
        if not self._node:
            return

        if self._prefetch_worker_task is not None and not self._prefetch_worker_task.done():
            return

        self._prefetch_worker_task = self._bot.loop.create_task(self._prefetch_worker())

    async def _prefetch_worker(self) -> None:
        """Walks the queue's upcoming tracks front-to-back, awaiting each
        YouTube track's prefetch fully before starting the next - see
        _prefetch_next_direct_track for why this is sequential, not
        concurrent. A later add_track()/TrackStartEvent call spawns a fresh
        worker if this one has already finished.

        Re-reads the queue fresh on every iteration rather than looping over
        one snapshot taken at the start. This matters because
        _prefetch_next_direct_track() treats "a worker is already running"
        as "it'll pick up anything new on its own" and no-ops - which was
        only true if the *running* worker could actually see tracks added
        mid-pass. With a one-time snapshot it couldn't: queue 3 songs in
        quick succession while the first is still downloading, and the
        other two's add_track() calls both correctly no-op (a worker is
        running) - but that worker's snapshot was taken before they existed,
        so it finished after the first song and exited without ever
        touching the other two. They'd only get picked up whenever the next
        unrelated TrackStartEvent happened to fire. Re-reading each iteration
        means a track added mid-pass is simply still there on the next loop
        around.

        `attempted` bounds a single pass to one try per video id - without
        it, a video whose resolve just failed (prefetch_direct_track()
        doesn't cache failures) would look identical to a brand new track on
        the very next iteration and get retried forever, since the queue
        itself never changes just because a resolve failed.

        Also skips anything Node.is_direct_resolve_cached() already has
        fresh, for a related reason: without it, every fresh pass (one per
        add_track() call, once the previous pass has finished) would call
        _prefetch_and_notify() for the *entire* queue again, including
        tracks that finished prefetching long ago - prefetch_direct_track()'s
        own cache check makes that a no-download no-op either way, but
        _prefetch_and_notify unconditionally sends a "loading" IPC status
        before checking anything, so the dashboard would flash every
        already-ready track's indicator back to loading-then-ready on every
        single song added.
        """
        attempted: set[str] = set()
        while True:
            next_track = next(
                (
                    t for t in self.queue.tracks(incTrack=False)
                    if t.source == "youtube"
                    and t.identifier
                    and t.identifier not in attempted
                    and not self._node.is_direct_resolve_cached(t.identifier)
                ),
                None,
            )
            if next_track is None:
                return

            attempted.add(next_track.identifier)
            await self._prefetch_and_notify(next_track)

    async def _prefetch_and_notify(self, track: Track) -> None:
        """Resolves track's direct URL in the background and reports progress to
        the dashboard (prefetchStatus), so the UI can show a subtle indicator -
        and, while the yt-dlp download is actually running, a live progress
        fraction - on the upcoming queue item while it's being pre-loaded.
        """
        if self.is_ipc_connected:
            await self.send_ws({"op": "prefetchStatus", "trackId": track.track_id, "status": "loading", "progress": 0})

        prefetch_task = self._bot.loop.create_task(self._node.prefetch_direct_track(track.identifier, track.title))

        while not prefetch_task.done():
            await asyncio.sleep(self.PREFETCH_PROGRESS_POLL_SECONDS)
            if not self.is_ipc_connected:
                continue
            progress = await self._node.get_prefetch_progress(track.identifier)
            if progress is not None:
                await self.send_ws({
                    "op": "prefetchStatus",
                    "trackId": track.track_id,
                    "status": "loading",
                    "progress": progress,
                })

        resolved = await prefetch_task

        if self.is_ipc_connected:
            await self.send_ws({
                "op": "prefetchStatus",
                "trackId": track.track_id,
                "status": "ready" if resolved else "unavailable"
            })

        await self._report_cache_stats()

    async def _resolve_and_notify_current_track(self, track: Track) -> Optional[str]:
        """Patch 13: like _prefetch_and_notify above, but for the track
        about to play *right now* (play()'s on-demand resolve), not an
        upcoming queued one. Patch 11 made resolve_direct_track() wait for
        a full yt-dlp download on a disk-cache miss instead of streaming
        immediately - which could otherwise look like nothing was
        happening for however long that download takes, including on the
        very first song of a session. Reports the exact same
        prefetchStatus progress events used for upcoming tracks, just
        keyed by this track's own track_id, so the dashboard's Now Playing
        panel can show a "this song is being cached" indicator/progress
        bar instead of sitting silently.
        """
        if self.is_ipc_connected:
            await self.send_ws({"op": "prefetchStatus", "trackId": track.track_id, "status": "loading", "progress": 0})

        resolve_task = self._bot.loop.create_task(self._node.resolve_direct_track(track.identifier, track.title))

        while not resolve_task.done():
            await asyncio.sleep(self.PREFETCH_PROGRESS_POLL_SECONDS)
            if not self.is_ipc_connected:
                continue
            progress = await self._node.get_prefetch_progress(track.identifier)
            if progress is not None:
                await self.send_ws({
                    "op": "prefetchStatus",
                    "trackId": track.track_id,
                    "status": "loading",
                    "progress": progress,
                })

        resolved = await resolve_task

        if self.is_ipc_connected:
            await self.send_ws({
                "op": "prefetchStatus",
                "trackId": track.track_id,
                "status": "ready" if resolved else "unavailable"
            })

        return resolved

    async def _report_cache_stats(self) -> None:
        """Pushes the direct-URL resolve cache's current size to the dashboard
        (entry count + approximate bytes of resolved-URL/metadata strings -
        not audio). Called after anything that can change the cache so the
        dashboard stays roughly live; harmless if called more than strictly
        necessary since it's just a couple of small integers.
        """
        if self.is_ipc_connected and self._node:
            await self.send_ws({"op": "cacheStats", **self._node.get_direct_resolve_cache_stats()})

    async def do_next(self):
        """Processes the next track in the queue."""
        if self._current or self.is_playing or not self.channel:
            return
        
        if self._paused:
            self._paused = False

        if self._track_is_stuck:
            await asyncio.sleep(10)
            self._track_is_stuck = False

        if not self.guild.me.voice:
            await self.connect(timeout=0.0, reconnect=True)
        
        self.pause_votes.clear()
        self.resume_votes.clear()
        self.skip_votes.clear()
        self.previous_votes.clear()
        self.shuffle_votes.clear()
        self.stop_votes.clear()

        track = self.queue.get()

        if not track:
            if self.autoplay and await self.get_recommendations():
                return await self.do_next()
            if self.queue.is_empty:
                self._schedule_inactive_cleanup_timer()
        else:
            # Set before the trackLoading send below: send_ws awaits, so an
            # initPlayer handled during it must already see this track as
            # current (has_dequeued_track), matching the position it reports.
            self._loading_track = track
            if self.is_ipc_connected:
                # Patch 26: include the (already-advanced) queue position, same
                # value trackUpdate sends - without it every dashboard kept the
                # previous position for the whole caching window, showing the
                # loading song in both Now Playing and the queue and sending
                # off-by-one indexes for remove/move/skip-to.
                await self.send_ws({"op": "trackLoading", "trackId": track.track_id, "currentQueuePosition": self.queue._position, "isPaused": self._paused})

            resumed = bool(getattr(self, "_resume_start_ms", 0))
            start = getattr(self, "_resume_start_ms", 0) or track.position
            self._resume_start_ms = 0
            if not 0 <= start < track.length:
                start = track.position
            try:
                await self.play(track, start=start)
            except cantus.CantusUnavailable as e:
                # Patch 46: a Cantus song that can't be played - say why, go on.
                self._loading_track = None
                try:
                    await self.context.channel.send(f"Skipped **{track.title}**: {e}", delete_after=30)
                except Exception:
                    pass
                return await self.do_next()
            except Exception as e:
                self._logger.error(f"Something went wrong while playing music in {self.guild.name}({self.guild.id})", exc_info=e)
                await asyncio.sleep(5)
                return await self.do_next()
            finally:
                self._loading_track = None

            if not track.requester.bot:
                self._bot.loop.create_task(MongoDBHandler.update_user(track.requester.id, {
                    "$push": {"history": {"$each": [track.track_id], "$slice": -25}}
                }))

            self._bot.loop.create_task(self._record_guild_play_history(track))
            if not resumed:  # carrying on after a restart isn't a new play
                self._bot.loop.create_task(self._record_history_events("played", [track]))

        await self.invoke_controller()
        await self.update_voice_status()

        if self.is_ipc_connected:
            await self.send_ws({
                "op": "trackUpdate", 
                "currentQueuePosition": self.queue._position if track else self.queue._position + 1,
                "trackId": track.track_id if track else None,
                "isPaused": self._paused
            })

    async def _record_guild_play_history(self, track: Track) -> None:
        """Patch 9: records every track that actually starts playing, per
        guild, so the dashboard's Home/Explore pages can be built from real
        playback history instead of a hardcoded demo playlist.

        Split into two buckets by `track.source`: an `http`/`local` source
        means the track came from a Discord-message attachment (someone's
        own uploaded audio file), everything else (youtube/spotify/
        soundcloud/deezer/applemusic) is a normal searched/"bot" track.

        Keyed by `track.identifier` (falling back to `uri`/`track_id`),
        hashed so arbitrary identifier text can never collide with Mongo's
        reserved `.`/`$` characters in a field name. Stores both a capped
        recency list (for "recently played") and a running per-track play
        count (for "top played"). Best-effort and fire-and-forget: a
        failure here must never affect playback itself.
        """
        if cantus.is_private(track):
            return  # Patch 46: someone's own Cantus song isn't server history
        try:
            category = "upload" if track.source in ("http", "local") else "bot"
            raw_key = track.identifier or track.uri or track.track_id
            key = hashlib.sha1(raw_key.encode()).hexdigest()

            history_field = f"{category}History"
            count_field = f"{category}TopCounts.{key}.count"
            track_id_field = f"{category}TopCounts.{key}.trackId"
            title_field = f"{category}TopCounts.{key}.title"

            await MongoDBHandler.update_settings(self.guild.id, {
                "$push": {history_field: {"$each": [track.track_id], "$slice": -60}},
                "$inc": {count_field: 1},
                "$set": {track_id_field: track.track_id, title_field: track.title},
            }, upsert=True)
        except Exception as e:
            self._logger.debug(
                f"Failed to record guild play history for {track.title!r} in {self.guild.name}({self.guild.id})",
                exc_info=e
            )

    async def _record_history_events(self, kind: str, tracks: List[Track]) -> None:
        """Patch 45: the uncapped full history - one event per song played or
        added, with time, server and requester (None for the bot itself)."""
        try:
            now = datetime.datetime.now(datetime.timezone.utc)
            events = []
            for track in tracks:
                requester = getattr(track, "requester", None)
                is_youtube = getattr(track, "source", "") == "youtube" and getattr(track, "identifier", None)
                events.append({
                    "kind": kind,
                    "guild_id": self.guild.id,
                    "user_id": None if requester is None or getattr(requester, "bot", False) else requester.id,
                    "track_id": track.track_id,
                    "video_id": track.identifier if is_youtube else (getattr(track, "uri", None) or track.track_id),
                    "at": now,
                    "source": "live",
                    # Patch 46: only its owner's history lists a Cantus song.
                    **({"private": True} if cantus.is_private(track) else {}),
                })
            await MongoDBHandler.add_history_events(events)
        except Exception as e:
            self._logger.debug(f"Failed to record {kind} history events", exc_info=e)

    async def _record_user_added_history(self, tracks: List[Track]) -> None:
        """Patch 44: the personal "Recently added by you" list - every song
        each (non-bot) requester added, kept on their own user record so it
        follows them across servers. Capped at 100 like the guild list."""
        try:
            by_user: Dict[int, List[str]] = {}
            for track in tracks:
                requester = getattr(track, "requester", None)
                if requester is None or getattr(requester, "bot", False):
                    continue
                by_user.setdefault(requester.id, []).append(track.track_id)
            for user_id, track_ids in by_user.items():
                await MongoDBHandler.update_user(user_id, {
                    "$push": {"addedHistory": {"$each": track_ids, "$slice": -100}},
                })
        except Exception as e:
            self._logger.debug(f"Failed to record user added history for {len(tracks)} track(s)", exc_info=e)

    async def _record_guild_added_history(self, tracks: List[Track]) -> None:
        """Patch 17: records every track the moment it's *added* to the
        queue, per guild - a separate, broader list than
        _record_guild_play_history's "Recently played" (Patch 9), which
        only logs a track once it actually starts playing. A track that
        gets skipped/removed before its turn still shows up here, which is
        the point: this is a log of what people queued up, not of what
        the bot got around to actually playing.

        Deliberately not split into bot/upload buckets like Patch 9 - this
        is meant to be one simple "recently added" list regardless of
        source. Capped at 100 (a bit more headroom than play history's 60,
        since additions can churn faster than actual plays - e.g. a big
        playlist import). Best-effort and fire-and-forget: a failure here
        must never affect adding the track to the real queue, which has
        already happened by the time this runs.
        """
        try:
            # Patch 46: someone's own Cantus songs aren't server history.
            track_ids = [track.track_id for track in tracks if not cantus.is_private(track)]
            if not track_ids:
                return
            await MongoDBHandler.update_settings(self.guild.id, {
                "$push": {"addedHistory": {"$each": track_ids, "$slice": -100}},
            }, upsert=True)
        except Exception as e:
            self._logger.debug(
                f"Failed to record guild added history for {len(tracks)} track(s) in {self.guild.name}({self.guild.id})",
                exc_info=e
            )

    async def invoke_controller(self):
        """Sends or updates the music controller message in the designated channel."""
        if not self.settings.get('controller', True) or self._updating or not self.channel:
            return
        
        self._updating = True

        try:            
            embed, view = self.build_embed(self.current), InteractiveController(self)
            if not self.controller:
                if request_channel_data := self.settings.get("music_request_channel"):
                    if channel := self.bot.get_channel(request_channel_data.get("text_channel_id")):
                        try:
                            self.controller = await channel.fetch_message(request_channel_data.get("controller_msg_id"))
                            await self.controller.edit(embed=embed, view=view)
                        except errors.NotFound:
                            self.controller = None
                
                # Send a new controller message if none exists
                if not self.controller:
                    self.controller = await dispatch_message(self.context, content=embed, view=view, delete_after=None, requires_fetch=True)

            elif not await self.is_position_fresh():
                try:
                    await self.controller.delete()
                except errors.NotFound:
                    self.controller = None
                    
                self.controller = await dispatch_message(self.context, content=embed, view=view, delete_after=None, requires_fetch=True)

            else:
                await self.controller.edit(embed=embed, view=view)
        
        except errors.Forbidden:
            self._logger.warning(f"Missing permission to update the music controller on {self.guild.name}({self.guild.id})")

        except Exception as e:
            self._logger.error(f"Something went wrong while sending music controller to {self.guild.name}({self.guild.id})", exc_info=e)
        
        finally:
            self._updating = False

    async def is_position_fresh(self):
        """Checks if the current controller message is among the most recent messages."""
        try:
            async for message in self.context.channel.history(limit=5):
                if message.id == self.controller.id:
                    return True
        except:
            pass

        return False
    
    async def teardown(self):
        """Cleans up the player and associated resources."""
        try:
            await MongoDBHandler.update_settings(self.guild.id, {"$set": {
                "last_active": (timeNow := round(time.time())), 
                "played_time": round(self.settings.get("played_time", 0) + ((timeNow - self.joinTime) / 60), 2)
            }})
            
            if self.is_ipc_connected:
                await self.send_ws({"op": "playerClose"})
        except:
            pass

        try:
            await self.update_voice_status(remove_status=True)
            self._cancel_inactive_cleanup_timer()
            if self.controller:
                if self.controller.id == self.settings.get("music_request_channel", {}).get("controller_msg_id"):
                    await self.controller.edit(embed=self.build_embed(), view=None)
                else: 
                    await self.controller.delete()
        except:
            pass

        try:
            await self.destroy()
        except:
            pass

    async def get_tracks(
        self,
        query: str,
        *,
        requester: Member,
        search_type: SearchType = None
    ) -> Union[List[Track], Playlist]:
        """Fetches tracks from the node's REST api to parse into Lavalink.

        You can also pass in a discord.py Context object to get a
        Context object on any track you search.
        """
        if not search_type:
            search_type = Config().search_platform
            
        return await self._node.get_tracks(query, requester=requester, search_type=search_type)

    async def connect(self, *, timeout: float, reconnect: bool, self_deaf: bool = True, self_mute: bool = False):
        """Connects the player to a voice channel."""
        await self.guild.change_voice_state(channel=self.channel, self_deaf=True, self_mute=self_mute)
        self._node._players[self.guild.id] = self
        self._is_connected = True

        if self.channel:
            self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been connected to {self.channel.name}({self.channel.id}).")
            
    async def stop(self):
        """Stops the currently playing track."""
        self._current = None
        await self.send(method=RequestMethod.PATCH, data={'encodedTrack': None})

    async def disconnect(self, *, force: bool = False):
        """Disconnects the player from voice."""
        try:
            await self.guild.change_voice_state(channel=None)
        finally:
            self.cleanup()
            self._is_connected = False
            self.channel = None
        
        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been disconnected from a voice channel.")

    async def destroy(self):
        """Disconnects and destroys the player, and runs internal cleanup."""
        
        try:
            await self.disconnect()
        except:
            # 'NoneType' has no attribute '_get_voice_client_key' raised by self.cleanup() ->
            # assume we're already disconnected and cleaned up
            assert self.channel is None and not self.is_connected
        
        self._node._players.pop(self.guild.id)
        await self.send(method=RequestMethod.DELETE)
    
    async def play(
        self,
        track: Track,
        *,
        start: int = 0,
        end: int = 0,
        ignore_if_playing: bool = False
    ) -> Track:
        """Plays a track."""
        if not self._node:
            return track

        encoded_track = track.track_id

        if track.source == "youtube" and track.identifier:
            try:
                resolved_id = await self._resolve_and_notify_current_track(track)
            except Exception as e:
                resolved_id = None
                self._logger.warning(f"[DirectResolver] unexpected error resolving {track.title}: {e}")

            if resolved_id:
                # Only the outgoing play command uses the resolved http-source
                # track. track.track_id (the youtube-encoded blob) stays
                # untouched so dashboard sync, Mongo history, and the "add to
                # favourites" flow keep the real title/artwork instead of the
                # generic metadata Lavalink probes off a raw http stream.
                encoded_track = resolved_id
            else:
                self._logger.debug(f"[DirectResolver] falling back to native youtube client for {track.title}")

            # Backfill safety net for the one path that can still skip the
            # disk cache: a fresh _direct_resolve_cache TTL hit (the `cached`
            # branch at the very top of resolve_direct_track()) returns
            # without ever touching stream_cache at all. Everything else -
            # including a cold cache miss - now downloads to disk and waits
            # for it inside resolve_direct_track() itself (Patch 11), so this
            # call is just cheap/idempotent insurance for that one case, not
            # the primary way a track gets cached any more. Still
            # fire-and-forget: never awaited, so it can't delay this play()
            # call.
            self._bot.loop.create_task(self._node.prefetch_direct_track(track.identifier, track.title))

            await self._report_cache_stats()

            # Patch 10/11: per-track loudness normalization. Since Patch 11
            # made resolve_direct_track() above wait for a full download on
            # a cache miss, the measurement StreamCache._measure_gain takes
            # right after that download finishes is now available by the
            # time we get here for almost every youtube track - including
            # the very first play of a session, not just later replays.
            # Still defaults to neutral (1.0) if resolution fell back to
            # streaming (download failed) or measurement itself failed.
            self._loudness_gain = await self._node.get_track_gain(track.identifier)
        elif track.source == cantus.SOURCE:
            # Patch 46: a fresh play link from Cantus (queued Cantus songs
            # never hold one - they expire). CantusUnavailable propagates to
            # do_next, which skips the song with a note.
            encoded_track, self._loudness_gain = await cantus.resolve_for_play(self, track)
        else:
            # Non-youtube tracks (Spotify/SoundCloud/a Discord attachment)
            # are never disk-cached, so there's nothing to measure - always
            # neutral. Must still be reset here, not left over from whatever
            # the previous track's gain was (filters persist across tracks).
            self._loudness_gain = 1.0

        data = {
            "encodedTrack": encoded_track,
            "position": str(start or 0),
            "filters": self._filters_payload_with_gain(),
            # Patch 27: restate the volume on every track start so Lavalink
            # always matches what the bot (and every dashboard) thinks it is.
            # The restart-restore path never sent it, so after a restart the
            # bot played at Lavalink's default 100 while showing the saved
            # value (e.g. 24).
            "volume": self._volume,
        }

        if end or track.end_time:
            data["endTime"] = str(end or track.end_time)

        await self.send(method=RequestMethod.PATCH, query=f"noReplace={ignore_if_playing}", data=data)
        if self._node.yt_ratelimit:
            await self._node.yt_ratelimit.handle_request()

        self._current = track

        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) playing {track.title} from uri {track.uri} with a length of {track.length}")
        return self._current

    async def _retry_direct_resolve(self, track: Track, resume_at: float) -> None:
        """Recovers a direct-resolved YouTube track from a mid-stream
        `loadFailed` (see docs/direct-url-resolver.md's "Known limitations" -
        the signed googlevideo.com url getting redirected to a different CDN
        edge, or otherwise invalidated, partway through playback) instead of
        letting it be discarded like a normal track end.

        Evicts the stale cached url/encoded-track for this video so play()
        is forced to fetch a fresh signed url from invidious-companion rather
        than handing Lavalink the same dead url again, then resumes from
        approximately where playback died. `resume_at` must be captured by
        the caller from TrackEndEvent.position (captured at event-construction
        time, before player._current gets cleared) - self.position would
        read 0 by the time this runs, since it depends on player._current
        which is already gone. It can lag the real failure point by up to
        playerUpdateInterval, since it reflects Lavalink's last periodic
        player-state push, not the exact failure instant.

        Called instead of do_next() from cogs/listeners.py's
        on_voicelink_track_end when reason == "loadFailed" and this video
        hasn't exhausted MAX_DIRECT_RESOLVE_RETRIES yet.
        """
        self._node.invalidate_direct_resolve_cache(track.identifier)

        try:
            await self.play(track, start=int(resume_at))
        except Exception as e:
            self._logger.error(
                f"[DirectResolver] retry failed for {track.title} in {self.guild.name}({self.guild.id}), skipping to next track",
                exc_info=e,
            )
            self._current = None
            await self.do_next()

    def _validate_time(self, track: Track, start_time: int, end_time: int) -> None:
        """Validates the start and end times for a track."""
        if start_time or end_time:
            if not end_time:
                end_time = track.length

            if start_time >= end_time:
                raise VoicelinkException(self.get_msg("time.invalidStartTime"))

            track_length = track.length
            if not 0 <= start_time <= track_length:
                raise VoicelinkException(self.get_msg("time.invalidStartTime", format_ms(track_length)))
            if not 0 <= end_time <= track_length:
                raise VoicelinkException(self.get_msg("time.invalidEndTime", format_ms(track_length)))

            track.position = start_time
            track.end_time = end_time

    def _cancel_inactive_cleanup_timer(self) -> None:
        """Cancels the per-player inactivity cleanup timer (if any)."""
        task: Optional[asyncio.Task] = getattr(self, "_inactive_cleanup_task", None)
        if task and not task.done():
            task.cancel()
        self._inactive_cleanup_task = None

    def _schedule_inactive_cleanup_timer(self) -> None:
        """Schedules per-player cleanup after inactivity."""
        self._cancel_inactive_cleanup_timer()
        self._alone_timer_pending = False
        seconds = Config().timer_settings.get("inactive_player_cleanup", 600)
        self._inactive_cleanup_task = self.bot.loop.create_task(
            self._inactive_cleanup_timer_worker(seconds)
        )

    def _schedule_alone_leave_timer(self) -> None:
        """Patch 44: nobody (no human) is left in the voice channel - leave
        after timer_settings.alone_leave seconds (default 10), or pause in
        24/7 mode (the cleanup worker decides). Was only the 10-minute idle
        timer, and not even that if the music was paused when people left."""
        self._cancel_inactive_cleanup_timer()
        seconds = Config().timer_settings.get("alone_leave", 10)
        self._alone_timer_pending = True
        self._inactive_cleanup_task = self.bot.loop.create_task(
            self._inactive_cleanup_timer_worker(seconds)
        )

    def _cancel_alone_leave_timer(self) -> None:
        """Someone came back before the bot left: cancel, and if the player is
        idle, go back to the normal inactivity timer."""
        if not getattr(self, "_alone_timer_pending", False):
            return
        self._alone_timer_pending = False
        self._cancel_inactive_cleanup_timer()
        if not self.is_playing and self.queue.is_empty:
            self._schedule_inactive_cleanup_timer()
        
    async def add_track(self, raw_tracks: Union[Track, List[Track]], *, start_time: int = 0, end_time: int = 0, at_front: bool = False, duplicate: bool = True) -> int:
        """Adds one or more tracks to the queue."""
        # Patch 46: a Cantus song from someone else's library (e.g. clicked in
        # a history list or a shared playlist) can only be queued by its owner.
        listed = raw_tracks if isinstance(raw_tracks, List) else [raw_tracks]
        allowed = [t for t in listed if not cantus.is_foreign(t, getattr(getattr(t, "requester", None), "id", None))]
        if not allowed:
            raise VoicelinkException(cantus.FOREIGN_NOTE)
        if len(allowed) != len(listed):
            raw_tracks = allowed
        tracks: List[Track] = []
        # Patch 40: only the current and upcoming songs count as duplicates -
        # with duplicates turned off, a song that already played (history)
        # could never be queued again, e.g. re-added from the History list.
        _duplicate_tracks = [] if self.queue._allow_duplicate and duplicate else [track.uri for track in self.queue.tracks(incTrack=self.has_dequeued_track)]
        raw_tracks = raw_tracks[0] if isinstance(raw_tracks, List) and len(raw_tracks) == 1 else raw_tracks

        try:
            if (is_list := isinstance(raw_tracks, List)):
                for track in raw_tracks:
                    if track.uri in _duplicate_tracks:
                        continue

                    self._validate_time(track, start_time, end_time)
                    # Patch 34: "play next" with several songs keeps their
                    # order (put_at_front() each one reversed them).
                    self.queue.put_at_index(len(tracks) + 1, track) if at_front else self.queue.put(track)
                    tracks.append(track)
                    _duplicate_tracks.append(track.uri)
            else:
                if raw_tracks.uri in _duplicate_tracks:
                    raise DuplicateTrack(self.get_msg("queue.errors.duplicateTrack"))
                
                self._validate_time(raw_tracks, start_time, end_time)
                position = self.queue.put_at_front(raw_tracks) if at_front else self.queue.put(raw_tracks)
                tracks.append(raw_tracks)
                
        finally:
            if tracks:
                if self.channel.members and len([m for m in self.channel.members if not m.bot]) > 0:
                    self._cancel_inactive_cleanup_timer()
                if self.is_ipc_connected:
                    await self.send_ws({"op": "addTrack", "tracks": [track.track_id for track in tracks], "position": (1 if at_front else -1) if is_list else position}, tracks[0].requester)

                self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been added {len(tracks)} tracks into the queue.")

                # _prefetch_next_direct_track() normally only runs from
                # TrackStartEvent, which only has something to prefetch if the
                # next track was already queued *before* the current one
                # started. In the common case of users adding songs one at a
                # time while something is already playing, the queue is still
                # empty at that moment and the add happens afterwards - so the
                # newly-added "next" track would otherwise never get a head
                # start at all. Safe/idempotent to call again here: it just
                # re-derives the current next-track and resolve_direct_track's
                # own cache+lock dedups a redundant resolve.
                #
                # Patch 16: this used to be gated on `self.is_playing`, which
                # meant adding several songs at once to an *idle* player (the
                # common "first add" case, or a playlist import) left every
                # track after the one do_next() immediately consumes sitting
                # with zero prefetch/caching indicator at all - the worker
                # only used to start once TrackStartEvent fired for that first
                # track, which Patch 11 can now delay by however long its own
                # pre-cache download takes. Calling it unconditionally starts
                # the background worker right away regardless - it only ever
                # walks `queue.tracks(incTrack=False)` (everything *after* the
                # current position), so it can't double up with whatever
                # do_next()/play() is doing for the track about to become
                # current; worst case (nothing was playing and this is the
                # very first track) it briefly races StreamCache's own
                # download-dedup for that one video, which already collapses
                # to a single shared download - never a real duplicate.
                self._prefetch_next_direct_track()

                self._bot.loop.create_task(self._record_guild_added_history(tracks))
                self._bot.loop.create_task(self._record_user_added_history(tracks))
                self._bot.loop.create_task(self._record_history_events("added", tracks))

                return len(tracks) if is_list else position
    
    async def remove_track(self, index: int, index2: int = None, remove_target: Member = None, requester: Member = None, expected_track_id: Optional[str] = None) -> Dict[int, Track]:
        """Removes one or more tracks from the queue.

        expected_track_id (optional): the track the dashboard believes sits
        at `index` when the user clicked remove. The index is just a
        snapshot from whenever the context menu was opened - if the queue
        changed in the meantime (another add/remove, or a track finishing
        and the position advancing), it can point at a different track by
        the time this runs. Re-locate the real index by id instead of
        trusting a possibly-stale position, so this never deletes an
        unrelated track.
        """
        if expected_track_id is not None:
            upcoming = self.queue.tracks(incTrack=False)
            if not (1 <= index <= len(upcoming)) or upcoming[index - 1].track_id != expected_track_id:
                for i, track in enumerate(upcoming, start=1):
                    if track.track_id == expected_track_id:
                        index, index2 = i, i
                        break
                else:
                    return {}

        removed_tracks = self.queue.remove(index, index2, remove_target)
        if removed_tracks and self.is_ipc_connected:
            await self.send_ws({
                "op": "removeTrack",
                "indexes": list(removed_tracks.keys()),
                "firstTrackId": list(removed_tracks.values())[0].track_id
            }, requester=requester)

        return removed_tracks
    
    async def seek(self, position: float, requester: Member = None) -> float:
        """Seeks to a position in the currently playing track milliseconds"""
        if not self._current:
            raise VoicelinkException("Nothing is playing right now")
        
        if position < 0 or position > self._current.length:
            raise TrackInvalidPosition("Seek position must be between 0 and the track length")

        await self.send(method=RequestMethod.PATCH, data={"position": position})
        if self.is_ipc_connected:
            await self.send_ws({"op": "updatePosition", "position": position}, requester)
        
        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been seeking to {position}.")
        return self._position

    async def set_pause(self, pause: bool, requester: Member = None) -> bool:
        """Sets the pause state of the currently playing track."""

        self._paused = pause
        self.pause_votes.clear() if pause else self.resume_votes.clear()
        await self.send(method=RequestMethod.PATCH, data={"paused": pause})

        if self.is_ipc_connected:
            await self.send_ws({"op": "updatePause", "pause": pause}, requester)
        
        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been {'paused' if pause else 'resumed'}.")
        return self._paused

    async def set_volume(self, volume: int, requester: Member = None, persist: bool = True) -> int:
        """Sets the player's shared volume, 0-100 (clamped; Lavalink itself accepts up to 1000).

        Patch 27: also saves it as the guild's volume (unless `persist` is
        False - the Discord mute button, so unmute can restore the pre-mute
        level). Only the dashboard used to save it, so a /volume command or a
        Discord controller button change was lost on the next restart.
        """
        volume = max(0, min(100, int(volume)))  # Patch 28: one shared 0-100 bot volume
        await self.send(method=RequestMethod.PATCH, data={"volume": volume})
        self._volume = volume
        if persist:
            self.settings["volume"] = volume
            self._bot.loop.create_task(MongoDBHandler.update_settings(self.guild.id, {"$set": {"volume": volume}}))

        if self.is_ipc_connected:
            await self.send_ws({"op": "updateVolume", "volume": volume}, requester)

        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been update the volume to {volume}.")
        return self._volume

    async def shuffle(self, queue_type: str, requester: Member = None) -> None:
        """Shuffles the tracks in the specified queue or history."""
        replacement = self.queue.tracks() if queue_type == "queue" else self.queue.history()
        if len(replacement) < 3:
            raise VoicelinkException(self.get_msg('player.controls.shuffle.error'))
        
        shuffle(replacement)
        self.queue.replace(queue_type, replacement)
        self.shuffle_votes.clear()
        if self.is_ipc_connected:
            await self.send_ws({
                "op": "shuffleTrack",
                "tracks": [{"trackId": track.track_id, "requesterId": str(track.requester.id)} for track in replacement],
                "queue_type": queue_type
            }, requester)
        
        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been shuffled the queue.")

    async def swap_track(self, index1: int, index2: int, requester: Member = None) -> Tuple[Track, Track]:
       """Swaps two tracks in the queue at the specified indices."""
       track1, track2 = self.queue.swap(index1, index2)
       if self.is_ipc_connected:
           await self.send_ws({
                "op": "swapTrack",
                "index1": {"index": index1, "trackId": track1.track_id},
                "index2": {"index": index2, "trackId": track2.track_id}
            }, requester)
       return track1, track2

    async def move_track(self, index: int, new_index: int, requester: Member = None) -> Optional[Track]:
        """Moves a track from its current position to a new position in the queue."""
        moved_track = self.queue.move(index, new_index)

        if self.is_ipc_connected:
            await self.send_ws({"op": "moveTrack", "movedTrack": {"index": index, "trackId": moved_track.track_id}, "newIndex": new_index}, requester)

        return moved_track
    
    async def set_repeat(self, mode: LoopType = None, requester: Member = None) -> LoopType:
        """Sets the repeat mode for the queue."""
        if not mode:
            mode = self.queue._repeat.next()
        
        if not isinstance(mode, LoopType):
            raise VoicelinkException("Invalid repeat mode.")
        
        self.queue._repeat.set_mode(mode)
        
        if self.is_ipc_connected:
            await self.send_ws({"op": "repeatTrack", "repeatMode": mode.name.lower()}, requester)

        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been update the repeat mode to {mode.name.lower()}.")
        return mode
    
    def _filters_payload_with_gain(self) -> Dict[str, Any]:
        """Patch 10: the user's active Effects payload, plus the current
        track's loudness-normalization gain as a bare `"volume"` key
        (Lavalink's native filter spec - a flat gain multiplier, not nested
        under its own key like every other filter here).

        Always includes "volume" explicitly, even when it's 1.0 (unchanged):
        Lavalink filters are a property of the player session, not the
        track, and persist as-is across `play()` calls (a PATCH only
        changes the keys you send) - so every filters PATCH must restate
        the current gain, or a previous track's boost/cut would silently
        keep applying to a track that was never measured.
        """
        return {**self._filters.get_all_payloads(), "volume": self._loudness_gain}

    async def add_filter(self, filter: Filter, requester: Member = None, fast_apply: bool = False) -> Filters:
        """Adds a filter to the player's audio stream."""
        try:
            self._filters.add_filter(filter=filter)
        except FilterTagAlreadyInUse:
            raise FilterTagAlreadyInUse(self.get_msg("effects.tagInUse"))

        payload = self._filters_payload_with_gain()
        await self.send(method=RequestMethod.PATCH, data={"filters": payload})
        if fast_apply:
            await self.seek(self.position)
        
        if self.is_ipc_connected:
            await self.send_ws({
                "op": "updateFilter",
                "filter": {"tag": filter.tag, "scope": filter.scope, "payload": filter.payload},
                "type": "add"
            }, requester)

        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been applied a {filter.tag} filter.")
        return self._filters

    async def clear_queue(self, queue_type: str, requester: Member = None) -> None:
        """Clears the queue or the history of tracks."""
        queue_type = queue_type.lower()
        if queue_type == 'history':
            # Patch 34: has_dequeued_track, not is_playing - while a song is
            # still caching it's already off the queue but not "playing" yet,
            # and clearing the history used to delete it too.
            self.queue.history_clear(self.has_dequeued_track)
        elif queue_type == "queue":
            self.queue.clear()
        
        if self.is_ipc_connected:
            await self.send_ws({
                "op": "clearQueue",
                "queueType": queue_type
            }, requester)

    async def remove_filter(self, filter_tag: str, requester: Member = None, fast_apply: bool = False) -> Filters:
        self._filters.remove_filter(filter_tag=filter_tag)
        payload = self._filters_payload_with_gain()
        await self.send(method=RequestMethod.PATCH, data={"filters": payload})
        if fast_apply:
            await self.seek(self.position)
        
        if self.is_ipc_connected:
            await self.send_ws({
                "op": "updateFilter",
                "filter": {"tag": filter_tag},
                "type": "remove"
            }, requester)

        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been removed a {filter_tag} filter.")
        return self._filters
    
    async def reset_filter(self, *, requester: Member = None, fast_apply=False) -> None:
        """Resets all filters applied to the player's audio stream."""
        if not self._filters:
            raise FilterInvalidArgument("You must have filters applied first in order to use this method.")
        
        self._filters.reset_filters()
        # Patch 10: resetting the user's Effects must not also silence the
        # (separate, system-managed) loudness-normalization gain - still
        # restate it explicitly rather than sending an empty filters object.
        await self.send(method=RequestMethod.PATCH, data={"filters": {"volume": self._loudness_gain}})
        if fast_apply:
            await self.seek(self.position)

        if self.is_ipc_connected:
            await self.send_ws({
                "op": "updateFilter",
                "type": "reset"
            }, requester)

        self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been removed all filters.")

    async def change_node(self, identifier: str = None) -> None:
        """Changes the audio processing node for the guild.."""
        try:
            node = NodePool.get_node(identifier=identifier)
        except:
            return await self.teardown()

        self._node._players.pop(self.guild.id)
        self._node = node
        self._node._players[self.guild.id] = self

        await self._dispatch_voice_update(self._voice_state)

        if self.current:
            await self.play(self.current, start=self.position)
            self._last_update = time.time() * 1000

            if self.is_paused:
                await self.set_pause(True)
    
    async def get_recommendations(self, *, track: Optional[Track] = None) -> bool:
        """Fetches and adds recommended tracks based on the provided track or recent history."""
        if not track:
            try:
                track = choice(self.queue.history(incTrack=True)[-5:])
            except IndexError:
                return False

        tracks = await track.get_recommendations(self._node)
        if tracks:
            await self.add_track(tracks, duplicate=False)
            
            self._logger.debug(f"Player in {self.guild.name}({self.guild.id}) has been requested recommendations.")
            return True
        return False
    
    async def update_voice_status(self, remove_status: bool = False) -> None:
        """Updates the voice status of the channel based on the specified template."""
        template = self.settings.get("stage_announce_template", Config().voice_status_template)
        if not template or not self.channel:
            return
        
        try:
            rv = {key: func() if callable(func) else func for key, func in self._ph.variables.items()}
            status = None if remove_status else self._ph.replace(text=template, variables=rv)
            # if self.channel.status != status:
            if self.channel.type == ChannelType.voice:
                await self.channel.edit(status=status)

        except Exception as e:
            self._logger.error(
                f"Failed to update voice status in channel '{self.channel.name}' "
                f"({self.channel.id}) for guild '{self.channel.guild}' "
                f"({self.channel.guild.id})", 
                exc_info=e
            )

    async def send_ws(self, payload, requester: Member = None):
        """Sends a WebSocket payload to the bot's IPC (Inter-Process Communication) system."""
        payload['guildId'] = str(self.guild.id)
        if requester:
            payload['requesterId'] = str(requester.id)
        await self._ipc_client.send(payload)

    async def _inactive_cleanup_timer_worker(self, seconds: int) -> None:
        try:
            await asyncio.sleep(seconds)
            if not self._guild or not self._node:
                return
            if self._guild.id not in self._node._players:
                return

            self._alone_timer_pending = False
            members = self.channel.members if self.channel else []
            # Patch 44: only people count - two bots (e.g. production and
            # staging) left together in a channel are still "alone".
            has_non_bot = any(not m.bot for m in members)
            # Empty channel: always pause/teardown. If people remain: only when idle (not playing + empty queue).
            if has_non_bot and (self.is_playing or not self.queue.is_empty):
                return

            self._inactive_cleanup_task = None

            if self.settings.get("24/7", False):
                if not self.is_paused:
                    await self.set_pause(True)
            else:
                await self.teardown()
        except asyncio.CancelledError:
            return
        except Exception as e:
            # Logger is best-effort; timer cleanup should never crash the loop.
            try:
                guild_id = self._guild.id if self._guild else "unknown"
            except Exception:
                guild_id = "unknown"
            if self._logger:
                self._logger.error(
                    f"Inactive cleanup timer failed for guild {guild_id}",
                    exc_info=e
                )