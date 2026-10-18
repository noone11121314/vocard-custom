"""Unit tests for Player._record_guild_play_history (local-patches.md's
Patch 9: dashboard Home/Explore pages built from real, per-guild play
history instead of a hardcoded demo playlist / one user's own 25-track cap).

Only this one helper is unit tested in isolation, same approach as
_report_cache_stats/_retry_direct_resolve in test_player_direct_resolve.py -
do_next() itself (which fires this fire-and-forget) is too entangled to be
worth mocking just to cover its one new line.
"""
from __future__ import annotations

import hashlib

from unittest.mock import AsyncMock, MagicMock

from voicelink.player import Player

from tests.conftest import make_track


def make_bare_player() -> Player:
    player = Player.__new__(Player)
    player._logger = MagicMock()
    player._guild = MagicMock(id=555, name="Test Guild")
    return player


async def test_records_a_searched_track_under_the_bot_history_bucket(monkeypatch):
    player = make_bare_player()
    update_settings = AsyncMock(return_value=True)
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", update_settings)
    track = make_track(source="youtube", identifier="vidA", track_id="ENC_A", title="Song A")

    await player._record_guild_play_history(track)

    update_settings.assert_awaited_once()
    guild_id, data = update_settings.call_args.args
    kwargs = update_settings.call_args.kwargs
    assert guild_id == 555
    assert kwargs.get("upsert") is True
    assert data["$push"] == {"botHistory": {"$each": ["ENC_A"], "$slice": -60}}

    key = hashlib.sha1(b"vidA").hexdigest()
    assert data["$inc"] == {f"botTopCounts.{key}.count": 1}
    assert data["$set"] == {
        f"botTopCounts.{key}.trackId": "ENC_A",
        f"botTopCounts.{key}.title": "Song A",
    }


async def test_records_an_attachment_track_under_the_upload_history_bucket(monkeypatch):
    """A Discord-message-attachment play decodes with source "http"/"local"
    (see Track.source / Lavalink's generic probe decoder) rather than
    youtube/spotify/etc - that's the only signal distinguishing a
    self-uploaded play from a normal searched one.
    """
    player = make_bare_player()
    update_settings = AsyncMock(return_value=True)
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", update_settings)
    track = make_track(source="http", identifier=None, uri="https://cdn.discordapp.com/x.mp3", track_id="ENC_B", title="My Upload")

    await player._record_guild_play_history(track)

    data = update_settings.call_args.args[1]
    assert data["$push"] == {"uploadHistory": {"$each": ["ENC_B"], "$slice": -60}}

    key = hashlib.sha1(b"https://cdn.discordapp.com/x.mp3").hexdigest()
    assert f"uploadTopCounts.{key}.count" in data["$inc"]


async def test_treats_local_source_the_same_as_http_as_an_upload(monkeypatch):
    player = make_bare_player()
    update_settings = AsyncMock(return_value=True)
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", update_settings)
    track = make_track(source="local", identifier="file1", track_id="ENC_C")

    await player._record_guild_play_history(track)

    data = update_settings.call_args.args[1]
    assert "uploadHistory" in data["$push"]


async def test_spotify_and_soundcloud_both_count_as_bot_not_upload(monkeypatch):
    player = make_bare_player()
    update_settings = AsyncMock(return_value=True)
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", update_settings)

    for source in ("spotify", "soundcloud", "deezer", "applemusic"):
        update_settings.reset_mock()
        track = make_track(source=source, identifier=f"id-{source}", track_id="ENC")
        await player._record_guild_play_history(track)
        data = update_settings.call_args.args[1]
        assert "botHistory" in data["$push"], f"{source} should count as a bot/searched play"


async def test_falls_back_to_uri_when_identifier_is_missing(monkeypatch):
    player = make_bare_player()
    update_settings = AsyncMock(return_value=True)
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", update_settings)
    track = make_track(source="youtube", identifier=None, uri="https://example.com/a", track_id="ENC")

    await player._record_guild_play_history(track)

    data = update_settings.call_args.args[1]
    key = hashlib.sha1(b"https://example.com/a").hexdigest()
    assert f"botTopCounts.{key}.count" in data["$inc"]


async def test_never_raises_when_the_database_write_fails(monkeypatch):
    """Fire-and-forget from do_next(): a Mongo outage must never surface as
    a playback-breaking exception.
    """
    player = make_bare_player()
    monkeypatch.setattr(
        "voicelink.player.MongoDBHandler.update_settings",
        AsyncMock(side_effect=ConnectionError("db down")),
    )
    track = make_track(source="youtube", identifier="vidA", track_id="ENC")

    await player._record_guild_play_history(track)  # must not raise

    player._logger.debug.assert_called_once()


# --- _record_guild_added_history (Patch 17: "Recently added" list) --------


async def test_records_every_added_track_id_regardless_of_source(monkeypatch):
    player = make_bare_player()
    update_settings = AsyncMock(return_value=True)
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", update_settings)
    tracks = [
        make_track(source="youtube", track_id="ENC_A"),
        make_track(source="spotify", track_id="ENC_B"),
        make_track(source="http", track_id="ENC_C"),
    ]

    await player._record_guild_added_history(tracks)

    update_settings.assert_awaited_once()
    guild_id, data = update_settings.call_args.args
    kwargs = update_settings.call_args.kwargs
    assert guild_id == 555
    assert kwargs.get("upsert") is True
    assert data["$push"] == {"addedHistory": {"$each": ["ENC_A", "ENC_B", "ENC_C"], "$slice": -100}}


async def test_does_nothing_for_an_empty_track_list(monkeypatch):
    player = make_bare_player()
    update_settings = AsyncMock(return_value=True)
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_settings", update_settings)

    await player._record_guild_added_history([])

    update_settings.assert_not_called()


async def test_added_history_never_raises_when_the_database_write_fails(monkeypatch):
    player = make_bare_player()
    monkeypatch.setattr(
        "voicelink.player.MongoDBHandler.update_settings",
        AsyncMock(side_effect=ConnectionError("db down")),
    )
    tracks = [make_track(source="youtube", track_id="ENC_A")]

    await player._record_guild_added_history(tracks)  # must not raise

    player._logger.debug.assert_called_once()
