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
SONG = {"id": "s1", "title": "Song One", "artists": "Artist", "album": "Album", "duration_ms": 184000,
        "cover": "https://img.example/c.jpg", "quality": "16-bit/44.1kHz", "isrc": "JP0000000001"}


class FakeCantus:
    """Each Discord user id maps to a linked library (or none -> 409)."""

    def __init__(self) -> None:
        self.libraries = {OWNER: {"s1": SONG}, FRIEND: {}}
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
        if request.headers.get("Authorization") != f"Bearer {APP_KEY}":
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
    try:
        yield f
    finally:
        await server.close()
        if cantus._session is not None:
            await cantus._session.close()
            cantus._session = None
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
    await cantus.unlink(OWNER)
    assert (OWNER, "s1") not in cantus._links
    with pytest.raises(cantus.CantusError):
        await cantus.me(OWNER)


async def test_a_queued_track_has_the_songs_metadata_and_no_play_link(fake):
    track = cantus.build_track(SONG, OWNER, member(OWNER))
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
    track = cantus.build_track(SONG, OWNER, member(OWNER))

    assert await cantus.resolve_for_play(player, track) == "LAVALINK_ENCODED"
    await cantus.resolve_for_play(player, track)

    loaded = player._node.send.await_args.args[1]
    assert loaded.startswith("loadtracks?identifier=http") and "play%2Fs1" in loaded
    assert sum(1 for c in fake.calls if c[1].endswith("/stream")) == 1  # reused, not asked twice


async def test_a_link_that_stopped_working_is_replaced_once(fake):
    player = player_in({OWNER})
    player._node.send = AsyncMock(side_effect=[{"loadType": "error"}, {"loadType": "track", "data": {"encoded": "NEW"}}])
    cantus._links[(OWNER, "s1")] = (time.monotonic(), "http://stale/link")

    assert await cantus.resolve_for_play(player, cantus.build_track(SONG, OWNER, member(OWNER))) == "NEW"
    assert sum(1 for c in fake.calls if c[1].endswith("/stream")) == 1


async def test_someone_else_replaying_the_owners_song_uses_the_owners_library(fake):
    """A friend clicks the owner's song in this server's history: the friend's
    own library doesn't have it (404), so it's played from the owner's."""
    player = player_in({OWNER, FRIEND})
    track = cantus.build_track(SONG, OWNER, member(OWNER))
    track.requester = member(FRIEND)

    assert await cantus.resolve_for_play(player, track) == "LAVALINK_ENCODED"
    users = [c[4] for c in fake.calls if c[1].endswith("/stream")]
    assert users == [str(FRIEND), str(OWNER)]


async def test_the_owners_library_is_only_used_while_they_are_in_the_server(fake):
    player = player_in({FRIEND})
    track = cantus.build_track(SONG, OWNER, member(OWNER))
    track.requester = member(STRANGER)
    with pytest.raises(cantus.CantusUnavailable) as e:
        await cantus.resolve_for_play(player, track)
    assert "linked" in str(e.value)
    assert all(c[4] != str(OWNER) for c in fake.calls)


async def test_a_song_removed_from_the_library_is_skipped_with_a_reason(fake):
    del fake.libraries[OWNER]["s1"]
    with pytest.raises(cantus.CantusUnavailable) as e:
        await cantus.resolve_for_play(player_in({OWNER}), cantus.build_track(SONG, OWNER, member(OWNER)))
    assert "no longer" in str(e.value)


async def test_a_revoked_app_key_is_skipped_with_a_reason(fake):
    Config({"client_id": 1, "cantus": {"url": cantus.base_url(), "app_key": "revoked"}})
    with pytest.raises(cantus.CantusUnavailable) as e:
        await cantus.resolve_for_play(player_in({OWNER}), cantus.build_track(SONG, OWNER, member(OWNER)))
    assert "app key" in str(e.value)


async def test_unmeasured_songs_get_the_typical_gain_and_measured_ones_their_own(fake, monkeypatch):
    monkeypatch.setattr("voicelink.stream_cache.get_typical_gain", AsyncMock(return_value=0.5))
    assert await cantus.gain("s1") == 0.5
    cantus._gains["s1"] = 0.7
    assert await cantus.gain("s1") == 0.7


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
    monkeypatch.setattr("voicelink.player.cantus.resolve_for_play", AsyncMock(return_value="FRESH"))
    monkeypatch.setattr("voicelink.player.cantus.gain", AsyncMock(return_value=0.6))
    track = cantus.build_track(SONG, OWNER, member(OWNER))

    await player.play(track)

    sent = player.send.await_args.kwargs["data"]
    assert sent["encodedTrack"] == "FRESH" and player._loudness_gain == 0.6
    assert player._current is track  # the queue/history keep the metadata track
