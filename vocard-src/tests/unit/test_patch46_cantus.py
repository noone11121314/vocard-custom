"""Patch 46 (vocard-custom issue #1): Cantus Flaccus songs and playlists.

voicelink/cantus.py against a fake Cantus (a real aiohttp server on
localhost speaking the documented /cantus/api/v1 shapes), plus the places
that keep a Cantus library private. Covers: connecting with a server + a
personal token only (no app key, no pairing), the token on every call,
queued tracks carrying real metadata but no play link, a fresh link at play
time from the owner's library only, every skip reason, public-address-only
servers, the default server, and privacy - others can't re-queue or save
someone's Cantus song, it stays out of server-wide history, no payload ever
carries a play link, while queue controls (move, swap, remove) still work
for everyone.
"""
from __future__ import annotations

import time
import types
from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp import web
from aiohttp.test_utils import TestServer

from voicelink import cantus
from voicelink.config import Config
from voicelink.exceptions import VoicelinkException
from voicelink.transformer import decode

OWNER, FRIEND = 1001, 1002
GOOD_TOKEN = "cfa_goodtoken1234"
FRIEND_TOKEN = "cfa_friendtoken5678"
SONG = {"id": "s1", "title": "Song One", "artists": "Artist", "album": "Album", "duration_ms": 184000,
        "cover": "https://img.example/c.jpg", "quality": "16-bit/44.1kHz", "isrc": "JP0000000001"}
PLAY_PATH = "/cantus/api/v1/play/"


class FakeCantus:
    """Each personal token opens one library."""

    def __init__(self) -> None:
        self.libraries = {GOOD_TOKEN: {"s1": SONG}, FRIEND_TOKEN: {}}
        self.usernames = {GOOD_TOKEN: "babab1t", FRIEND_TOKEN: "friend"}
        self.playlists = {GOOD_TOKEN: [{"id": "p1", "name": "Road trip", "songs": [SONG, {**SONG, "id": "s2", "title": "Song Two"}]}]}
        self.calls = []
        self.app = web.Application()
        r = self.app.router
        r.add_get("/cantus/api/v1/me", self.me)
        r.add_get("/cantus/api/v1/songs", self.songs)
        r.add_get("/cantus/api/v1/songs/{id}/stream", self.stream)
        r.add_get("/cantus/api/v1/playlists", self.list_playlists)
        r.add_get("/cantus/api/v1/playlists/{id}", self.get_playlist)

    def _token(self, request):
        self.calls.append((request.method, request.path, dict(request.query), request.headers.get("Authorization"), request.headers.get("X-Cantus-Discord-User")))
        token = request.headers.get("Authorization", "").removeprefix("Bearer ")
        if token not in self.libraries:
            raise web.HTTPUnauthorized(text='{"error":"bad token"}', content_type="application/json")
        return token

    async def me(self, request):
        t = self._token(request)
        return web.json_response({"id": 3, "username": self.usernames[t], "songs": len(self.libraries[t])})

    async def songs(self, request):
        t = self._token(request)
        songs = [s for s in self.libraries[t].values() if request.query.get("q", "").lower() in s["title"].lower()]
        return web.json_response({"songs": songs, "total": len(songs), "offset": int(request.query.get("offset", 0))})

    async def stream(self, request):
        t = self._token(request)
        song = self.libraries[t].get(request.match_info["id"])
        if not song:
            return web.json_response({"error": "not found"}, status=404)
        n = sum(1 for c in self.calls if c[1].endswith("/stream"))
        return web.json_response({"url": f"{PLAY_PATH}{song['id']}?u=3&e=9&s=sig{n}", "song": song})

    async def list_playlists(self, request):
        t = self._token(request)
        return web.json_response({"playlists": [{"id": p["id"], "name": p["name"], "songs": len(p["songs"])} for p in self.playlists.get(t, [])]})

    async def get_playlist(self, request):
        t = self._token(request)
        for p in self.playlists.get(t, []):
            if p["id"] == request.match_info["id"]:
                return web.json_response(p)
        return web.json_response({"error": "not found"}, status=404)


@pytest.fixture
async def fake(monkeypatch):
    f = FakeCantus()
    server = TestServer(f.app)
    await server.start_server()
    f.server = str(server.make_url("")).rstrip("/")
    previous = Config._instance
    Config({"client_id": 1, "cantus": {"url": f.server, "dashboard_url": "https://dash.example"}})
    for store in (cantus._links, cantus._gains, cantus._default_server):
        store.clear()
    links, saved = {}, {}
    f.stored, f.saved_default = links, saved
    monkeypatch.setattr("voicelink.mongodb.MongoDBHandler.get_cantus_link", AsyncMock(side_effect=lambda uid: links.get(int(uid))))
    monkeypatch.setattr("voicelink.mongodb.MongoDBHandler.set_cantus_link", AsyncMock(side_effect=lambda uid, link: links.__setitem__(int(uid), {"_id": int(uid), **link})))
    monkeypatch.setattr("voicelink.mongodb.MongoDBHandler.delete_cantus_link", AsyncMock(side_effect=lambda uid: links.pop(int(uid), None) is not None))
    monkeypatch.setattr("voicelink.mongodb.MongoDBHandler.set_cantus_app", AsyncMock(side_effect=lambda app: (saved.clear(), saved.update(app or {}))))
    monkeypatch.setattr("voicelink.mongodb.MongoDBHandler.get_cantus_app", AsyncMock(side_effect=lambda: dict(saved) or None))
    try:
        yield f
    finally:
        await server.close()
        if cantus._session is not None:
            await cantus._session.close()
            cantus._session = None
        Config._instance = previous


@pytest.fixture
def allow_localhost(monkeypatch):
    """The fake Cantus runs on 127.0.0.1, which Cantus servers may not use;
    let it through (the refusal itself is tested below)."""
    monkeypatch.setattr("voicelink.cantus._is_public", lambda address: True)


async def connected(fake, user=OWNER, token=GOOD_TOKEN):
    await cantus.connect_token(user, fake.server, token)


def member(uid, bot=False):
    return types.SimpleNamespace(id=uid, bot=bot)


def player_in(load_type="track"):
    player = MagicMock()
    player._node.send = AsyncMock(return_value={"loadType": load_type, "data": {"encoded": "LAVALINK_ENCODED"}})
    player._bot.loop.create_task = MagicMock(side_effect=lambda coro: coro.close())
    return player


def owners_track(fake, requester=OWNER):
    return cantus.build_track(SONG, OWNER, member(requester), fake.server)


def make_yt(requester_id, video="yt1"):
    from voicelink.objects import Track
    from voicelink.transformer import encode
    info = {"title": f"YT {video}", "author": "A", "length": 1000, "identifier": video, "isStream": False, "uri": f"https://youtu.be/{video}",
            "artworkUrl": None, "isrc": None, "sourceName": "youtube", "position": 0}
    return Track(track_id=encode(info), info=info, requester=member(requester_id))


# --- connecting: server + personal token only ---------------------------------

async def test_connecting_checks_the_token_and_stores_it(fake, allow_localhost):
    account = await cantus.connect_token(OWNER, fake.server + "/", f"  {GOOD_TOKEN} ")
    assert account["username"] == "babab1t"
    assert fake.stored[OWNER]["server"] == fake.server and fake.stored[OWNER]["token"] == GOOD_TOKEN
    assert fake.calls[-1][3:] == (f"Bearer {GOOD_TOKEN}", None)  # the token alone - no app key, no Discord header


async def test_every_call_uses_the_users_own_token(fake, allow_localhost):
    await connected(fake)
    await cantus.search_songs(OWNER, "one", limit=500, offset=25)
    assert fake.calls[-1][2] == {"q": "one", "limit": "100", "offset": "25"}
    assert fake.calls[-1][3] == f"Bearer {GOOD_TOKEN}"


async def test_a_bad_token_is_refused_and_not_stored(fake, allow_localhost):
    with pytest.raises(cantus.CantusError) as e:
        await cantus.connect_token(OWNER, fake.server, "cfa_wrong")
    assert e.value.code == "token_revoked" and OWNER not in fake.stored


async def test_someone_not_connected_is_told_so_without_any_call(fake):
    with pytest.raises(cantus.CantusError) as e:
        await cantus.search_songs(FRIEND, "song")
    assert e.value.not_linked and fake.calls == []


async def test_status_never_returns_the_token(fake, allow_localhost):
    await connected(fake)
    state = await cantus.status(OWNER)
    assert state["connected"] and state["username"] == "babab1t" and state["songs"] == 1
    assert state["tokenHint"] == "cfa_…1234" and GOOD_TOKEN not in repr(state)


async def test_disconnect_forgets_the_token(fake, allow_localhost):
    await connected(fake)
    cantus._links[(OWNER, "s1")] = (time.monotonic(), "x")
    assert await cantus.disconnect(OWNER)
    assert OWNER not in fake.stored and (OWNER, "s1") not in cantus._links
    assert not await cantus.disconnect(OWNER)


async def test_an_admin_saved_default_server_survives_a_restart(fake, allow_localhost):
    Config({"client_id": 1})
    assert cantus.base_url() == ""
    await cantus.save_default_server(fake.server + "/?from=settings")
    assert cantus.base_url() == fake.server
    cantus._default_server.clear()  # restart
    await cantus.load_default_server()
    assert cantus.base_url() == fake.server


# --- /cantus link: the steps, not pairing ---------------------------------------

def _interaction(user_id=OWNER):
    i = MagicMock()
    i.user = member(user_id)
    i.response.send_message = AsyncMock()
    i.response.defer = AsyncMock()
    i.followup.send = AsyncMock()
    return i


async def test_cantus_link_explains_the_token_steps(fake):
    from cogs.cantus import Cantus
    i = _interaction()
    await Cantus.link.callback(Cantus(MagicMock()), i)
    text, kwargs = i.response.send_message.await_args.args[0], i.response.send_message.await_args.kwargs
    assert kwargs.get("ephemeral") is True
    assert "connect code" in text and "Connect other apps" in text and "Settings -> Cantus" in text and "https://dash.example" in text
    assert fake.calls == []  # no pairing request anywhere


async def test_cantus_unlink_forgets_the_token(fake, allow_localhost):
    from cogs.cantus import Cantus
    await connected(fake)
    i = _interaction()
    await Cantus.unlink.callback(Cantus(MagicMock()), i)
    assert OWNER not in fake.stored and "forgotten" in i.followup.send.await_args.args[0]


# --- tracks: metadata, never a play link ----------------------------------------

async def test_a_queued_track_has_the_songs_metadata_and_no_play_link(fake):
    track = owners_track(fake)
    info = decode(track.track_id)
    assert (info["title"], info["author"], info["length"], info["artworkUrl"], info["sourceName"]) == ("Song One", "Artist", 184000, "https://img.example/c.jpg", "cantus")
    assert info["identifier"] == f"cantus:{OWNER}:s1"
    assert PLAY_PATH not in track.track_id and PLAY_PATH not in str(info)
    assert cantus.owner_of(track) == OWNER and cantus.owner_of(info) == OWNER


async def test_song_by_id(fake, allow_localhost):
    await connected(fake)
    assert (await cantus.song_by_id(OWNER, "s1"))["title"] == "Song One"
    assert await cantus.song_by_id(OWNER, "nope") is None


async def test_play_time_link_comes_from_the_owners_library_and_goes_only_to_lavalink(fake, allow_localhost):
    await connected(fake)
    player = player_in()
    encoded, _ = await cantus.resolve_for_play(player, owners_track(fake))
    assert encoded == "LAVALINK_ENCODED"
    loaded = player._node.send.await_args.args[1]
    assert loaded.startswith("loadtracks?identifier=http") and "play%2Fs1" in loaded
    await cantus.resolve_for_play(player, owners_track(fake))
    assert sum(1 for c in fake.calls if c[1].endswith("/stream")) == 1  # reused within the minute


async def test_a_link_that_stopped_working_is_replaced_once(fake, allow_localhost):
    await connected(fake)
    player = player_in()
    player._node.send = AsyncMock(side_effect=[{"loadType": "error"}, {"loadType": "track", "data": {"encoded": "NEW"}}])
    cantus._links[(OWNER, "s1")] = (time.monotonic(), f"{fake.server}/stale")
    assert (await cantus.resolve_for_play(player, owners_track(fake)))[0] == "NEW"


@pytest.mark.parametrize("setup, reason", [
    ("removed", "no longer"),
    ("revoked", "revoked"),
    ("disconnected", "isn't connected"),
])
async def test_skip_reasons(fake, allow_localhost, setup, reason):
    await connected(fake)
    if setup == "removed":
        del fake.libraries[GOOD_TOKEN]["s1"]
    elif setup == "revoked":
        fake.stored[OWNER]["token"] = "cfa_revoked"
    else:
        await cantus.disconnect(OWNER)
    with pytest.raises(cantus.CantusUnavailable) as e:
        await cantus.resolve_for_play(player_in(), owners_track(fake))
    assert reason in str(e.value)


async def test_someone_else_can_never_play_from_the_owners_library(fake, allow_localhost):
    """A friend re-queues the owner's song: refused, and the owner's token
    is never used for it."""
    await connected(fake)
    fake.calls.clear()
    with pytest.raises(cantus.CantusUnavailable) as e:
        await cantus.resolve_for_play(player_in(), owners_track(fake, requester=FRIEND))
    assert "someone else's" in str(e.value) and fake.calls == []


# --- privacy ------------------------------------------------------------------------

def test_privacy_helpers(fake):
    track = owners_track(fake)
    assert cantus.is_private(track) and not cantus.is_foreign(track, OWNER) and cantus.is_foreign(track, FRIEND)
    yt = types.SimpleNamespace(source="youtube", identifier="abc")
    assert not cantus.is_private(yt) and not cantus.is_foreign(yt, FRIEND)


def _queue_player():
    from voicelink.player import Player
    from voicelink.queue import Queue
    p = Player.__new__(Player)
    p._logger = MagicMock()
    p._guild = MagicMock(id=555)
    p._current = None
    p._loading_track = None
    p.queue = Queue(size=50, allow_duplicate=True, get_msg=lambda *k: "m")
    p.channel = MagicMock(members=[])
    p._ipc_client = MagicMock(_is_connected=False)
    p._ipc_connection = False
    p._prefetch_next_direct_track = MagicMock()
    p._bot = MagicMock()
    p._bot.loop.create_task = MagicMock(side_effect=lambda coro: coro.close())
    return p


async def test_others_cannot_queue_someones_cantus_song(fake):
    p = _queue_player()
    with pytest.raises(VoicelinkException) as e:
        await p.add_track(owners_track(fake, requester=FRIEND))
    assert str(e.value) == cantus.FOREIGN_NOTE and p.queue.count == 0


async def test_a_shared_list_drops_only_the_other_peoples_cantus_songs(fake):
    p = _queue_player()
    yt = make_yt(FRIEND)
    await p.add_track([owners_track(fake, requester=FRIEND), yt])
    assert [t.track_id for t in p.queue.tracks()] == [yt.track_id]


async def test_the_owner_can_queue_their_own(fake):
    p = _queue_player()
    await p.add_track(owners_track(fake))
    assert p.queue.count == 1


async def test_queue_controls_still_work_on_someones_cantus_song(fake):
    """User: other people can still move it up and down, skip it, remove it -
    only re-adding and saving are the owner's."""
    p = _queue_player()
    mine, a, b = owners_track(fake), make_yt(FRIEND, "a"), make_yt(FRIEND, "b")
    await p.add_track([mine, a, b])
    titles = lambda: [t.title for t in p.queue.tracks()]
    p.queue.move(1, 3)  # a friend drags the Cantus song to the bottom
    assert titles() == ["YT a", "YT b", "Song One"]
    p.queue.swap(1, 3)
    assert titles() == ["Song One", "YT b", "YT a"]
    removed = p.queue.remove(1)  # and remove it
    assert list(removed.values()) == [mine] and "Song One" not in titles()


async def test_cantus_songs_stay_out_of_server_history_and_stats(fake, monkeypatch):
    update = AsyncMock()
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", update)
    p = _queue_player()
    await p._record_guild_play_history(owners_track(fake))
    assert update.await_count == 0
    await p._record_guild_added_history([owners_track(fake), make_yt(OWNER)])
    pushed = update.await_args.args[1]["$push"]["addedHistory"]["$each"]
    assert pushed == [make_yt(OWNER).track_id]


async def test_full_history_marks_cantus_events_private_and_the_server_view_skips_them(fake, monkeypatch):
    add = AsyncMock()
    monkeypatch.setattr("voicelink.player.MongoDBHandler.add_history_events", add)
    await _queue_player()._record_history_events("played", [owners_track(fake), make_yt(OWNER)])
    events = add.await_args.args[0]
    assert events[0]["private"] is True and events[0]["user_id"] == OWNER and "private" not in events[1]
    assert PLAY_PATH not in repr(events)

    from voicelink.ipc.methods import getFullHistory
    get = AsyncMock(return_value=[])
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_history_songs", get)
    monkeypatch.setattr("voicelink.ipc.methods._resolve_guild_for_user", lambda bot, uid, data: types.SimpleNamespace(id=555, name="S"))
    await getFullHistory(MagicMock(), {"userId": str(FRIEND), "scope": "guild"})
    assert get.await_args.args[0] == {"guild_id": 555, "kind": "played", "private": {"$ne": True}}


def _playlist_mocks(monkeypatch):
    monkeypatch.setattr("voicelink.ipc.methods._getPlaylist", AsyncMock(return_value={"type": "playlist", "tracks": [], "name": "Favourite"}))
    update_user = AsyncMock()
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.update_user", update_user)
    monkeypatch.setattr("voicelink.ipc.methods.Config.get_playlist_config", lambda self: (5, 500, 10))
    return update_user


async def test_others_cannot_save_someones_cantus_song_to_a_playlist(fake, monkeypatch):
    from voicelink.ipc.methods import updatePlaylist
    update_user = _playlist_mocks(monkeypatch)
    reply = await updatePlaylist(MagicMock(), {"userId": str(FRIEND), "type": "addTrack", "playlistId": "200", "trackId": owners_track(fake).track_id})
    assert cantus.FOREIGN_NOTE in str(reply) and update_user.await_count == 0


async def test_the_owner_saves_the_song_id_never_a_play_link(fake, monkeypatch):
    from voicelink.ipc.methods import updatePlaylist
    update_user = _playlist_mocks(monkeypatch)
    await updatePlaylist(MagicMock(), {"userId": str(OWNER), "type": "addTrack", "playlistId": "200", "trackId": owners_track(fake).track_id})
    saved = update_user.await_args.args[1]["$push"]["playlist.200.tracks"]
    assert decode(saved)["identifier"] == f"cantus:{OWNER}:s1" and PLAY_PATH not in saved


async def test_the_queue_payload_never_carries_a_play_link(fake, allow_localhost):
    """What dashboards get when a Cantus song is queued and when it plays."""
    await connected(fake)
    p = _queue_player()
    p._ipc_client = MagicMock(_is_connected=True)
    p._ipc_connection = True
    p.send_ws = AsyncMock()
    track = owners_track(fake)
    await p.add_track(track)
    await cantus.resolve_for_play(player_in(), track)  # a link now exists...
    assert p.send_ws.await_count and PLAY_PATH not in repr(p.send_ws.await_args_list)  # ...but never reached a payload
    assert PLAY_PATH not in track.track_id and PLAY_PATH not in (track.uri or "")


# --- public addresses only ----------------------------------------------------------

@pytest.mark.parametrize("server", [
    "http://127.0.0.1:2333", "http://10.0.0.5", "http://192.168.1.1", "http://172.18.0.3",
    "http://169.254.169.254", "http://[::1]", "http://0.0.0.0", "http://localhost",
])
async def test_private_and_internal_servers_are_refused(fake, server):
    with pytest.raises(cantus.CantusError) as e:
        await cantus.connect_token(OWNER, server, GOOD_TOKEN)
    assert e.value.code in ("private_address", "unresolvable")
    assert fake.calls == [] and OWNER not in fake.stored


@pytest.mark.parametrize("raw", ["", "ftp://x.example", "https://user:pw@x.example", "https://x.example:99999"])
def test_malformed_server_addresses_are_refused(raw):
    with pytest.raises(cantus.CantusError):
        cantus.normalize_server(raw)


def test_server_addresses_are_normalized():
    assert cantus.normalize_server("music.example.com/?x=1#y") == "https://music.example.com"
    assert cantus.normalize_server("https://example.com/cantus-prefix/") == "https://example.com/cantus-prefix"  # issue #2: path prefix kept
    assert cantus.normalize_server("http://music.example.com:8080/") == "http://music.example.com:8080"


def test_a_play_link_on_another_server_is_refused():
    assert cantus._full_url("/cantus/api/v1/play/s1?s=x", "https://a.example") == "https://a.example/cantus/api/v1/play/s1?s=x"
    with pytest.raises(cantus.CantusError):
        cantus._full_url("http://lavalink:2333/v4/sessions", "https://a.example")


async def test_the_resolver_refuses_names_pointing_inside(monkeypatch):
    resolver = cantus._PublicResolver()
    monkeypatch.setattr(resolver._inner, "resolve", AsyncMock(return_value=[{"host": "172.18.0.2", "port": 80}]))
    with pytest.raises(OSError):
        await resolver.resolve("sneaky.example", 80)
    monkeypatch.setattr(resolver._inner, "resolve", AsyncMock(return_value=[{"host": "93.184.216.34", "port": 80}]))
    assert (await resolver.resolve("ok.example", 80))[0]["host"] == "93.184.216.34"


# --- loudness -----------------------------------------------------------------------

async def test_unmeasured_songs_get_the_typical_gain_and_measured_ones_their_own(fake, allow_localhost, monkeypatch):
    monkeypatch.setattr("voicelink.stream_cache.get_typical_gain", AsyncMock(return_value=0.5))
    await connected(fake)
    assert (await cantus.resolve_for_play(player_in(), owners_track(fake)))[1] == 0.5
    cantus._gains["127.0.0.1:s1"] = 0.7
    assert (await cantus.resolve_for_play(player_in(), owners_track(fake)))[1] == 0.7


async def test_the_measurement_never_logs_the_play_link(fake, monkeypatch):
    seen = {}

    async def measure(source, low_priority=False):
        seen["str"], seen["name"] = str(source), source.name
        return 0.8

    monkeypatch.setattr("voicelink.stream_cache.StreamCache._measure_gain", staticmethod(measure))
    monkeypatch.setattr("voicelink.mongodb.MongoDBHandler.set_cantus_gain", AsyncMock())
    await cantus._measure("h:s1", "http://host/cantus/api/v1/play/s1?s=SECRET")
    assert seen == {"str": "http://host/cantus/api/v1/play/s1?s=SECRET", "name": "cantus:h:s1"}


async def test_player_play_uses_the_fresh_cantus_track(fake, monkeypatch):
    from tests.unit.test_player_direct_resolve import make_bare_player
    player = make_bare_player()
    monkeypatch.setattr("voicelink.player.cantus.resolve_for_play", AsyncMock(return_value=("FRESH", 0.6)))
    track = owners_track(fake)
    await player.play(track)
    assert player.send.await_args.kwargs["data"]["encodedTrack"] == "FRESH" and player._loudness_gain == 0.6
    assert player._current is track  # the queue/history keep the metadata track


# --- the dashboard form (cantusSave) -------------------------------------------------

def _bot(owner_ids):
    bot = MagicMock()
    bot.get_user = lambda uid: types.SimpleNamespace(id=uid)
    bot.is_owner = AsyncMock(side_effect=lambda user: user.id in owner_ids)
    return bot


def _dj(monkeypatch, is_dj):
    monkeypatch.setattr("voicelink.ipc.methods._require_cache_manager_permission", AsyncMock(return_value=1 if is_dj else {"op": "errorMsg"}))


async def test_a_member_only_pastes_a_token_and_the_default_server_is_used(fake, allow_localhost, monkeypatch):
    from voicelink.ipc.methods import cantusSave
    _dj(monkeypatch, False)
    reply = await cantusSave(_bot(set()), {"userId": str(OWNER), "server": "https://ignored.example", "token": GOOD_TOKEN})
    assert reply["ok"] and reply["connected"] and fake.stored[OWNER]["server"] == fake.server
    assert reply["canSetServer"] is False and GOOD_TOKEN not in repr(reply)


async def test_an_admin_sets_the_default_server_and_connects(fake, allow_localhost, monkeypatch):
    from voicelink.ipc.methods import cantusSave
    _dj(monkeypatch, True)
    Config({"client_id": 1})
    reply = await cantusSave(_bot(set()), {"userId": str(OWNER), "server": fake.server, "token": GOOD_TOKEN})
    assert reply["ok"] and "Default server saved" in reply["message"] and "connected as babab1t" in reply["message"]
    assert reply["canSetServer"] and cantus.base_url() == fake.server and "app_key" not in repr(fake.saved_default)


async def test_nothing_typed_is_reported(fake, monkeypatch):
    from voicelink.ipc.methods import cantusSave
    _dj(monkeypatch, False)
    reply = await cantusSave(_bot(set()), {"userId": str(OWNER), "server": ""})
    assert reply["ok"] is False and "connect code" in reply["message"]


async def test_the_dashboard_explains_a_refused_server(fake, monkeypatch):
    from voicelink.ipc.methods import cantusSave
    _dj(monkeypatch, True)
    Config({"client_id": 1})
    reply = await cantusSave(_bot(set()), {"userId": str(OWNER), "server": "http://10.0.0.1", "token": GOOD_TOKEN})
    assert reply["ok"] is False and "public" in reply["message"] and not reply["connected"]


def test_dashboard_messages_with_a_token_are_never_logged():
    from voicelink.ipc.client import _redact
    assert "cfa_secret" not in _redact({"op": "cantusSave", "server": "https://x", "token": "cfa_secret"})
    assert _redact({"op": "x", "token": "abc"}) == "{'op': 'x', 'token': '…'}"
    assert "cfa_zzz" not in _redact("something cfa_zzz something")


# --- the dashboard's Cantus tab ------------------------------------------------------

async def test_the_tab_lists_your_songs_with_queueable_ids_and_no_play_link(fake, allow_localhost):
    from voicelink.ipc.methods import cantusSongs
    await connected(fake)
    reply = await cantusSongs(MagicMock(), {"userId": str(OWNER), "query": "one", "offset": 0})
    assert reply["op"] == "cantusSongs" and reply["query"] == "one" and not reply["append"]
    song = reply["songs"][0]
    assert (song["title"], song["artists"], song["quality"], song["durationMs"]) == ("Song One", "Artist", "16-bit/44.1kHz", 184000)
    assert decode(song["trackId"])["identifier"] == f"cantus:{OWNER}:s1"
    assert PLAY_PATH not in repr(reply) and GOOD_TOKEN not in repr(reply)
    assert fake.calls[-1][2] == {"q": "one", "limit": "50"}


async def test_the_tab_pages_on_with_offset(fake, allow_localhost):
    from voicelink.ipc.methods import cantusSongs
    await connected(fake)
    reply = await cantusSongs(MagicMock(), {"userId": str(OWNER), "query": "", "offset": 50})
    assert reply["append"] and fake.calls[-1][2] == {"limit": "50", "offset": "50"}


async def test_the_tab_lists_playlists_and_a_playlists_songs(fake, allow_localhost):
    from voicelink.ipc.methods import cantusPlaylist, cantusPlaylists
    await connected(fake)
    lists = await cantusPlaylists(MagicMock(), {"userId": str(OWNER)})
    assert lists["playlists"] == [{"id": "p1", "name": "Road trip", "songs": 2}]
    one = await cantusPlaylist(MagicMock(), {"userId": str(OWNER), "playlistId": "p1"})
    assert one["name"] == "Road trip" and [s["title"] for s in one["songs"]] == ["Song One", "Song Two"]
    assert all(decode(s["trackId"])["identifier"].startswith(f"cantus:{OWNER}:") for s in one["songs"])
    assert PLAY_PATH not in repr(one)


async def test_the_tab_tells_someone_not_connected_to_connect(fake):
    from voicelink.ipc.methods import cantusSongs
    reply = await cantusSongs(MagicMock(), {"userId": str(FRIEND)})
    assert reply["notConnected"] and "Settings" in reply["error"] and "songs" not in reply and fake.calls == []


async def test_the_tab_only_ever_shows_your_own_library(fake, allow_localhost):
    """Two connected people, two libraries: each sees only their own."""
    from voicelink.ipc.methods import cantusSongs
    await connected(fake)
    await connected(fake, FRIEND, FRIEND_TOKEN)
    mine = await cantusSongs(MagicMock(), {"userId": str(OWNER)})
    theirs = await cantusSongs(MagicMock(), {"userId": str(FRIEND)})
    assert [s["title"] for s in mine["songs"]] == ["Song One"] and theirs["songs"] == []



# --- issue #2: one connect code ------------------------------------------------------

@pytest.mark.parametrize("raw, server, token", [
    ("cantus+https://cfa_abc123@music.bababm.win", "https://music.bababm.win", "cfa_abc123"),
    ("  cantus+http://cfa_abc123@195.85.19.166:6080  ", "http://195.85.19.166:6080", "cfa_abc123"),
    ("cantus+https://cfa_abc123@example.com/cantus-prefix/", "https://example.com/cantus-prefix", "cfa_abc123"),
    ("cfa_abc123", None, "cfa_abc123"),
])
def test_connect_codes_and_bare_tokens_are_parsed(raw, server, token):
    assert cantus.parse_connect_code(raw) == (server, token)


@pytest.mark.parametrize("raw", [
    "", "hello", "https://music.bababm.win", "cantus+https://music.bababm.win",
    "cantus+https://abc123@music.bababm.win",  # a token without cfa_
    "cantus+ftp://cfa_x@music.bababm.win", "cantus+https://cfa_x@", "cfa_with space", "cfa_x@host",
])
def test_anything_else_is_not_a_connect_code(raw):
    with pytest.raises(cantus.CantusError) as e:
        cantus.parse_connect_code(raw)
    assert e.value.code == "bad_code"


async def test_a_connect_code_stores_its_own_server_per_person(fake, allow_localhost, monkeypatch):
    """The code can point at a different Cantus than the default; each
    person keeps their own server."""
    from voicelink.ipc.methods import cantusSave
    _dj(monkeypatch, False)
    Config({"client_id": 1, "cantus": {"url": "https://default.example"}})
    code = fake.server.replace("http://", f"cantus+http://{GOOD_TOKEN}@")
    reply = await cantusSave(_bot(set()), {"userId": str(OWNER), "code": code})
    assert reply["ok"] and reply["connected"] and reply["server"] == fake.server
    assert fake.stored[OWNER]["server"] == fake.server and fake.stored[OWNER]["token"] == GOOD_TOKEN
    assert GOOD_TOKEN not in repr(reply) and code not in repr(reply)
    assert [s["id"] for s in (await cantus.search_songs(OWNER))["songs"]] == ["s1"]  # their own server is used


async def test_a_bare_token_still_uses_the_default_server(fake, allow_localhost, monkeypatch):
    from voicelink.ipc.methods import cantusSave
    _dj(monkeypatch, False)
    reply = await cantusSave(_bot(set()), {"userId": str(OWNER), "code": GOOD_TOKEN})
    assert reply["ok"] and fake.stored[OWNER]["server"] == fake.server


async def test_a_bare_token_without_a_default_server_asks_for_the_whole_code(fake, monkeypatch):
    from voicelink.ipc.methods import cantusSave
    _dj(monkeypatch, False)
    Config({"client_id": 1})
    reply = await cantusSave(_bot(set()), {"userId": str(OWNER), "code": GOOD_TOKEN})
    assert reply["ok"] is False and "whole connect code" in reply["message"] and fake.calls == []


@pytest.mark.parametrize("code, wanted", [
    ("not a code", "isn't a Cantus connect code or token"),
    ("cantus+https://cfa_wrong@{host}", "refused (revoked or mistyped)"),
])
async def test_connect_code_errors_in_plain_words(fake, allow_localhost, monkeypatch, code, wanted):
    from voicelink.ipc.methods import cantusSave
    _dj(monkeypatch, False)
    code = code.replace("https://", "http://").replace("{host}", fake.server.split("://")[1])
    reply = await cantusSave(_bot(set()), {"userId": str(OWNER), "code": code})
    assert reply["ok"] is False and wanted in reply["message"] and OWNER not in fake.stored


async def test_an_unreachable_cantus_is_named(fake, monkeypatch):
    from voicelink.ipc.methods import cantusSave
    _dj(monkeypatch, False)
    monkeypatch.setattr("voicelink.cantus.check_public_url", AsyncMock())
    reply = await cantusSave(_bot(set()), {"userId": str(OWNER), "code": f"cantus+http://{GOOD_TOKEN}@127.0.0.1:9"})
    assert reply["ok"] is False and reply["message"] == "Cantus at http://127.0.0.1:9 didn't answer."


async def test_people_connected_before_issue_2_keep_working(fake, allow_localhost):
    """Migration: a connection saved with the old two-field form - exactly
    what's stored on the live bot (server, token, username, connected_at) -
    plays, searches and shows as connected with no change."""
    fake.stored[OWNER] = {"_id": OWNER, "server": fake.server, "token": GOOD_TOKEN, "username": "babab1t", "connected_at": 1760900000.0}
    state = await cantus.status(OWNER)
    assert state["connected"] and state["username"] == "babab1t" and state["tokenHint"] == "cfa_…1234"
    assert (await cantus.resolve_for_play(player_in(), owners_track(fake)))[0] == "LAVALINK_ENCODED"


async def test_a_page_loaded_before_issue_2_can_still_save(fake, allow_localhost, monkeypatch):
    """An open dashboard tab with the old JS still sends server + token."""
    from voicelink.ipc.methods import cantusSave
    _dj(monkeypatch, False)
    reply = await cantusSave(_bot(set()), {"userId": str(OWNER), "server": fake.server, "token": GOOD_TOKEN})
    assert reply["ok"] and fake.stored[OWNER]["token"] == GOOD_TOKEN


def test_a_play_link_under_a_path_prefix():
    server = "https://example.com/cantus-prefix"
    assert cantus._full_url("/cantus-prefix/cantus/api/v1/play/s1?s=x", server) == "https://example.com/cantus-prefix/cantus/api/v1/play/s1?s=x"
    assert cantus._full_url("/cantus/api/v1/play/s1?s=x", server) == "https://example.com/cantus-prefix/cantus/api/v1/play/s1?s=x"


def test_a_connect_code_is_never_logged():
    from voicelink.ipc.client import _redact
    assert "cfa_secret" not in _redact({"op": "cantusSave", "code": "cantus+https://cfa_secret@x.example"})
    assert "cfa_secret" not in _redact("pasted cantus+https://cfa_secret@x.example by mistake")


# --- Patch 47: "similar songs" for a Cantus song -------------------------------------

class _RecNode:
    """Lavalink's loadtracks: a YouTube Music search, then a YouTube mix."""

    def __init__(self, search_ids, mix_ids):
        self._available = True
        self.bot = MagicMock()
        self.queries = []
        self.search_ids, self.mix_ids = search_ids, mix_ids

    async def get_tracks(self, query, requester=None):
        self.queries.append(query)
        if query.startswith("ytmsearch:"):
            return [make_yt(None, v) for v in self.search_ids]
        return [make_yt(None, v) for v in self.mix_ids]


async def test_a_cantus_song_gets_youtube_recommendations_via_its_youtube_match(fake):
    node = _RecNode(["match1", "other"], ["match1", "rec1", "rec2"])
    recs = await owners_track(fake).get_recommendations(node)
    assert node.queries[0] == "ytmsearch:Artist - Song One"
    assert node.queries[1] == "https://www.youtube.com/watch?v=match1&list=RDmatch1"
    assert [t.identifier for t in recs] == ["rec1", "rec2"]  # not the YouTube copy of the same song


async def test_no_youtube_match_means_no_recommendations(fake):
    node = _RecNode([], ["x"])
    assert await owners_track(fake).get_recommendations(node) == []
    assert len(node.queries) == 1


async def test_youtube_songs_still_get_their_own_mix(fake):
    node = _RecNode([], ["rec1"])
    recs = await make_yt(OWNER, "abc").get_recommendations(node)
    assert node.queries == ["https://www.youtube.com/watch?v=abc&list=RDabc"] and [t.identifier for t in recs] == ["rec1"]
