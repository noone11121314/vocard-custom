"""Issue #8: a skip (Player.stop) that raced a slow song start left Lavalink
silent while the bot thought a song played; no TrackEnd ever came, so every
later skip did nothing. After a stop the player now checks Lavalink and
moves on itself when nothing plays."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from voicelink.enums import RequestMethod
from voicelink.player import Player

from tests.conftest import make_track


def make_player(lavalink_track=None) -> Player:
    player = Player.__new__(Player)
    player._logger = MagicMock()
    player._bot = MagicMock()
    player._bot.loop = asyncio.get_running_loop()
    player._current = None
    player._loading_track = None
    player._track_end_count = 0
    player._stop_check_task = None
    player.channel = MagicMock()
    player._guild = MagicMock()
    player.STOP_CHECK_DELAY = 0
    player.do_next = AsyncMock()

    async def send(method, query=None, data={}):
        if method == RequestMethod.GET:
            return {"track": lavalink_track}
        return {}

    player.send = AsyncMock(side_effect=send)
    return player


async def finish(player):
    await asyncio.sleep(0)
    await player._stop_check_task


async def test_stop_with_no_trackend_and_silence_moves_on():
    # The stuck state: the bot thought a song played, Lavalink had nothing.
    player = make_player(lavalink_track=None)
    player._current = make_track(title="A Little Love")

    await player.stop()
    await finish(player)

    player.do_next.assert_awaited_once()
    assert player._current is None


async def test_race_trackend_came_but_bot_still_thinks_a_song_plays():
    player = make_player(lavalink_track=None)

    await player.stop()
    player._track_end_count += 1                 # TrackEnd for the old song
    player._current = make_track(title="B")      # play() landed, then Lavalink took the stop
    await finish(player)

    player.do_next.assert_awaited_once()


async def test_normal_skip_leaves_it_alone():
    player = make_player(lavalink_track={"encoded": "x"})

    await player.stop()
    player._track_end_count += 1
    player._current = make_track(title="next")
    await finish(player)

    player.do_next.assert_not_awaited()


async def test_queue_ended_normally_does_nothing_more():
    player = make_player(lavalink_track=None)

    await player.stop()
    player._track_end_count += 1                 # TrackEnd came, do_next found nothing
    await finish(player)

    player.do_next.assert_not_awaited()


async def test_waits_while_a_song_is_still_starting():
    player = make_player(lavalink_track=None)
    player._loading_track = make_track(title="slow cantus song")
    asyncio.get_running_loop().call_later(0.05, lambda: setattr(player, "_loading_track", None))
    sleeps = []
    real_sleep = asyncio.sleep

    async def fast_sleep(s):
        sleeps.append(s)
        await real_sleep(0.03)

    import voicelink.player as player_mod
    orig = player_mod.asyncio.sleep
    player_mod.asyncio.sleep = fast_sleep
    try:
        await player.stop()
        await player._stop_check_task
    finally:
        player_mod.asyncio.sleep = orig

    assert 1 in sleeps                           # it waited for the start
    player.do_next.assert_awaited_once()         # still silent afterwards: move on


async def test_a_new_stop_replaces_the_pending_check():
    player = make_player(lavalink_track=None)
    player.STOP_CHECK_DELAY = 10
    await player.stop()
    first = player._stop_check_task
    await player.stop()
    await asyncio.sleep(0)

    assert first.cancelled() or first.done()
    player._stop_check_task.cancel()


async def test_no_channel_any_more_does_nothing():
    player = make_player(lavalink_track=None)
    player._current = make_track()
    await player.stop()
    player.channel = None
    await finish(player)

    player.do_next.assert_not_awaited()
