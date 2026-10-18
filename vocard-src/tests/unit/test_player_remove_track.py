"""Unit tests for Player.remove_track()'s expected_track_id verification.

Context (see docs/local-patches.md's Patch 19): the dashboard's "remove"
context-menu action sends a queue *index* captured whenever the context menu
was opened - a snapshot that can go stale if the queue changes (another
add/remove, or the current track advancing) before the user actually clicks
remove. The backend used to trust that index unconditionally, so a stale
index could delete a completely unrelated track. remove_track() now takes an
optional expected_track_id and re-locates the real index by id when the
index no longer points at the track the dashboard thinks it does.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from voicelink.player import Player
from voicelink.queue import Queue

from tests.conftest import make_track


def make_player_with_queue(tracks, position: int = 0) -> Player:
    player = Player.__new__(Player)
    player._logger = MagicMock()
    player._guild = MagicMock(id=555, name="Test Guild")
    player._ipc_client = MagicMock(_is_connected=False)
    player._ipc_connection = None

    queue = Queue(size=1000, allow_duplicate=True, get_msg=lambda key: key)
    queue._queue = list(tracks)
    queue._position = position
    player.queue = queue

    return player


async def test_removes_by_index_when_track_id_matches():
    track_a = make_track(track_id="A", title="Song A")
    track_b = make_track(track_id="B", title="Song B")
    player = make_player_with_queue([track_a, track_b])

    removed = await player.remove_track(1, expected_track_id="A")

    assert list(removed.values())[0].track_id == "A"
    assert player.queue.tracks(incTrack=False) == [track_b]


async def test_falls_back_to_locating_by_track_id_when_index_is_stale():
    """The dashboard thinks "A" is still at index 1, but another add/remove
    has since shifted it to index 2 - removing by the stale index would
    delete "B" (a different song) instead. The real index must be found by
    id so the correct track is removed.
    """
    track_b = make_track(track_id="B", title="Song B")
    track_a = make_track(track_id="A", title="Song A")
    player = make_player_with_queue([track_b, track_a])

    removed = await player.remove_track(1, expected_track_id="A")

    assert list(removed.values())[0].track_id == "A"
    assert player.queue.tracks(incTrack=False) == [track_b]


async def test_returns_empty_when_track_id_not_found_anywhere():
    track_a = make_track(track_id="A", title="Song A")
    track_b = make_track(track_id="B", title="Song B")
    player = make_player_with_queue([track_a, track_b])

    removed = await player.remove_track(1, expected_track_id="does-not-exist")

    assert removed == {}
    assert player.queue.tracks(incTrack=False) == [track_a, track_b]


async def test_removes_by_raw_index_when_no_track_id_given():
    """No expected_track_id (e.g. a future caller that doesn't have one) -
    falls back to the old unverified-index behavior.
    """
    track_a = make_track(track_id="A", title="Song A")
    track_b = make_track(track_id="B", title="Song B")
    player = make_player_with_queue([track_a, track_b])

    removed = await player.remove_track(1)

    assert list(removed.values())[0].track_id == "A"
    assert player.queue.tracks(incTrack=False) == [track_b]


async def test_verification_only_considers_upcoming_tracks_not_history():
    """Index/id resolution must only search the upcoming portion of the
    queue (tracks(incTrack=False)) - a track that's already been played
    (history, before the current position) must never be matched/removed.
    """
    history_track = make_track(track_id="OLD", title="Already played")
    current_track = make_track(track_id="CUR", title="Currently playing")
    upcoming_track = make_track(track_id="A", title="Song A")
    player = make_player_with_queue([history_track, current_track, upcoming_track], position=2)

    removed = await player.remove_track(1, expected_track_id="A")

    assert list(removed.values())[0].track_id == "A"
    assert player.queue.tracks(incTrack=False) == []
    assert player.queue._queue == [history_track, current_track]
