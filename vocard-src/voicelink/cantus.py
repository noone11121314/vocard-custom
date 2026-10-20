"""Cantus Flaccus: play a user's own songs and playlists from their Cantus
library (vocard-custom issue #1; Patch 46).

A Discord user is connected to Cantus in one of two ways; both end up as
"this Discord user's Cantus library":

- **Token** (from the dashboard): their own server address and personal
  `cfa_` token, stored per Discord user in Mongo `CantusLinks`. Calls go to
  *their* server with `Authorization: Bearer <their token>`. Because the
  server is whatever the user typed, every request (and every play link
  handed to Lavalink) is checked to resolve only to public addresses, so a
  member can't point the bot at Lavalink, Mongo or anything else internal.
- **Pairing** (`/cantus link`): settings.json -> "cantus": {"url", "app_key"}
  (a Cantus admin makes the key). Calls go to that server with the app key
  plus `X-Cantus-Discord-User: <id>`; Cantus answers for the account the
  user allowed (409 "not_linked" if none).

A token connection wins when a user has both. Nothing from the library is
kept between commands (Cantus is asked live every time).

A Cantus song is queued as a Track whose encoded id we build ourselves, with
the song's real title/artists/length/cover (Lavalink's http source knows
none of them) and `identifier = "cantus:<discord user id>:<song id>"` - the
Discord user whose library it came from. It never holds a play link: those
are signed and expire after 12 hours, which would break a song that waits in
the queue, is resumed after a restart or is replayed from history. Instead
Player.play() calls `resolve_for_play()` right before the song starts, which
asks Cantus for a link and has Lavalink load it - so a song removed from the
library, or a user who disconnected, is noticed at once.

Tokens, the app key and play links are credentials: none is ever logged or
sent back to a dashboard (it only ever sees `cfa_…<last 4>`).
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
# A link fetched moments ago (when the song was picked) is reused only
# briefly: links are fetched right before each song, so a removal or a
# disconnect takes effect on the next song.
LINK_REUSE_SECONDS = 60

# The app key/server an admin saved from the dashboard (Mongo CantusConfig);
# wins over settings.json, so a new key works without a restart.
_app_override: Dict[str, Any] = {}

_session: Optional[aiohttp.ClientSession] = None
_public_session: Optional[aiohttp.ClientSession] = None
_links: Dict[Tuple[int, str], Tuple[float, str]] = {}
_gains: Dict[str, float] = {}
_measuring: set = set()


class CantusError(Exception):
    """A Cantus API error. `status` is the HTTP status (0: unreachable or not
    configured, 400: refused by Vocard before calling); `code` names it."""

    def __init__(self, status: int, code: str = "") -> None:
        super().__init__(code or f"Cantus answered {status}")
        self.status, self.code = status, code

    @property
    def not_linked(self) -> bool:
        return self.status == 409 or self.code == "not_configured"


class CantusUnavailable(Exception):
    """A queued Cantus song can't be played right now; the player skips it
    with this message as the note."""


def _settings() -> Dict[str, Any]:
    try:
        configured = dict(Config().cantus or {})
    except Exception:
        configured = {}
    return {**configured, **{k: v for k, v in _app_override.items() if v}}


async def load_app() -> None:
    """At startup: the app key/server saved from the dashboard, if any."""
    from .mongodb import MongoDBHandler
    try:
        saved = await MongoDBHandler.get_cantus_app() or {}
    except Exception:
        saved = {}
    _app_override.clear()
    _app_override.update({k: saved[k] for k in ("url", "app_key") if saved.get(k)})


def app_key_hint() -> str:
    key = str(_settings().get("app_key") or "")
    return f"{key[:4]}…{key[-4:]}" if key else ""


async def save_app(url: str, app_key: Optional[str] = None) -> None:
    """Dashboard (admins): checks the key against that server, then saves it.
    No key given: keep the saved one (e.g. only the server address changed).
    Cantus has no "who am I" for apps, so ask for a pairing code that can't
    exist: a valid key gets 404, a wrong one 401/403."""
    server = normalize_server(url)
    app_key = (app_key or "").strip() or str(_settings().get("app_key") or "")
    if not app_key or len(app_key) > 512 or any(c.isspace() for c in app_key):
        raise CantusError(400, "bad_key")
    try:
        await api("GET", "/link/vocard-key-check", account={"mode": "pairing", "server": server, "token": app_key})
    except CantusError as e:
        if e.status in (401, 403):
            raise CantusError(e.status, "bad_key")
        if e.status != 404:
            raise
    from .mongodb import MongoDBHandler
    await MongoDBHandler.set_cantus_app({"url": server, "app_key": app_key, "saved_at": time.time()})
    _app_override.clear()
    _app_override.update({"url": server, "app_key": app_key})


async def save_server(url: str) -> None:
    """Dashboard (admins): just the shared server address, before (or
    without) an app key - members then only paste their personal token."""
    server = normalize_server(url)
    from .mongodb import MongoDBHandler
    saved = await MongoDBHandler.get_cantus_app() or {}
    await MongoDBHandler.set_cantus_app({**{k: v for k, v in saved.items() if k != "_id"}, "url": server, "saved_at": time.time()})
    _app_override["url"] = server


async def remove_app() -> None:
    from .mongodb import MongoDBHandler
    await MongoDBHandler.set_cantus_app(None)
    _app_override.clear()


def base_url() -> str:
    """The pairing server (settings.json)."""
    return str(_settings().get("url") or "").rstrip("/")


def is_configured() -> bool:
    """Whether pairing is available (a server and an app key are set)."""
    return bool(base_url() and _settings().get("app_key"))


# --- only public addresses for user-given servers --------------------------

def _is_public(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address.split("%", 1)[0])
    except ValueError:
        return False
    return ip.is_global and not ip.is_multicast


class _PublicResolver(AbstractResolver):
    """DNS for user-given servers: refuses names that resolve to private,
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
        _session = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15))
    return _session


def _get_public_session() -> aiohttp.ClientSession:
    global _public_session
    if _public_session is None or _public_session.closed:
        _public_session = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=15),
            connector=aiohttp.TCPConnector(resolver=_PublicResolver()),
        )
    return _public_session


# --- accounts ----------------------------------------------------------------

async def _account(discord_id: Optional[int]) -> Dict[str, Any]:
    """How to call Cantus for this Discord user (token connection first)."""
    if discord_id is not None:
        from .mongodb import MongoDBHandler
        link = await MongoDBHandler.get_cantus_link(discord_id)
        if link and link.get("token") and link.get("server"):
            return {"mode": "token", "server": link["server"], "token": link["token"]}
    if not is_configured():
        raise CantusError(0, "not_configured")
    return {"mode": "pairing", "server": base_url(), "token": _settings()["app_key"]}


async def api(method: str, path: str, discord_id: Optional[int] = None, *, params: Optional[Dict[str, Any]] = None,
              json: Any = None, account: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Calls `<server>/cantus/api/v1<path>` for `discord_id` (or as the bot)."""
    acct = account or await _account(discord_id)
    headers = {"Authorization": f"Bearer {acct['token']}"}
    if acct["mode"] == "pairing" and discord_id is not None:
        headers["X-Cantus-Discord-User"] = str(discord_id)
    url = f"{acct['server']}{API}{path}"
    if acct["mode"] == "token":
        await check_public_url(url)
        session = _get_public_session()
    else:
        session = _get_session()
    try:
        async with session.request(method, url, params=params, json=json, headers=headers) as resp:
            try:
                data = await resp.json(content_type=None)
            except Exception:
                data = None
            if resp.status >= 300:
                code = data.get("error", "") if isinstance(data, dict) else ""
                if resp.status == 401 and acct["mode"] == "token":
                    code = "token_revoked"
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
    await check_public_url(server)
    me_ = await api("GET", "/me", account={"mode": "token", "server": server, "token": token})
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
    result: Dict[str, Any] = {"connected": False, "pairingAvailable": is_configured(), "defaultServer": base_url()}
    if link and link.get("token"):
        result.update(connected=True, mode="token", server=link.get("server"), username=link.get("username"), tokenHint=token_hint(link["token"]))
        try:
            me_ = await me(discord_id)
            result.update(username=me_.get("username") or result["username"], songs=me_.get("songs"))
        except CantusError as e:
            result["problem"] = e.code or str(e.status)
        return result
    if is_configured():
        try:
            me_ = await me(discord_id)
            result.update(connected=True, mode="pairing", server=base_url(), username=me_.get("username"), songs=me_.get("songs"))
        except CantusError as e:
            if not e.not_linked:
                result["problem"] = e.code or str(e.status)
    return result


async def disconnect(discord_id: int) -> None:
    """Forgets a token connection and ends a pairing (both, if both)."""
    from .mongodb import MongoDBHandler
    _forget_links_of(discord_id)
    await MongoDBHandler.delete_cantus_link(discord_id)
    if is_configured():
        try:
            await api("DELETE", "/link", discord_id, account={"mode": "pairing", "server": base_url(), "token": _settings()["app_key"]})
        except CantusError as e:
            if not e.not_linked:
                raise


# --- pairing -----------------------------------------------------------------

def _pairing_account() -> Dict[str, Any]:
    if not is_configured():
        raise CantusError(0, "not_configured")
    return {"mode": "pairing", "server": base_url(), "token": _settings()["app_key"]}


async def start_link(discord_id: int, discord_name: str) -> Dict[str, Any]:
    """{code, url, expires}: the page the user opens to press Allow."""
    acct = _pairing_account()
    data = await api("POST", "/link/start", account=acct, json={"discord_id": str(discord_id), "discord_name": discord_name})
    if data.get("url"):
        data["url"] = _full_url(data["url"], acct["server"])
    return data


async def link_status(code: str) -> Dict[str, Any]:
    """{status: "pending"|"linked"|"denied"|"expired", username?}"""
    return await api("GET", f"/link/{quote(code, safe='')}", account=_pairing_account())


async def wait_for_link(code: str, timeout: float = 600, interval: float = 3) -> Dict[str, Any]:
    """Polls until the user pressed Allow or Deny, the code expired (or
    Cantus restarted and forgot it: 404) or `timeout`."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            state = await link_status(code)
        except CantusError as e:
            if e.status == 404:
                return {"status": "expired"}
            state = {"status": "pending"}  # a hiccup: keep waiting
        if state.get("status") in ("linked", "denied", "expired"):
            return state
        await asyncio.sleep(interval)
    return {"status": "expired"}


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


# --- tracks ------------------------------------------------------------------

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
        data = await api("GET", f"/songs/{quote(song_id, safe='')}/stream", discord_id, account=acct)
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
    data = await api("GET", f"/songs/{quote(song_id, safe='')}/stream", discord_id, account=acct)
    if not data.get("url"):
        raise CantusError(404, "no play link")
    url = _full_url(data["url"], acct["server"])
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


def _skip_reason(e: CantusError, mode: Optional[str]) -> str:
    if e.not_linked:
        return "it needs a connected Cantus account (`/cantus link` or the dashboard)."
    if e.code == "token_revoked":
        return "the Cantus token was revoked - reconnect it in the dashboard."
    if e.status in (401, 403):
        return "Cantus refused the bot (its app key may have been revoked)." if mode == "pairing" else "Cantus refused it."
    if e.status == 404:
        return "it's no longer in the Cantus library."
    if e.code in ("private_address", "foreign_link", "bad_url"):
        return "the Cantus server address isn't allowed."
    return "Cantus didn't answer."


async def resolve_for_play(player, track: Track) -> Tuple[str, float]:
    """Player.play()'s hook: (a Lavalink track, its loudness gain) for a
    queued Cantus song. Raises CantusUnavailable (with the reason) if it
    can't be played."""
    parsed = parse_identifier(track.identifier)
    if not parsed:
        raise CantusUnavailable("it isn't a valid Cantus song.")
    _, song_id = parsed
    reason = "its owner isn't in this server."
    for discord_id in _accounts_to_try(track, getattr(player, "guild", None)):
        acct = None
        for attempt in range(2):
            try:
                acct = acct or await _account(discord_id)
                url = await stream_url(discord_id, song_id, fresh=attempt > 0, account=acct)
                if acct["mode"] == "token":
                    await check_public_url(url)  # Lavalink fetches it, not us
            except CantusError as e:
                reason = _skip_reason(e, acct and acct["mode"])
                break
            try:
                response = await player._node.send(RequestMethod.GET, f"loadtracks?identifier={quote(url, safe='')}")
            except Exception:
                response = {}
            if (response or {}).get("loadType") == "track":
                key = _gain_key(acct["server"], song_id)
                player._bot.loop.create_task(_measure(key, url))
                return response["data"]["encoded"], await gain(key)
            # A reused link that stopped working: try once with a new one.
            _links.pop((discord_id, song_id), None)
            reason = "its file couldn't be loaded."
    raise CantusUnavailable(reason)


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
