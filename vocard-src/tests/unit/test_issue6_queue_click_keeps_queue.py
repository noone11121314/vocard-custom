"""Issue #6: a normal member clicking a song in the dashboard queue
(skipTo with keepQueue) moves it to the top and plays it, instead of jumping
past everyone's songs in between. The skip still goes through the vote;
DJs and the plain skipTo (right-click "Skip to", Next) keep the jump."""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from voicelink.enums import LoopType
from voicelink.ipc import methods


def make_player(votes_needed=2, current_requester=None):
    player = MagicMock()
    player.move_track = AsyncMock()
    player.stop = AsyncMock()
    player.set_repeat = AsyncMock()
    player.queue._repeat.mode = LoopType.OFF
    player.skip_votes = set()
    player.required = lambda: votes_needed
    player.current.requester = current_requester or MagicMock(name="someone else")
    player.get_msg = lambda key: "{} {} {}"
    return player


@pytest.fixture
def privileged(monkeypatch):
    state = {"value": False}

    async def fake(player, member, check_user_join=True):
        return state["value"]

    monkeypatch.setattr(methods, "_is_privileged_fresh", fake)
    return state


async def test_normal_member_click_moves_song_to_top_and_plays_it(privileged):
    member = MagicMock()
    player = make_player(votes_needed=1)

    await methods.skipTo(player, member, {"index": 4, "keepQueue": True})

    player.move_track.assert_awaited_once_with(4, 1, member)
    player.queue.skipto.assert_not_called()  # nothing passed over
    player.stop.assert_awaited_once()


async def test_normal_member_click_without_enough_votes_still_moves_but_waits(privileged):
    member = MagicMock()
    player = make_player(votes_needed=3)

    await methods.skipTo(player, member, {"index": 4, "keepQueue": True})

    player.move_track.assert_awaited_once_with(4, 1, member)
    assert member in player.skip_votes
    player.stop.assert_not_awaited()


async def test_requester_of_current_song_skips_without_vote(privileged):
    member = MagicMock()
    player = make_player(votes_needed=5, current_requester=member)

    await methods.skipTo(player, member, {"index": 3, "keepQueue": True})

    player.move_track.assert_awaited_once_with(3, 1, member)
    player.stop.assert_awaited_once()


async def test_dj_click_keeps_the_jump(privileged):
    privileged["value"] = True
    player = make_player()

    await methods.skipTo(player, MagicMock(), {"index": 4, "keepQueue": True})

    player.move_track.assert_not_awaited()
    player.queue.skipto.assert_called_once_with(4)
    player.stop.assert_awaited_once()


async def test_plain_skipto_still_jumps(privileged):
    player = make_player(votes_needed=1)

    await methods.skipTo(player, MagicMock(), {"index": 4})

    player.move_track.assert_not_awaited()
    player.queue.skipto.assert_called_once_with(4)


async def test_clicking_the_next_song_does_not_move_anything(privileged):
    player = make_player(votes_needed=1)

    await methods.skipTo(player, MagicMock(), {"index": 1, "keepQueue": True})

    player.move_track.assert_not_awaited()
    player.queue.skipto.assert_not_called()
    player.stop.assert_awaited_once()
