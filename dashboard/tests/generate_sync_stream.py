"""Drives the *real* voicelink Player/Queue (and the real IPC initPlayer
method) through a scripted multi-user session and records every message
the bot would broadcast to the dashboards, plus the bot's true queue state
at the moment each message was sent.

Only the edges are stubbed: Discord (guild/channel/members), Lavalink
(play/REST) and MongoDB. Output (JSON lines) is replayed into two real
dashboard instances by sync_harness.js - see run_sync_test.sh.

Run inside the bot image with vocard-src mounted at /app.
"""
from __future__ import annotations

import asyncio
import json
import sys
import types
from unittest.mock import AsyncMock, MagicMock

sys.path.insert(0, "/app")

from voicelink import stream_cache
from voicelink.ipc import methods
from voicelink.player import Player
from voicelink.queue import Queue

# Usage: generate_sync_stream.py <track ids json> <out.jsonl>            (scripted session)
#        generate_sync_stream.py <track ids json> <out prefix> --fuzz <first seed> <seeds> <steps>
TRACK_IDS = json.load(open(sys.argv[1]))
FUZZ = "--fuzz" in sys.argv
OUT = None if FUZZ else open(sys.argv[2], "w")

USER_A = types.SimpleNamespace(id=111, bot=False, name="alice", display_avatar=types.SimpleNamespace(url="a.png"))
USER_B = types.SimpleNamespace(id=222, bot=False, name="bob", display_avatar=types.SimpleNamespace(url="b.png"))


_uid = 0


def make_track(i: int):
    global _uid
    _uid += 1  # distinct uri per call so allow_duplicate stays the only dedup rule in play
    track_id = TRACK_IDS[i % len(TRACK_IDS)]
    t = types.SimpleNamespace(
        track_id=track_id, uri=f"https://www.youtube.com/watch?v=fixture{i}&n={_uid}", title=f"T{i}",
        requester=USER_A, length=200_000, position=0, end_time=None, source="youtube",
        identifier=f"fixture{i}", is_stream=False,
    )
    t.data = {"track_id": track_id, "requester_id": USER_A.id}
    return t


def truth(player: Player) -> dict:
    current = player.queue._position - 1 if player.has_dequeued_track else None
    return {
        "queue": [t.track_id for t in player.queue._queue],
        "current": current,
        "upcomingStart": player.queue._position,
        "isPaused": player._paused,
        "volume": player._volume,
    }


def emit(kind: str, payload: dict, player: Player, label: str = "") -> None:
    OUT.write(json.dumps({"kind": kind, "msg": payload, "truth": truth(player), "label": label}) + "\n")


def build_player() -> Player:
    p = Player.__new__(Player)
    p._logger = MagicMock()
    p._guild = MagicMock(id=999)
    p._guild.name = "Fixture Guild"
    p._guild.me.voice = True
    p.channel = MagicMock(id=555)
    p.channel.name = "General"
    p.channel.members = [USER_A, USER_B]
    p._ipc_client = MagicMock(_is_connected=True)
    p._ipc_connection = True
    p._current = None
    p._loading_track = None
    p._resume_start_ms = 0
    p._paused = False
    p._is_connected = True
    p._track_is_stuck = False
    p._volume = 100
    p._last_position = 0
    p._last_update = 0
    p.settings = {"autoplay": False}
    p.queue = Queue(size=1000, allow_duplicate=True, get_msg=lambda *k: "msg")
    for votes in ("pause_votes", "resume_votes", "skip_votes", "previous_votes", "shuffle_votes", "stop_votes"):
        setattr(p, votes, set())
    p._bot = MagicMock()
    p._bot.loop.create_task = lambda coro: coro.close()
    p.invoke_controller = AsyncMock()
    p.update_voice_status = AsyncMock()
    p.send = AsyncMock()  # Lavalink REST
    p._prefetch_next_direct_track = lambda: None
    p._schedule_inactive_cleanup_timer = lambda: None
    p.is_privileged = lambda *a, **k: True
    p.dj = USER_A
    from voicelink.filters import Filters
    p._filters = Filters()
    p._loudness_gain = 1.0
    p._node = None

    async def send_ws(payload, requester=None):
        payload["guildId"] = "999"
        if requester:
            payload["requesterId"] = str(requester.id)
        emit("broadcast", payload, p)

    p.send_ws = send_ws
    return p


async def snapshot_for(player: Player, who: str) -> None:
    data = await methods.initPlayer(player, USER_B if who == "B" else USER_A, {"userId": "222" if who == "B" else "111"})
    emit("initPlayer", data, player, label=who)


async def finish_current(player: Player, during_loading=None) -> None:
    """Simulates the current song ending (or being skipped): Lavalink's
    TrackEnd clears _current, then the listener calls do_next(). Optional
    `during_loading` runs while the next song is still caching."""
    async def play(track, start=0, **_):
        if during_loading:
            await during_loading()
        player._current = track
        return track

    player.play = play
    player._current = None
    await player.do_next()


def stub_edges() -> None:
    stream_cache.list_entries = AsyncMock(return_value=[])
    from voicelink import mongodb
    mongodb.MongoDBHandler.update_user = AsyncMock()
    mongodb.MongoDBHandler.update_settings = AsyncMock()
    methods.MongoDBHandler.update_settings = AsyncMock()


async def fuzz(seed: int, steps: int) -> None:
    """Random multi-user session (Patch 34). Every operation goes through the
    real Player/Queue; operations the bot itself refuses (e.g. shuffling a
    2-song queue, an out-of-range index) are simply skipped, like a refused
    dashboard click."""
    import random
    from voicelink.exceptions import VoicelinkException
    rng = random.Random(seed)
    p = build_player()
    await snapshot_for(p, "A")
    joined_b = False
    users = (USER_A, USER_B)

    def upcoming():
        return len(p.queue.tracks())

    async def one_action():
        r = rng.random()
        who = rng.choice(users)
        if r < 0.22:
            n = rng.choice((1, 1, 2, 3))
            await p.add_track([make_track(rng.randrange(10)) for _ in range(n)] if n > 1 else make_track(rng.randrange(10)))
        elif r < 0.30:
            n = rng.choice((1, 1, 2, 3))
            await p.add_track([make_track(rng.randrange(10)) for _ in range(n)] if n > 1 else make_track(rng.randrange(10)), at_front=True)
        elif r < 0.42 and upcoming():
            i = rng.randint(1, upcoming())
            expected = p.queue.tracks()[i - 1].track_id if rng.random() < 0.8 else p.queue.tracks()[rng.randrange(upcoming())].track_id
            await p.remove_track(i, requester=who, expected_track_id=expected)
        elif r < 0.52 and upcoming() >= 2:
            await p.move_track(rng.randint(1, upcoming()), rng.randint(1, upcoming()), who)
        elif r < 0.58 and upcoming() >= 2:
            a, b = rng.sample(range(1, upcoming() + 1), 2)
            await p.swap_track(a, b, who)
        elif r < 0.64:
            await p.shuffle(rng.choice(("queue", "history")), who)
        elif r < 0.68:
            await p.clear_queue(rng.choice(("queue", "history")), who)
        elif r < 0.76:
            await p.set_pause(not p._paused, who)
        elif r < 0.82:
            await p.set_volume(rng.randint(0, 100), who)
        else:
            return "next"

    async def safe(coro_factory):
        try:
            return await coro_factory()
        except VoicelinkException:
            return None

    for _ in range(steps):
        r = rng.random()
        if not joined_b and r < 0.08:
            await snapshot_for(p, "B"); joined_b = True
            continue
        if r < 0.03:
            await snapshot_for(p, "A2")  # A reloads its page
            continue
        if await safe(one_action) == "next":
            async def during():
                for _ in range(rng.randint(0, 3)):
                    if await safe(one_action) == "next":
                        continue
                if rng.random() < 0.15:
                    await snapshot_for(p, "A2")
            await finish_current(p, during_loading=during if rng.random() < 0.5 else None)
    await snapshot_for(p, "FINAL")


async def main() -> None:
    stub_edges()
    p = build_player()
    await snapshot_for(p, "A")  # dashboard A connects to an idle player

    # A adds three songs; the first starts playing (caches first).
    await p.add_track([make_track(0), make_track(1), make_track(2)])
    await finish_current(p)

    # A adds one more; B's dashboard connects while it plays.
    await p.add_track(make_track(3))
    await snapshot_for(p, "B")

    await p.set_pause(True, USER_B)
    await p.set_volume(40, USER_A)
    await p.set_volume(140, USER_B)  # out of range: the bot clamps it to 100
    await p.set_volume(40, USER_A)
    await p.set_pause(False, USER_A)

    # Song ends; the next one takes a while to cache. While it caches:
    # B adds two songs, A removes one, B moves one, and a third dashboard
    # (re)connects as A - all mid-caching.
    async def during_caching():
        await p.add_track([make_track(4), make_track(5)])
        await p.remove_track(1, requester=USER_A, expected_track_id=p.queue.tracks()[0].track_id)
        await p.move_track(3, 1, USER_B)
        await snapshot_for(p, "A2")

    await finish_current(p, during_loading=during_caching)

    await p.swap_track(1, 2, USER_A)
    await p.add_track(make_track(6), at_front=True)  # "play next"
    await p.shuffle("queue", USER_B)
    await finish_current(p)
    await p.add_track(make_track(7))
    await finish_current(p)
    await p.shuffle("history", USER_A)
    await p.clear_queue("history", USER_B)
    await p.clear_queue("queue", USER_A)
    await p.add_track([make_track(8), make_track(9)])
    await finish_current(p)
    await snapshot_for(p, "FINAL")
    OUT.close()


if FUZZ:
    i = sys.argv.index("--fuzz")
    first, count, steps = (int(x) for x in sys.argv[i + 1:i + 4])
    stub_edges()
    for seed in range(first, first + count):
        OUT = open(f"{sys.argv[2]}{seed}.jsonl", "w")
        asyncio.run(fuzz(seed, steps))
        OUT.close()
else:
    asyncio.run(main())
