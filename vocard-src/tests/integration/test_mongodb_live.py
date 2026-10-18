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
    for attr in ("_client", "_db", "_settings_db", "_users_db"):
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
