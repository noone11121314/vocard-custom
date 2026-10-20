"""Patch 46 (vocard-custom issue #1): Cantus Flaccus songs and playlists.

voicelink/cantus.py against a fake Cantus (a real aiohttp server on
localhost speaking the documented /cantus/api/v1 shapes), plus Player.play()'s
Cantus branch. Covers: the app key + Discord user headers on every call,
not-linked (409) handling, pairing (pending -> linked / expired), unlink,
search paging, queued tracks carrying real metadata but no play link, a
fresh link fetched at play time (reused, refreshed when it stops working),
the owner fallback for songs replayed by someone else, and every skip reason.
"""
from __future__ import annotations

import asyncio
import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from voicelink import cantus
from voicelink.config import Config
from voicelink.transformer import decode

APP_KEY = "test-app-key"
OWNER, FRIEND, STRANGER = 1001, 1002, 1003
GOOD_TOKEN = "cfa_goodtoken1234"
TOKEN_ACCOUNT = 2001  # the library a personal token opens
SONG = {"id": "s1", "title": "Song One", "artists": "Artist", "album": "Album", "duration_ms": 184000,
        "cover": "https://img.example/c.jpg", "quality": "16-bit/44.1kHz", "isrc": "JP0000000001"}


class FakeCantus:
    """Each Discord user id maps to a linked library (or none -> 409)."""

    def __init__(self) -> None:
        self.libraries = {OWNER: {"s1": SONG}, FRIEND: {}, TOKEN_ACCOUNT: {"t1": {**SONG, "id": "t1", "title": "Token Song"}}}
        self.playlists = {OWNER: [{"id": "p1", "name": "Road trip", "songs": [SONG, {**SONG, "id": "s2", "title": "Song Two"}]}]}
        self.calls = []
        self.pairing = {}
        self.app = web.Application()
        r = self.app.router
        r.add_get("/cantus/api/v1/me", self.me)
        r.add_get("/cantus/api/v1/songs", self.songs)
        r.add_get("/cantus/api/v1/songs/{id}/stream", self.stream)
        r.add_get("/cantus/api/v1/playlists", self.list_playlists)
        r.add_get("/cantus/api/v1/playlists/{id}", self.get_playlist)
        r.add_post("/cantus/api/v1/link/start", self.link_start)
        r.add_get("/cantus/api/v1/link/{code}", self.link_status)
        r.add_delete("/cantus/api/v1/link", self.unlink)

    def _user(self, request):
        self.calls.append((request.method, request.path, dict(request.query), request.headers.get("Authorization"), request.headers.get("X-Cantus-Discord-User")))
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer cfa_"):  # a personal token: that account, no Discord header
            if auth != f"Bearer {GOOD_TOKEN}" or "X-Cantus-Discord-User" in request.headers:
                raise web.HTTPUnauthorized(text='{"error":"bad token"}', content_type="application/json")
            return TOKEN_ACCOUNT
        if auth != f"Bearer {APP_KEY}":
            raise web.HTTPUnauthorized(text='{"error":"bad app key"}', content_type="application/json")
        uid = int(request.headers.get("X-Cantus-Discord-User", 0))
        if uid not in self.libraries:
            raise web.HTTPConflict(text='{"error":"not_linked"}', content_type="application/json")
        return uid

    async def me(self, request):
        uid = self._user(request)
        return web.json_response({"id": uid, "username": f"user{uid}", "songs": len(self.libraries[uid])})

    async def songs(self, request):
        uid = self._user(request)
        songs = [s for s in self.libraries[uid].values() if request.query.get("q", "").lower() in s["title"].lower()]
        return web.json_response({"songs": songs, "total": len(songs), "offset": int(request.query.get("offset", 0))})

    async def stream(self, request):
        uid = self._user(request)
        song = self.libraries[uid].get(request.match_info["id"])
        if not song:
            return web.json_response({"error": "not found"}, status=404)
        n = sum(1 for c in self.calls if c[1].endswith("/stream"))
        return web.json_response({"url": f"/cantus/api/v1/play/{song['id']}?u={uid}&e=9&s=sig{n}", "song": song})

    async def list_playlists(self, request):
        uid = self._user(request)
        return web.json_response({"playlists": [{"id": p["id"], "name": p["name"], "songs": len(p["songs"])} for p in self.playlists.get(uid, [])]})

    async def get_playlist(self, request):
        uid = self._user(request)
        for p in self.playlists.get(uid, []):
            if p["id"] == request.match_info["id"]:
                return web.json_response(p)
        return web.json_response({"error": "not found"}, status=404)

    async def link_start(self, request):
        self.calls.append(("POST", request.path, await request.json(), request.headers.get("Authorization"), None))
        self.pairing["K7QX"] = ["pending", "pending", "linked"]
        return web.json_response({"code": "K7QX", "url": "/link?code=K7QX", "expires": "2026-10-20T13:00:00Z"})

    async def link_status(self, request):
        states = self.pairing.get(request.match_info["code"])
        if not states:
            return web.json_response({"error": "not found"}, status=404)
        state = states.pop(0) if len(states) > 1 else states[0]
        return web.json_response({"status": state, **({"username": "user1001"} if state == "linked" else {})})

    async def unlink(self, request):
        uid = self._user(request)
        del self.libraries[uid]
        return web.json_response({"ok": True})


@pytest.fixture
async def fake(monkeypatch):
    f = FakeCantus()
    server = TestServer(f.app)
    await server.start_server()
    previous = Config._instance
    Config({"client_id": 1, "cantus": {"url": str(server.make_url("")).rstrip("/"), "app_key": APP_KEY}})
    cantus._links.clear()
    cantus._gains.clear()
    links = {}
    f.stored = links
    monkeypatch.setattr("voicelink.mongodb.MongoDBHandler.get_cantus_link", AsyncMock(side_effect=lambda uid: links.get(int(uid))))
    monkeypatch.setattr("voicelink.mongodb.MongoDBHandler.set_cantus_link", AsyncMock(side_effect=lambda uid, link: links.__setitem__(int(uid), {"_id": int(uid), **link})))
    monkeypatch.setattr("voicelink.mongodb.MongoDBHandler.delete_cantus_link", AsyncMock(side_effect=lambda uid: links.pop(int(uid), None) is not None))
    try:
        yield f
    finally:
        await server.close()
        for name in ("_session", "_public_session"):
            if getattr(cantus, name) is not None:
                await getattr(cantus, name).close()
                setattr(cantus, name, None)
        Config._instance = previous


def member(uid, bot=False):
    return types.SimpleNamespace(id=uid, bot=bot)


def player_in(guild_members, load_type="track"):
    player = MagicMock()
    player.guild.get_member = lambda uid: member(uid) if uid in guild_members else None
    player._node.send = AsyncMock(return_value={"loadType": load_type, "data": {"encoded": "LAVALINK_ENCODED"}})
    player._bot.loop.create_task = MagicMock(side_effect=lambda coro: coro.close())
    return player


async def test_every_call_sends_the_app_key_and_the_discord_user(fake):
    me = await cantus.me(OWNER)
    assert me["id"] == OWNER
    assert fake.calls[-1][3:] == (f"Bearer {APP_KEY}", str(OWNER))


async def test_an_unlinked_discord_user_is_reported_as_not_linked(fake):
    with pytest.raises(cantus.CantusError) as e:
        await cantus.search_songs(STRANGER, "song")
    assert e.value.not_linked and e.value.code == "not_linked"


async def test_without_settings_nothing_is_called(fake, monkeypatch):
    Config({"client_id": 1})
    with pytest.raises(cantus.CantusError) as e:
        await cantus.me(OWNER)
    assert e.value.code == "not_configured" and fake.calls == []


async def test_search_sends_query_limit_and_offset(fake):
    result = await cantus.search_songs(OWNER, "one", limit=500, offset=25)
    assert [s["id"] for s in result["songs"]] == ["s1"]
    assert fake.calls[-1][2] == {"q": "one", "limit": "100", "offset": "25"}


async def test_pairing_waits_until_the_user_presses_allow(fake):
    pairing = await cantus.start_link(OWNER, "owner")
    assert pairing["url"].endswith("/link?code=K7QX") and pairing["url"].startswith("http")
    assert fake.calls[-1][2] == {"discord_id": str(OWNER), "discord_name": "owner"}
    result = await cantus.wait_for_link(pairing["code"], timeout=5, interval=0)
    assert result == {"status": "linked", "username": "user1001"}


async def test_pairing_reports_an_expired_or_unknown_code(fake):
    fake.pairing["OLD"] = ["expired"]
    assert (await cantus.wait_for_link("OLD", timeout=5, interval=0))["status"] == "expired"
    assert (await cantus.wait_for_link("NOPE", timeout=5, interval=0))["status"] == "expired"


async def test_unlink_forgets_the_account_and_its_links(fake):
    cantus._links[(OWNER, "s1")] = (0.0, "x")
    await cantus.disconnect(OWNER)
    assert (OWNER, "s1") not in cantus._links
    with pytest.raises(cantus.CantusError):
        await cantus.me(OWNER)


async def test_a_queued_track_has_the_songs_metadata_and_no_play_link(fake):
    track = cantus.build_track(SONG, OWNER, member(OWNER), cantus.base_url())
    info = decode(track.track_id)
    assert (info["title"], info["author"], info["length"], info["artworkUrl"], info["sourceName"]) == ("Song One", "Artist", 184000, "https://img.example/c.jpg", "cantus")
    assert info["identifier"] == f"cantus:{OWNER}:s1" and "play/" not in (info["uri"] or "")
    assert track.source == "cantus" and cantus.parse_identifier(track.identifier) == (OWNER, "s1")


async def test_song_by_id_returns_none_for_someone_elses_song(fake):
    assert (await cantus.song_by_id(OWNER, "s1"))["title"] == "Song One"
    assert (OWNER, "s1") in cantus._links  # its link is kept for play()
    assert await cantus.song_by_id(OWNER, "nope") is None


async def test_play_time_link_is_loaded_into_lavalink_and_reused(fake):
    player = player_in({OWNER})
    track = cantus.build_track(SONG, OWNER, member(OWNER), cantus.base_url())

    assert (await cantus.resolve_for_play(player, track))[0] == "LAVALINK_ENCODED"
    await cantus.resolve_for_play(player, track)

    loaded = player._node.send.await_args.args[1]
    assert loaded.startswith("loadtracks?identifier=http") and "play%2Fs1" in loaded
    assert sum(1 for c in fake.calls if c[1].endswith("/stream")) == 1  # reused, not asked twice


async def test_a_link_that_stopped_working_is_replaced_once(fake):
    player = player_in({OWNER})
    player._node.send = AsyncMock(side_effect=[{"loadType": "error"}, {"loadType": "track", "data": {"encoded": "NEW"}}])
    cantus._links[(OWNER, "s1")] = (time.monotonic(), "http://stale/link")

    assert (await cantus.resolve_for_play(player, cantus.build_track(SONG, OWNER, member(OWNER), cantus.base_url())))[0] == "NEW"
    assert sum(1 for c in fake.calls if c[1].endswith("/stream")) == 1


async def test_someone_else_replaying_the_owners_song_uses_the_owners_library(fake):
    """A friend clicks the owner's song in this server's history: the friend's
    own library doesn't have it (404), so it's played from the owner's."""
    player = player_in({OWNER, FRIEND})
    track = cantus.build_track(SONG, OWNER, member(OWNER), cantus.base_url())
    track.requester = member(FRIEND)

    assert (await cantus.resolve_for_play(player, track))[0] == "LAVALINK_ENCODED"
    users = [c[4] for c in fake.calls if c[1].endswith("/stream")]
    assert users == [str(FRIEND), str(OWNER)]


async def test_the_owners_library_is_only_used_while_they_are_in_the_server(fake):
    player = player_in({FRIEND})
    track = cantus.build_track(SONG, OWNER, member(OWNER), cantus.base_url())
    track.requester = member(STRANGER)
    with pytest.raises(cantus.CantusUnavailable) as e:
        await cantus.resolve_for_play(player, track)
    assert "connected Cantus account" in str(e.value)
    assert all(c[4] != str(OWNER) for c in fake.calls)


async def test_a_song_removed_from_the_library_is_skipped_with_a_reason(fake):
    del fake.libraries[OWNER]["s1"]
    with pytest.raises(cantus.CantusUnavailable) as e:
        await cantus.resolve_for_play(player_in({OWNER}), cantus.build_track(SONG, OWNER, member(OWNER), cantus.base_url()))
    assert "no longer" in str(e.value)


async def test_a_revoked_app_key_is_skipped_with_a_reason(fake):
    Config({"client_id": 1, "cantus": {"url": cantus.base_url(), "app_key": "revoked"}})
    with pytest.raises(cantus.CantusUnavailable) as e:
        await cantus.resolve_for_play(player_in({OWNER}), cantus.build_track(SONG, OWNER, member(OWNER), cantus.base_url()))
    assert "app key" in str(e.value)


async def test_unmeasured_songs_get_the_typical_gain_and_measured_ones_their_own(fake, monkeypatch):
    monkeypatch.setattr("voicelink.stream_cache.get_typical_gain", AsyncMock(return_value=0.5))
    player = player_in({OWNER})
    track = cantus.build_track(SONG, OWNER, member(OWNER), cantus.base_url())
    assert (await cantus.resolve_for_play(player, track))[1] == 0.5
    cantus._gains["127.0.0.1:s1"] = 0.7
    assert (await cantus.resolve_for_play(player, track))[1] == 0.7


async def test_the_measurement_never_logs_the_play_link(fake, monkeypatch):
    seen = {}

    async def measure(source, low_priority=False):
        seen["str"], seen["name"] = str(source), source.name
        return 0.8

    monkeypatch.setattr("voicelink.stream_cache.StreamCache._measure_gain", staticmethod(measure))
    monkeypatch.setattr("voicelink.mongodb.MongoDBHandler.set_cantus_gain", AsyncMock())
    await cantus._measure("s1", "http://host/cantus/api/v1/play/s1?s=SECRET")
    assert seen == {"str": "http://host/cantus/api/v1/play/s1?s=SECRET", "name": "cantus:s1"}
    assert cantus._gains["s1"] == 0.8


async def test_player_play_uses_the_fresh_cantus_track(fake, monkeypatch):
    from tests.unit.test_player_direct_resolve import make_bare_player
    player = make_bare_player()
    monkeypatch.setattr("voicelink.player.cantus.resolve_for_play", AsyncMock(return_value=("FRESH", 0.6)))
    track = cantus.build_track(SONG, OWNER, member(OWNER), cantus.base_url())

    await player.play(track)

    sent = player.send.await_args.kwargs["data"]
    assert sent["encodedTrack"] == "FRESH" and player._loudness_gain == 0.6
    assert player._current is track  # the queue/history keep the metadata track


# --- token connections (dashboard) ---------------------------------------------

@pytest.fixture
def allow_localhost(monkeypatch):
    """The fake Cantus runs on 127.0.0.1, which user-given servers may not
    use; let it through for the token tests (the refusal is tested below)."""
    monkeypatch.setattr("voicelink.cantus._is_public", lambda address: True)


async def test_connecting_with_a_token_checks_it_and_stores_it(fake, allow_localhost):
    server = cantus.base_url()
    account = await cantus.connect_token(STRANGER, server + "/some/page", f"  {GOOD_TOKEN} ")

    assert account["username"] == f"user{TOKEN_ACCOUNT}"
    assert fake.stored[STRANGER]["server"] == server and fake.stored[STRANGER]["token"] == GOOD_TOKEN
    assert fake.calls[-1][3:] == (f"Bearer {GOOD_TOKEN}", None)  # its own token, no Discord header


async def test_a_token_user_searches_and_plays_from_their_own_server(fake, allow_localhost):
    await cantus.connect_token(STRANGER, cantus.base_url(), GOOD_TOKEN)

    assert [s["id"] for s in (await cantus.search_songs(STRANGER, "token"))["songs"]] == ["t1"]
    track = cantus.build_track({**SONG, "id": "t1"}, STRANGER, member(STRANGER), await cantus.server_of(STRANGER))
    encoded, _ = await cantus.resolve_for_play(player_in({STRANGER}), track)
    assert encoded == "LAVALINK_ENCODED"
    assert all(c[3] == f"Bearer {GOOD_TOKEN}" for c in fake.calls[-2:])


async def test_status_never_returns_the_token(fake, allow_localhost):
    await cantus.connect_token(STRANGER, cantus.base_url(), GOOD_TOKEN)
    state = await cantus.status(STRANGER)
    assert state["connected"] and state["mode"] == "token" and state["tokenHint"] == "cfa_…1234"
    assert GOOD_TOKEN not in repr(state)


async def test_a_bad_token_is_refused_and_not_stored(fake, allow_localhost):
    with pytest.raises(cantus.CantusError) as e:
        await cantus.connect_token(STRANGER, cantus.base_url(), "cfa_wrong")
    assert e.value.code == "token_revoked" and STRANGER not in fake.stored


async def test_a_revoked_token_skips_its_songs_with_a_reason(fake, allow_localhost):
    await cantus.connect_token(STRANGER, cantus.base_url(), GOOD_TOKEN)
    fake.stored[STRANGER]["token"] = "cfa_revoked"
    track = cantus.build_track({**SONG, "id": "t1"}, STRANGER, member(STRANGER), cantus.base_url())
    with pytest.raises(cantus.CantusUnavailable) as e:
        await cantus.resolve_for_play(player_in({STRANGER}), track)
    assert "revoked" in str(e.value)


async def test_disconnect_forgets_the_token(fake, allow_localhost):
    await cantus.connect_token(STRANGER, cantus.base_url(), GOOD_TOKEN)
    await cantus.disconnect(STRANGER)
    assert STRANGER not in fake.stored
    with pytest.raises(cantus.CantusError) as e:
        await cantus.me(STRANGER)
    assert e.value.not_linked  # STRANGER has no pairing either


@pytest.mark.parametrize("server", [
    "http://127.0.0.1:2333", "http://10.0.0.5", "http://192.168.1.1", "http://172.18.0.3",
    "http://169.254.169.254", "http://[::1]", "http://0.0.0.0", "http://localhost",
])
async def test_private_and_internal_servers_are_refused(fake, server):
    with pytest.raises(cantus.CantusError) as e:
        await cantus.connect_token(STRANGER, server, GOOD_TOKEN)
    assert e.value.code in ("private_address", "unresolvable")
    assert fake.calls == [] and STRANGER not in fake.stored


@pytest.mark.parametrize("raw", ["", "ftp://x.example", "https://user:pw@x.example", "https://x.example:99999"])
def test_malformed_server_addresses_are_refused(raw):
    with pytest.raises(cantus.CantusError):
        cantus.normalize_server(raw)


def test_server_addresses_are_normalized():
    assert cantus.normalize_server("music.example.com/some/page?x=1") == "https://music.example.com"
    assert cantus.normalize_server("http://music.example.com:8080/") == "http://music.example.com:8080"


def test_a_play_link_on_another_server_is_refused():
    assert cantus._full_url("/cantus/api/v1/play/s1?s=x", "https://a.example") == "https://a.example/cantus/api/v1/play/s1?s=x"
    assert cantus._full_url("https://a.example/p", "https://a.example") == "https://a.example/p"
    with pytest.raises(cantus.CantusError):
        cantus._full_url("http://lavalink:2333/v4/sessions", "https://a.example")


async def test_the_public_resolver_refuses_names_pointing_inside(monkeypatch):
    resolver = cantus._PublicResolver()
    monkeypatch.setattr(resolver._inner, "resolve", AsyncMock(return_value=[{"host": "172.18.0.2", "port": 80}]))
    with pytest.raises(OSError):
        await resolver.resolve("sneaky.example", 80)
    monkeypatch.setattr(resolver._inner, "resolve", AsyncMock(return_value=[{"host": "93.184.216.34", "port": 80}]))
    assert (await resolver.resolve("ok.example", 80))[0]["host"] == "93.184.216.34"


def test_dashboard_messages_with_a_token_are_never_logged():
    from voicelink.ipc.client import _redact
    assert "cfa_secret" not in _redact({"op": "cantusConnect", "server": "https://x", "token": "cfa_secret"})
    assert _redact({"op": "x", "token": "abc"}) == "{'op': 'x', 'token': '…'}"
    assert "cfa_zzz" not in _redact("something cfa_zzz something")


async def test_the_dashboard_connect_reply_never_contains_the_token(fake, allow_localhost):
    from voicelink.ipc.methods import cantusConnect
    reply = await cantusConnect(None, {"userId": str(STRANGER), "server": cantus.base_url(), "token": GOOD_TOKEN})
    assert reply["op"] == "cantusStatus" and reply["ok"] and reply["connected"]
    assert GOOD_TOKEN not in repr(reply)


async def test_the_dashboard_explains_a_refused_server(fake):
    from voicelink.ipc.methods import cantusConnect
    bad = await cantusConnect(None, {"userId": str(STRANGER), "server": "http://10.0.0.1", "token": GOOD_TOKEN})
    assert bad["ok"] is False and "public" in bad["message"] and not bad["connected"]
