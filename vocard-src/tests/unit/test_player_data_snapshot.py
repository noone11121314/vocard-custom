"""Unit tests for Player.data's queue position snapshot (local-patches.md's
Patch 20: fixes a restart-persistence regression introduced by Patch 18).

Patch 18 removed a `- 1` from cogs/listeners.py's restore logic, reasoning
that Queue.get() reads `self._queue[self._position]` as the next track to
play, so restoring the saved position verbatim should be correct. That
missed that do_next() (the thing restore actually calls to resume playback)
itself calls Queue.get() - which, for a track that's actually mid-playback,
has already been called once and left `queue._position` one past it. Restore
should land back on the *current* track, not the one after it, and
Player.position/is_paused being restored (seek + pause) only make sense
against that same interrupted track.

The fix moves the adjustment to the save side (here), since Player.data is
the one place that definitively knows whether there's a current track (self.
_current) versus a queue that's never been played from yet - restoring to
pick up the right track with no extra arithmetic in cogs/listeners.py.
"""
from __future__ import annotations

from unittest.mock import MagicMock

from voicelink.player import Player
from voicelink.queue import Queue

from tests.conftest import make_track


def make_queue_track(track_id: str):
    track = make_track(track_id=track_id)
    track.data = {"track_id": track.track_id, "requester_id": track.requester.id}
    return track


def make_bare_player(*, queue_tracks, position: int, current=None) -> Player:
    player = Player.__new__(Player)
    player._guild = MagicMock(id=555)
    player.channel = MagicMock(id=777)
    player.dj = MagicMock(id=999)
    player._paused = False
    player._is_connected = True
    player._current = current
    player._last_position = 0
    player._last_update = 0
    player.settings = {}

    queue = Queue(size=1000, allow_duplicate=True, get_msg=lambda key: key)
    queue._queue = list(queue_tracks)
    queue._position = position
    player.queue = queue

    return player


def test_saves_position_one_before_the_live_value_when_something_is_current():
    """queue._position is already one past the current track (Queue.get()
    advances past it when it's first pulled to play) - the saved position
    must point *at* it, not past it, so a restore's do_next() resumes the
    same track instead of skipping to the next one.
    """
    current = make_queue_track("CUR")
    player = make_bare_player(queue_tracks=[current], position=1, current=current)

    assert player.data["queue"]["position"] == 0


def test_saves_the_raw_position_when_nothing_has_ever_played():
    """Position 0 with no current track means the queue has upcoming
    tracks that have never been pulled yet - restoring 0 as-is is already
    correct (do_next() should start from the first track, not skip it).
    """
    upcoming = make_queue_track("UP")
    player = make_bare_player(queue_tracks=[upcoming], position=0, current=None)

    assert player.data["queue"]["position"] == 0


def test_saves_position_one_before_the_live_value_mid_queue():
    history = make_queue_track("OLD")
    current = make_queue_track("CUR")
    upcoming = make_queue_track("NEXT")
    player = make_bare_player(queue_tracks=[history, current, upcoming], position=2, current=current)

    assert player.data["queue"]["position"] == 1


def test_saves_position_one_before_the_live_value_while_a_track_is_still_caching():
    """Patch 23: during Patch 11's blocking pre-cache, do_next() has already
    pulled the track off the queue (Queue.get() advanced _position) but
    play() hasn't set _current yet. A shutdown in that window must still
    resume that same track, not skip it.
    """
    loading = make_queue_track("LOADING")
    player = make_bare_player(queue_tracks=[loading], position=1, current=None)
    player._loading_track = loading

    assert player.has_dequeued_track is True
    assert player.data["queue"]["position"] == 0


def test_has_dequeued_track_is_false_when_idle():
    upcoming = make_queue_track("UP")
    player = make_bare_player(queue_tracks=[upcoming], position=0, current=None)

    assert player.has_dequeued_track is False
    assert player.loading_track is None
