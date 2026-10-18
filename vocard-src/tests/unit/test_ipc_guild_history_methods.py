"""Unit tests for the Patch 9 IPC methods added to voicelink/ipc/methods.py:
_resolve_guild_for_user, _top_tracks_from_counts, getGuildHistory and
getGuildTopTracks. These power the dashboard's Home ("Recently played") and
Explore ("Trending") pages from real per-guild play history (written by
Player._record_guild_play_history, see test_player_guild_history.py)
instead of one user's own 25-track cap or a hardcoded demo playlist.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from voicelink.ipc.methods import (
    _resolve_guild_for_user,
    _top_tracks_from_counts,
    getGuildHistory,
    getGuildAddedHistory,
    getGuildTopTracks,
)


def make_guild(guild_id: int, member_ids=()):
    guild = MagicMock()
    guild.id = guild_id
    members = set(member_ids)
    guild.get_member = MagicMock(side_effect=lambda uid: object() if uid in members else None)
    return guild


def make_bot(guilds_by_id=None, user=None):
    bot = MagicMock()
    guilds_by_id = guilds_by_id or {}
    bot.get_guild = MagicMock(side_effect=lambda gid: guilds_by_id.get(gid))
    bot.get_user = MagicMock(return_value=user)
    return bot


# --- _resolve_guild_for_user -------------------------------------------


def test_resolves_the_preferred_guild_when_user_is_a_member_there():
    guild = make_guild(111, member_ids=[42])
    bot = make_bot(guilds_by_id={111: guild})

    result = _resolve_guild_for_user(bot, 42, {"guildId": "111"})

    assert result is guild


def test_falls_back_to_a_mutual_guild_when_guild_id_is_missing():
    mutual_guild = make_guild(222, member_ids=[42])
    user = MagicMock(mutual_guilds=[mutual_guild])
    bot = make_bot(user=user)

    result = _resolve_guild_for_user(bot, 42, {})

    assert result is mutual_guild


def test_falls_back_to_a_mutual_guild_when_guild_id_is_invalid():
    mutual_guild = make_guild(222, member_ids=[42])
    user = MagicMock(mutual_guilds=[mutual_guild])
    bot = make_bot(user=user)

    result = _resolve_guild_for_user(bot, 42, {"guildId": "not-a-number"})

    assert result is mutual_guild


def test_falls_back_past_the_preferred_guild_when_user_is_not_a_member_there():
    preferred_guild = make_guild(111, member_ids=[])  # user left/never joined
    mutual_guild = make_guild(222, member_ids=[42])
    user = MagicMock(mutual_guilds=[mutual_guild])
    bot = make_bot(guilds_by_id={111: preferred_guild}, user=user)

    result = _resolve_guild_for_user(bot, 42, {"guildId": "111"})

    assert result is mutual_guild


def test_does_not_revisit_the_preferred_guild_as_a_mutual_guild():
    guild = make_guild(111, member_ids=[42])
    user = MagicMock(mutual_guilds=[guild])
    bot = make_bot(guilds_by_id={111: guild}, user=user)

    result = _resolve_guild_for_user(bot, 42, {"guildId": "111"})

    assert result is guild
    guild.get_member.assert_called_once_with(42)  # only checked once, not twice


def test_returns_none_when_no_guild_or_user_resolves():
    bot = make_bot(user=None)

    result = _resolve_guild_for_user(bot, 42, {})

    assert result is None


def test_returns_none_when_user_is_a_member_of_no_mutual_guild():
    mutual_guild = make_guild(222, member_ids=[])
    user = MagicMock(mutual_guilds=[mutual_guild])
    bot = make_bot(user=user)

    result = _resolve_guild_for_user(bot, 42, {})

    assert result is None


# --- _top_tracks_from_counts --------------------------------------------


def test_ranks_by_descending_play_count():
    counts = {
        "a": {"count": 3, "trackId": "ENC_A"},
        "b": {"count": 10, "trackId": "ENC_B"},
        "c": {"count": 1, "trackId": "ENC_C"},
    }

    assert _top_tracks_from_counts(counts) == ["ENC_B", "ENC_A", "ENC_C"]


def test_truncates_to_the_requested_limit():
    counts = {str(i): {"count": i, "trackId": f"ENC_{i}"} for i in range(5)}

    result = _top_tracks_from_counts(counts, limit=2)

    assert result == ["ENC_4", "ENC_3"]


def test_skips_entries_missing_a_track_id():
    counts = {
        "a": {"count": 5, "trackId": "ENC_A"},
        "corrupt": {"count": 99},
    }

    assert _top_tracks_from_counts(counts) == ["ENC_A"]


def test_handles_an_empty_or_missing_counts_map():
    assert _top_tracks_from_counts({}) == []
    assert _top_tracks_from_counts(None) == []


# --- getGuildHistory ------------------------------------------------------


async def test_get_guild_history_returns_both_buckets_for_a_resolved_guild(monkeypatch):
    guild = make_guild(111, member_ids=[42])
    bot = make_bot(guilds_by_id={111: guild})
    monkeypatch.setattr(
        "voicelink.ipc.methods.MongoDBHandler.get_settings",
        AsyncMock(return_value={"botHistory": ["A", "B"], "uploadHistory": ["C"]}),
    )

    result = await getGuildHistory(bot, {"userId": "42", "guildId": "111"})

    assert result["op"] == "getGuildHistory"
    assert result["userId"] == "42"
    assert result["guildId"] == "111"
    assert result["botHistory"] == ["A", "B"]
    assert result["uploadHistory"] == ["C"]


async def test_get_guild_history_returns_empty_lists_when_no_guild_resolves(monkeypatch):
    bot = make_bot(user=None)
    get_settings = AsyncMock()
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_settings", get_settings)

    result = await getGuildHistory(bot, {"userId": "42"})

    assert result == {"op": "getGuildHistory", "userId": "42", "botHistory": [], "uploadHistory": []}
    get_settings.assert_not_called()


async def test_get_guild_history_defaults_missing_fields_to_empty_lists(monkeypatch):
    guild = make_guild(111, member_ids=[42])
    bot = make_bot(guilds_by_id={111: guild})
    monkeypatch.setattr(
        "voicelink.ipc.methods.MongoDBHandler.get_settings",
        AsyncMock(return_value={"_id": 111}),  # nothing ever played here yet
    )

    result = await getGuildHistory(bot, {"userId": "42", "guildId": "111"})

    assert result["botHistory"] == []
    assert result["uploadHistory"] == []


# --- getGuildAddedHistory (Patch 17) ---------------------------------------


async def test_get_guild_added_history_returns_the_list_for_a_resolved_guild(monkeypatch):
    guild = make_guild(111, member_ids=[42])
    bot = make_bot(guilds_by_id={111: guild})
    monkeypatch.setattr(
        "voicelink.ipc.methods.MongoDBHandler.get_settings",
        AsyncMock(return_value={"addedHistory": ["A", "B", "C"]}),
    )

    result = await getGuildAddedHistory(bot, {"userId": "42", "guildId": "111"})

    assert result["op"] == "getGuildAddedHistory"
    assert result["userId"] == "42"
    assert result["guildId"] == "111"
    assert result["addedHistory"] == ["A", "B", "C"]


async def test_get_guild_added_history_returns_empty_list_when_no_guild_resolves(monkeypatch):
    bot = make_bot(user=None)
    get_settings = AsyncMock()
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_settings", get_settings)

    result = await getGuildAddedHistory(bot, {"userId": "42"})

    assert result == {"op": "getGuildAddedHistory", "userId": "42", "addedHistory": []}
    get_settings.assert_not_called()


async def test_get_guild_added_history_defaults_missing_field_to_empty_list(monkeypatch):
    guild = make_guild(111, member_ids=[42])
    bot = make_bot(guilds_by_id={111: guild})
    monkeypatch.setattr(
        "voicelink.ipc.methods.MongoDBHandler.get_settings",
        AsyncMock(return_value={"_id": 111}),
    )

    result = await getGuildAddedHistory(bot, {"userId": "42", "guildId": "111"})

    assert result["addedHistory"] == []


# --- getGuildTopTracks ------------------------------------------------------


async def test_get_guild_top_tracks_ranks_each_bucket_independently(monkeypatch):
    guild = make_guild(111, member_ids=[42])
    bot = make_bot(guilds_by_id={111: guild})
    monkeypatch.setattr(
        "voicelink.ipc.methods.MongoDBHandler.get_settings",
        AsyncMock(return_value={
            "botTopCounts": {"a": {"count": 1, "trackId": "BOT_LOW"}, "b": {"count": 9, "trackId": "BOT_HIGH"}},
            "uploadTopCounts": {"c": {"count": 2, "trackId": "UP_ONLY"}},
        }),
    )

    result = await getGuildTopTracks(bot, {"userId": "42", "guildId": "111"})

    assert result["botTracks"] == ["BOT_HIGH", "BOT_LOW"]
    assert result["uploadTracks"] == ["UP_ONLY"]


async def test_get_guild_top_tracks_returns_empty_lists_when_no_guild_resolves(monkeypatch):
    bot = make_bot(user=None)
    get_settings = AsyncMock()
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_settings", get_settings)

    result = await getGuildTopTracks(bot, {"userId": "42"})

    assert result == {"op": "getGuildTopTracks", "userId": "42", "botTracks": [], "uploadTracks": []}
    get_settings.assert_not_called()
