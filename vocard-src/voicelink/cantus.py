"""Cantus Flaccus: play a user's own songs and playlists from their Cantus
library (vocard-custom issue #1; Patch 46).

**Connecting** (owner's decision, issue #1): each Discord user connects once
in the dashboard (Settings -> Cantus) with a Cantus server address and a
personal `cfa_` token (Cantus -> Account -> Connect other apps). It's stored
per Discord user in Mongo `CantusLinks`; calls go to that server with
`Authorization: Bearer <token>`. An admin can save a default server address
(Mongo `CantusConfig`, else settings.json -> "cantus" -> "url") so members
only paste their token. Revoking the token on Cantus cuts Vocard off.

**Only public servers:** because the server is whatever a user typed, every
request (and every play link handed to Lavalink) is checked to resolve only
to public addresses, so nobody can point the bot at Lavalink, Mongo or
anything else internal.

**Private libraries:** a Cantus song belongs to whoever played it. Others see
it only while it's queued or playing; it's left out of server-wide history,
top lists and Full history's "This server", and only its owner can queue it
again or save it (`is_foreign()`; callers in player.py, ipc/methods.py,
views/). Nothing from a library is kept between commands - Cantus is asked
live every time.

**Tracks:** a Cantus song is queued as a Track we encode ourselves, with the
song's real title/artists/length/cover and `identifier =
"cantus:<discord user id>:<song id>"` (whose library). It never holds a play
link: those are signed, expire after 12 hours and let anyone holding one
stream the file, so they're fetched right before each song
(`resolve_for_play()`) and handed only to Lavalink. Queue, history and
dashboards only ever see the song id.

Tokens and play links are never logged or sent to a dashboard (it only ever
sees `cfa_…<last 4>`).
"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import socket
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import quote, urlsplit

import aiohttp
from aiohttp.abc import AbstractResolver

from .config import Config
from .enums import RequestMethod
from .objects import Track
from .transformer import encode

logger = logging.getLogger("vocard.cantus")

API = "/cantus/api/v1"
SOURCE = "cantus"
FOREIGN_NOTE = "That song is from someone's own Cantus library."
# A link fetched moments ago (when the song was picked) is reused only
# briefly: links are fetched right before each song, so a removal or a
# disconnect takes effect on the next song.
LINK_REUSE_SECONDS = 60

# The default server an admin saved from the dashboard (Mongo CantusConfig);
# wins over settings.json.
_default_server: Dict[str, str] = {}

_session: Optional[aiohttp.ClientSession] = None
_links: Dict[Tuple[int, str], Tuple[float, str]] = {}
_gains: Dict[str, float] = {}
_measuring: set = set()


class CantusError(Exception):
    """A Cantus API error. `status` is the HTTP status (0: unreachable,
    400: refused by Vocard before calling); `code` names it."""

    def __init__(self, status: int, code: str = "") -> None:
        super().__init__(code or f"Cantus answered {status}")
        self.status, self.code = status, code

    @property
    def not_linked(self) -> bool:
        return self.code == "not_linked"


class CantusUnavailable(Exception):
    """A queued Cantus song can't be played right now; the player skips it
    with this message as the note."""


# --- the default server ------------------------------------------------------

def base_url() -> str:
    """The default server members connect to (admin-saved, else settings)."""
    if _default_server.get("url"):
        return _default_server["url"]
    try:
        return str((Config().cantus or {}).get("url") or "").rstrip("/")
    except Exception:
        return ""


def dashboard_url() -> str:
    """Where `/cantus link` sends people (settings.json -> cantus -> dashboard_url)."""
    try:
        return str((Config().cantus or {}).get("dashboard_url") or "").rstrip("/")
    except Exception:
        return ""


async def load_default_server() -> None:
    from .mongodb import MongoDBHandler
    try:
        saved = await MongoDBHandler.get_cantus_app() or {}
    except Exception:
        saved = {}
    _default_server.clear()
    if saved.get("url"):
        _default_server["url"] = saved["url"]


async def save_default_server(url: str) -> str:
    """Dashboard (admins): the server address members connect to."""
    server = normalize_server(url)
    await check_public_url(server)
    from .mongodb import MongoDBHandler
    await MongoDBHandler.set_cantus_app({"url": server, "saved_at": time.time()})
    _default_server["url"] = server
    return server


# --- only public addresses ---------------------------------------------------

def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


class _PublicResolver(AbstractResolver):
    """DNS for Cantus servers: refuses names that resolve to private,
    loopback, link-local or other non-public addresses (covers redirects
    and a name that changes what it points to between checks)."""

    def __init__(self) -> None:
        self._inner = aiohttp.ThreadedResolver()

    async def resolve(self, host: str, port: int = 0, family: int = socket.AF_INET) -> List[Dict[str, Any]]:
        infos = await self._inner.resolve(host, port, family)
        if not infos or not all(_is_public(i["host"]) for i in infos):
            raise OSError(f"{host} is not a public address")
        return infos

    async def close(self) -> None:
        await self._inner.close()


async def check_public_url(url: str) -> None:
    """Raises CantusError(400, "private_address") unless `url` is http(s) and
    its host resolves only to public addresses."""
    parts = urlsplit(url)
    host = parts.hostname
    if parts.scheme not in ("http", "https") or not host:
        raise CantusError(400, "bad_url")
    try:
        ipaddress.ip_address(host)
        addresses = [host]
    except ValueError:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80), type=socket.SOCK_STREAM)
        except OSError:
            raise CantusError(0, "unresolvable")
        addresses = [info[4][0] for info in infos]
    if not addresses or not all(_is_public(a) for a in addresses):
        raise CantusError(400, "private_address")


def normalize_server(raw: str) -> str:
    """"music.example.com" / "https://music.example.com/x" ->
    "https://music.example.com". Raises CantusError(400, "bad_url")."""
    raw = (raw or "").strip()
    if not raw:
        raise CantusError(400, "bad_url")
    if "://" not in raw:
        raw = f"https://{raw}"
    parts = urlsplit(raw)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise CantusError(400, "bad_url")
    try:
        port = parts.port
    except ValueError:
        raise CantusError(400, "bad_url")
    host = parts.hostname if ":" not in parts.hostname else f"[{parts.hostname}]"
    return f"{parts.scheme}://{host}{f':{port}' if port else ''}"


def _get_session() -> aiohttp.ClientSession:
    global _session
    if _session is None or _session.closed:
        _session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(resolver=_PublicResolver()),
        )
    return _session


# --- connections ---------------------------------------------------------------

async def _account(discord_id: int) -> Dict[str, Any]:
    from .mongodb import MongoDBHandler
    link = await MongoDBHandler.get_cantus_link(discord_id)
    if not link or not link.get("token") or not link.get("server"):
        raise CantusError(409, "not_linked")
    return {"server": link["server"], "token": link["token"]}


async def api(method: str, path: str, discord_id: Optional[int] = None, *, params: Optional[Dict[str, Any]] = None,
              account: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Calls `<server>/cantus/api/v1<path>` with `discord_id`'s token."""
    acct = account or await _account(discord_id)
    url = f"{acct['server']}{API}{path}"
    await check_public_url(url)
    try:
        async with _get_session().request(method, url, params=params, headers={"Authorization": f"Bearer {acct['token']}"}) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            if resp.status >= 300:
                code = "token_revoked" if resp.status == 401 else (data.get("error", "") if isinstance(data, dict) else "")
                raise CantusError(resp.status, code)
            return data if isinstance(data, dict) else {}
    except CantusError:
        raise
    except (aiohttp.ClientError, asyncio.TimeoutError, OSError) as e:
        raise CantusError(0, f"unreachable ({type(e).__name__})")


def token_hint(token: str) -> str:
    return f"cfa_…{token[-4:]}" if token else ""


async def connect_token(discord_id: int, server: str, token: str) -> Dict[str, Any]:
    """Dashboard "Save": checks the token with GET /me on that server, then
    stores it for this Discord user. Returns the account ({id, username,
    songs}). 401 -> CantusError(401, "token_revoked")."""
    server = normalize_server(server)
    token = (token or "").strip()
    if not token or len(token) > 512 or any(c.isspace() for c in token):
        raise CantusError(400, "bad_token")
    me_ = await api("GET", "/me", account={"server": server, "token": token})
    from .mongodb import MongoDBHandler
    _forget_links_of(discord_id)
    await MongoDBHandler.set_cantus_link(discord_id, {
        "server": server, "token": token, "username": me_.get("username"), "connected_at": time.time(),
    })
    return me_


async def status(discord_id: int) -> Dict[str, Any]:
    """What the dashboard shows - never the token itself."""
    from .mongodb import MongoDBHandler
    link = await MongoDBHandler.get_cantus_link(discord_id)
    result: Dict[str, Any] = {"connected": False, "defaultServer": base_url()}
    if link and link.get("token"):
        result.update(connected=True, server=link.get("server"), username=link.get("username"), tokenHint=token_hint(link["token"]))
        try:
            me_ = await me(discord_id)
            result.update(username=me_.get("username") or result["username"], songs=me_.get("songs"))
        except CantusError as e:
            result["problem"] = e.code or str(e.status)
    return result


async def disconnect(discord_id: int) -> bool:
    """Forgets this Discord user's token."""
    from .mongodb import MongoDBHandler
    _forget_links_of(discord_id)
    return await MongoDBHandler.delete_cantus_link(discord_id)


def _forget_links_of(discord_id: int) -> None:
    for key in [k for k in _links if k[0] == discord_id]:
        _links.pop(key, None)


# --- library -----------------------------------------------------------------

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


async def server_of(discord_id: int) -> str:
    return (await _account(discord_id))["server"]


# --- tracks and privacy --------------------------------------------------------

def choice_label(text: str) -> str:
    """Discord allows 100 characters in an autocomplete choice."""
    return text if len(text) <= 100 else text[:99] + "…"


def song_label(song: Dict[str, Any]) -> str:
    label = song.get("title") or "Unknown"
    if song.get("artists"):
        label = f"{label} - {song['artists']}"
    return choice_label(label)


def build_track(song: Dict[str, Any], discord_id: int, requester, server: str) -> Track:
    """A queueable Track for a song from `discord_id`'s library on `server`
    (no play link inside - see the module docstring)."""
    info = {
        "title": song.get("title") or "Unknown",
        "author": song.get("artists") or "Unknown",
        "length": int(song.get("duration_ms") or 0),
        "identifier": f"{SOURCE}:{int(discord_id)}:{song['id']}",
        "isStream": False,
        "uri": f"{server}/?song={quote(str(song['id']))}",
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


def owner_of(track) -> Optional[int]:
    """The Discord user whose Cantus library a track is from (None: not a
    Cantus track). Takes a Track, or a decoded info dict."""
    if isinstance(track, dict):
        source, identifier = track.get("sourceName"), track.get("identifier")
    else:
        source, identifier = getattr(track, "source", None), getattr(track, "identifier", None)
    if source != SOURCE:
        return None
    parsed = parse_identifier(identifier)
    return parsed[0] if parsed else None


def is_foreign(track, user_id: Optional[int]) -> bool:
    """True for a Cantus song from someone else's library: `user_id` may not
    queue it again, save it, or see it in history."""
    owner = owner_of(track)
    return owner is not None and owner != user_id


def is_private(track) -> bool:
    """Cantus songs never go into server-wide history or stats."""
    return owner_of(track) is not None


def _full_url(url: str, server: str) -> str:
    """Cantus returns paths; an absolute link must point at the same server."""
    url = str(url)
    if url.startswith("/"):
        return f"{server}{url}"
    a, b = urlsplit(url), urlsplit(server)
    if (a.scheme, a.hostname, a.port) != (b.scheme, b.hostname, b.port):
        raise CantusError(502, "foreign_link")
    return url


async def song_by_id(discord_id: int, song_id: str) -> Optional[Dict[str, Any]]:
    """One of `discord_id`'s songs by id (None if it isn't theirs). Asks for
    its play link, the only per-song endpoint, and keeps the link briefly
    for the play that follows."""
    acct = await _account(discord_id)
    try:
        data = await api("GET", f"/songs/{quote(song_id, safe='')}/stream", account=acct)
    except CantusError as e:
        if e.status == 404:
            return None
        raise
    song = data.get("song") or {}
    if not song.get("id"):
        return None
    if data.get("url"):
        _links[(discord_id, str(song["id"]))] = (time.monotonic(), _full_url(data["url"], acct["server"]))
    return song


async def stream_url(discord_id: int, song_id: str, *, fresh: bool = False, account: Optional[Dict[str, Any]] = None) -> str:
    """A full play link for one of `discord_id`'s songs."""
    key = (discord_id, song_id)
    cached = _links.get(key)
    if cached and not fresh and time.monotonic() - cached[0] < LINK_REUSE_SECONDS:
        return cached[1]
    acct = account or await _account(discord_id)
    data = await api("GET", f"/songs/{quote(song_id, safe='')}/stream", account=acct)
    if not data.get("url"):
        raise CantusError(404, "no play link")
    url = _full_url(data["url"], acct["server"])
    _links[key] = (time.monotonic(), url)
    return url


def _skip_reason(e: CantusError) -> str:
    if e.not_linked:
        return "its owner's Cantus account isn't connected any more."
    if e.code == "token_revoked":
        return "the Cantus token was revoked - reconnect it in the dashboard (Settings -> Cantus)."
    if e.status in (401, 403):
        return "Cantus refused it."
    if e.status == 404:
        return "it's no longer in the Cantus library."
    if e.code in ("private_address", "foreign_link", "bad_url"):
        return "the Cantus server address isn't allowed."
    return "Cantus didn't answer."


async def resolve_for_play(player, track: Track) -> Tuple[str, float]:
    """Player.play()'s hook: (a Lavalink track, its loudness gain) for a
    queued Cantus song, from its owner's library only. Raises
    CantusUnavailable (with the reason) if it can't be played."""
    parsed = parse_identifier(track.identifier)
    if not parsed:
        raise CantusUnavailable("it isn't a valid Cantus song.")
    owner, song_id = parsed
    requester = getattr(track, "requester", None)
    if requester is not None and getattr(requester, "id", None) != owner:
        raise CantusUnavailable("it's from someone else's own Cantus library.")
    acct = None
    for attempt in range(2):
        try:
            acct = acct or await _account(owner)
            url = await stream_url(owner, song_id, fresh=attempt > 0, account=acct)
            await check_public_url(url)  # Lavalink fetches it, not us
        except CantusError as e:
            raise CantusUnavailable(_skip_reason(e))
        try:
            response = await player._node.send(RequestMethod.GET, f"loadtracks?identifier={quote(url, safe='')}")
        except Exception:
            response = {}
        if (response or {}).get("loadType") == "track":
            key = _gain_key(acct["server"], song_id)
            player._bot.loop.create_task(_measure(key, url))
            return response["data"]["encoded"], await gain(key)
        # A reused link that stopped working: try once with a new one.
        _links.pop((owner, song_id), None)
    raise CantusUnavailable("its file couldn't be loaded.")


# --- loudness ----------------------------------------------------------------

def _gain_key(server: str, song_id: str) -> str:
    return f"{urlsplit(server).hostname}:{song_id}"


class _Source:
    """Lets StreamCache._measure_gain read a URL; `name` is what it logs, so
    the signed link never reaches the log."""

    def __init__(self, url: str, name: str) -> None:
        self._url, self.name = url, name

    def __str__(self) -> str:
        return self._url


async def gain(key: str) -> float:
    """The song's measured loudness gain, else the stream cache's typical
    gain (Patch 35), else 1.0 - the same normalization as YouTube songs."""
    if key in _gains:
        return _gains[key]
    try:
        from . import stream_cache
        typical = await stream_cache.get_typical_gain()
    except Exception:
        typical = None
    return typical if typical is not None else 1.0


async def _measure(key: str, url: str) -> None:
    """Measures a song once (in the background, while it plays), so every
    later play of it is normalized exactly."""
    if key in _gains or key in _measuring:
        return
    _measuring.add(key)
    try:
        from .stream_cache import StreamCache
        from .mongodb import MongoDBHandler
        measured = await StreamCache._measure_gain(_Source(url, f"cantus:{key}"), low_priority=True)
        if measured is not None:
            _gains[key] = measured
            await MongoDBHandler.set_cantus_gain(key, measured)
    except Exception as e:
        logger.debug(f"[Cantus] loudness measurement failed for {key}: {type(e).__name__}")
    finally:
        _measuring.discard(key)


async def load_gains() -> None:
    """Measured gains survive restarts (Mongo `CantusGains`)."""
    try:
        from .mongodb import MongoDBHandler
        _gains.update(await MongoDBHandler.get_cantus_gains())
    except Exception:
        pass
