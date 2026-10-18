"""Unit tests for Patch 10 (per-track loudness normalization) on
voicelink.player.Player: _filters_payload_with_gain, play()'s gain
computation/reset, and add_filter/remove_filter/reset_filter preserving the
current gain across user-driven Effects changes.

Player is constructed via __new__ to skip its heavy constructor, same
approach as test_player_direct_resolve.py.
"""
from __future__ import annotations

import asyncio

from unittest.mock import AsyncMock, MagicMock

from voicelink.filters import Filters, Tremolo
from voicelink.player import Player

from tests.conftest import make_track


def make_bare_player() -> Player:
    player = Player.__new__(Player)
    player._node = MagicMock()
    player._node.yt_ratelimit = None
    player._node.get_direct_resolve_cache_stats = MagicMock(return_value={"entries": 0, "approxBytes": 0})
    player._node.resolve_direct_track = AsyncMock(return_value=None)
    player._node.prefetch_direct_track = AsyncMock(return_value=None)
    player._node.get_track_gain = AsyncMock(return_value=1.0)
    player._filters = Filters()
    player._volume = 100
    player._loudness_gain = 1.0
    player._logger = MagicMock()
    player.send = AsyncMock()
    player.send_ws = AsyncMock()
    player._current = None
    player._bot = MagicMock()
    player._bot.loop = asyncio.get_event_loop()
    player._guild = MagicMock()
    player.queue = MagicMock()
    player.queue._allow_duplicate = True
    player.queue._queue = []
    player.channel = MagicMock(members=[])
    player._is_connected = False
    player._ipc_client = MagicMock(_is_connected=False)
    player._ipc_connection = False
    return player


# --- _filters_payload_with_gain --------------------------------------------


def test_payload_with_gain_includes_volume_even_when_neutral():
    player = make_bare_player()

    assert player._filters_payload_with_gain() == {"volume": 1.0}


def test_payload_with_gain_merges_with_active_user_effects():
    player = make_bare_player()
    player._loudness_gain = 1.5
    player._filters.add_filter(filter=Tremolo())

    payload = player._filters_payload_with_gain()

    assert payload["volume"] == 1.5
    assert "tremolo" in payload


# --- play() computes/resets the gain ---------------------------------------


async def test_play_applies_the_measured_gain_for_a_youtube_track():
    player = make_bare_player()
    player._node.get_track_gain = AsyncMock(return_value=1.4)
    track = make_track(source="youtube", identifier="vidA", track_id="ENC")

    await player.play(track)

    player._node.get_track_gain.assert_awaited_once_with("vidA")
    assert player._loudness_gain == 1.4
    assert player.send.call_args.kwargs["data"]["filters"] == {"volume": 1.4}


async def test_play_resets_gain_to_neutral_for_a_non_youtube_track():
    """Regression guard: filters persist across tracks on the Lavalink
    session, so without an explicit reset here, a soundcloud/spotify track
    played right after a boosted youtube one would silently inherit that
    boost instead of playing at its native volume.
    """
    player = make_bare_player()
    player._loudness_gain = 1.8  # left over from a previous youtube track
    track = make_track(source="soundcloud", track_id="ENC")

    await player.play(track)

    assert player._loudness_gain == 1.0
    assert player.send.call_args.kwargs["data"]["filters"] == {"volume": 1.0}


async def test_play_defaults_to_neutral_for_an_unmeasured_youtube_track():
    player = make_bare_player()
    player._node.get_track_gain = AsyncMock(return_value=1.0)  # nothing measured yet
    track = make_track(source="youtube", identifier="vidB", track_id="ENC")

    await player.play(track)

    assert player.send.call_args.kwargs["data"]["filters"] == {"volume": 1.0}


async def test_play_combines_gain_with_active_user_effects_in_one_payload():
    player = make_bare_player()
    player._filters.add_filter(filter=Tremolo())
    player._node.get_track_gain = AsyncMock(return_value=1.3)
    track = make_track(source="youtube", identifier="vidC", track_id="ENC")

    await player.play(track)

    sent_filters = player.send.call_args.kwargs["data"]["filters"]
    assert sent_filters["volume"] == 1.3
    assert "tremolo" in sent_filters


# --- add_filter/remove_filter/reset_filter preserve the current gain -------


async def test_add_filter_preserves_the_current_gain():
    player = make_bare_player()
    player._loudness_gain = 1.2

    await player.add_filter(filter=Tremolo())

    assert player.send.call_args.kwargs["data"]["filters"]["volume"] == 1.2


async def test_remove_filter_preserves_the_current_gain():
    player = make_bare_player()
    player._loudness_gain = 1.2
    player._filters.add_filter(filter=Tremolo())

    await player.remove_filter(filter_tag="tremolo")

    assert player.send.call_args.kwargs["data"]["filters"]["volume"] == 1.2


async def test_reset_filter_clears_user_effects_but_keeps_the_gain():
    player = make_bare_player()
    player._loudness_gain = 1.2
    player._filters.add_filter(filter=Tremolo())

    await player.reset_filter()

    sent_filters = player.send.call_args.kwargs["data"]["filters"]
    assert sent_filters == {"volume": 1.2}  # tremolo gone, gain untouched
