"""Integration tests against the real MongoDB (the vocard-db container),
each in a throwaway database that is dropped afterwards.

Guards against: settings changes not reaching a running player (Patch 32),
the volume not being saved (Patch 27), and the history lists' $push/$slice
caps and per-track counters (Patches 9/17/21) - the kinds of query mistakes
mocked unit tests can't see.

Needs VOCARD_TEST_MONGO_URI (run_tests.sh sets it from .env); skipped
otherwise.
"""
from __future__ import annotations

import asyncio
import hashlib
import os
import uuid
from unittest.mock import MagicMock

import pytest

from voicelink.ipc.methods import getUserHistory
from voicelink.mongodb import MongoDBHandler
from voicelink.player import Player
from voicelink.pool import NodePool

from tests.conftest import make_track

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.environ.get("VOCARD_TEST_MONGO_URI"), reason="VOCARD_TEST_MONGO_URI not set"),
]

GUILD = 555000111


@pytest.fixture
async def db(monkeypatch):
    for attr in ("_client", "_db", "_settings_db", "_users_db", "_history_db"):
        monkeypatch.setattr(MongoDBHandler, attr, None)
    monkeypatch.setattr(MongoDBHandler, "_lock", asyncio.Lock())
    monkeypatch.setattr(MongoDBHandler, "_settings_buffer", {})
    monkeypatch.setattr(MongoDBHandler, "_users_buffer", {})
    monkeypatch.setattr(MongoDBHandler, "_last_access", {})
    name = f"vocard_test_{uuid.uuid4().hex[:10]}"
    await MongoDBHandler.init(os.environ["VOCARD_TEST_MONGO_URI"], name)
    try:
        yield MongoDBHandler
    finally:
        await MongoDBHandler._client.drop_database(name)
        MongoDBHandler._client.close()


async def _raw_settings():
    return await MongoDBHandler._settings_db.find_one({"_id": GUILD})


def _bare_player() -> Player:
    p = Player.__new__(Player)
    p._logger = MagicMock()
    p._guild = MagicMock(id=GUILD)
    p._guild.name = "Test Guild"
    p.send = MagicMock(side_effect=lambda **kw: asyncio.sleep(0))
    p._ipc_client = MagicMock(_is_connected=False)
    p._ipc_connection = False
    p._volume = 100
    p.settings = {}
    p._bot = MagicMock()
    p._bot.loop = asyncio.get_running_loop()
    return p


async def _settle():
    for _ in range(20):  # let fire-and-forget create_task() writes finish
        await asyncio.sleep(0.02)


async def test_settings_are_saved_and_read_back(db):
    await db.update_settings(GUILD, {"$set": {"dj": 999, "lang": "VI"}}, upsert=True)

    raw = await _raw_settings()
    assert raw["dj"] == 999 and raw["lang"] == "VI"
    assert (await db.get_settings(GUILD, force_refresh=True))["dj"] == 999

    await db.update_settings(GUILD, {"$unset": {"dj": None}})
    assert "dj" not in await _raw_settings()


async def test_a_settings_change_reaches_the_running_player(db, monkeypatch):
    player = MagicMock(settings={"lang": "EN"})
    monkeypatch.setattr(NodePool, "_nodes", {"DEFAULT": MagicMock(_players={GUILD: player})})
    await db.update_settings(GUILD, {"$set": {"lang": "EN"}}, upsert=True)

    await db.update_settings(GUILD, {"$set": {"dj": 999}})
    assert player.settings["dj"] == 999

    await db.update_settings(GUILD, {"$unset": {"dj": None}})
    assert "dj" not in player.settings


async def test_volume_changes_are_saved(db):
    player = _bare_player()

    await player.set_volume(70)
    await _settle()

    assert (await _raw_settings())["volume"] == 70


async def test_play_history_is_capped_and_counts_plays_per_track(db, monkeypatch):
    player = _bare_player()
    monkeypatch.setattr(NodePool, "_nodes", {})
    for i in range(65):
        await player._record_guild_play_history(make_track(identifier=f"v{i % 5}", track_id=f"T{i}", title=f"Song {i % 5}"))

    raw = await _raw_settings()
    assert len(raw["botHistory"]) == 60
    assert raw["botHistory"][-1] == "T64"  # newest last
    key = hashlib.sha1(b"v0").hexdigest()
    assert raw["botTopCounts"][key]["count"] == 13  # v0 was played 13 of the 65 times


async def test_added_history_is_capped_at_100(db, monkeypatch):
    player = _bare_player()
    monkeypatch.setattr(NodePool, "_nodes", {})

    await player._record_guild_added_history([make_track(track_id=f"A{i}") for i in range(105)])

    raw = await _raw_settings()
    assert len(raw["addedHistory"]) == 100
    assert raw["addedHistory"][0] == "A5" and raw["addedHistory"][-1] == "A104"


async def test_dotted_counter_keys_never_leak_into_a_live_players_settings(db, monkeypatch):
    live = MagicMock(settings={})
    monkeypatch.setattr(NodePool, "_nodes", {"DEFAULT": MagicMock(_players={GUILD: live})})

    await _bare_player()._record_guild_play_history(make_track(identifier="vx", track_id="TX"))

    assert not any("." in k for k in live.settings)


async def test_user_history_is_read_back_for_the_dashboard(db):
    await db._users_db.insert_one({"_id": 4242, "playlist": {}, "history": ["H1", "H2"], "inbox": []})

    result = await getUserHistory(bot=None, data={"userId": "4242"})

    assert result["history"] == ["H1", "H2"]


async def test_mute_keeps_the_saved_volume_and_unmute_restores_it(db):
    """Patch 42: the dashboard speaker icon."""
    from voicelink.ipc.methods import updateVolume
    player = _bare_player()
    await player.set_volume(60)
    await _settle()

    await updateVolume(player, MagicMock(), {"mute": True, "volume": 0})
    await _settle()
    assert player.volume == 0
    assert (await _raw_settings())["volume"] == 60  # muting didn't overwrite it

    await updateVolume(player, MagicMock(), {"mute": False, "volume": 50})
    await _settle()
    assert player.volume == 60


async def test_songs_added_in_different_servers_land_in_one_personal_list(db, monkeypatch):
    """Patch 44: "Recently added by you" follows the user across servers."""
    import types
    await db._users_db.insert_one({"_id": 7001, "playlist": {}, "history": [], "addedHistory": [], "inbox": []})
    me = types.SimpleNamespace(id=7001, bot=False)
    in_server_a, in_server_b = _bare_player(), _bare_player()
    in_server_b._guild = MagicMock(id=GUILD + 1)

    await in_server_a._record_user_added_history([types.SimpleNamespace(track_id="A1", requester=me)])
    await in_server_b._record_user_added_history([types.SimpleNamespace(track_id="B1", requester=me), types.SimpleNamespace(track_id="B2", requester=me)])

    stored = (await db._users_db.find_one({"_id": 7001}))["addedHistory"]
    assert stored == ["A1", "B1", "B2"]
    result = await getUserHistory(bot=None, data={"userId": "7001"})
    assert result["addedHistory"] == ["A1", "B1", "B2"]


async def test_the_personal_added_list_is_capped_at_100(db):
    import types
    await db._users_db.insert_one({"_id": 7002, "playlist": {}, "history": [], "addedHistory": [], "inbox": []})
    me = types.SimpleNamespace(id=7002, bot=False)

    await _bare_player()._record_user_added_history([types.SimpleNamespace(track_id=f"S{i}", requester=me) for i in range(130)])

    stored = (await db._users_db.find_one({"_id": 7002}))["addedHistory"]
    assert len(stored) == 100 and stored[0] == "S30" and stored[-1] == "S129"


async def test_full_history_is_paged_newest_first_per_user_and_per_server(db, monkeypatch):
    """Patch 45: HistoryEvents - every play/add kept (no cap), read back 100
    at a time, newest first, for "You" (any server) and "This server"."""
    import datetime as _dt
    import types
    from voicelink.ipc.methods import getFullHistory
    me = types.SimpleNamespace(id=7003, bot=False)
    other = types.SimpleNamespace(id=7004, bot=False)
    player = _bare_player()
    for i in range(130):
        t = types.SimpleNamespace(track_id=f"T{i}", requester=me if i % 2 else other, source="youtube", identifier=f"v{i}", uri=None)
        await player._record_history_events("played", [t])
        await asyncio.sleep(0.003)  # distinct milliseconds, as real plays are
    await player._record_history_events("added", [types.SimpleNamespace(track_id="ADD", requester=me, source="youtube", identifier="vadd", uri=None)])
    assert await db._history_db.count_documents({}) == 131

    guild = types.SimpleNamespace(id=GUILD, name="Test Guild")
    bot = MagicMock(get_guild=MagicMock(return_value=guild))
    monkeypatch.setattr("voicelink.ipc.methods._resolve_guild_for_user", lambda bot, uid, data: guild)

    page1 = await getFullHistory(bot, {"userId": "7003", "scope": "guild"})
    assert len(page1["events"]) == 100 and not page1["done"] and page1["guildName"] == "Test Guild"
    assert page1["events"][0]["trackId"] == "T129"  # newest first; "added" isn't in the server view
    page2 = await getFullHistory(bot, {"userId": "7003", "scope": "guild", "offset": page1["offset"]})
    assert len(page2["events"]) == 30 and page2["done"] and page2["append"]
    assert {e["trackId"] for e in page1["events"] + page2["events"]} == {f"T{i}" for i in range(130)}

    mine = await getFullHistory(bot, {"userId": "7003", "scope": "user"})
    assert mine["events"][0] == {"trackId": "ADD", "kind": "added", "at": mine["events"][0]["at"], "guildName": "Test Guild", "plays": 0, "adds": 1}
    assert len(mine["events"]) == 66 and mine["done"]  # 65 plays + 1 add, only mine
    assert _dt.datetime.fromisoformat(mine["events"][0]["at"]).tzinfo is not None


async def test_a_duplicate_import_event_is_skipped_not_fatal(db):
    import datetime as _dt
    at = _dt.datetime(2026, 10, 1, 12, 0, tzinfo=_dt.timezone.utc)
    e = {"kind": "played", "guild_id": GUILD, "user_id": None, "track_id": "X", "video_id": "vx", "at": at, "source": "import"}
    await db.add_history_events([e])
    await db.add_history_events([e, dict(e, video_id="vy", track_id="Y")])
    assert await db._history_db.count_documents({}) == 2


async def test_full_history_lists_each_song_once_at_its_latest_time(db, monkeypatch):
    """A song played three times (in two servers) and added once is one row:
    at its latest time, with the counts - not four rows."""
    import types
    from voicelink.ipc.methods import getFullHistory
    me = types.SimpleNamespace(id=7005, bot=False)
    song = lambda tid, vid: types.SimpleNamespace(track_id=tid, requester=me, source="youtube", identifier=vid, uri=None)
    a, b = _bare_player(), _bare_player()
    b._guild = MagicMock(id=GUILD + 1)
    for p, kind, t in [(a, "added", song("S", "vs")), (a, "played", song("S", "vs")), (a, "played", song("O", "vo")), (b, "played", song("S2", "vs")), (b, "played", song("S2", "vs"))]:
        await p._record_history_events(kind, [t])
        await asyncio.sleep(0.003)
    monkeypatch.setattr("voicelink.ipc.methods._resolve_guild_for_user", lambda bot, uid, data: None)
    bot = MagicMock()
    bot.get_guild.side_effect = lambda gid: types.SimpleNamespace(name=f"G{gid}")

    mine = await getFullHistory(bot, {"userId": "7005", "scope": "user"})

    rows = [(e["trackId"], e["plays"], e["adds"], e["guildName"]) for e in mine["events"]]
    assert rows == [("S2", 3, 1, f"G{GUILD + 1}"), ("O", 1, 0, f"G{GUILD}")]


async def test_paging_never_drops_songs_added_at_the_same_moment(db, monkeypatch):
    """A 150-song playlist added at once: all share one timestamp, and both
    pages together still list every song exactly once."""
    import types
    from voicelink.ipc.methods import getFullHistory
    me = types.SimpleNamespace(id=7006, bot=False)
    await _bare_player()._record_history_events("added", [types.SimpleNamespace(track_id=f"P{i}", requester=me, source="youtube", identifier=f"p{i}", uri=None) for i in range(150)])
    p1 = await getFullHistory(MagicMock(), {"userId": "7006", "scope": "user"})
    p2 = await getFullHistory(MagicMock(), {"userId": "7006", "scope": "user", "offset": p1["offset"]})
    ids = [e["trackId"] for e in p1["events"] + p2["events"]]
    assert len(ids) == 150 and len(set(ids)) == 150 and p2["done"]
