# Testing

This project has no CI and no test suite from upstream. The suite under
`tests/` covers three things: the
[direct URL resolver](direct-url-resolver.md) feature (`voicelink/direct_resolver.py`,
plus the additions to `voicelink/pool.py`'s `Node`, `voicelink/player.py`'s
`Player`, `voicelink/events.py`'s `TrackEndEvent`, and `cogs/listeners.py`);
Patch 9's per-guild play history (`Player._record_guild_play_history`,
`voicelink/ipc/methods.py`'s `getGuildHistory`/`getGuildTopTracks`); and
Patch 10's per-track loudness normalization (`stream_cache.py`'s
`_measure_gain`, `Player._filters_payload_with_gain`/`play()`'s gain
wiring) - see `../../docs/local-patches.md` for both. It's a starting
point, not project-wide coverage — most of the bot (commands, queue
mechanics, most of IPC, dashboard sync, etc.) has no tests yet.

## Layout

```
tests/
├── conftest.py                              shared fixtures (fake Track, real-but-mocked Node)
├── unit/
│   ├── test_direct_resolver.py              resolve_youtube_direct_url() in isolation
│   ├── test_node_direct_resolve.py          Node cache/lock/prefetch/prune, on-demand-never-touches-
│   │                                             stream_cache, invalidate_direct_resolve_cache
│   ├── test_player_direct_resolve.py        Player.play() + prefetch scheduling, _retry_direct_resolve,
│   │                                             TrackStartEvent retry-reset, TrackEndEvent position capture
│   ├── test_stream_cache.py                 StreamCache: yt-dlp subprocess hand-off/dedup/timeout,
│   │                                             no-TTL reuse/view_count, popularity+queued-elsewhere eviction,
│   │                                             HTTP handler, prefetch_to_disk()/get_local_url()
│   ├── test_nodepool.py                     NodePool.is_video_queued() - the queued-elsewhere check
│   │                                             stream_cache's eviction consults before deleting anything
│   ├── test_listeners_direct_resolve.py     cogs/listeners.py's loadFailed retry-vs-advance branching
│   ├── test_player_guild_history.py         Patch 9: Player._record_guild_play_history (bot vs. upload split)
│   ├── test_ipc_guild_history_methods.py    Patch 9: _resolve_guild_for_user, _top_tracks_from_counts,
│   │                                             getGuildHistory, getGuildTopTracks
│   └── test_player_loudness_gain.py         Patch 10: _filters_payload_with_gain, play()'s gain
│                                                 computation/reset, add/remove/reset_filter preserving gain
└── integration/
    └── test_direct_resolve_live.py          hits the real invidious-companion + Lavalink containers,
                                              and the local stream-cache proxy's HTTP endpoint
```

`test_stream_cache.py` also covers Patch 10's `_measure_gain` (ffmpeg loudnorm
parsing/clamping/failure modes) and the `gain` field's sidecar persistence/
`_adopt_existing_files` recovery, alongside its original yt-dlp-download
coverage; `test_node_direct_resolve.py` also covers `Node.get_track_gain`.

416 unit tests, 41 integration tests (457 total), plus the dashboard suites below.

## Why the tests are structured this way

- **Player is built via `Player.__new__(Player)`**, not `Player(...)`. The real
  constructor pulls in `NodePool.get_node()`, the IPC client, and other
  bot-boot state that isn't relevant to testing `play()`'s resolve/fallback
  branch or `_prefetch_next_direct_track()`'s scheduling logic. `__new__` skips
  `__init__` entirely; each test then sets only the attributes the method
  under test actually reads.
- **Track objects are `types.SimpleNamespace`, not real `voicelink.objects.Track`.**
  `Track.__init__` requires a loaded `Config()` singleton (for
  `search_platform`/source-emoji lookups) that these tests have no reason to
  stand up. The fakes carry exactly the attributes `play()` and the prefetch
  method touch (`source`, `identifier`, `track_id`, `title`, `uri`, `length`,
  `position`, `end_time`, `requester`).
- **`Node` is constructed for real** (`Node(pool=..., bot=..., host=..., ...)`)
  with only `bot`/`session`/`logger` mocked. `Node.__init__` does no I/O
  (no websocket connect, no HTTP calls) — it's just attribute assignment — so
  there's no reason to fake the class itself; only its network-facing
  collaborators (`Node.send`, `direct_resolver.resolve_youtube_direct_url`)
  are mocked per test.

## Running the unit tests

No real services required. Uses the actual project dependencies (discord.py,
aiohttp, etc.) from the built `vocard-patched` image rather than installing a
separate local Python environment:

```bash
docker run --rm -v /root/vocard/vocard-src:/app -w /app vocard-patched:latest \
  bash -c "pip install -q -r requirements-dev.txt && pytest tests/unit -v"
```

## Running the integration tests

These hit the **real, currently-deployed** `invidious-companion` and
`lavalink` containers over the actual `vocard` Docker network — not stubs.
They need the same environment variables the `vocard` container itself runs
with, and network access to those two service hostnames.

Since `prefetch_direct_track()` hands Lavalink a URL on `voicelink/stream_cache.py`'s
local proxy (serving a file it just downloaded via yt-dlp) instead of the raw
CDN url, **Lavalink also needs to be able to reach this test run back** - so
the container needs a predictable name/alias on the network, and
`STREAM_CACHE_BASE_URL` needs to point at it. This also means the test run
needs real outbound network access to YouTube itself (not just to
invidious-companion/Lavalink) for yt-dlp's own extraction to work.

Load the secret from the project's `.env` first: `set -a; . ../.env; set +a`.

```bash
docker run --rm --network vocard --name vocard-test --network-alias vocard-test \
  -v /root/vocard/vocard-src:/app -w /app \
  -e INVIDIOUS_COMPANION_URL=http://invidious-companion:8282 \
  -e INVIDIOUS_COMPANION_SECRET=$INVIDIOUS_COMPANION_SECRET \
  -e STREAM_CACHE_BASE_URL=http://vocard-test:2334 \
  vocard-patched:latest \
  bash -c "pip install -q -r requirements-dev.txt && pytest tests/integration -v -m integration"
```

(If a previous run didn't get cleaned up and `--name vocard-test` fails with
"name already in use", `docker rm -f vocard-test` first.)

The suite skips itself (not fails) if `INVIDIOUS_COMPANION_SECRET` is unset or
if Lavalink/invidious-companion aren't reachable — safe to leave in a repo
that might get checked out somewhere without that network. A `STREAM_CACHE_BASE_URL`
mismatch doesn't skip, though - it fails `test_full_pipeline_resolves_a_playable_lavalink_track`
and `test_resolved_track_is_actually_served_by_the_local_cache_proxy` (Lavalink
can't reach back to fetch the proxied url), which is the point: that's exactly
the failure mode a real misconfigured deploy would hit.

Tests use a stable, always-available video
(`dQw4w9WgXcQ` — Rick Astley, "Never Gonna Give You Up") specifically so the
suite doesn't flake because of some other video going region-locked or getting
pulled.

## Running everything

From the project root, with the stack up:

```bash
./run_tests.sh               # every suite, then a PASS/FAIL summary (non-zero exit on any failure)
./run_tests.sh --quick       # unit tests + the scripted dashboard sync test only
./run_tests.sh --no-youtube  # skip the integration tests that download from YouTube
```

It loads `.env` itself (companion secret, MongoDB login) and runs:

| Suite | What it uses | Guards against |
|---|---|---|
| unit (`tests/unit`) | fakes only | logic regressions, fast |
| integration (`tests/integration`) | real ffmpeg, MongoDB (throwaway DB), Lavalink, invidious-companion, a real websocket server | the seams between components - see below |
| `dashboard/tests/run_sync_test.sh` | real `Player`/`Queue` code + two jsdom dashboards running the real dashboard JS | dashboards drifting out of sync with the bot; the page-reload rules |
| `dashboard/tests/fuzz_sync.sh [seeds] [steps]` | same, with random multi-user sessions | the same, for sequences nobody thought to script |
| `dashboard/tests/run_sync_test.sh` also runs `test_dashboard_user.py` | the dashboard server override, in the stock dashboard image | several tabs/devices per user (Patch 43) |
| `dashboard/tests/browser_check.sh` | real Chromium, JS minified with the dashboard's own jsmin; a fake websocket plays the bot | a page broken only after minification or only in a real browser; the queue panel's History and Shuffle buttons (Patches 38-41) |

The integration suites (Patch 34):

- `test_stream_cache_live.py`: ffmpeg-generated audio through the real
  cache HTTP server (including the Range requests Lavalink seeks with) and
  the real loudness measurement. Catches ffmpeg output-format changes.
- `test_ipc_client_live.py`: the real dashboard connection against an
  in-process websocket server. Covers rejected handshakes, retries,
  reconnecting after a drop, and a deliberate disconnect staying
  disconnected.
- `test_mongodb_live.py`: settings, volume, history caps and per-track
  counters against the real MongoDB, in a `vocard_test_<random>` database
  dropped after each test. Needs `VOCARD_TEST_MONGO_URI`.
- `test_session_restore_live.py`: restart persistence end to end, save to
  restore to `do_next()`, with real `Player`/`Queue`.
- `test_direct_resolve_live.py`: the original YouTube/companion/Lavalink
  pipeline (needs internet).

When you fix a bug, add the test that would have caught it to the suite
closest to where it lived. For the dashboard, `fuzz_sync.sh 100 80` is a good
long run before shipping queue or sync changes.

## What's covered

| Behavior | Unit | Integration |
|---|---|---|
| Companion request shape (headers, body, URL) | ✅ | — |
| Itag preference / bitrate fallback selection | ✅ | — |
| Companion failure modes (403, not-playable, no audio formats, timeout) | ✅ | — |
| `Node` cache hit/miss/expiry | ✅ | — |
| Cache not populated on any failure | ✅ | — |
| Concurrent-resolve dedup (asyncio.Lock) | ✅ | — |
| Cache TTL pruning | ✅ | — |
| `play()` sends resolved URL but keeps native `track_id` | ✅ | — |
| `play()` falls back to native track on any resolver failure | ✅ | — |
| `play()` skips resolver for non-YouTube / no-identifier tracks | ✅ | — |
| `_prefetch_next_direct_track` (re)starts the sequential worker only when none is already running | ✅ | — |
| `_prefetch_worker` walks the whole upcoming queue in order, skipping non-YouTube/no-identifier tracks | ✅ | — |
| `_prefetch_worker` skips a track `Node.is_direct_resolve_cached` already has fresh (regression: a fresh worker pass used to re-notify the whole queue on every `add_track()`) | ✅ | — |
| `_prefetch_worker` picks up a track added mid-pass instead of looping over a stale snapshot (regression: rapidly adding 3 songs only prefetched the first) | ✅ | — |
| `_prefetch_worker` attempts a failing video exactly once per pass instead of retrying it forever | ✅ | — |
| `_prefetch_worker` never runs more than one prefetch concurrently, however many tracks are queued | ✅ | — |
| `is_direct_resolve_cached`: fresh entry, unknown video, expired entry | ✅ | — |
| `_prefetch_and_notify` sends `prefetchStatus: loading` then `ready`/`unavailable` over IPC | ✅ | — |
| `_prefetch_and_notify` sends nothing (and never polls progress) when IPC is disconnected | ✅ | — |
| `_prefetch_and_notify` polls and reports increasing progress fractions while the download is in flight | ✅ | — |
| `_prefetch_and_notify` sends no progress update when the download's size is still unknown | ✅ | — |
| `get_prefetch_progress` delegates to `stream_cache.get_progress`, swallowing any error | ✅ | — |
| `get_direct_resolve_cache_stats` reports entry count / approx bytes, reflects writes and pruning | ✅ | — |
| `play()`/`_prefetch_and_notify` push `cacheStats` over IPC when connected, not when disconnected or for non-YouTube tracks | ✅ | — |
| `play()` also fires a background `prefetch_direct_track` disk-cache download for the *current* track (not just upcoming ones - a track with no prior prefetch, e.g. the first song of a session, would otherwise never get disk-cached), fire-and-forget (never awaited, never blocks `play()`), skipped for non-YouTube tracks | ✅ | — |
| `resolve_direct_track` (on-demand path) checks `stream_cache.get_local_url` first and serves a disk-cache hit with no companion call, even with no fresh `_direct_resolve_cache` entry; a fresher `_direct_resolve_cache` entry still wins without consulting the disk cache; a `stream_cache` error falls through safely to the companion resolver | ✅ | — |
| `prefetch_direct_track` downloads via `stream_cache.prefetch_to_disk`, then loads the local url into Lavalink and caches it | ✅ | — |
| `prefetch_direct_track` returns `None` without caching when the yt-dlp download fails, or swallows an outright exception | ✅ | — |
| `prefetch_direct_track`/`resolve_direct_track` both skip all work on an existing cache hit | ✅ | — |
| `StreamCache.download`: yt-dlp subprocess invocation (format/output path), nonzero-exit and missing-file failure, timeout + process kill, concurrent-call dedup, fresh-entry reuse | ✅ | — |
| `StreamCache.download`: successful entries are reused indefinitely (no TTL), bumping `view_count` on each reuse; a failed entry blocks retries for `_FAILED_RETRY_COOLDOWN` then retries fresh | ✅ | — |
| `StreamCache.download` parses live progress from yt-dlp's stdout, ignoring non-progress lines and tolerating an unparseable/`NA` one | ✅ | — |
| `StreamCache.progress`: unknown key, already-ready entry, unknown total, correct/clamped fraction | ✅ | — |
| `get_progress` reflects live state and returns `None` for an unknown key | ✅ | — |
| Disk-cache eviction: leaves everything alone under budget, never evicts in-progress entries, evicts least-viewed first when over budget, never evicts an entry that's queued elsewhere (falls through to the next one, or stays over budget if all are), eviction deletes the backing file | ✅ | — |
| `StreamCache.list_entries`: empty cache, status (ready/downloading/failed), size/view count, title, live progress for a downloading entry, reflects the queued-elsewhere check | ✅ | — |
| `StreamCache.download` stores a cosmetic `title` on first download, backfills it on reuse if missing, never overwrites an existing one | ✅ | — |
| `StreamCache.delete_entry`: removes a ready or failed entry and its file, ignores the queued-elsewhere check (unlike `_prune`), refuses an unknown or still-downloading key | ✅ | — |
| `StreamCache.stats`: empty cache, sums only ready entries against `_MAX_TOTAL_BYTES` | ✅ | — |
| `list_entries`/`delete_entry`/`get_stats` module-level wrappers round-trip correctly | ✅ | — |
| `StreamCache._adopt_existing_files`: rebuilds entries from pre-existing `.audio` files on `start()` (so a restart doesn't make an already-cached file invisible), ignores non-`.audio` files, never clobbers an already-known entry, prunes immediately if the adopted total is already over budget; `_get_cache()` adopts end-to-end | ✅ | — |
| Title sidecar (`{key}.meta.json`): written on a successful download and on a title-backfilled reuse, never written when there's no title to save, recovered by `_adopt_existing_files` on restart (tolerates a missing/corrupt sidecar), removed by `delete_entry` alongside the audio file | ✅ | — |
| `NodePool.is_video_queued`: no nodes, no match, current track, upcoming track in a different guild, non-YouTube track with the same id, multiple nodes | ✅ | — |
| HTTP handler: full file, byte-range (206), HEAD, 404 for unknown/in-progress/failed entries | ✅ | — |
| `prefetch_to_disk`/`get_local_url` start the proxy server exactly once (singleton), and `get_local_url` returns `None` until a download is actually ready | ✅ | — |
| `invalidate_direct_resolve_cache` evicts an entry and forces a fresh companion resolve next call | ✅ | — |
| `loadFailed` on an eligible track retries via `Player._retry_direct_resolve` instead of `do_next()`, within a per-video retry budget that resets on a successful restart | ✅ | — |
| Non-`loadFailed` reasons and non-eligible tracks (not YouTube, no identifier, budget exhausted) fall through to the normal `do_next()` advance | ✅ | — |
| `on_voicelink_track_exception`'s message wording and `_track_is_stuck` handling match whether a retry will follow | ✅ | — |
| `TrackEndEvent.position` is captured before `Player._current` is cleared (regression: `Player.position` alone would read 0 by listener-run time) | ✅ | — |
| `TrackStartEvent` resets a video's retry budget once it's playing again | ✅ | — |
| `add_track()` triggers the next-track prefetch check when a track is already playing (single add and batch add) | ✅ | — |
| `add_track()` does not trigger a prefetch check when nothing is playing | ✅ | — |
| **Patch 9:** `Player._record_guild_play_history` splits a played track into `botHistory`/`uploadHistory` + running play counts by `track.source` (http/local = upload, everything else = bot), falls back to `uri`/`track_id` when there's no `identifier`, never raises on a Mongo write failure | ✅ | — |
| **Patch 9:** `_resolve_guild_for_user` resolves the preferred `guildId`, falls back across the user's mutual guilds, skips a guild the user isn't a member of, returns `None` when nothing resolves | ✅ | — |
| **Patch 9:** `_top_tracks_from_counts` ranks by descending play count, truncates to the limit, skips an entry with no `trackId`, handles an empty/missing map | ✅ | — |
| **Patch 9:** `getGuildHistory`/`getGuildTopTracks` return both buckets for a resolved guild, empty lists (without querying Mongo) when no guild resolves, default missing fields to empty lists | ✅ | — |
| **Patch 10:** `_measure_gain` computes a boost for a quiet track and a cut for a loud one, clamps a measurement beyond ±8dB, returns `None` when ffmpeg is missing/times out/produces unparseable output or no `input_i` | ✅ | — |
| **Patch 10:** a successful download measures and persists `gain` to the `{key}.meta.json` sidecar (merged with `title`, not overwriting it); a failed measurement pass leaves `gain` unmeasured without failing the download | ✅ | — |
| **Patch 10:** `_adopt_existing_files` recovers `gain` from its sidecar, defaults to `None` without one | ✅ | — |
| **Patch 10:** `Node.get_track_gain` delegates to `stream_cache.get_gain`, defaults to `1.0` when unmeasured or on any error | ✅ | — |
| **Patch 10:** `_filters_payload_with_gain` always includes `"volume"` (even when neutral) merged with active user Effects | ✅ | — |
| **Patch 10:** `play()` applies the measured gain for a youtube track, resets to neutral for a non-youtube track (regression: filters persist across tracks), combines gain with active Effects in one payload | ✅ | — |
| **Patch 10:** `add_filter`/`remove_filter`/`reset_filter` all preserve the current gain - resetting a user's own Effects doesn't silence normalization | ✅ | — |
| Real companion resolves a real playable googlevideo.com URL | — | ✅ |
| Real companion returns nothing for a bogus video id | — | ✅ |
| Full real pipeline (companion → Lavalink `loadtracks`) produces a track | — | ✅ |
| Second resolve for the same video is a cache hit against real services (network spy: 1 call, not 2) | — | ✅ |
| `prefetch_direct_track` against real services (real yt-dlp download) warms the cache | — | ✅ |
| The resolved local-proxy url is independently fetchable and streams real audio bytes | — | ✅ |

## What's *not* covered (and why)

- **An actual mid-stream `302` redirect from Lavalink/lavaplayer itself** -
  the retry *logic* (branching on `reason == "loadFailed"`, eligibility,
  budget, resume position) is fully unit-tested (see `test_listeners_direct_resolve.py`
  and the `_retry_direct_resolve`/`TrackEndEvent.position` tests in
  `test_player_direct_resolve.py`), but reproducing a *real* signed-url
  redirect/expiry from lavaplayer deterministically would need a real,
  sufficiently long-lived track and isn't practical to assert on in a test
  run. See "Recovering from mid-stream failures" in
  [direct-url-resolver.md](direct-url-resolver.md).
- **`Player._dispatch_event`/`do_next` end-to-end through a real Discord voice
  connection.** That would need a full bot + live voice gateway harness,
  disproportionate to what this feature touches. The prefetch trigger itself
  (`_prefetch_next_direct_track`) is unit-tested directly instead of via the
  event dispatch path that calls it. This also means `do_next()`'s new
  `trackLoading` IPC send (see `docs/direct-url-resolver.md` and
  `../../docs/local-patches.md`, Patch 5) has no dedicated unit test — it's one
  line in an otherwise untestably-entangled method — and was instead verified
  by rebuild + redeploy + log/DOM inspection.
- **The dashboard frontend itself** (the loading spinner / prefetch badge /
  cache manager modal JS/HTML in `dashboard/overrides/`) has no automated
  tests — this project has no JS test tooling. Verified manually post-deploy
  (see Patches 5 and 7's verification commands in `../../docs/local-patches.md`).
- **Most of `voicelink/ipc/methods.py`** (e.g. `getCacheEntries`/
  `deleteCacheEntry`, every `PlayerMethod`) still has no tests — it's
  near-zero-coverage project-wide (commands, the rest of `Player`, most IPC
  methods, etc.). Patch 9's `getGuildHistory`/`getGuildTopTracks`/
  `_resolve_guild_for_user`/`_top_tracks_from_counts` are the first
  exception (see `test_ipc_guild_history_methods.py`) - they're plain
  `SystemMethod`s taking simple `(bot, data)` args with `discord.py` objects
  stood in via lightweight `MagicMock`s, not the full `Bot`/`Guild`/`Member`
  mocking infrastructure a `PlayerMethod` would need. The
  `voicelink/stream_cache.py` methods `getCacheEntries`/`deleteCacheEntry`
  delegate to (`list_entries`/`delete_entry`/`get_stats`) are fully
  unit-tested directly instead. If you add a test for something else, put
  it under `tests/unit/` or `tests/integration/` following the same pattern
  and it'll be picked up automatically (no registration needed beyond the
  file living under `tests/`).

## Adding a dev dependency

`requirements-dev.txt` layers on top of `requirements.txt` (`-r requirements.txt`
at the top) so it always matches production dependency versions. Add new test
tooling there, not to `requirements.txt` — the production Docker image should
never carry `pytest`.
