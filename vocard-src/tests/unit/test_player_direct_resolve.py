"""Unit tests for the direct-URL-resolution integration on voicelink.player.Player:
play()'s resolve-then-fallback branch, _prefetch_next_direct_track's
sequential-worker scheduling (_prefetch_worker), and _prefetch_and_notify's
prefetchStatus IPC messages. Player is constructed via __new__ to skip its
heavy constructor (NodePool/IPC/Config wiring) and given only the attributes
these methods touch.

Note: do_next()'s "trackLoading" IPC send (the counterpart that marks the
*currently* loading track, as opposed to the prefetched *next* one) is not
unit tested here - do_next() is one large, untestably entangled method (vote
resets, voice connect, controller/embed rebuild, Mongo history, IPC trackUpdate,
etc.) and isolating just its one new line isn't worth the mocking required.
It's covered instead by manual verification after deploy (see docs/local-patches.md).
"""
from __future__ import annotations

import asyncio
import time

from unittest.mock import AsyncMock, MagicMock

from voicelink.filters import Filters
from voicelink.player import Player

from tests.conftest import make_track


def make_bare_player() -> Player:
    player = Player.__new__(Player)
    player._node = MagicMock()
    player._node.yt_ratelimit = None
    player._node.get_direct_resolve_cache_stats = MagicMock(return_value={"entries": 0, "approxBytes": 0})
    # Nothing pre-cached by default - _prefetch_worker's skip-if-cached check
    # would otherwise see a truthy MagicMock and skip every track. Tests that
    # care about that skip behavior override this themselves.
    player._node.is_direct_resolve_cached = MagicMock(return_value=False)
    # No progress known by default - tests that care about the prefetch
    # progress bar override this themselves.
    player._node.get_prefetch_progress = AsyncMock(return_value=None)
    # play() fires this off in the background to backfill the disk cache for
    # the current track - tests that care about that call override it themselves.
    player._node.prefetch_direct_track = AsyncMock(return_value=None)
    # Patch 10: per-track loudness normalization - neutral/unmeasured by
    # default. Tests that care about a real measured gain override this
    # themselves (see test_player_loudness_gain.py).
    player._node.get_track_gain = AsyncMock(return_value=1.0)
    player._filters = Filters()
    player._volume = 100
    player._loudness_gain = 1.0
    # Zero rather than the real default: _prefetch_and_notify's progress-poll
    # loop sleeps this long between polls, and tests shouldn't pay real
    # wall-clock time for that.
    player.PREFETCH_PROGRESS_POLL_SECONDS = 0
    player._logger = MagicMock()
    player.send = AsyncMock()
    player.send_ws = AsyncMock()
    player._current = None
    player._bot = MagicMock()
    # Real loop by default so self._bot.loop.create_task(...) (used by
    # _prefetch_and_notify/_prefetch_next_direct_track) actually schedules
    # coroutines instead of swallowing them as a MagicMock call. Tests that
    # want to inspect/intercept create_task itself override this themselves.
    player._bot.loop = asyncio.get_event_loop()
    player._guild = MagicMock()
    player.queue = MagicMock()
    player.queue._allow_duplicate = True
    player.queue._queue = []
    player.channel = MagicMock(members=[])
    player._direct_resolve_retry_counts = {}
    player._prefetch_worker_task = None
    player._is_connected = False
    # IPC disconnected by default; tests that exercise IPC sends opt in explicitly.
    player._ipc_client = MagicMock(_is_connected=False)
    player._ipc_connection = False
    return player


def connect_ipc(player: Player) -> None:
    player._ipc_client = MagicMock(_is_connected=True)
    player._ipc_connection = True


# --- play() ------------------------------------------------------------


async def test_play_sends_resolved_direct_url_but_keeps_native_metadata():
    player = make_bare_player()
    player._node.resolve_direct_track = AsyncMock(return_value="RESOLVED_ENCODED")
    track = make_track(source="youtube", identifier="vidA", track_id="NATIVE_ENCODED")

    result = await player.play(track)

    sent_data = player.send.call_args.kwargs["data"]
    assert sent_data["encodedTrack"] == "RESOLVED_ENCODED"
    # dashboard sync / Mongo history / favourites all read track.track_id, which
    # must stay the native id so title/artwork aren't lost to a raw-stream probe.
    assert result is track
    assert result.track_id == "NATIVE_ENCODED"
    assert player._current is track


async def test_play_falls_back_to_native_encoding_when_resolver_returns_none():
    player = make_bare_player()
    player._node.resolve_direct_track = AsyncMock(return_value=None)
    track = make_track(track_id="NATIVE_ENCODED")

    await player.play(track)

    assert player.send.call_args.kwargs["data"]["encodedTrack"] == "NATIVE_ENCODED"


async def test_play_falls_back_and_logs_warning_when_resolver_raises():
    player = make_bare_player()
    player._node.resolve_direct_track = AsyncMock(side_effect=RuntimeError("companion down"))
    track = make_track(track_id="NATIVE_ENCODED")

    await player.play(track)  # must not propagate the exception

    assert player.send.call_args.kwargs["data"]["encodedTrack"] == "NATIVE_ENCODED"
    player._logger.warning.assert_called_once()


async def test_play_never_calls_resolver_for_non_youtube_source():
    player = make_bare_player()
    player._node.resolve_direct_track = AsyncMock()
    track = make_track(source="soundcloud", track_id="NATIVE_ENCODED")

    await player.play(track)

    player._node.resolve_direct_track.assert_not_called()
    assert player.send.call_args.kwargs["data"]["encodedTrack"] == "NATIVE_ENCODED"


async def test_play_fires_a_background_disk_cache_download_for_the_current_track():
    """Regression guard: resolve_direct_track() only ever *reads* an
    already-finished disk entry, never starts a new download (no head start
    for the current track), so without this, a track reached with no prior
    prefetch - most commonly the first song of a session - would never get
    yt-dlp-downloaded at all. play() must still kick it off, just never
    await it (fire-and-forget - see play()'s comment).
    """
    player = make_bare_player()
    player._node.resolve_direct_track = AsyncMock(return_value="RESOLVED_ENCODED")
    track = make_track(source="youtube", identifier="vidA", title="My Song")

    await player.play(track)
    await asyncio.sleep(0)  # let the fire-and-forget task actually run

    player._node.prefetch_direct_track.assert_awaited_once_with("vidA", "My Song")


async def test_play_does_not_wait_on_the_background_cache_download():
    player = make_bare_player()
    player._node.resolve_direct_track = AsyncMock(return_value="RESOLVED_ENCODED")
    captured = {}

    def fake_create_task(coro):
        captured["coro"] = coro
        coro.close()  # proves play() scheduled it rather than awaiting it inline
        return MagicMock()

    player._bot.loop.create_task = MagicMock(side_effect=fake_create_task)
    track = make_track(source="youtube", identifier="vidA")

    await asyncio.wait_for(player.play(track), timeout=1)  # must not hang

    assert "coro" in captured


async def test_play_never_fires_a_background_download_for_non_youtube_source():
    player = make_bare_player()
    track = make_track(source="soundcloud", track_id="NATIVE_ENCODED")

    await player.play(track)
    await asyncio.sleep(0)

    player._node.prefetch_direct_track.assert_not_called()


async def test_play_reports_cache_stats_when_ipc_connected_and_track_is_youtube():
    player = make_bare_player()
    connect_ipc(player)
    player._node.resolve_direct_track = AsyncMock(return_value="RESOLVED_ENCODED")
    player._node.get_direct_resolve_cache_stats = MagicMock(return_value={"entries": 3, "approxBytes": 512})
    track = make_track(source="youtube", identifier="vidA")

    await player.play(track)

    # Patch 13: play() now also sends prefetchStatus loading/ready events
    # around the resolve (see _resolve_and_notify_current_track) - cacheStats
    # is still sent, just no longer the only/last thing on the wire.
    sent_ops = [c.args[0]["op"] for c in player.send_ws.call_args_list]
    assert sent_ops == ["prefetchStatus", "prefetchStatus", "cacheStats"]
    assert player.send_ws.call_args_list[-1].args[0] == {"op": "cacheStats", "entries": 3, "approxBytes": 512}


async def test_play_does_not_report_cache_stats_for_non_youtube_track():
    player = make_bare_player()
    connect_ipc(player)
    track = make_track(source="soundcloud")

    await player.play(track)

    player.send_ws.assert_not_called()


async def test_play_never_calls_resolver_when_track_has_no_identifier():
    player = make_bare_player()
    player._node.resolve_direct_track = AsyncMock()
    track = make_track(identifier=None, track_id="NATIVE_ENCODED")

    await player.play(track)

    player._node.resolve_direct_track.assert_not_called()


async def test_play_returns_early_without_sending_when_node_missing():
    player = make_bare_player()
    player._node = None
    track = make_track()

    result = await player.play(track)

    assert result is track
    player.send.assert_not_called()


# --- _prefetch_next_direct_track (worker start/no-op gating) -------------


def test_prefetch_starts_a_worker_task_when_none_is_running():
    player = make_bare_player()
    player._bot.loop = MagicMock()
    created_task = MagicMock()
    player._bot.loop.create_task = MagicMock(return_value=created_task)
    # Stubbed so _prefetch_next_direct_track() never actually creates a real
    # coroutine here (which create_task, being mocked, would then never
    # await/close) - _prefetch_worker's own behavior is tested separately below.
    sentinel = object()
    player._prefetch_worker = MagicMock(return_value=sentinel)

    player._prefetch_next_direct_track()

    player._bot.loop.create_task.assert_called_once_with(sentinel)
    assert player._prefetch_worker_task is created_task


def test_prefetch_is_noop_while_a_worker_is_already_running():
    player = make_bare_player()
    player._bot.loop = MagicMock()
    running_task = MagicMock()
    running_task.done.return_value = False
    player._prefetch_worker_task = running_task

    player._prefetch_next_direct_track()

    player._bot.loop.create_task.assert_not_called()
    assert player._prefetch_worker_task is running_task


def test_prefetch_starts_a_fresh_worker_once_the_previous_one_finished():
    player = make_bare_player()
    player._bot.loop = MagicMock()
    finished_task = MagicMock()
    finished_task.done.return_value = True
    player._prefetch_worker_task = finished_task
    new_task = MagicMock()
    player._bot.loop.create_task = MagicMock(return_value=new_task)
    sentinel = object()
    player._prefetch_worker = MagicMock(return_value=sentinel)

    player._prefetch_next_direct_track()

    player._bot.loop.create_task.assert_called_once_with(sentinel)
    assert player._prefetch_worker_task is new_task


def test_prefetch_is_noop_when_node_missing():
    player = make_bare_player()
    player._node = None
    player._bot.loop = MagicMock()

    player._prefetch_next_direct_track()

    player._bot.loop.create_task.assert_not_called()


# --- _prefetch_worker (sequential, whole-queue prefetch) ------------------
#
# Deliberately not a fixed lookahead window: invidious-companion and the
# yt-dlp prefetch path both egress to YouTube from the same host, so firing
# a download per queued track at once would look like a scraping burst from
# a single IP. A strictly sequential worker - one prefetch fully awaited
# before the next starts, however long the queue is - never creates that
# burst signature, so the whole upcoming queue can be covered safely.


async def test_prefetch_worker_processes_every_youtube_track_in_queue_order():
    player = make_bare_player()
    tracks = [
        make_track(identifier="vidA", source="soundcloud"),
        make_track(identifier="vidB", source="youtube"),
        make_track(identifier="vidC", source="youtube"),
        make_track(identifier=None, source="youtube"),
        make_track(identifier="vidD", source="youtube"),
    ]
    player.queue.tracks = MagicMock(return_value=tracks)
    player._prefetch_and_notify = AsyncMock()

    await player._prefetch_worker()

    # Called repeatedly now (re-reads the queue each pass - see
    # test_prefetch_worker_picks_up_tracks_added_mid_pass below for why),
    # not just once; just confirm it's always asked for the same thing.
    player.queue.tracks.assert_called_with(incTrack=False)
    processed_ids = [call.args[0].identifier for call in player._prefetch_and_notify.call_args_list]
    assert processed_ids == ["vidB", "vidC", "vidD"]


async def test_prefetch_worker_is_noop_on_an_empty_queue():
    player = make_bare_player()
    player.queue.tracks = MagicMock(return_value=[])
    player._prefetch_and_notify = AsyncMock()

    await player._prefetch_worker()

    player._prefetch_and_notify.assert_not_called()


async def test_prefetch_worker_skips_tracks_already_cached():
    """Regression: without this skip, every fresh worker pass (one per
    add_track() call once the previous pass has finished) re-ran
    _prefetch_and_notify for the *entire* queue, including tracks that
    finished prefetching long ago - no second download (prefetch_direct_track
    is its own cache hit), but the dashboard would flash loading-then-ready
    for the whole list every time just one more song got added.
    """
    player = make_bare_player()
    tracks = [
        make_track(identifier="vidCached1", source="youtube"),
        make_track(identifier="vidNew", source="youtube"),
        make_track(identifier="vidCached2", source="youtube"),
    ]
    player.queue.tracks = MagicMock(return_value=tracks)
    player._node.is_direct_resolve_cached = MagicMock(
        side_effect=lambda vid: vid in ("vidCached1", "vidCached2")
    )
    player._prefetch_and_notify = AsyncMock()

    await player._prefetch_worker()

    processed_ids = [call.args[0].identifier for call in player._prefetch_and_notify.call_args_list]
    assert processed_ids == ["vidNew"]


async def test_prefetch_worker_picks_up_tracks_added_mid_pass():
    """Regression: quickly adding 3 songs while the first is still being
    prefetched. add_track() for the 2nd and 3rd correctly sees a worker
    already running and no-ops, trusting it to pick them up on its own -
    which only works if the worker re-reads the queue on every iteration
    instead of looping over a snapshot taken before they ever existed (the
    bug: the worker would finish after the 1st track and exit, never having
    seen the other two - they'd then sit unprefetched until an unrelated
    TrackStartEvent happened to fire later).
    """
    player = make_bare_player()
    tracks = [make_track(identifier="vidA", source="youtube")]
    player.queue.tracks = MagicMock(side_effect=lambda incTrack=False: list(tracks))

    async def notify_side_effect(track):
        if track.identifier == "vidA":
            # Simulates add_track() for 2 more songs landing while vidA's
            # prefetch is still in flight - mutates the queue mid-pass, same
            # as the real queue would under rapid clicking.
            tracks.append(make_track(identifier="vidB", source="youtube"))
            tracks.append(make_track(identifier="vidC", source="youtube"))

    player._prefetch_and_notify = AsyncMock(side_effect=notify_side_effect)

    await player._prefetch_worker()

    processed_ids = [call.args[0].identifier for call in player._prefetch_and_notify.call_args_list]
    assert processed_ids == ["vidA", "vidB", "vidC"]


async def test_prefetch_worker_never_retries_a_failed_video_within_the_same_pass():
    """Regression guard for the fix above: re-reading the queue each
    iteration must not re-attempt a video whose resolve just failed -
    prefetch_direct_track() doesn't cache failures, so without the
    `attempted` tracking this would loop on the same failing video forever
    instead of moving on (or returning once nothing else is left).
    """
    player = make_bare_player()
    tracks = [
        make_track(identifier="vidBad", source="youtube"),
        make_track(identifier="vidGood", source="youtube"),
    ]
    player.queue.tracks = MagicMock(return_value=tracks)
    player._prefetch_and_notify = AsyncMock()  # never actually caches anything

    await player._prefetch_worker()

    processed_ids = [call.args[0].identifier for call in player._prefetch_and_notify.call_args_list]
    assert processed_ids == ["vidBad", "vidGood"]  # each attempted exactly once, then the pass ends


async def test_prefetch_worker_never_runs_more_than_one_prefetch_at_a_time():
    player = make_bare_player()
    tracks = [make_track(identifier=f"vid{i}", source="youtube") for i in range(3)]
    player.queue.tracks = MagicMock(return_value=tracks)
    in_flight = 0
    max_in_flight = 0

    async def fake_prefetch_and_notify(track):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0)  # yield control, like a real await would
        in_flight -= 1

    player._prefetch_and_notify = fake_prefetch_and_notify

    await player._prefetch_worker()

    assert max_in_flight == 1


# --- add_track's prefetch trigger ---------------------------------------
#
# Regression coverage for the "still laggy" production report: in real usage,
# songs usually get queued one at a time *while something is already
# playing*, which happens after that current track's TrackStartEvent already
# fired and found nothing upcoming to prefetch. Without add_track also
# triggering the check, the newly-queued next track never gets a head start
# at all and always falls back to the raw, throttled CDN url - exactly as if
# none of the prefetch/stream-cache work existed.


async def test_add_track_triggers_prefetch_when_a_track_is_already_playing():
    player = make_bare_player()
    player._current = make_track(identifier="vidCurrent", source="youtube")
    player._is_connected = True
    player.queue.put = MagicMock(return_value=1)
    player._prefetch_next_direct_track = MagicMock()
    new_track = make_track(identifier="vidNext", source="youtube")

    await player.add_track(new_track)

    player._prefetch_next_direct_track.assert_called_once()


async def test_add_track_triggers_prefetch_even_when_nothing_is_playing():
    """Patch 16: this used to assert the opposite (not called while idle) -
    that gate meant every track after the one do_next() was about to
    immediately consume (adding several songs at once to an idle player)
    got zero prefetch/caching indicator until the first track's own
    TrackStartEvent eventually fired, which Patch 11 can delay by however
    long that first track's own pre-cache download takes. Now unconditional -
    _prefetch_worker only ever walks tracks *after* the current position, so
    it can't collide with whatever do_next()/play() does for the track about
    to become current.
    """
    player = make_bare_player()
    assert player.is_playing is False
    player.queue.put = MagicMock(return_value=1)
    player._prefetch_next_direct_track = MagicMock()
    new_track = make_track(identifier="vidNext", source="youtube")

    await player.add_track(new_track)

    player._prefetch_next_direct_track.assert_called_once()


async def test_add_track_triggers_prefetch_for_a_batch_add_too():
    player = make_bare_player()
    player._current = make_track(identifier="vidCurrent", source="youtube")
    player._is_connected = True
    player.queue.put = MagicMock(return_value=1)
    player._prefetch_next_direct_track = MagicMock()
    new_tracks = [make_track(identifier="vidA", source="youtube"), make_track(identifier="vidB", source="youtube")]

    await player.add_track(new_tracks)

    player._prefetch_next_direct_track.assert_called_once()


# --- _prefetch_and_notify (prefetchStatus IPC messages) ------------------


async def test_prefetch_and_notify_reports_loading_then_ready_then_cache_stats():
    player = make_bare_player()
    connect_ipc(player)
    player._node.prefetch_direct_track = AsyncMock(return_value="RESOLVED_ENCODED")
    player._node.get_direct_resolve_cache_stats = MagicMock(return_value={"entries": 1, "approxBytes": 20})
    track = make_track(identifier="vidNext", track_id="NATIVE_NEXT")

    await player._prefetch_and_notify(track)

    assert player.send_ws.call_count == 3
    loading_call, ready_call, stats_call = (c.args[0] for c in player.send_ws.call_args_list)
    assert loading_call == {"op": "prefetchStatus", "trackId": "NATIVE_NEXT", "status": "loading", "progress": 0}
    assert ready_call == {"op": "prefetchStatus", "trackId": "NATIVE_NEXT", "status": "ready"}
    assert stats_call == {"op": "cacheStats", "entries": 1, "approxBytes": 20}


async def test_prefetch_and_notify_reports_unavailable_when_resolution_fails():
    player = make_bare_player()
    connect_ipc(player)
    player._node.prefetch_direct_track = AsyncMock(return_value=None)
    track = make_track(identifier="vidNext", track_id="NATIVE_NEXT")

    await player._prefetch_and_notify(track)

    _, status_call, _ = (c.args[0] for c in player.send_ws.call_args_list)
    assert status_call["status"] == "unavailable"


async def test_prefetch_and_notify_sends_nothing_when_ipc_disconnected():
    player = make_bare_player()  # IPC disconnected by default
    player._node.prefetch_direct_track = AsyncMock(return_value="RESOLVED_ENCODED")
    track = make_track(identifier="vidNext")

    await player._prefetch_and_notify(track)

    player.send_ws.assert_not_called()
    player._node.prefetch_direct_track.assert_awaited_once_with("vidNext", "Test Track")
    player._node.get_prefetch_progress.assert_not_called()  # no point polling with nowhere to send it


async def test_prefetch_and_notify_reports_progress_updates_while_downloading():
    player = make_bare_player()
    connect_ipc(player)
    finish_event = asyncio.Event()
    progress_calls = []

    async def slow_prefetch(video_id, title):
        await finish_event.wait()
        return "RESOLVED_ENCODED"

    async def fake_progress(video_id):
        progress_calls.append(None)
        if len(progress_calls) >= 2:
            finish_event.set()
        return 0.1 * len(progress_calls)

    player._node.prefetch_direct_track = slow_prefetch
    player._node.get_prefetch_progress = fake_progress
    track = make_track(identifier="vidNext", track_id="NATIVE_NEXT")

    await player._prefetch_and_notify(track)

    sent = [c.args[0] for c in player.send_ws.call_args_list]
    loading_sends = [s for s in sent if s.get("status") == "loading"]
    progress_values = [s["progress"] for s in loading_sends]

    assert progress_values[0] == 0  # the initial send, before any polling
    assert len(progress_values) >= 3  # initial + at least the 2 real polls above
    assert progress_values[1] < progress_values[-1]  # genuinely increasing, not repeats
    assert sent[-2]["status"] in ("ready", "unavailable")
    assert sent[-1]["op"] == "cacheStats"


async def test_prefetch_and_notify_does_not_send_a_progress_update_when_unknown():
    player = make_bare_player()
    connect_ipc(player)
    # default player._node.get_prefetch_progress already returns None

    async def slow_prefetch(video_id, title):
        await asyncio.sleep(0)
        return "RESOLVED_ENCODED"

    player._node.prefetch_direct_track = slow_prefetch
    track = make_track(identifier="vidNext", track_id="NATIVE_NEXT")

    await player._prefetch_and_notify(track)

    sent = [c.args[0] for c in player.send_ws.call_args_list]
    loading_sends = [s for s in sent if s.get("status") == "loading"]
    assert len(loading_sends) == 1  # only the initial send - no poll ever had a value to report


# --- _resolve_and_notify_current_track (Patch 13: now-playing caching bar) -
#
# Same reporting shape as _prefetch_and_notify above, but wraps
# resolve_direct_track() (the on-demand, currently-playing-track path -
# which Patch 11 made block on a full download for a cache miss) instead of
# prefetch_direct_track() (the background, upcoming-track path). Lets the
# dashboard's Now Playing panel show a caching indicator/progress bar
# instead of sitting silently while a brand-new video downloads.


async def test_resolve_and_notify_reports_loading_then_ready():
    player = make_bare_player()
    connect_ipc(player)
    player._node.resolve_direct_track = AsyncMock(return_value="RESOLVED_ENCODED")
    track = make_track(identifier="vidNow", track_id="NATIVE_NOW", title="Now Playing Song")

    result = await player._resolve_and_notify_current_track(track)

    assert result == "RESOLVED_ENCODED"
    assert player.send_ws.call_count == 2
    loading_call, ready_call = (c.args[0] for c in player.send_ws.call_args_list)
    assert loading_call == {"op": "prefetchStatus", "trackId": "NATIVE_NOW", "status": "loading", "progress": 0}
    assert ready_call == {"op": "prefetchStatus", "trackId": "NATIVE_NOW", "status": "ready"}
    player._node.resolve_direct_track.assert_awaited_once_with("vidNow", "Now Playing Song")


async def test_resolve_and_notify_reports_unavailable_when_resolution_fails():
    player = make_bare_player()
    connect_ipc(player)
    player._node.resolve_direct_track = AsyncMock(return_value=None)
    track = make_track(identifier="vidNow", track_id="NATIVE_NOW")

    result = await player._resolve_and_notify_current_track(track)

    assert result is None
    _, status_call = (c.args[0] for c in player.send_ws.call_args_list)
    assert status_call["status"] == "unavailable"


async def test_resolve_and_notify_sends_nothing_when_ipc_disconnected():
    player = make_bare_player()  # IPC disconnected by default
    player._node.resolve_direct_track = AsyncMock(return_value="RESOLVED_ENCODED")
    track = make_track(identifier="vidNow")

    await player._resolve_and_notify_current_track(track)

    player.send_ws.assert_not_called()
    player._node.get_prefetch_progress.assert_not_called()


async def test_resolve_and_notify_reports_progress_updates_while_downloading():
    player = make_bare_player()
    connect_ipc(player)
    finish_event = asyncio.Event()
    progress_calls = []

    async def slow_resolve(video_id, title):
        await finish_event.wait()
        return "RESOLVED_ENCODED"

    async def fake_progress(video_id):
        progress_calls.append(None)
        if len(progress_calls) >= 2:
            finish_event.set()
        return 0.1 * len(progress_calls)

    player._node.resolve_direct_track = slow_resolve
    player._node.get_prefetch_progress = fake_progress
    track = make_track(identifier="vidNow", track_id="NATIVE_NOW")

    await player._resolve_and_notify_current_track(track)

    sent = [c.args[0] for c in player.send_ws.call_args_list]
    loading_sends = [s for s in sent if s.get("status") == "loading"]
    progress_values = [s["progress"] for s in loading_sends]

    assert progress_values[0] == 0
    assert len(progress_values) >= 3
    assert progress_values[1] < progress_values[-1]
    assert sent[-1]["status"] in ("ready", "unavailable")


# --- _report_cache_stats --------------------------------------------------


async def test_report_cache_stats_sends_when_connected():
    player = make_bare_player()
    connect_ipc(player)
    player._node.get_direct_resolve_cache_stats = MagicMock(return_value={"entries": 5, "approxBytes": 1024})

    await player._report_cache_stats()

    player.send_ws.assert_awaited_once_with({"op": "cacheStats", "entries": 5, "approxBytes": 1024})


async def test_report_cache_stats_sends_nothing_when_disconnected():
    player = make_bare_player()  # IPC disconnected by default

    await player._report_cache_stats()

    player.send_ws.assert_not_called()


async def test_report_cache_stats_sends_nothing_when_node_missing():
    player = make_bare_player()
    connect_ipc(player)
    player._node = None

    await player._report_cache_stats()  # must not raise (no node to ask)

    player.send_ws.assert_not_called()


# --- _retry_direct_resolve (loadFailed recovery) --------------------------


async def test_retry_direct_resolve_invalidates_cache_and_resumes_at_position():
    player = make_bare_player()
    player._node.invalidate_direct_resolve_cache = MagicMock()
    player._node.resolve_direct_track = AsyncMock(return_value="FRESH_ENCODED")
    track = make_track(source="youtube", identifier="vidA", track_id="NATIVE")

    await player._retry_direct_resolve(track, resume_at=45000.0)

    player._node.invalidate_direct_resolve_cache.assert_called_once_with("vidA")
    sent_data = player.send.call_args.kwargs["data"]
    assert sent_data["position"] == "45000"
    assert sent_data["encodedTrack"] == "FRESH_ENCODED"
    assert player._current is track


async def test_retry_direct_resolve_falls_back_to_do_next_when_play_fails():
    player = make_bare_player()
    player._node.invalidate_direct_resolve_cache = MagicMock()
    player.play = AsyncMock(side_effect=RuntimeError("lavalink down"))
    player.do_next = AsyncMock()
    track = make_track(source="youtube", identifier="vidA")

    await player._retry_direct_resolve(track, resume_at=1000.0)

    player.do_next.assert_awaited_once()
    assert player._current is None
    player._logger.error.assert_called_once()


# --- TrackStartEvent resets the retry budget -------------------------------


async def test_track_start_event_resets_retry_count_for_the_now_playing_video():
    player = make_bare_player()
    player._current = make_track(source="youtube", identifier="vidA")
    player._direct_resolve_retry_counts = {"vidA": 2, "vidOther": 1}
    player.queue.tracks = MagicMock(return_value=[])  # nothing upcoming to prefetch

    await player._dispatch_event({"type": "TrackStartEvent"})

    assert "vidA" not in player._direct_resolve_retry_counts
    assert player._direct_resolve_retry_counts["vidOther"] == 1


# --- TrackEndEvent captures position before _current is cleared -----------
#
# Regression test: Player.position reads player._current, which
# Player._dispatch_event clears for any non-"replaced" TrackEndEvent. If the
# event didn't snapshot the position at construction time (before that
# clear), Player._retry_direct_resolve would always resume from 0 instead of
# where playback actually died.


async def test_track_end_event_position_reflects_elapsed_time_not_post_clear_zero():
    player = make_bare_player()
    track = make_track(source="youtube", identifier="vidA", length=200_000)
    player._current = track
    player._ending_track = track
    player._is_connected = True
    player._paused = False
    player._last_position = 30_000
    player._last_update = time.time() * 1000

    await player._dispatch_event({"type": "TrackEndEvent", "reason": "loadFailed"})

    assert player._current is None  # cleared as normal for a non-"replaced" end
    event_name, dispatched_player, dispatched_track, reason, position = player._bot.dispatch.call_args.args
    assert event_name == "voicelink_track_end"
    assert reason == "loadFailed"
    assert position >= 30_000  # not the post-clear 0 this test guards against
