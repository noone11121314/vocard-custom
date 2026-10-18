"""Patch 40: with "duplicate tracks" turned off, only the current and
upcoming songs count as duplicates - a song that already played (in the
history) can be queued again, e.g. re-added from the dashboard's History list.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from voicelink.exceptions import DuplicateTrack
from voicelink.player import Player
from voicelink.queue import Queue


def song(n):
    return types.SimpleNamespace(uri=f"https://www.youtube.com/watch?v={n}", track_id=f"T{n}",
                                 requester=MagicMock(bot=False), length=200_000, position=0, end_time=None)


def make_player(queue_items, position, current=None, allow_duplicate=False) -> Player:
    p = Player.__new__(Player)
    p._logger = MagicMock()
    p._guild = MagicMock(id=1)
    p.channel = MagicMock(members=[])
    p._ipc_client = MagicMock(_is_connected=False)
    p._ipc_connection = None
    p._current = current
    p._loading_track = None
    p._bot = MagicMock()
    p._bot.loop.create_task = lambda coro: coro.close()
    p._prefetch_next_direct_track = lambda: None
    p.get_msg = lambda *k: "duplicate"
    q = Queue(size=100, allow_duplicate=allow_duplicate, get_msg=lambda *k: "msg")
    q._queue = list(queue_items)
    q._position = position
    p.queue = q
    return p


async def test_a_song_from_the_history_can_be_queued_again():
    a, b = song("a"), song("b")
    p = make_player([a, b], position=2, current=b)  # a played, b playing

    await p.add_track(song("a"))

    assert [t.uri for t in p.queue.tracks()] == [a.uri]


async def test_the_current_song_still_counts_as_a_duplicate():
    a, b = song("a"), song("b")
    p = make_player([a, b], position=2, current=b)

    with pytest.raises(DuplicateTrack):
        await p.add_track(song("b"))


async def test_an_upcoming_song_still_counts_as_a_duplicate():
    a, b, c = song("a"), song("b"), song("c")
    p = make_player([a, b, c], position=2, current=b)  # c is next

    with pytest.raises(DuplicateTrack):
        await p.add_track(song("c"))


async def test_after_the_queue_finished_every_played_song_can_be_queued_again():
    a, b = song("a"), song("b")
    p = make_player([a, b], position=2, current=None)  # finished, nothing current

    await p.add_track([song("a"), song("b")])

    assert [t.uri for t in p.queue.tracks()] == [a.uri, b.uri]
