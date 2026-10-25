"""Issue #9: normal members get more control over the queue. They remove
their own songs at once, vote to remove one song someone else added, and
seek in the song that's playing when they requested it. DJs are unchanged."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from voicelink.ipc import methods
from voicelink.player import Player


def song(requester, track_id):
    track = MagicMock(name=track_id)
    track.requester = requester
    track.track_id = track_id
    track.title = track_id
    return track


def make_player(upcoming, votes_needed=2, privileged=()) -> Player:
    player = Player.__new__(Player)
    player.queue = MagicMock()
    player.queue.tracks = lambda incTrack=False: list(upcoming)
    player.remove_votes = {}
    player._current = None
    player.required = lambda: votes_needed
    player.is_privileged = lambda member, check_user_join=True: member in privileged
    player.remove_track = AsyncMock(side_effect=lambda index, index2=None, *a, **k: {index: upcoming[index - 1]})
    return player


alice, bob, carol = MagicMock(name="alice"), MagicMock(name="bob"), MagicMock(name="carol")


async def test_member_removes_own_song_at_once():
    player = make_player([song(bob, "a"), song(alice, "b")])

    result, removed = await player.member_remove_track(alice, 2, expected_track_id="b")

    assert result == "removed"
    player.remove_track.assert_awaited_once()
    assert player.remove_track.await_args.args[:2] == (2, 2)


async def test_someone_elses_song_needs_the_vote():
    player = make_player([song(bob, "a"), song(bob, "b")], votes_needed=2)

    result, info = await player.member_remove_track(alice, 1, expected_track_id="a")
    assert result == "voted"
    assert info[1:] == (1, 2)
    player.remove_track.assert_not_awaited()

    result, _ = await player.member_remove_track(alice, 1, expected_track_id="a")
    assert result == "alreadyVoted"

    result, _ = await player.member_remove_track(carol, 1, expected_track_id="a")
    assert result == "removed"
    player.remove_track.assert_awaited_once()
    assert player.remove_votes == {}


async def test_vote_follows_the_song_when_the_index_moved():
    upcoming = [song(bob, "x"), song(bob, "a")]
    player = make_player(upcoming, votes_needed=1)

    # the menu still said index 1, but "a" is now second
    result, _ = await player.member_remove_track(alice, 1, expected_track_id="a")

    assert result == "removed"
    assert player.remove_track.await_args.args[:2] == (2, 2)


async def test_range_with_other_peoples_songs_is_denied():
    player = make_player([song(alice, "a"), song(bob, "b"), song(alice, "c")])

    result, _ = await player.member_remove_track(alice, 1, 3)

    assert result == "denied"
    player.remove_track.assert_not_awaited()


async def test_range_of_own_songs_goes_at_once():
    player = make_player([song(alice, "a"), song(alice, "b"), song(bob, "c")])

    result, _ = await player.member_remove_track(alice, 1, 2)

    assert result == "removed"
    player.remove_track.assert_awaited_once()


async def test_dj_removes_anything():
    player = make_player([song(bob, "a"), song(carol, "b")], votes_needed=5, privileged=(alice,))

    result, _ = await player.member_remove_track(alice, 1, 2)

    assert result == "removed"
    player.remove_track.assert_awaited_once()


async def test_song_gone_from_queue_removes_nothing():
    player = make_player([song(bob, "a")])

    result, removed = await player.member_remove_track(alice, 1, expected_track_id="zzz")

    assert (result, removed) == ("removed", {})
    player.remove_track.assert_not_awaited()


def test_can_seek_own_playing_song_only():
    player = make_player([])
    player._current = song(alice, "now")

    assert player.can_seek(alice)
    assert not player.can_seek(bob)

    player._current = None
    assert not player.can_seek(alice)


@pytest.fixture
def not_privileged(monkeypatch):
    async def fake(player, member, check_user_join=True):
        return False

    monkeypatch.setattr(methods, "_is_privileged_fresh", fake)


async def test_dashboard_remove_vote_is_announced(not_privileged):
    player = MagicMock()
    player.member_remove_track = AsyncMock(return_value=("voted", (song(bob, "a"), 1, 2)))
    player.get_msg = lambda key: "{0} {1} [{2}/{3}]"
    player.guild.id = 42

    reply = await methods.removeTrack(player, alice, {"index": 1, "trackId": "a"})

    assert reply["op"] == "errorMsg" and reply["guildId"] == "42"
    assert "[1/2]" in reply["msg"]
    player.remove_track.assert_not_called()


async def test_dashboard_seek_by_requester_of_playing_song(not_privileged):
    player = MagicMock()
    player.can_seek = lambda member: member is alice
    player.seek = AsyncMock()
    player.get_msg = lambda key: "nope"

    await methods.updatePosition(player, alice, {"position": 5000})
    player.seek.assert_awaited_once_with(5000, alice)

    reply = await methods.updatePosition(player, bob, {"position": 9000})
    assert reply["op"] == "errorMsg"
    player.seek.assert_awaited_once()
