"""Patch 45: the full (uncapped) history - every song played or added is
recorded as an event, and getFullHistory pages through it for the
dashboard's Full history page."""
from __future__ import annotations

import datetime
import types
from unittest.mock import AsyncMock, MagicMock

from voicelink.ipc.methods import getFullHistory
from voicelink.player import Player

HUMAN = types.SimpleNamespace(id=1, bot=False)
A_BOT = types.SimpleNamespace(id=9, bot=True)


def bare_player() -> Player:
    p = Player.__new__(Player)
    p._logger = MagicMock()
    p._guild = MagicMock(id=555)
    return p


def track(tid, requester, source="youtube", identifier="vid", uri=None):
    return types.SimpleNamespace(track_id=tid, requester=requester, source=source, identifier=identifier, uri=uri)


async def test_events_record_kind_server_requester_and_video(monkeypatch):
    add = AsyncMock()
    monkeypatch.setattr("voicelink.player.MongoDBHandler.add_history_events", add)

    await bare_player()._record_history_events("played", [track("T1", HUMAN, identifier="abc"), track("T2", A_BOT, source="http", identifier="x", uri="http://f/a.mp3")])

    events = add.await_args.args[0]
    assert [(e["kind"], e["guild_id"], e["user_id"], e["track_id"], e["video_id"]) for e in events] == [
        ("played", 555, 1, "T1", "abc"),
        ("played", 555, None, "T2", "http://f/a.mp3"),  # the bot's own (autoplay) songs belong to nobody
    ]
    assert events[0]["at"].tzinfo is not None


async def test_a_database_error_never_breaks_playback(monkeypatch):
    monkeypatch.setattr("voicelink.player.MongoDBHandler.add_history_events", AsyncMock(side_effect=RuntimeError("db down")))
    await bare_player()._record_history_events("added", [track("T1", HUMAN)])  # must not raise


def _event(i):
    return {"track_id": f"T{i}", "kind": "played", "guild_id": 555, "at": datetime.datetime(2026, 10, 1, 12, 0) + datetime.timedelta(minutes=i)}


async def test_user_scope_queries_the_users_songs_and_pages_by_offset(monkeypatch):
    get = AsyncMock(return_value=[_event(2), _event(1)])
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_history_songs", get)
    bot = MagicMock()
    bot.get_guild.return_value.name = "Srv"

    result = await getFullHistory(bot, {"userId": "42", "scope": "user", "offset": 100})

    query, offset, limit = get.await_args.args
    assert query == {"user_id": 42} and offset == 100 and limit == 100
    assert [e["trackId"] for e in result["events"]] == ["T2", "T1"]
    assert result["events"][0]["at"] == "2026-10-01T12:02:00+00:00" and result["events"][0]["guildName"] == "Srv"
    assert result["done"] and result["append"] and result["offset"] == 102


async def test_guild_scope_lists_only_plays_in_the_users_server(monkeypatch):
    get = AsyncMock(return_value=[_event(i) for i in range(100)])
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_history_songs", get)
    monkeypatch.setattr("voicelink.ipc.methods._resolve_guild_for_user", lambda bot, uid, data: types.SimpleNamespace(id=555, name="Srv"))

    result = await getFullHistory(MagicMock(), {"userId": "42", "scope": "guild", "offset": "garbage"})

    query, offset, _ = get.await_args.args
    assert query == {"guild_id": 555, "kind": "played", "private": {"$ne": True}} and offset == 0  # Patch 46: no one's own Cantus songs
    assert result["guildName"] == "Srv" and not result["done"] and not result["append"]


async def test_guild_scope_without_a_shared_server_is_empty(monkeypatch):
    get = AsyncMock()
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_history_songs", get)
    monkeypatch.setattr("voicelink.ipc.methods._resolve_guild_for_user", lambda bot, uid, data: None)

    result = await getFullHistory(MagicMock(), {"userId": "42", "scope": "guild"})

    assert result["events"] == [] and result["done"]
    get.assert_not_awaited()
