"""End-to-end test of restart persistence (Patches 18/20/23/25): a real
Player/Queue is saved by the real save_last_session(), then restored by the
real Listeners.restore_last_session_players() into a fresh real Player that
resumes through the real do_next(). Only Discord (channel, members), Lavalink
(play/REST) and MongoDB are stubbed.

Four bugs lived here on 2026-10-18 alone: resuming one song too late (the
"-1" removed by Patch 18), losing the saved position (seek before the track
started), skipping a song that was still caching at shutdown, and failing to
clear a bind-mounted session file. Each test below would have caught one.
"""
from __future__ import annotations

import asyncio
import json
import types
from unittest.mock import AsyncMock, MagicMock

import pytest

import voicelink
from cogs import listeners as listeners_module
from cogs.listeners import Listeners
from voicelink import session
from voicelink.player import Player
from voicelink.pool import NodePool
from voicelink.queue import Queue

pytestmark = pytest.mark.integration

HUMAN = types.SimpleNamespace(id=111, bot=False, voice=types.SimpleNamespace(self_deaf=False))
BOT_ME = types.SimpleNamespace(id=999, bot=True, voice=types.SimpleNamespace(self_deaf=True))


def make_track(i: int, requester=HUMAN):
    t = types.SimpleNamespace(
        track_id=f"TRACK{i}", uri=f"https://www.youtube.com/watch?v=v{i}", title=f"Song {i}",
        requester=requester, length=240_000, position=0, end_time=None, source="youtube",
        identifier=f"v{i}", is_stream=False,
    )
    t.data = {"track_id": t.track_id, "requester_id": requester.id}
    return t


def make_player(guild, channel, settings=None) -> Player:
    p = Player.__new__(Player)
    p._logger = MagicMock()
    p._guild = guild
    p.channel = channel
    p._ipc_client = MagicMock(_is_connected=False)
    p._ipc_connection = False
    p._current = None
    p._loading_track = None
    p._resume_start_ms = 0
    p._paused = False
    p._is_connected = True
    p._track_is_stuck = False
    p._volume = 100
    p._last_position = 0
    p._last_update = 0
    p.settings = dict(settings or {})
    p.queue = Queue(size=1000, allow_duplicate=True, get_msg=lambda *k: "msg")
    for votes in ("pause_votes", "resume_votes", "skip_votes", "previous_votes", "shuffle_votes", "stop_votes"):
        setattr(p, votes, set())
    p._bot = MagicMock()
    p._bot.loop.create_task = lambda coro: coro.close()
    p.invoke_controller = AsyncMock()
    p.update_voice_status = AsyncMock()
    p.send = AsyncMock()  # Lavalink REST
    p._prefetch_next_direct_track = lambda: None
    p.dj = HUMAN
    p.played = []

    async def play(track, start=0, **_):
        p.played.append((track.track_id, start))
        p._current = track
        return track

    p.play = play
    return p


@pytest.fixture
def world(tmp_path, monkeypatch):
    guild = MagicMock(id=555)
    guild.name = "Guild"
    guild.me = BOT_ME
    guild.get_member = lambda uid: HUMAN if uid == HUMAN.id else None
    channel = MagicMock(id=777)
    channel.guild = guild
    channel.members = [HUMAN, BOT_ME]
    session_file = tmp_path / "last-session.json"
    session_file.write_text("[]")  # exists, like the bind-mounted file
    monkeypatch.setattr(session.Config, "LAST_SESSION_FILE_DIR", str(session_file))
    monkeypatch.setattr(listeners_module.Config, "LAST_SESSION_FILE_DIR", str(session_file))
    monkeypatch.setattr(voicelink.mongodb.MongoDBHandler, "update_user", AsyncMock())
    monkeypatch.setattr(listeners_module.MongoDBHandler, "get_settings", AsyncMock(return_value={}))

    restored = []

    def player_factory(bot, ch, ctx, settings):
        p = make_player(guild, ch, settings)
        restored.append(p)
        return p

    monkeypatch.setattr(voicelink, "Player", player_factory)
    monkeypatch.setattr(voicelink.Track, "decode", staticmethod(lambda track_id: {"title": track_id}))
    monkeypatch.setattr(voicelink, "Track", _track_from_saved(voicelink.Track))
    channel.connect = AsyncMock(side_effect=lambda cls: cls)

    real_sleep = asyncio.sleep
    monkeypatch.setattr(asyncio, "sleep", lambda d, *a, **k: real_sleep(0))

    bot = MagicMock()
    bot.wait_until_ready = AsyncMock()
    bot.get_channel = lambda cid: channel if cid == channel.id else None
    cog = Listeners.__new__(Listeners)
    cog.bot = bot
    return types.SimpleNamespace(guild=guild, channel=channel, cog=cog, restored=restored, file=session_file, monkeypatch=monkeypatch)


def _track_from_saved(real_track_cls):
    def build(track_id, info, requester):
        i = int(track_id.replace("TRACK", ""))
        return make_track(i, requester=requester)
    build.decode = real_track_cls.decode
    return build


async def _save_and_restore(world, before: Player) -> Player:
    world.monkeypatch.setattr(NodePool, "_nodes", {"DEFAULT": MagicMock(_players={world.guild.id: before})})
    session.save_last_session()
    saved = json.loads(world.file.read_text())
    assert len(saved) == 1
    await world.cog.restore_last_session_players()
    assert len(world.restored) == 1
    return world.restored[0]


async def test_resumes_the_same_paused_song_at_the_same_position(world):
    before = make_player(world.guild, world.channel)
    for i in range(4):
        before.queue.put(make_track(i))
    await before.do_next()  # song 0
    before._current = None
    await before.do_next()  # song 1 - now playing
    before._paused = True
    before._last_position = 61_000

    after = await _save_and_restore(world, before)

    assert after.played == [("TRACK1", 61_000)]  # same song, same spot (not song 2, not 0:00)
    assert after._paused is True
    assert [t.track_id for t in after.queue._queue] == ["TRACK0", "TRACK1", "TRACK2", "TRACK3"]
    assert after.queue.history() == [after.queue._queue[0]]  # song 0 still in history
    assert [t.track_id for t in after.queue.tracks()] == ["TRACK2", "TRACK3"]
    assert world.file.read_text() == "[]"  # cleared, not deleted (bind mount)


async def test_a_queue_that_never_started_starts_from_the_first_song(world):
    before = make_player(world.guild, world.channel)
    for i in range(3):
        before.queue.put(make_track(i))

    after = await _save_and_restore(world, before)

    assert after.played == [("TRACK0", 0)]


async def test_a_song_still_caching_at_shutdown_is_not_skipped(world):
    before = make_player(world.guild, world.channel)
    for i in range(3):
        before.queue.put(make_track(i))
    await before.do_next()  # song 0 played
    before._current = None
    before._loading_track = before.queue.get()  # song 1 pulled off the queue, still downloading

    after = await _save_and_restore(world, before)

    assert after.played == [("TRACK1", 0)]


async def test_nothing_is_restored_when_nobody_is_listening(world):
    world.channel.members = [BOT_ME]
    before = make_player(world.guild, world.channel)
    before.queue.put(make_track(0))
    world.monkeypatch.setattr(NodePool, "_nodes", {"DEFAULT": MagicMock(_players={world.guild.id: before})})
    session.save_last_session()

    await world.cog.restore_last_session_players()

    assert world.restored == []
    assert world.file.read_text() == "[]"
