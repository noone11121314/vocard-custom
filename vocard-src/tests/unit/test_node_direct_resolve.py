"""Unit tests for the direct-URL-resolution additions on voicelink.pool.Node:
resolve_direct_track's cache/lock behaviour, prefetch_direct_track,
_prune_direct_resolve_cache, and get_direct_resolve_cache_stats. Lavalink,
invidious-companion, and yt-dlp are all mocked out; see tests/integration for
the real-network versions of these paths.
"""
from __future__ import annotations

import asyncio
import time
from urllib.parse import quote

import pytest
from unittest.mock import AsyncMock

from voicelink import direct_resolver, stream_cache


@pytest.fixture(autouse=True)
def no_disk_cache_hit_by_default(monkeypatch):
    """resolve_direct_track() checks stream_cache.get_local_url() first (see
    its own module-level test section below) - default that to "nothing
    disk-cached" for every other test in this file, so they keep exercising
    the invidious-companion/_direct_resolve_cache path they're actually
    about without a real StreamCache singleton (and its real TCP listener)
    spinning up as a side effect. Tests that care about the disk-cache-hit
    path override this themselves.

    Also defaults stream_cache.get_gain() (Patch 10's per-track loudness
    normalization) to "unmeasured" for the same reason - see its own test
    section near the bottom of this file.

    Also defaults stream_cache.prefetch_to_disk() (Patch 11's wait-for-
    pre-cache-before-playing behavior) to "download unavailable" (False) -
    on a disk-cache miss, resolve_direct_track() now tries this before
    falling back to invidious-companion (see the "on-demand resolve now
    waits for a pre-cache download" section below); defaulting it to False
    keeps every other test in this file exercising the companion-fallback
    path they're actually about, without a real yt-dlp subprocess spawning
    as a side effect. Tests that care about the pre-cache-wait path
    override this themselves.
    """
    monkeypatch.setattr(stream_cache, "get_local_url", AsyncMock(return_value=None))
    monkeypatch.setattr(stream_cache, "get_gain", AsyncMock(return_value=None))
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", AsyncMock(return_value=False))


async def test_cache_hit_skips_companion_and_lavalink(node, monkeypatch):
    node._direct_resolve_cache["vid1"] = (time.monotonic(), "CACHED_ENCODED")
    resolver_mock = AsyncMock(return_value="http://should-not-be-called")
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", resolver_mock)
    node.send = AsyncMock()

    result = await node.resolve_direct_track("vid1")

    assert result == "CACHED_ENCODED"
    resolver_mock.assert_not_called()
    node.send.assert_not_called()


async def test_cache_miss_resolves_via_companion_and_lavalink_then_caches(node, monkeypatch):
    resolver_mock = AsyncMock(return_value="http://direct-url")
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", resolver_mock)
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "NEW_ENCODED"}})

    result = await node.resolve_direct_track("vid2")

    assert result == "NEW_ENCODED"
    resolver_mock.assert_awaited_once_with(node._session, "vid2")
    node.send.assert_awaited_once()
    assert node._direct_resolve_cache["vid2"][1] == "NEW_ENCODED"


async def test_expired_cache_entry_triggers_refetch(node, monkeypatch):
    node._direct_resolve_cache["vid3"] = (time.monotonic() - node._DIRECT_RESOLVE_CACHE_TTL - 1, "STALE")
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", AsyncMock(return_value="http://fresh"))
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "REFRESHED"}})

    result = await node.resolve_direct_track("vid3")

    assert result == "REFRESHED"
    assert node._direct_resolve_cache["vid3"][1] == "REFRESHED"


async def test_disk_cache_hit_is_served_even_with_no_direct_resolve_cache_entry(node, monkeypatch):
    """Regression guard (2026-10-11): resolve_direct_track() used to only
    ever check `_direct_resolve_cache` (a 30-minute cache of *resolved*
    urls), never stream_cache's actual disk cache (no TTL) - so a video
    fully downloaded hours ago, with no fresh `_direct_resolve_cache` entry
    (e.g. after a bot restart, or just past the 30-minute window), would
    get re-resolved via invidious-companion as if it had never been cached
    at all, even though the file was sitting right there. Must hit the disk
    cache first and never call the companion resolver.
    """
    monkeypatch.setattr(stream_cache, "get_local_url", AsyncMock(return_value="http://vocard:2334/stream/vid5"))
    resolver_mock = AsyncMock(return_value="http://should-not-be-called")
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", resolver_mock)
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "FROM_DISK"}})

    result = await node.resolve_direct_track("vid5")

    assert result == "FROM_DISK"
    resolver_mock.assert_not_called()
    assert node._direct_resolve_cache["vid5"][1] == "FROM_DISK"


async def test_disk_cache_hit_still_defers_to_a_fresher_direct_resolve_cache_entry(node, monkeypatch):
    """The short-lived encoded-track cache is checked first and wins if
    it's fresh - no reason to re-ask stream_cache/Lavalink for something
    already resolved moments ago.
    """
    node._direct_resolve_cache["vid6"] = (time.monotonic(), "ALREADY_RESOLVED")
    get_local_url_mock = AsyncMock()
    monkeypatch.setattr(stream_cache, "get_local_url", get_local_url_mock)

    result = await node.resolve_direct_track("vid6")

    assert result == "ALREADY_RESOLVED"
    get_local_url_mock.assert_not_called()


async def test_disk_cache_check_failure_falls_through_to_companion(node, monkeypatch):
    monkeypatch.setattr(stream_cache, "get_local_url", AsyncMock(side_effect=RuntimeError("cache unavailable")))
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", AsyncMock(return_value="http://fresh"))
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "FROM_COMPANION"}})

    result = await node.resolve_direct_track("vid7")  # must not raise

    assert result == "FROM_COMPANION"


async def test_returns_none_and_does_not_cache_when_companion_has_no_url(node, monkeypatch):
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", AsyncMock(return_value=None))
    node.send = AsyncMock()

    result = await node.resolve_direct_track("vid4")

    assert result is None
    node.send.assert_not_called()
    assert "vid4" not in node._direct_resolve_cache


async def test_returns_none_and_does_not_cache_on_lavalink_exception(node, monkeypatch):
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", AsyncMock(return_value="http://x"))
    node.send = AsyncMock(side_effect=RuntimeError("lavalink down"))

    result = await node.resolve_direct_track("vid5")

    assert result is None
    assert "vid5" not in node._direct_resolve_cache


async def test_returns_none_and_does_not_cache_when_lavalink_rejects_load(node, monkeypatch):
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", AsyncMock(return_value="http://x"))
    node.send = AsyncMock(return_value={"loadType": "error"})

    result = await node.resolve_direct_track("vid6")

    assert result is None
    assert "vid6" not in node._direct_resolve_cache


async def test_concurrent_calls_for_same_video_dedupe_to_one_resolve(node, monkeypatch):
    call_count = 0

    async def slow_resolve(session, video_id):
        nonlocal call_count
        call_count += 1
        await asyncio.sleep(0.05)
        return "http://slow"

    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", slow_resolve)
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "DEDUPED"}})

    first, second = await asyncio.gather(
        node.resolve_direct_track("vid7"),
        node.resolve_direct_track("vid7"),
    )

    assert first == second == "DEDUPED"
    assert call_count == 1
    node.send.assert_awaited_once()


async def test_lock_is_released_after_resolution_so_later_calls_are_not_blocked(node, monkeypatch):
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", AsyncMock(return_value="http://x"))
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "FIRST"}})
    await node.resolve_direct_track("vid8")

    assert "vid8" not in node._direct_resolve_locks


def test_prune_removes_only_expired_entries(node):
    now = time.monotonic()
    node._direct_resolve_cache["fresh"] = (now, "FRESH")
    node._direct_resolve_cache["stale"] = (now - node._DIRECT_RESOLVE_CACHE_TTL - 1, "STALE")

    node._prune_direct_resolve_cache()

    assert node._direct_resolve_cache == {"fresh": (now, "FRESH")}


def test_prune_is_a_noop_on_empty_cache(node):
    node._prune_direct_resolve_cache()

    assert node._direct_resolve_cache == {}


async def test_prune_runs_automatically_after_a_successful_resolve(node, monkeypatch):
    node._direct_resolve_cache["stale"] = (time.monotonic() - node._DIRECT_RESOLVE_CACHE_TTL - 1, "STALE")
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", AsyncMock(return_value="http://x"))
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "NEW"}})

    await node.resolve_direct_track("vid9")

    assert "stale" not in node._direct_resolve_cache
    assert node._direct_resolve_cache["vid9"][1] == "NEW"


# --- invalidate_direct_resolve_cache (used by Player._retry_direct_resolve) -


def test_invalidate_direct_resolve_cache_removes_the_entry(node):
    node._direct_resolve_cache["vid30"] = (time.monotonic(), "STALE")

    node.invalidate_direct_resolve_cache("vid30")

    assert "vid30" not in node._direct_resolve_cache


def test_invalidate_direct_resolve_cache_is_a_noop_for_an_unknown_video(node):
    node.invalidate_direct_resolve_cache("never-cached")  # must not raise

    assert "never-cached" not in node._direct_resolve_cache


# --- is_direct_resolve_cached (Player._prefetch_worker's skip check) -----


def test_is_direct_resolve_cached_true_for_a_fresh_entry(node):
    node._direct_resolve_cache["vid50"] = (time.monotonic(), "ENCODED")

    assert node.is_direct_resolve_cached("vid50") is True


def test_is_direct_resolve_cached_false_for_an_unknown_video(node):
    assert node.is_direct_resolve_cached("never-cached") is False


def test_is_direct_resolve_cached_false_for_an_expired_entry(node):
    node._direct_resolve_cache["vid51"] = (time.monotonic() - node._DIRECT_RESOLVE_CACHE_TTL - 1, "ENCODED")

    assert node.is_direct_resolve_cached("vid51") is False


async def test_invalidated_entry_forces_a_fresh_companion_resolve(node, monkeypatch):
    """Regression for the loadFailed retry path: invalidating a cached entry
    must actually force resolve_direct_track to hit the companion again,
    not just silently keep serving the stale encoded track.
    """
    resolver_mock = AsyncMock(side_effect=["http://stale", "http://fresh"])
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", resolver_mock)
    node.send = AsyncMock(side_effect=[
        {"loadType": "track", "data": {"encoded": "STALE_ENCODED"}},
        {"loadType": "track", "data": {"encoded": "FRESH_ENCODED"}},
    ])

    first = await node.resolve_direct_track("vid31")
    node.invalidate_direct_resolve_cache("vid31")
    second = await node.resolve_direct_track("vid31")

    assert first == "STALE_ENCODED"
    assert second == "FRESH_ENCODED"
    assert resolver_mock.await_count == 2


# --- get_direct_resolve_cache_stats (dashboard display only) -------------


def test_cache_stats_on_empty_cache(node):
    assert node.get_direct_resolve_cache_stats() == {"entries": 0, "approxBytes": 0}


def test_cache_stats_reports_entry_count_and_approx_bytes(node):
    node._direct_resolve_cache["vidA"] = (time.monotonic(), "x" * 10)
    node._direct_resolve_cache["vidB"] = (time.monotonic(), "y" * 25)

    stats = node.get_direct_resolve_cache_stats()

    assert stats == {"entries": 2, "approxBytes": 35}


async def test_cache_stats_reflects_writes_and_pruning(node, monkeypatch):
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", AsyncMock(return_value="http://x"))
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "ENCODED"}})

    await node.resolve_direct_track("vid13")
    assert node.get_direct_resolve_cache_stats() == {"entries": 1, "approxBytes": len("ENCODED")}

    node._direct_resolve_cache["vid13"] = (time.monotonic() - node._DIRECT_RESOLVE_CACHE_TTL - 1, "ENCODED")
    node._prune_direct_resolve_cache()

    assert node.get_direct_resolve_cache_stats() == {"entries": 0, "approxBytes": 0}


# --- on-demand resolve now waits for a pre-cache download (Patch 11) -------
#
# Historically, resolve_direct_track() (play()'s on-demand/cache-miss path)
# always fell straight back to invidious-companion's raw CDN url and never
# started a yt-dlp download itself, to avoid racing an in-progress download
# against immediate playback (risked tripping Lavalink's own HTTP read
# timeout mid-stream). Patch 11 (2026-10-18) changes this on explicit
# request ("I can happily wait the song to be pre-cached before playing to
# prevent lag"): on a disk-cache miss, it now downloads the video and waits
# for it to finish *before* handing anything to Lavalink at all - so there's
# no partially-downloaded file in flight for playback to outrun any more,
# removing the original race entirely. invidious-companion is now only a
# fallback for when the download itself fails.


async def test_on_demand_resolve_waits_for_a_download_and_serves_it(node, monkeypatch):
    prefetch_mock = AsyncMock(return_value=True)
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", prefetch_mock)
    # First call (before any download) is the initial cache-miss check;
    # second call (after prefetch_to_disk "finishes") finds it ready.
    monkeypatch.setattr(stream_cache, "get_local_url", AsyncMock(side_effect=[None, "http://vocard:2334/stream/vid20"]))
    resolver_mock = AsyncMock(return_value="http://should-not-be-called")
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", resolver_mock)
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "FROM_FRESH_DOWNLOAD"}})

    result = await node.resolve_direct_track("vid20", "My Song")

    assert result == "FROM_FRESH_DOWNLOAD"
    prefetch_mock.assert_awaited_once_with("vid20", "https://www.youtube.com/watch?v=vid20", "My Song")
    resolver_mock.assert_not_called()
    assert node._direct_resolve_cache["vid20"][1] == "FROM_FRESH_DOWNLOAD"


async def test_on_demand_resolve_falls_back_to_companion_when_download_fails(node, monkeypatch):
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", AsyncMock(return_value=False))
    resolver_mock = AsyncMock(return_value="http://raw-url")
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", resolver_mock)
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "FROM_COMPANION"}})

    result = await node.resolve_direct_track("vid21")

    assert result == "FROM_COMPANION"
    resolver_mock.assert_awaited_once()


async def test_on_demand_resolve_falls_back_to_companion_when_download_raises(node, monkeypatch):
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", AsyncMock(side_effect=RuntimeError("yt-dlp blew up")))
    resolver_mock = AsyncMock(return_value="http://raw-url")
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", resolver_mock)
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "FROM_COMPANION"}})

    result = await node.resolve_direct_track("vid22")  # must not raise

    assert result == "FROM_COMPANION"
    resolver_mock.assert_awaited_once()


async def test_on_demand_resolve_falls_back_to_companion_when_download_succeeds_but_url_still_missing(node, monkeypatch):
    """Belt-and-suspenders: download() reported success but get_local_url()
    somehow still has nothing (e.g. evicted the instant it finished) -
    must not treat that as a hard failure, just fall through same as any
    other miss.
    """
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", AsyncMock(return_value=True))
    monkeypatch.setattr(stream_cache, "get_local_url", AsyncMock(return_value=None))
    resolver_mock = AsyncMock(return_value="http://raw-url")
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", resolver_mock)
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "FROM_COMPANION"}})

    result = await node.resolve_direct_track("vid23")

    assert result == "FROM_COMPANION"
    resolver_mock.assert_awaited_once()


async def test_on_demand_resolve_still_checks_disk_cache_before_downloading(node, monkeypatch):
    """A plain cache hit must not trigger a redundant download attempt."""
    monkeypatch.setattr(stream_cache, "get_local_url", AsyncMock(return_value="http://vocard:2334/stream/vid24"))
    prefetch_mock = AsyncMock()
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", prefetch_mock)
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "FROM_DISK"}})

    result = await node.resolve_direct_track("vid24")

    assert result == "FROM_DISK"
    prefetch_mock.assert_not_called()


# --- prefetch_direct_track (yt-dlp-to-disk background path) --------------


async def test_prefetch_skips_all_work_on_a_cache_hit(node, monkeypatch):
    node._direct_resolve_cache["vid14"] = (time.monotonic(), "CACHED")
    prefetch_mock = AsyncMock()
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", prefetch_mock)

    result = await node.prefetch_direct_track("vid14")

    assert result == "CACHED"
    prefetch_mock.assert_not_called()


async def test_prefetch_downloads_via_ytdlp_and_loads_the_local_url_into_lavalink(node, monkeypatch):
    prefetch_mock = AsyncMock(return_value=True)
    get_url_mock = AsyncMock(return_value="http://vocard:2334/stream/vid15")
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", prefetch_mock)
    monkeypatch.setattr(stream_cache, "get_local_url", get_url_mock)
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "PREFETCHED"}})

    result = await node.prefetch_direct_track("vid15", "My Song")

    assert result == "PREFETCHED"
    assert node._direct_resolve_cache["vid15"][1] == "PREFETCHED"
    prefetch_mock.assert_awaited_once_with("vid15", "https://www.youtube.com/watch?v=vid15", "My Song")
    sent_path = node.send.call_args.args[1]
    assert sent_path == f"loadtracks?identifier={quote('http://vocard:2334/stream/vid15')}"


async def test_prefetch_returns_none_when_ytdlp_download_fails(node, monkeypatch):
    prefetch_mock = AsyncMock(return_value=False)
    get_url_mock = AsyncMock()
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", prefetch_mock)
    monkeypatch.setattr(stream_cache, "get_local_url", get_url_mock)
    node.send = AsyncMock()

    result = await node.prefetch_direct_track("vid16")

    assert result is None
    assert "vid16" not in node._direct_resolve_cache
    get_url_mock.assert_not_called()
    node.send.assert_not_called()


async def test_prefetch_returns_none_if_download_succeeded_but_no_local_url(node, monkeypatch):
    """Defensive: download() reporting success but get_local_url() somehow
    coming back empty (e.g. evicted between the two calls) must not crash or
    hand Lavalink an empty identifier.
    """
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", AsyncMock(return_value=True))
    monkeypatch.setattr(stream_cache, "get_local_url", AsyncMock(return_value=None))
    node.send = AsyncMock()

    result = await node.prefetch_direct_track("vid17")

    assert result is None
    node.send.assert_not_called()


async def test_prefetch_swallows_exceptions_and_returns_none(node, monkeypatch):
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", AsyncMock(side_effect=RuntimeError("yt-dlp not found")))

    result = await node.prefetch_direct_track("vidX")  # must not raise

    assert result is None


async def test_prefetch_then_play_time_hits_cache_not_network(node, monkeypatch):
    """End-to-end within the Node: simulates the prefetch-while-playing flow
    and asserts the follow-up resolve_direct_track call (what play() does) is
    a pure cache read with no further companion/yt-dlp/Lavalink calls.
    """
    monkeypatch.setattr(stream_cache, "prefetch_to_disk", AsyncMock(return_value=True))
    monkeypatch.setattr(stream_cache, "get_local_url", AsyncMock(return_value="http://vocard:2334/stream/vid11"))
    resolver_mock = AsyncMock(return_value="http://should-not-be-called")
    monkeypatch.setattr(direct_resolver, "resolve_youtube_direct_url", resolver_mock)
    node.send = AsyncMock(return_value={"loadType": "track", "data": {"encoded": "WARM"}})

    await node.prefetch_direct_track("vid11")
    result = await node.resolve_direct_track("vid11")

    assert result == "WARM"
    resolver_mock.assert_not_called()
    node.send.assert_awaited_once()


# --- get_prefetch_progress (dashboard progress bar) -----------------------


async def test_get_prefetch_progress_delegates_to_stream_cache(node, monkeypatch):
    monkeypatch.setattr(stream_cache, "get_progress", AsyncMock(return_value=0.42))

    result = await node.get_prefetch_progress("vid40")

    assert result == 0.42


async def test_get_prefetch_progress_returns_none_on_any_error(node, monkeypatch):
    monkeypatch.setattr(stream_cache, "get_progress", AsyncMock(side_effect=RuntimeError("boom")))

    result = await node.get_prefetch_progress("vid41")  # must not raise

    assert result is None


# --- get_track_gain (Patch 10: per-track loudness normalization) ----------


async def test_get_track_gain_delegates_to_stream_cache(node, monkeypatch):
    monkeypatch.setattr(stream_cache, "get_gain", AsyncMock(return_value=1.25))

    result = await node.get_track_gain("vid50")

    assert result == 1.25


async def test_get_track_gain_defaults_to_neutral_when_unmeasured(node, monkeypatch):
    monkeypatch.setattr(stream_cache, "get_gain", AsyncMock(return_value=None))

    result = await node.get_track_gain("vid51")

    assert result == 1.0


async def test_get_track_gain_defaults_to_neutral_on_any_error(node, monkeypatch):
    monkeypatch.setattr(stream_cache, "get_gain", AsyncMock(side_effect=RuntimeError("boom")))

    result = await node.get_track_gain("vid52")  # must not raise

    assert result == 1.0
