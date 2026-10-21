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

import asyncio
import os
import re
import time
import aiohttp
import logging

from discord import Client, Member
from discord.ext.commands import Bot
from typing import Dict, Optional, Union, List, Any, TYPE_CHECKING
from urllib.parse import quote

from . import (
    __version__
)

from .enums import SearchType, NodeAlgorithm
from .exceptions import (
    NodeConnectionFailure,
    NodeCreationError,
    NodeException,
    NodeNotAvailable,
    NoNodesAvailable,
    TrackLoadError
)
from .objects import Playlist, Track
from .utils import ExponentialBackoff, NodeStats, NodeInfo, Ping
from .enums import RequestMethod
from .ratelimit import YTRatelimit, YTToken, STRATEGY
from .config import Config

if TYPE_CHECKING:
    from .player import Player

URL_REGEX = re.compile(
    r"https?://(?:www\.)?.+"
)

NODE_VERSION = "v4"

class Node:
    """The base class for a node. 
       This node object represents a Lavalink node.
    """

    def __init__(
        self,
        *,
        pool,
        bot: Bot,
        host: str,
        port: int,
        password: str,
        identifier: str,
        secure: bool = False,
        heartbeat: int = 30,
        yt_ratelimit: dict = None,
        session: Optional[aiohttp.ClientSession] = None,
        resume_key: Optional[str] = None,
        logger: Optional[logging.Logger] = None
    ):
        self._bot: Bot = bot
        self._host: str = host
        self._port: int = port
        self._pool: NodePool = pool
        self._password: str = password
        self._identifier: str = identifier
        self._heartbeat: int = heartbeat
        self._secure: bool = secure
        self._logger: Optional[logging.Logger] = logger
        self._stats: Optional[NodeStats] = None

        self._websocket_uri: str = f"{'wss' if self._secure else 'ws'}://{self._host}:{self._port}/" + NODE_VERSION + "/websocket"
        self._rest_uri: str = f"{'https' if self._secure else 'http'}://{self._host}:{self._port}"

        self._session: aiohttp.ClientSession = session or aiohttp.ClientSession()
        self._websocket: aiohttp.ClientWebSocketResponse = None
        self._task: asyncio.Task = None

        self.resume_key: str = resume_key or str(os.urandom(8).hex())
        self._session_id: str = None
        self._available: bool = None

        self._headers: Dict[str, str] = {
            "Authorization": self._password,
            "User-Id": str(bot.user.id),
            "Client-Name": f"Voicelink/{__version__}",
            'Resume-Key': self.resume_key
        }

        self._players: Dict[int, Player] = {}
        self._info: Optional[NodeInfo] = None
        
        self.yt_ratelimit: Optional[YTRatelimit] = STRATEGY.get(yt_ratelimit.get("strategy"))(self, yt_ratelimit) if yt_ratelimit and yt_ratelimit.get("tokens") else None

        self._bot.add_listener(self._update_handler, "on_socket_response")

        # video_id -> (resolved_at, encoded_track). Populated by prefetch_direct_track()
        # while the previous track is still playing, so play() can skip straight to a
        # cache hit instead of paying the companion + loadtracks round-trip at track start.
        self._direct_resolve_cache: Dict[str, tuple] = {}
        self._direct_resolve_locks: Dict[str, asyncio.Lock] = {}
        self._DIRECT_RESOLVE_CACHE_TTL: int = 1800

    def __repr__(self):
        return (
            f"<Voicelink.node ws_uri={self._websocket_uri} rest_uri={self._rest_uri} "
            f"player_count={len(self._players)}>"
        )
    
    def get_player(self, guild_id: int) -> Optional[Player]:
        """Takes a guild ID as a parameter. Returns a voicelink Player object."""
        return self._players.get(guild_id, None)
    
    @property
    def is_connected(self) -> bool:
        """"Property which returns whether this node is connected or not"""
        return self._websocket is not None and not self._websocket.closed


    @property
    def stats(self) -> NodeStats:
        """Property which returns the node stats."""
        return self._stats

    @property
    def players(self) -> Dict[int, Player]:
        """Property which returns a dict containing the guild ID and the player object."""
        return self._players

    @property
    def bot(self) -> Bot:
        """Property which returns the discord.py client linked to this node"""
        return self._bot

    @property
    def player_count(self) -> int:
        """Property which returns how many players are connected to this node"""
        return len(self.players)

    @property
    def pool(self) -> NodePool:
        """Property which returns the pool this node is apart of"""
        return self._pool

    @property
    def latency(self) -> float:
        """Property which returns the latency of the node"""
        return Ping(self._host, port=self._port).get_ping()

    async def _update_handler(self, data: dict) -> None:
        await self._bot.wait_until_ready()

        if not data:
            return

        if data["t"] == "VOICE_SERVER_UPDATE":
            guild_id = int(data["d"]["guild_id"])
            try:
                player = self._players[guild_id]
                await player.on_voice_server_update(data["d"])
            except KeyError:
                return

        elif data["t"] == "VOICE_STATE_UPDATE":
            if int(data["d"]["user_id"]) != self._bot.user.id:
                return

            guild_id = int(data["d"]["guild_id"])
            try:
                player = self._players[guild_id]
                await player.on_voice_state_update(data["d"])
            except KeyError:
                return
            
    async def _listen(self) -> None:
        backoff = ExponentialBackoff(base=7)

        while True:
            try:
                msg = await self._websocket.receive()

                if msg.type == aiohttp.WSMsgType.CLOSED:
                    self._available = False
                    self._logger.warning(f"WebSocket closed for node [{self._identifier}]")
                    break

                elif msg.type == aiohttp.WSMsgType.ERROR:
                    self._logger.error(f"WebSocket error for node [{self._identifier}]")
                    break
                
                self._bot.loop.create_task(self._handle_payload(msg.json()))

            except aiohttp.ClientConnectionError as e:
                self._logger.error(f"Connection error: {e}")
                self._available = False
                break
            
            except Exception as e:
                self._logger.exception(f"Unexpected error: {e}")
                self._available = False
                break

        while not self._available:
            retry = backoff.delay()
            self._logger.info(f"Trying to reconnect node [{self._identifier}] in {round(retry)}s")
            await asyncio.sleep(retry)
            try:
                await self.connect()
            except Exception as e:
                self._logger.error(f"Reconnection failed: {e}")

    async def _handle_payload(self, data: dict) -> None:
        op = data.get("op", None)
        if not op:
            return

        if op == "ready":
            self._session_id = data.get("sessionId")

        if op == "stats":
            self._stats = NodeStats(data)
            return

        if "guildId" in data:
            if not (player := self._players.get(int(data["guildId"]))):
                return

        if op == "event":
            await player._dispatch_event(data)
        elif op == "playerUpdate":
            await player._update_state(data)

    async def send(self, method: RequestMethod, query: str, data: Union[dict, str] = {}) -> dict:
        if not self._available:
            raise NodeNotAvailable(f"The node '{self._identifier}' is unavailable.")
        
        uri: str = f"{self._rest_uri}/{NODE_VERSION}/{query}"
        async with self._session.request(
            method=method.value,
            url=uri,
            headers={"Authorization": self._password},
            json=data
        ) as resp:
            if resp.status >= 300:
                raise NodeException(f"Getting errors from Lavalink REST api")
            
            if method == RequestMethod.DELETE:
                return await resp.json(content_type=None)

            return await resp.json()

    async def connect(self) -> Node:
        """Initiates a connection with a Lavalink node and adds it to the node pool."""

        try:
            if self._available:
                self._logger.info(f"Node [{self._identifier}] already connected.")
                return
            
            self._websocket = await self._session.ws_connect(
                self._websocket_uri, headers=self._headers, heartbeat=self._heartbeat
            )

            self._task = self._bot.loop.create_task(self._listen())
            self._available = True
            self._info = NodeInfo(await self.send(RequestMethod.GET, query="info"))
            
            self._logger.info(f"Node [{self._identifier}] is connected!")
        
        except aiohttp.ClientConnectorError:
            raise NodeConnectionFailure(
                f"The connection to node '{self._identifier}' failed."
            )
        except aiohttp.WSServerHandshakeError:
            raise NodeConnectionFailure(
                f"The password for node '{self._identifier}' is invalid."
            )
        except aiohttp.InvalidURL:
            raise NodeConnectionFailure(
                f"The URI for node '{self._identifier}' is invalid."
            )
        
        if self.players:
            await self.reconnect()

        return self
              
    async def disconnect(self, remove_from_pool: bool = False) -> None:
        """Disconnects a connected Lavalink node and removes it from the node pool.
           This also destroys any players connected to the node.
        """
        for player in self.players.copy().values():
            await player.teardown()
        
        await self._websocket.close()
        if remove_from_pool:
            del self._pool._nodes[self._identifier]
        self._available = False
        self._task.cancel()
        
        self._logger.info(f"Node [{self._identifier}] is disconnected!")

    async def reconnect(self) -> None:
        await asyncio.sleep(10)
        for player in self.players.copy().values():
            await asyncio.sleep(3)
            try:
                if player._voice_state:
                    await player._dispatch_voice_update(player._voice_state)

                if player.current:
                    await player.play(track=player.current, start=min(player._last_position, player.current.length))

                    if player.is_paused:
                        await player.set_pause(True)
            except:
                await player.teardown()
            await asyncio.sleep(2)

    async def build_track(
        self,
        identifier: str,
        requester: Member = None
    ) -> Track:
        """
        Builds a track using a valid track identifier

        You can also pass in a discord.py Context object to get a
        Context object on the track it builds.
        """

        data = await self.send(RequestMethod.GET, f"decodetrack?encodedTrack={identifier}")
        return Track(track_id=identifier, info=data, requester=requester)

    async def get_tracks(
        self,
        query: str,
        *,
        requester: Member,
        search_type: SearchType = None
    ) -> Union[List[Track], Playlist]:
        """
        Fetches tracks from the node's REST api to parse into Lavalink.

        You can also pass in a discord.py Context object to get a
        Context object on any track you search.
        """
        if not search_type:
            search_type = Config().search_platform
            
        if not URL_REGEX.match(query) and ':' not in query:
            query = f"{search_type}:{query}"

        response: dict[str, Any] = await self.send(RequestMethod.GET, f"loadtracks?identifier={quote(query)}")
        data = response.get("data")
        load_type = response.get("loadType")

        if not load_type:
            raise TrackLoadError("There was an error while trying to load this track.")
        
        elif load_type == "empty":
            return None

        elif load_type == "error":
            raise TrackLoadError(f"{data['message']} [{data['severity']}]")

        elif load_type in ("playlist", "recommendations"):
            return Playlist(playlist_info=data["info"], tracks=data["tracks"], requester=requester)

        elif load_type == "search":
            return [Track(track_id=track["encoded"], info=track["info"], requester=requester) for track in data]

        elif load_type == "track":
            return [Track(track_id=data["encoded"], info=data["info"], requester=requester)]
    
    async def resolve_direct_track(self, video_id: str, title: str = "") -> Optional[str]:
        """Resolves a direct, bot-detection-free stream URL for a YouTube video via
        invidious-companion, then asks Lavalink to build an http-source encoded
        track for it. Returns the encoded track id, or None if resolution failed
        (caller should fall back to the native youtube-plugin track).

        This is play()'s on-demand path - needed right now. Always checks
        stream_cache's disk cache first - a cheap, local, no-network
        dict-and-stat read - and serves that if a *finished* download is
        already sitting there, before ever touching invidious-companion.

        (Regression note, 2026-10-11: this used to skip stream_cache
        entirely and rely only on `_direct_resolve_cache` - a 30-minute
        cache of *resolved* urls, not of what's on disk - to decide whether
        a previously-prefetched video got served locally. Once that 30
        minutes passed, a video that had been fully disk-cached for hours
        would get re-resolved via invidious-companion as if it had never
        been cached at all, even though the file was sitting right there.)

        Patch 11 (2026-10-18): on a disk-cache *miss*, this now downloads
        the video via yt-dlp and *waits* for it to finish before playing,
        rather than immediately falling back to streaming - an explicit,
        requested tradeoff ("I can happily wait the song to be pre-cached
        before playing to prevent lag"), so every song plays from a fully
        local file, including the very first one of a session, not just
        ones that got a prefetch head start. This is safe to block on,
        unlike the old in-memory-buffer design this module's docstring
        describes: nothing has been handed to Lavalink yet at this point,
        so there is no partially-downloaded file for it to "outrun" - the
        failure mode that used to make blocking here dangerous no longer
        applies once downloads go to a complete, static disk file first.
        Falls through to invidious-companion's raw CDN url exactly as
        before only if the download itself fails (blocked, timed out,
        unavailable, etc.) - a broken video must still play somehow, never
        just refuse to.
        """
        cached = self._direct_resolve_cache.get(video_id)
        if cached and time.monotonic() - cached[0] < self._DIRECT_RESOLVE_CACHE_TTL:
            return cached[1]

        try:
            from . import stream_cache
            local_url = await stream_cache.get_local_url(video_id)
        except Exception:
            local_url = None

        if local_url:
            return await self._load_and_cache_direct_url(video_id, local_url)

        try:
            from . import stream_cache
            downloaded = await stream_cache.prefetch_to_disk(
                video_id, f"https://www.youtube.com/watch?v={video_id}", title, urgent=True
            )
            if downloaded:
                local_url = await stream_cache.get_local_url(video_id)
                if local_url:
                    return await self._load_and_cache_direct_url(video_id, local_url)
        except Exception as e:
            self._logger.debug(f"[DirectResolver] pre-cache download failed for {video_id}, falling back to streaming: {e}")

        lock = self._direct_resolve_locks.setdefault(video_id, asyncio.Lock())
        try:
            async with lock:
                # Re-check: another caller may have resolved it while we waited on the lock.
                cached = self._direct_resolve_cache.get(video_id)
                if cached and time.monotonic() - cached[0] < self._DIRECT_RESOLVE_CACHE_TTL:
                    return cached[1]

                from .direct_resolver import resolve_youtube_direct_url

                url = await resolve_youtube_direct_url(self._session, video_id)
                if not url:
                    self._logger.warning(f"[CacheRoute] {video_id} could not be cached or streamed")
                    return None

                # Patch 37: the last resort - should be rare since Patch 36.
                self._logger.warning(f"[CacheRoute] streaming {video_id} from YouTube (not cached; plays unmeasured at the typical gain)")
                return await self._load_and_cache_direct_url(video_id, url)
        finally:
            self._direct_resolve_locks.pop(video_id, None)

    async def _load_and_cache_direct_url(self, video_id: str, url: str) -> Optional[str]:
        """Shared by resolve_direct_track() (raw CDN url) and
        prefetch_direct_track() (local stream_cache url): asks Lavalink to
        build an http-source encoded track for `url` and caches the result
        under `video_id`. Returns the encoded track, or None if Lavalink
        rejected/failed to load it.
        """
        try:
            response: dict[str, Any] = await self.send(RequestMethod.GET, f"loadtracks?identifier={quote(url)}")
        except Exception as e:
            self._logger.warning(f"[DirectResolver] Lavalink failed to load resolved url for {video_id}: {e}")
            return None

        if response.get("loadType") != "track":
            self._logger.warning(f"[DirectResolver] Lavalink rejected resolved url for {video_id}: {response.get('loadType')}")
            return None

        encoded = response["data"]["encoded"]
        self._direct_resolve_cache[video_id] = (time.monotonic(), encoded)
        self._prune_direct_resolve_cache()
        return encoded

    def _prune_direct_resolve_cache(self) -> None:
        """Drops expired entries so the cache doesn't grow forever across every
        distinct video ever played. Called opportunistically whenever a new
        entry is written, so no separate background task is needed.
        """
        now = time.monotonic()
        expired = [vid for vid, (resolved_at, _) in self._direct_resolve_cache.items() if now - resolved_at >= self._DIRECT_RESOLVE_CACHE_TTL]
        for vid in expired:
            self._direct_resolve_cache.pop(vid, None)

    def invalidate_direct_resolve_cache(self, video_id: str) -> None:
        """Evicts a cached resolved url/encoded-track for video_id, forcing the
        next resolve_direct_track() call to fetch a fresh signed CDN url
        instead of returning the same (possibly now-dead) one. Used by
        Player._retry_direct_resolve when Lavalink reports a mid-stream
        loadFailed on an already-resolved track - e.g. the signed url got
        redirected to a different CDN edge - so a retry doesn't just get
        handed the exact url that just failed.
        """
        self._direct_resolve_cache.pop(video_id, None)

    def is_direct_resolve_cached(self, video_id: str) -> bool:
        """True if resolve_direct_track(video_id) would currently be a pure
        cache hit. Used by Player._prefetch_worker() to skip a track that's
        already fully prefetched instead of re-running _prefetch_and_notify
        (and flashing its "loading" status to the dashboard) for it every
        time the worker restarts - which happens on every add_track() call
        once the previous pass has already finished, so without this check
        the *entire* queue re-flashes loading/ready each time one song gets
        added, even though nothing is actually re-downloaded (prefetch_direct_track's
        own cache check still made it a near-instant no-op either way).
        """
        cached = self._direct_resolve_cache.get(video_id)
        return bool(cached and time.monotonic() - cached[0] < self._DIRECT_RESOLVE_CACHE_TTL)

    def get_direct_resolve_cache_stats(self) -> Dict[str, int]:
        """Lightweight stats for dashboard display only - not used for any
        correctness/eviction decision. The cache holds resolved URLs/encoded
        track strings (metadata), not audio, so this is always tiny (KB, not
        MB) and self-bounded by the TTL pruning above; there's nothing here
        that needs a size cap.
        """
        entries = self._direct_resolve_cache.values()
        approx_bytes = sum(len(encoded) for _, encoded in entries)
        return {"entries": len(self._direct_resolve_cache), "approxBytes": approx_bytes}

    async def get_track_gain(self, video_id: str) -> float:
        """Patch 10: the loudness-normalization gain multiplier StreamCache
        measured for this video (1.0 = unchanged), for Player.play() to pass
        through to Lavalink as a "volume" filter. Always 1.0 - never raises,
        never blocks on a download - if nothing's been measured yet (not
        yet disk-cached, or the ffmpeg measurement pass itself failed): a
        missing measurement should silently mean "play at the original
        volume," never "fail to play."
        """
        try:
            from . import stream_cache
            gain = await stream_cache.get_gain(video_id)
            if gain is None:
                # Patch 35: not measured (e.g. streamed after a failed
                # download) - use the cache's typical gain instead of full
                # volume, which sounded much louder than its neighbours.
                gain = await stream_cache.get_typical_gain()
                if gain is not None:
                    self._logger.debug(f"[Loudness] {video_id} unmeasured - using the typical gain {gain:.2f}")
        except Exception:
            gain = None

        return gain if gain is not None else 1.0

    async def prefetch_direct_track(self, video_id: str, title: str = "") -> Optional[str]:
        """Warmup for an upcoming track, meant to be scheduled while the
        current track is still playing so its result is already cached by the
        time play() needs it. Never raises; returns the resolved encoded track
        (or None on failure) so callers that care about the outcome - e.g. to
        report prefetch status to the dashboard - can.

        Unlike resolve_direct_track(), this uses yt-dlp to fully download the
        track's audio to local disk (voicelink/stream_cache.py) rather than
        invidious-companion - it has a real head start (the previous track is
        still playing), so there's time for a full download, not just a
        signed-url lookup. If yt-dlp fails for any reason (blocked, timed out,
        video unavailable), this returns None and leaves no cache entry -
        resolve_direct_track() then falls back to invidious-companion on demand
        exactly as if prefetching had never been attempted.

        `title` is cosmetic only (dashboard cache-manager display) - see
        StreamCache.download().
        """
        cached = self._direct_resolve_cache.get(video_id)
        if cached and time.monotonic() - cached[0] < self._DIRECT_RESOLVE_CACHE_TTL:
            return cached[1]

        try:
            from . import stream_cache

            ok = await stream_cache.prefetch_to_disk(video_id, f"https://www.youtube.com/watch?v={video_id}", title)
            if not ok:
                return None

            local_url = await stream_cache.get_local_url(video_id)
            if not local_url:
                return None

            return await self._load_and_cache_direct_url(video_id, local_url)
        except Exception as e:
            self._logger.debug(f"[DirectResolver] yt-dlp prefetch failed for {video_id}: {e}")
            return None

    async def get_prefetch_progress(self, video_id: str) -> Optional[float]:
        """Fraction (0.0-1.0) of video_id's in-flight yt-dlp prefetch download
        completed so far, for Player._prefetch_and_notify() to report to the
        dashboard. None if there's nothing in progress (not started, already
        finished, or size not yet known) - see stream_cache.StreamCache.progress.
        Never raises: swallows any error the same way prefetch_direct_track
        does, since this is purely for a UI indicator.
        """
        try:
            from . import stream_cache
            return await stream_cache.get_progress(video_id)
        except Exception:
            return None

    async def update_refresh_yt_access_token(self, token: YTToken) -> dict:
        if not self._available:
            raise NodeNotAvailable(f"The node '{self._identifier}' is unavailable.")
        
        uri: str = f"{self._rest_uri}/youtube"
        async with self._session.request(
            method="POST",
            url=uri,
            headers={"Authorization": self._password},
            json={"refreshToken": token.token}
        ) as resp:
            if resp.status >= 300:
                raise NodeException(f"Getting errors from Lavalink REST api")

class NodePool:
    """The base class for the node pool.
       This holds all the nodes that are to be used by the bot.
    """

    _nodes: Dict[str, Node] = {}

    def __repr__(self):
        return f"<Voicelink.NodePool node_count={self.node_count}>"

    @property
    def nodes(self) -> Dict[str, Node]:
        """Property which returns a dict with the node identifier and the Node object."""
        return self._nodes

    @property
    def node_count(self) -> Optional[Node]:
        return len(self._nodes.values())

    @classmethod
    def apply_settings_update(cls, guild_id: int, update: dict) -> None:
        """Patch 32: mirrors a settings update's top-level $set/$unset keys
        into that guild's live player.settings. Dotted keys (per-track
        counters like botTopCounts.<key>.count) and $push/$inc aren't
        settings a player reads, so they're skipped."""
        for node in cls._nodes.values():
            player = node._players.get(guild_id)
            if player is None:
                continue
            for key, value in (update.get("$set") or {}).items():
                if "." not in key:
                    player.settings[key] = value
            for key in (update.get("$unset") or {}):
                if "." not in key:
                    player.settings.pop(key, None)

    @classmethod
    def is_video_queued(cls, video_id: str) -> bool:
        """True if video_id is currently playing or upcoming in *any*
        connected player's queue, across every guild and every node. Used by
        voicelink/stream_cache.py's eviction (see set_queued_checker there)
        to avoid deleting a cached download that's still actually needed
        somewhere, even if it's the least-popular entry over budget.
        """
        for node in cls._nodes.values():
            for player in node.players.values():
                for track in player.queue.tracks(incTrack=True):
                    if track.source == "youtube" and track.identifier == video_id:
                        return True
        return False

    @classmethod
    def get_best_node(cls, *, algorithm: NodeAlgorithm) -> Node:
        """Fetches the best node based on an NodeAlgorithm.
         This option is preferred if you want to choose the best node
         from a multi-node setup using either the node's latency
         or the node's voice region.
         Use NodeAlgorithm.BY_PING if you want to get the best node
         based on the node's latency.
         Use NodeAlgorithm.by_region if you want to get the best node
         based on the node's voice region. This method will only work
         if you set a voice region when you create a node.
         Use NodeAlgorithm.BY_PLAYERS if you want to get the best node
         based on how players it has. This method will return a node with
         the least amount of players
        """
        available_nodes = [node for node in cls._nodes.values() if node._available]

        if not available_nodes:
            raise NoNodesAvailable("There are no nodes available.")

        if algorithm == NodeAlgorithm.BY_PING:
            tested_nodes = {node: node.latency for node in available_nodes}
            return min(tested_nodes, key=tested_nodes.get)

        elif algorithm == NodeAlgorithm.BY_PLAYERS:
            tested_nodes = {node: len(node.players.keys()) for node in available_nodes}
            return min(tested_nodes, key=tested_nodes.get)

    @classmethod
    def get_node(cls, *, identifier: str = None) -> Node:
        """Fetches a node from the node pool using it's identifier.
           If no identifier is provided, it will choose a node at random.
        """

        available_nodes = { node
            for _, node in cls._nodes.items() if node.is_connected
        }

        if identifier:
            available_nodes = { node for node in available_nodes if node._identifier == identifier }

        if not available_nodes:
            raise NoNodesAvailable("There are no nodes available.")

        nodes = {node: len(node.players.keys()) for node in available_nodes}
        return min(nodes, key=nodes.get)

    @classmethod
    async def create_node(
        cls,
        *,
        bot: Client,
        host: str,
        port: str,
        password: str,
        identifier: str,
        secure: bool = False,
        heartbeat: int = 30,
        yt_ratelimit: dict = None,
        session: Optional[aiohttp.ClientSession] = None,
        resume_key: Optional[str] = None,
        logger: Optional[logging.Logger] = None
    ) -> Node:
        """Creates a Node object to be then added into the node pool.
        """
        if identifier in cls._nodes.keys():
            raise NodeCreationError(f"A node with identifier '{identifier}' already exists.")
        
        if not logger:
            logger = logging.getLogger("vocard")
            
        node = Node(
            pool=cls, bot=bot, host=host, port=port, password=password,
            identifier=identifier, secure=secure, heartbeat=heartbeat, yt_ratelimit=yt_ratelimit,
            session=session, resume_key=resume_key, logger=logger
        )

        try:
            await node.connect()
        except Exception:
            # Issue #3: start_nodes retries, so don't leave each failed
            # try's own session open.
            if session is None:
                await node._session.close()
            raise
        cls._nodes[node._identifier] = node
        return node


# Registers the real "is this video still needed anywhere" check with
# stream_cache's eviction logic - see NodePool.is_video_queued and
# stream_cache.set_queued_checker. Safe to do at module scope: stream_cache.py
# has no dependency on this module (the callback keeps it one-directional),
# so there's no import-order/circularity concern here.
from . import stream_cache as _stream_cache
_stream_cache.set_queued_checker(NodePool.is_video_queued)

# Patch 32: apply settings changes to already-running players (see
# MongoDBHandler._settings_listeners).
from .mongodb import MongoDBHandler as _MongoDBHandler
_MongoDBHandler.add_settings_listener(NodePool.apply_settings_update)

# Patch 36: let the disk cache fall back to invidious-companion's direct link
# when yt-dlp itself is blocked, instead of the song being streamed.
from .direct_resolver import resolve_youtube_direct_url as _resolve_youtube_direct_url
_stream_cache.set_url_resolver(_resolve_youtube_direct_url)