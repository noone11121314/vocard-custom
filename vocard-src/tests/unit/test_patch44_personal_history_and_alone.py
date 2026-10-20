"""Patch 44: personal "added by you" history across servers, and leaving
~10s after the bot is left alone in its voice channel."""
from __future__ import annotations

import asyncio
import types
from unittest.mock import AsyncMock, MagicMock

from voicelink.ipc.methods import getUserHistory
from voicelink.player import Player
from voicelink.queue import Queue


def track(tid, requester):
    return types.SimpleNamespace(track_id=tid, requester=requester)


_VOICE = types.SimpleNamespace(self_deaf=False)
HUMAN_A = types.SimpleNamespace(id=1, bot=False, voice=_VOICE)
HUMAN_B = types.SimpleNamespace(id=2, bot=False, voice=_VOICE)
A_BOT = types.SimpleNamespace(id=9, bot=True, voice=types.SimpleNamespace(self_deaf=True))


def bare_player() -> Player:
    p = Player.__new__(Player)
    p._logger = MagicMock()
    p._guild = MagicMock(id=555)
    p._current = None
    p._is_connected = True
    p._inactive_cleanup_task = None
    p._alone_timer_pending = False
    p.queue = Queue(size=10, allow_duplicate=True, get_msg=lambda *k: "m")
    p._bot = MagicMock()
    p._bot.loop.create_task = MagicMock(side_effect=lambda coro: (coro.close(), MagicMock(done=lambda: False))[1])
    return p


async def test_added_songs_are_recorded_on_each_requesters_own_record(monkeypatch):
    update_user = AsyncMock()
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_user", update_user)

    await bare_player()._record_user_added_history([track("T1", HUMAN_A), track("T2", HUMAN_B), track("T3", HUMAN_A), track("T4", A_BOT)])

    calls = {c.args[0]: c.args[1] for c in update_user.await_args_list}
    assert calls == {
        1: {"$push": {"addedHistory": {"$each": ["T1", "T3"], "$slice": -100}}},
        2: {"$push": {"addedHistory": {"$each": ["T2"], "$slice": -100}}},
    }  # bots are skipped


async def test_recording_never_raises(monkeypatch):
    monkeypatch.setattr("voicelink.player.MongoDBHandler.update_user", AsyncMock(side_effect=RuntimeError("db down")))

    await bare_player()._record_user_added_history([track("T1", HUMAN_A)])  # must not raise


async def test_user_history_includes_the_added_list(monkeypatch):
    lists = {"history": ["P1"], "addedHistory": ["A1", "A2"]}
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_user", AsyncMock(side_effect=lambda uid, d_type: lists[d_type]))

    result = await getUserHistory(bot=None, data={"userId": "42"})

    assert result["history"] == ["P1"] and result["addedHistory"] == ["A1", "A2"]


def test_the_alone_timer_uses_alone_leave_seconds(monkeypatch):
    p = bare_player()
    seconds = []
    monkeypatch.setattr(Player, "_inactive_cleanup_timer_worker", lambda self, s: (seconds.append(s), asyncio.sleep(0))[1])
    monkeypatch.setattr("voicelink.player.Config", lambda: types.SimpleNamespace(timer_settings={}))

    p._schedule_alone_leave_timer()

    assert seconds == [10] and p._alone_timer_pending is True


def test_someone_coming_back_cancels_it_and_restores_the_idle_timer(monkeypatch):
    p = bare_player()
    scheduled = []
    monkeypatch.setattr(Player, "_inactive_cleanup_timer_worker", lambda self, s: (scheduled.append(s), asyncio.sleep(0))[1])
    monkeypatch.setattr("voicelink.player.Config", lambda: types.SimpleNamespace(timer_settings={"alone_leave": 10, "inactive_player_cleanup": 600}))
    p._schedule_alone_leave_timer()

    p._cancel_alone_leave_timer()

    assert p._alone_timer_pending is False
    assert scheduled == [10, 600]  # idle (nothing playing, empty queue) -> normal 10-minute timer


def test_cancelling_does_nothing_without_a_pending_alone_timer(monkeypatch):
    p = bare_player()
    monkeypatch.setattr(Player, "_schedule_inactive_cleanup_timer", MagicMock())

    p._cancel_alone_leave_timer()

    p._schedule_inactive_cleanup_timer.assert_not_called()


async def test_voice_updates_start_and_cancel_the_alone_timer():
    from cogs.listeners import Listeners
    cog = Listeners.__new__(Listeners)
    channel = MagicMock()
    channel.id = 77
    player = MagicMock(channel=channel, settings={}, dj=HUMAN_A, is_ipc_connected=False)
    guild = MagicMock(voice_client=player)

    def voice(ch):
        return types.SimpleNamespace(channel=ch)

    # The last person leaves; only bots remain.
    channel.members = [A_BOT]
    leaver = MagicMock(bot=False, guild=guild)
    await cog.on_voice_state_update(leaver, voice(channel), voice(None))
    player._schedule_alone_leave_timer.assert_called_once()

    # Someone joins again.
    channel.members = [A_BOT, HUMAN_A]
    joiner = MagicMock(bot=False, guild=guild)
    await cog.on_voice_state_update(joiner, voice(None), voice(channel))
    player._cancel_alone_leave_timer.assert_called_once()
