"""Cantus Flaccus: play a user's own songs and playlists from their Cantus
library (vocard-custom issue #1; Patch 46).

Configured in settings.json -> "cantus": {"url": ..., "app_key": ...} (the
key comes from a Cantus admin: Admin -> Apps -> New app). Without both, the
`/cantus` commands just say Cantus isn't set up. Every API call sends the
app key plus `X-Cantus-Discord-User: <discord user id>`; Cantus answers for
that Discord user's linked account (409 "not_linked" if there isn't one).
Users link by pairing (`/cantus link` -> a Cantus page where they press
Allow), so Vocard never stores a Cantus credential of its own per user.

A Cantus song is queued as a Track whose encoded id we build ourselves, with
the song's real title/artists/length/cover (Lavalink's http source knows
none of them) and `identifier = "cantus:<discord user id>:<song id>"` - the
Discord user whose library it came from. It never holds a play link: those
are signed and expire after 12 hours, which would break a song that waits in
the queue, is resumed after a restart or is replayed from history. Instead
Player.play() calls `resolve_for_play()` right before the song starts, which
asks Cantus for a link (reusing one from the last few hours) and has
Lavalink load it.

The app key and play links are credentials: neither is ever logged.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Dict, Optional, Tuple
from urllib.parse import quote

import aiohttp

from .config import Config
from .enums import RequestMethod
from .objects import Track
from .transformer import encode

logger = logging.getLogger("vocard.cantus")

API = "/cantus/api/v1"
SOURCE = "cantus"
# Links last 12h; reuse one for at most 6h so a reused link always has hours
# left - long enough for any song, seeking included.
LINK_REUSE_SECONDS = 6 * 3600

_session: Optional[aiohttp.ClientSession] = None
_links: Dict[Tuple[int, str], Tuple[float, str]] = {}
_gains: Dict[str, float] = {}
_measuring: set = set()


class CantusError(Exception):
    """A Cantus API error. `status` is the HTTP status (0: unreachable or not
    configured); `code` is Cantus's {"error": ...} text."""

    def __init__(self, status: int, code: str = "") -> None:
        super().__init__(code or f"Cantus answered {status}")
        self.status, self.code = status, code

    @property
    def not_linked(self) -> bool:
        return self.status == 409


class CantusUnavailable(Exception):
    """A queued Cantus song can't be played right now; the player skips it
    with this message as the note."""


def _settings() -> Dict[str, Any]:
    try:
        return Config().cantus or {}
    except Exception:
        return {}


def base_url() -> str:
    return str(_settings().get("url") or "").rstrip("/")


def is_configured() -> bool:
    return bool(base_url() and _settings().get("app_key"))


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _session


async def api(method: str, path: str, discord_id: Optional[int] = None, *, params: Optional[Dict[str, Any]] = None, json: Any = None) -> Dict[str, Any]:
    """Calls `<url>/cantus/api/v1<path>` as the bot, on behalf of `discord_id`."""
    if not is_configured():
        raise CantusError(0, "not_configured")
    headers = {"Authorization": f"Bearer {_settings()['app_key']}"}
    if discord_id is not None:
        headers["X-Cantus-Discord-User"] = str(discord_id)
    try:
        async with _get_session().request(method, f"{base_url()}{API}{path}", params=params, json=json, headers=headers) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            if resp.status >= 300:
                raise CantusError(resp.status, data.get("error", "") if isinstance(data, dict) else "")
            return data if isinstance(data, dict) else {}
    except CantusError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError) as e:
        raise CantusError(0, f"unreachable ({type(e).__name__})")


# --- linking (pairing) -----------------------------------------------------

async def start_link(discord_id: int, discord_name: str) -> Dict[str, Any]:
    """{code, url, expires}: the page the user opens to press Allow."""
    data = await api("POST", "/link/start", json={"discord_id": str(discord_id), "discord_name": discord_name})
    if data.get("url"):
        data["url"] = _full_url(data["url"])
    return data


async def link_status(code: str) -> Dict[str, Any]:
    """{status: "pending"|"linked"|"expired", username?}"""
    return await api("GET", f"/link/{quote(code, safe='')}")


async def wait_for_link(code: str, timeout: float = 600, interval: float = 3) -> Dict[str, Any]:
    """Polls until the user pressed Allow, the code expired or `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            status = await link_status(code)
        except CantusError as e:
            if e.status == 404:
                return {"status": "expired"}
            status = {"status": "pending"}  # a hiccup: keep waiting
        if status.get("status") in ("linked", "expired"):
            return status
        await asyncio.sleep(interval)
    return {"status": "expired"}


async def unlink(discord_id: int) -> None:
    _forget_links_of(discord_id)
    await api("DELETE", "/link", discord_id)


def _forget_links_of(discord_id: int) -> None:
    for key in [k for k in _links if k[0] == discord_id]:
        _links.pop(key, None)


# --- library ---------------------------------------------------------------

async def me(discord_id: int) -> Dict[str, Any]:
    return await api("GET", "/me", discord_id)


async def search_songs(discord_id: int, query: str = "", limit: int = 25, offset: int = 0) -> Dict[str, Any]:
    params: Dict[str, Any] = {"limit": max(1, min(int(limit), 100))}
    if query:
        params["q"] = query
    if offset:
        params["offset"] = int(offset)
    return await api("GET", "/songs", discord_id, params=params)


async def playlists(discord_id: int) -> list:
    return (await api("GET", "/playlists", discord_id)).get("playlists", [])


async def playlist(discord_id: int, playlist_id: str) -> Dict[str, Any]:
    return await api("GET", f"/playlists/{quote(playlist_id, safe='')}", discord_id)


# --- tracks ----------------------------------------------------------------

def choice_label(text: str) -> str:
    """Discord allows 100 characters in an autocomplete choice."""
    return text if len(text) <= 100 else text[:99] + "…"


def song_label(song: Dict[str, Any]) -> str:
    label = song.get("title") or "Unknown"
    if song.get("artists"):
        label = f"{label} - {song['artists']}"
    return choice_label(label)


def build_track(song: Dict[str, Any], discord_id: int, requester) -> Track:
    """A queueable Track for a song from `discord_id`'s linked library (no
    play link inside - see the module docstring)."""
    info = {
        "title": song.get("title") or "Unknown",
        "author": song.get("artists") or "Unknown",
        "length": int(song.get("duration_ms") or 0),
        "identifier": f"{SOURCE}:{int(discord_id)}:{song['id']}",
        "isStream": False,
        "uri": f"{base_url()}/?song={quote(str(song['id']))}",
        "artworkUrl": song.get("cover"),
        "isrc": song.get("isrc"),
        "sourceName": SOURCE,
        "position": 0,
    }
    return Track(track_id=encode(info), info={**info, "isSeekable": True}, requester=requester)


def parse_identifier(identifier: str) -> Optional[Tuple[int, str]]:
    """"cantus:<discord user id>:<song id>" -> (discord user id, song id)."""
    parts = (identifier or "").split(":", 2)
    if len(parts) != 3 or parts[0] != SOURCE or not parts[2]:
        return None
    try:
        return int(parts[1]), parts[2]
    except ValueError:
        return None


def _full_url(url: str) -> str:
    return url if str(url).startswith("http") else f"{base_url()}{url}"


async def song_by_id(discord_id: int, song_id: str) -> Optional[Dict[str, Any]]:
    """One of `discord_id`'s songs by id (None if it isn't theirs). Asks for
    its play link, the only per-song endpoint, and keeps the link for play()."""
    try:
        data = await api("GET", f"/songs/{quote(song_id, safe='')}/stream", discord_id)
    except CantusError as e:
        if e.status == 404:
            return None
        raise
    song = data.get("song") or {}
    if not song.get("id"):
        return None
    if data.get("url"):
        _links[(discord_id, str(song["id"]))] = (time.monotonic(), _full_url(data["url"]))
    return song


async def stream_url(discord_id: int, song_id: str, *, fresh: bool = False) -> str:
    """A full play link for one of `discord_id`'s songs (reused for up to 6h)."""
    key = (discord_id, song_id)
    cached = _links.get(key)
    if cached and not fresh and time.monotonic() - cached[0] < LINK_REUSE_SECONDS:
        return cached[1]
    data = await api("GET", f"/songs/{quote(song_id, safe='')}/stream", discord_id)
    if not data.get("url"):
        raise CantusError(404, "no play link")
    url = _full_url(data["url"])
    _links[key] = (time.monotonic(), url)
    return url


def _accounts_to_try(track: Track, guild) -> list:
    """Whose library to play from: the requester's first (their own song,
    or a friend's song that's in their library too), then the song's owner
    - only while they're in this server (e.g. someone replays the owner's
    song from this server's history)."""
    owner, _ = parse_identifier(track.identifier)
    order = []
    requester = getattr(track, "requester", None)
    if requester is not None and not getattr(requester, "bot", False):
        order.append(requester.id)
    if owner not in order and (guild is None or guild.get_member(owner) is not None):
        order.append(owner)
    return order


async def resolve_for_play(player, track: Track) -> str:
    """Player.play()'s hook: a Lavalink track for a queued Cantus song.
    Raises CantusUnavailable (with the reason) if it can't be played."""
    parsed = parse_identifier(track.identifier)
    if not parsed:
        raise CantusUnavailable("it isn't a valid Cantus song.")
    if not is_configured():
        raise CantusUnavailable("Cantus isn't set up on this bot.")
    _, song_id = parsed
    reason = "its owner isn't in this server."
    for discord_id in _accounts_to_try(track, getattr(player, "guild", None)):
        for attempt in range(2):
            try:
                url = await stream_url(discord_id, song_id, fresh=attempt > 0)
            except CantusError as e:
                if e.not_linked:
                    reason = "it needs a linked Cantus account (`/cantus link`)."
                elif e.status in (401, 403):
                    reason = "Cantus refused the bot (its app key may have been revoked)."
                elif e.status == 404:
                    reason = "it's no longer in the Cantus library."
                else:
                    reason = "Cantus didn't answer."
                break
            try:
                response = await player._node.send(RequestMethod.GET, f"loadtracks?identifier={quote(url, safe='')}")
            except Exception:
                response = {}
            if (response or {}).get("loadType") == "track":
                player._bot.loop.create_task(_measure(song_id, url))
                return response["data"]["encoded"]
            # A reused link that stopped working: try once with a new one.
            _links.pop((discord_id, song_id), None)
            reason = "its file couldn't be loaded."
    raise CantusUnavailable(reason)


# --- loudness --------------------------------------------------------------

class _Source:
    """Lets StreamCache._measure_gain read a URL; `name` is what it logs, so
    the signed link never reaches the log."""

    def __init__(self, url: str, name: str) -> None:
        self._url, self.name = url, name

    def __str__(self) -> str:
        return self._url


async def gain(song_id: str) -> float:
    """The song's measured loudness gain, else the stream cache's typical
    gain (Patch 35), else 1.0 - the same normalization as YouTube songs."""
    if song_id in _gains:
        return _gains[song_id]
    try:
        from . import stream_cache
        typical = await stream_cache.get_typical_gain()
    except Exception:
        typical = None
    return typical if typical is not None else 1.0


async def _measure(song_id: str, url: str) -> None:
    """Measures a song once (in the background, while it plays), so every
    later play of it is normalized exactly."""
    if song_id in _gains or song_id in _measuring:
        return
    _measuring.add(song_id)
    try:
        from .stream_cache import StreamCache
        from .mongodb import MongoDBHandler
        measured = await StreamCache._measure_gain(_Source(url, f"cantus:{song_id}"), low_priority=True)
        if measured is not None:
            _gains[song_id] = measured
            await MongoDBHandler.set_cantus_gain(song_id, measured)
    except Exception as e:
        logger.debug(f"[Cantus] loudness measurement failed for {song_id}: {type(e).__name__}")
    finally:
        _measuring.discard(song_id)


async def load_gains() -> None:
    """Measured gains survive restarts (Mongo `CantusGains`)."""
    try:
        from .mongodb import MongoDBHandler
        _gains.update(await MongoDBHandler.get_cantus_gains())
    except Exception:
        pass
