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
