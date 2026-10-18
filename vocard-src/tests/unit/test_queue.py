"""Unit tests for voicelink/queue.py's Queue.replace() (local-patches.md
Patch 26). Found by the multi-dashboard sync test: shuffling the history
replaced one slot too many, deleting the currently playing track.
"""
from __future__ import annotations

from voicelink.queue import Queue


def make_queue(items, position):
    q = Queue(size=100, allow_duplicate=True, get_msg=lambda *k: "msg")
    q._queue = list(items)
    q._position = position
    return q


def test_replacing_the_history_keeps_the_current_track_and_the_upcoming_ones():
    # h1 h2 h3 played, CUR playing (position sits one past it), u1 u2 next.
    q = make_queue(["h1", "h2", "h3", "CUR", "u1", "u2"], position=4)

    q.replace("history", ["h3", "h1", "h2"])

    assert q._queue == ["h3", "h1", "h2", "CUR", "u1", "u2"]
    assert q._queue[q._position - 1] == "CUR"
    assert q.history() == ["h3", "h1", "h2"]


def test_history_replace_uses_the_same_slice_history_returns():
    q = make_queue(["h1", "h2", "CUR", "u1"], position=3)

    q.replace("history", list(reversed(q.history())))

    assert len(q._queue) == 4
    assert q.tracks() == ["u1"]


def test_replacing_the_queue_keeps_history_and_current():
    q = make_queue(["h1", "CUR", "u1", "u2", "u3"], position=2)

    q.replace("queue", ["u3", "u1", "u2"])

    assert q._queue == ["h1", "CUR", "u3", "u1", "u2"]


# --- Patch 34: remove/move work by position, not by "first equal track"


class _Song:
    """Equal by song id, like voicelink.objects.Track."""
    def __init__(self, song_id, copy):
        self.track_id, self.copy, self.requester = song_id, copy, None

    def __eq__(self, other):
        return isinstance(other, _Song) and other.track_id == self.track_id

    def __repr__(self):
        return f"{self.track_id}{self.copy}"


def test_removing_a_repeated_song_removes_the_copy_at_that_position():
    # A1 already played (history), B1 playing, A2 queued again.
    a1, b1, a2 = _Song("A", 1), _Song("B", 1), _Song("A", 2)
    q = make_queue([a1, b1, a2], position=2)

    removed = q.remove(1)  # first upcoming = A2

    assert list(removed.items()) == [(2, a2)]
    assert q._queue == [a1, b1] and q._queue[0] is a1  # the history copy survives


def test_moving_a_repeated_song_moves_the_copy_at_that_position():
    a1, cur, x, a2 = _Song("A", 1), _Song("C", 1), _Song("X", 1), _Song("A", 2)
    q = make_queue([a1, cur, x, a2], position=2)

    moved = q.move(2, 1)  # move A2 (2nd upcoming) to the front of the upcoming list

    assert moved is a2
    assert [s for s in q._queue] == [a1, cur, a2, x] and q._queue[0] is a1


def test_removing_a_range_reports_original_indexes():
    items = [_Song(c, 1) for c in "HCABCD"]
    q = make_queue(items, position=2)  # H history, C current, upcoming A B C D

    removed = q.remove(2, 3)  # B and the second C

    assert sorted(removed) == [3, 4]
    assert [t.track_id for t in q._queue] == ["H", "C", "A", "D"]
