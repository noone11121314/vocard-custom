"""Unit tests for the loadFailed-retry addition in cogs/listeners.py:
on_voicelink_track_end's branch to Player._retry_direct_resolve instead of
do_next(), on_voicelink_track_exception's messaging, and the shared
_is_retryable_direct_resolve eligibility check.

Player is a MagicMock standing in for the real voicelink.Player - these tests
only exercise the Listeners cog's own branching logic. Player._retry_direct_resolve
itself (resuming playback with a freshly-resolved url) is covered in
test_player_direct_resolve.py.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from cogs.listeners import Listeners

from tests.conftest import make_track


@pytest.fixture
def cog() -> Listeners:
    return Listeners.__new__(Listeners)


def make_player(retry_counts: dict | None = None) -> MagicMock:
    player = MagicMock()
    player._direct_resolve_retry_counts = {} if retry_counts is None else retry_counts
    player.MAX_DIRECT_RESOLVE_RETRIES = 2
    player.do_next = AsyncMock()
    player._retry_direct_resolve = AsyncMock()
    player.context.send = AsyncMock()
    player._track_is_stuck = False
    return player


# --- _is_retryable_direct_resolve ----------------------------------------


def test_is_retryable_for_fresh_youtube_track(cog):
    player = make_player()
    track = make_track(source="youtube", identifier="vidA")

    assert cog._is_retryable_direct_resolve(player, track) is True


def test_is_not_retryable_for_non_youtube_track(cog):
    player = make_player()
    track = make_track(source="soundcloud", identifier="vidA")

    assert cog._is_retryable_direct_resolve(player, track) is False


def test_is_not_retryable_without_identifier(cog):
    player = make_player()
    track = make_track(source="youtube", identifier=None)

    assert cog._is_retryable_direct_resolve(player, track) is False


def test_is_not_retryable_once_budget_exhausted(cog):
    player = make_player(retry_counts={"vidA": 2})
    track = make_track(source="youtube", identifier="vidA")

    assert cog._is_retryable_direct_resolve(player, track) is False


def test_is_retryable_with_one_retry_left(cog):
    player = make_player(retry_counts={"vidA": 1})
    track = make_track(source="youtube", identifier="vidA")

    assert cog._is_retryable_direct_resolve(player, track) is True


# --- on_voicelink_track_end -----------------------------------------------


async def test_track_end_retries_on_load_failed_within_budget(cog):
    player = make_player()
    track = make_track(source="youtube", identifier="vidA")

    await cog.on_voicelink_track_end(player, track, "loadFailed", 45000.0)

    player._retry_direct_resolve.assert_awaited_once_with(track, 45000.0)
    player.do_next.assert_not_called()
    assert player._direct_resolve_retry_counts["vidA"] == 1


async def test_track_end_increments_retry_count_on_successive_failures(cog):
    player = make_player(retry_counts={"vidA": 1})
    track = make_track(source="youtube", identifier="vidA")

    await cog.on_voicelink_track_end(player, track, "loadFailed", 10000.0)

    assert player._direct_resolve_retry_counts["vidA"] == 2
    player._retry_direct_resolve.assert_awaited_once()


async def test_track_end_falls_back_to_do_next_once_budget_exhausted(cog):
    player = make_player(retry_counts={"vidA": 2})
    track = make_track(source="youtube", identifier="vidA")

    await cog.on_voicelink_track_end(player, track, "loadFailed", 10000.0)

    player.do_next.assert_awaited_once()
    player._retry_direct_resolve.assert_not_called()


async def test_track_end_calls_do_next_for_a_normal_finish(cog):
    player = make_player()
    track = make_track(source="youtube", identifier="vidA")

    await cog.on_voicelink_track_end(player, track, "finished", 0.0)

    player.do_next.assert_awaited_once()
    player._retry_direct_resolve.assert_not_called()


async def test_track_end_calls_do_next_for_non_youtube_load_failed(cog):
    player = make_player()
    track = make_track(source="soundcloud", identifier="vidA")

    await cog.on_voicelink_track_end(player, track, "loadFailed", 10000.0)

    player.do_next.assert_awaited_once()
    player._retry_direct_resolve.assert_not_called()


async def test_track_end_does_not_mutate_retry_counts_when_not_retrying(cog):
    player = make_player()
    track = make_track(source="youtube", identifier="vidA")

    await cog.on_voicelink_track_end(player, track, "finished", 0.0)

    assert player._direct_resolve_retry_counts == {}


# --- on_voicelink_track_exception -----------------------------------------


async def test_track_exception_sends_reconnecting_message_when_retryable(cog):
    player = make_player()
    track = make_track(source="youtube", identifier="vidA")

    await cog.on_voicelink_track_exception(player, track, {"message": "boom"})

    sent = player.context.send.call_args.args[0]
    assert "Reconnecting" in sent
    assert player._track_is_stuck is False  # must not have been flagged stuck


async def test_track_exception_sends_next_song_message_and_sets_stuck_when_not_retryable(cog):
    player = make_player(retry_counts={"vidA": 2})
    track = make_track(source="youtube", identifier="vidA")

    await cog.on_voicelink_track_exception(player, track, {"message": "boom"})

    sent = player.context.send.call_args.args[0]
    assert "next song" in sent.lower()
    assert player._track_is_stuck is True


async def test_track_exception_sends_next_song_message_for_non_youtube_track(cog):
    player = make_player()
    track = make_track(source="soundcloud", identifier="vidA")

    await cog.on_voicelink_track_exception(player, track, {"message": "boom"})

    sent = player.context.send.call_args.args[0]
    assert "next song" in sent.lower()
    assert player._track_is_stuck is True


async def test_track_exception_swallows_send_failures(cog):
    player = make_player()
    player.context.send = AsyncMock(side_effect=RuntimeError("channel gone"))
    track = make_track(source="youtube", identifier="vidA")

    await cog.on_voicelink_track_exception(player, track, {"message": "boom"})  # must not raise
