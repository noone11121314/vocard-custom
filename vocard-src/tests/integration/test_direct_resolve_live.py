"""Integration tests for the direct-URL-resolution feature against the actual
deployed invidious-companion and Lavalink containers (same services and network
the production bot uses; see /root/vocard/docker-compose.yml).

Not run by a plain `pytest` invocation: opt in with `-m integration`. Requires
network access to the `vocard` docker network's `invidious-companion` and
`lavalink` hostnames, and the same INVIDIOUS_COMPANION_URL/SECRET env vars the
vocard container itself runs with. The whole module is skipped if either
service can't be reached, so it's safe to leave in a suite that might run
somewhere without that network.

Since prefetch_direct_track() hands Lavalink a url on voicelink/stream_cache.py's
local HTTP proxy (serving the file it just yt-dlp'd to disk), Lavalink must be
able to reach *this test process* back over the network to fetch it - not
just the other way around. That means the container running this suite needs
a predictable, reachable name on the `vocard` network, and STREAM_CACHE_BASE_URL
must be set to match it (see docs/testing.md's run command - it passes
`--name`/`--network-alias` and the matching env var together). Get this wrong
and it fails loudly, not silently: Lavalink's loadtracks call for the
unreachable local url comes back with a non-"track" loadType (or errors
outright), so prefetch_direct_track() returns None exactly like a real
failure would - `test_prefetch_warms_the_cache_before_play_needs_it` below is
what catches a networking misconfiguration.

This suite also exercises yt-dlp making real requests to YouTube directly
(not through invidious-companion) for the prefetch path - see
docs/direct-url-resolver.md's "yt-dlp prefetch to disk" section for why the
two resolvers are kept independent.
"""
from __future__ import annotations

import os
import time
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

from voicelink import direct_resolver
from voicelink.pool import Node

pytestmark = pytest.mark.integration

# Rick Astley - Never Gonna Give You Up: picked deliberately as a stable,
# always-available, non-region-locked video so this suite doesn't flake on
# whatever happens to be trending or age-restricted.
STABLE_VIDEO_ID = "dQw4w9WgXcQ"

LAVALINK_HOST = "lavalink"
LAVALINK_PORT = 2333
LAVALINK_PASSWORD = "youshallnotpass"


@pytest.fixture
async def live_session():
    # Function-scoped (not module-scoped): pytest-asyncio gives each test its
    # own event loop by default, and an aiohttp session/connector is bound to
    # whichever loop was running when it was created, so sharing one across
    # tests raises "Timeout context manager should be used inside a task".
    async with aiohttp.ClientSession() as session:
        yield session


@pytest.fixture(autouse=True)
async def reset_stream_cache_singleton():
    """voicelink/stream_cache.py's proxy server is a lazily-started
    module-level singleton, same pattern as the resolve cache's TTL. In
    production that's fine - one bot process, one event loop, for the whole
    lifetime of the server. But pytest-asyncio gives each test function its
    own event loop, and the singleton's aiohttp AppRunner/TCPSite is bound to
    whichever loop was running when it was first started - once that test's
    loop closes, the still-listening socket can no longer be serviced, and
    the next test's Lavalink loadtracks call against it hangs until Lavalink's
    own read timeout kills it with a SocketTimeoutException (looks exactly
    like a real stream_cache failure, but isn't one - it's a test-harness
    artifact). Reset the singleton before every test so each one starts (or
    reuses, within the same test) a proxy bound to its own current loop.
    """
    from voicelink import stream_cache

    if stream_cache._cache is not None:
        await stream_cache._cache.stop()
    stream_cache._cache = None
    yield


@pytest.fixture(autouse=True)
async def require_live_services(live_session):
    if not os.getenv("INVIDIOUS_COMPANION_SECRET"):
        pytest.skip("INVIDIOUS_COMPANION_SECRET not set; run inside the vocard container/network to exercise this suite")

    try:
        async with live_session.get(
            f"http://{LAVALINK_HOST}:{LAVALINK_PORT}/v4/info",
            headers={"Authorization": LAVALINK_PASSWORD},
            timeout=aiohttp.ClientTimeout(total=5),
        ) as resp:
            if resp.status >= 300:
                pytest.skip(f"Lavalink reachable but returned HTTP {resp.status}")
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        pytest.skip(f"Lavalink not reachable at {LAVALINK_HOST}:{LAVALINK_PORT}: {e}")

    try:
        async with live_session.post(
            f"{direct_resolver.COMPANION_BASE_URL}/companion/youtubei/v1/player",
            headers={"Authorization": f"Bearer {direct_resolver.COMPANION_SECRET}"},
            json={"videoId": STABLE_VIDEO_ID},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status >= 300:
                pytest.skip(f"invidious-companion reachable but returned HTTP {resp.status}")
    except (aiohttp.ClientError, TimeoutError, OSError) as e:
        pytest.skip(f"invidious-companion not reachable: {e}")


@pytest.fixture
async def live_node(live_session):
    bot = MagicMock()  # add_listener() is sync in real discord.py; AsyncMock would warn
    bot.user.id = 999999

    n = Node(
        pool=MagicMock(),
        bot=bot,
        host=LAVALINK_HOST,
        port=LAVALINK_PORT,
        password=LAVALINK_PASSWORD,
        identifier="INTEGRATION_TEST",
        session=live_session,
    )
    # Node.send() only gates on this flag for REST calls; the websocket
    # handshake Node.connect() normally does isn't needed for loadtracks.
    n._available = True
    n._logger = __import__("logging").getLogger("test.integration")
    return n


async def test_companion_resolves_a_real_playable_url(live_session):
    url = await direct_resolver.resolve_youtube_direct_url(live_session, STABLE_VIDEO_ID)

    assert url is not None
    assert url.startswith("https://")
    assert "googlevideo.com" in url


async def test_companion_returns_none_for_a_bogus_video_id(live_session):
    url = await direct_resolver.resolve_youtube_direct_url(live_session, "not-a-real-video-id")

    assert url is None


async def test_full_pipeline_resolves_a_playable_lavalink_track(live_node):
    encoded = await live_node.resolve_direct_track(STABLE_VIDEO_ID)

    assert encoded is not None
    assert isinstance(encoded, str)
    assert len(encoded) > 0


async def test_second_call_for_same_video_is_served_from_cache_not_network(live_node):
    """Patch 11 (2026-10-18): the very first on-demand resolve now downloads
    and waits via yt-dlp before ever trying invidious-companion (an
    explicit, requested tradeoff - see pool.py's resolve_direct_track
    docstring), so it no longer makes sense to assert the companion
    resolver was called exactly once on the first call. What still matters,
    and is still asserted here: a *second* call for the same video must be
    an instant cache hit with no repeat network/download work of any kind.
    """
    first = await live_node.resolve_direct_track(STABLE_VIDEO_ID)

    started = time.monotonic()
    second = await live_node.resolve_direct_track(STABLE_VIDEO_ID)
    elapsed = time.monotonic() - started

    assert second == first
    assert elapsed < 0.05  # a pure dict lookup (_direct_resolve_cache), not another resolve/download


async def test_prefetch_warms_the_cache_before_play_needs_it(live_node):
    await live_node.prefetch_direct_track(STABLE_VIDEO_ID)

    assert STABLE_VIDEO_ID in live_node._direct_resolve_cache

    spy = AsyncMock(wraps=direct_resolver.resolve_youtube_direct_url)
    with patch.object(direct_resolver, "resolve_youtube_direct_url", spy):
        result = await live_node.resolve_direct_track(STABLE_VIDEO_ID)

    assert result == live_node._direct_resolve_cache[STABLE_VIDEO_ID][1]
    spy.assert_not_called()


async def test_resolved_track_is_actually_served_by_the_local_cache_proxy(live_node, live_session):
    """Confirms the thing Lavalink itself relies on: once prefetch_direct_track()
    succeeds (a real yt-dlp download to disk), the local proxy url it handed
    to Lavalink is independently fetchable and streams real audio bytes - not
    just that Lavalink *accepted* the url at loadtracks time
    (test_full_pipeline_resolves_a_playable_lavalink_track above already
    covers that).

    plain resolve_direct_track() (what play()'s on-demand/cache-miss path
    uses) never touches stream_cache at all - it always goes straight to
    invidious-companion's raw CDN url - so prefetch_direct_track() is the only
    call shape that actually exercises yt-dlp/stream_cache against live
    services.
    """
    from voicelink import stream_cache

    encoded = await live_node.prefetch_direct_track(STABLE_VIDEO_ID)
    assert encoded is not None

    local_url = f"{stream_cache.PUBLIC_BASE_URL}/stream/{STABLE_VIDEO_ID}"
    async with live_session.get(local_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
        assert resp.status in (200, 206)
        chunk = await resp.content.read(65536)

    assert len(chunk) > 0
