# Direct URL Resolver (invidious-companion integration)

Local addition on top of stock Voicelink/Vocard. Not part of upstream — if you
ever rebuild this bot from a fresh clone of ChocoMeow/Vocard, this feature
(and its tests) needs to be reapplied. See [`../../docs/local-patches.md`](../../docs/local-patches.md)
for the full list of local deviations.

## Why this exists

Lavalink's youtube-plugin resolves and streams audio itself, but YouTube's bot
detection intermittently blocks Lavalink's fetches. To route around that, this
bot resolves a direct, signed `googlevideo.com` audio URL via a self-hosted
[invidious-companion](https://github.com/iv-org/invidious) instance (which
does its own YouTube handshake) and hands that URL to Lavalink as a plain
HTTP-source track instead of asking Lavalink's youtube-plugin to resolve the
video itself.

## How it fits into playback

```
Player.play(track)
  └─ track.source == "youtube"?
       └─ Node.resolve_direct_track(video_id)
            ├─ cache hit  → return cached encoded track            (fast path)
            └─ cache miss → POST invidious-companion  (get signed URL)
                          → GET Lavalink /v4/loadtracks?identifier=<url>
                          → cache the resulting encoded track
       └─ resolved?  → send encodedTrack = resolved blob to Lavalink
       └─ not resolved / any exception → send encodedTrack = track.track_id
                                          (native youtube-plugin track, unchanged)
```

Key property: **`track.track_id` (the native youtube-encoded blob) is never
replaced.** Only the outgoing `PATCH .../players/<id>` command uses the
resolved URL's encoded track. Everything else — dashboard sync, MongoDB
history, "add to favourites" — keeps reading `track.track_id`/`track.title`/
`track.thumbnail` from the original native track, so metadata and artwork
never degrade to whatever Lavalink probes off a raw HTTP stream.

Failure at any point (companion unreachable, companion says not playable,
Lavalink rejects the resolved URL) is caught and logged as a warning; playback
falls back to the native encoded track transparently. This feature can never
be the reason a track fails to play — at worst it's a no-op.

## Files

| File | Role |
|---|---|
| `voicelink/direct_resolver.py` | `resolve_youtube_direct_url()` — the invidious-companion HTTP call and audio-format selection (prefers itags `251, 250, 249, 140`, falls back to highest bitrate) |
| `voicelink/stream_cache.py` | `StreamCache`/`prefetch_to_disk()`/`get_local_url()` — the yt-dlp-backed, disk-based local HTTP proxy Lavalink actually streams from; see "yt-dlp prefetch to disk" below. `list_entries()`/`delete_entry()`/`get_stats()` back the dashboard's cache manager, see `local-patches.md`'s Patch 7 |
| `voicelink/ipc/methods.py` | `getCacheEntries`/`deleteCacheEntry` — the dashboard cache manager's IPC methods, see `local-patches.md`'s Patch 7 |
| `voicelink/pool.py` (`Node` class) | `resolve_direct_track()`, `prefetch_direct_track()`, `get_prefetch_progress()`, `_prune_direct_resolve_cache()`, `invalidate_direct_resolve_cache()` — the Lavalink `loadtracks` call, caching, locking, cache eviction, and forced-refresh-after-failure |
| `voicelink/pool.py` (`NodePool` class) | `is_video_queued()` — checks every connected player's queue across every guild; registered with `stream_cache` (`set_queued_checker()`) so its disk-cache eviction never deletes a video still actually needed, see "Disk cache eviction" below |
| `voicelink/player.py` (`Player` class) | `play()`'s resolve-or-fallback branch; `_prefetch_next_direct_track()`/`_prefetch_worker()` — the sequential background prefetch; `_prefetch_and_notify()` — polls/reports progress to the dashboard; `_retry_direct_resolve()` — recovers from a mid-stream `loadFailed`, see "Recovering from mid-stream failures" below |
| `voicelink/events.py` (`TrackEndEvent`) | Captures `position` at event-construction time (before `Player._current` is cleared) so a retry can resume from roughly where playback died |
| `cogs/listeners.py` | `on_voicelink_track_end`/`on_voicelink_track_exception` — routes a `loadFailed` for an eligible track to `Player._retry_direct_resolve()` instead of `do_next()` |

## Prefetching and caching

Resolving a track costs two sequential network round-trips (companion, then
Lavalink). Doing that at `play()` time — i.e. at every track start/skip —
was the original cause of noticeable lag between songs.

Instead, `Player._prefetch_next_direct_track()` runs on every `TrackStartEvent`:
it (re)starts a single background worker (`Player._prefetch_worker()`) that
walks the *entire* upcoming queue front-to-back (`queue.tracks(incTrack=False)`,
without consuming it) and awaits `Node.prefetch_direct_track()` for each
YouTube track in order, one at a time, while the current track keeps playing.
By the time each one becomes current, `play()`'s call to
`resolve_direct_track()` is usually a pure cache read.

**Why the whole queue, but strictly sequential, not concurrent:**
invidious-companion and yt-dlp both egress to YouTube from the same host, so
firing off a download for every track in a long playlist *at once* would
look like a scraping burst from a single IP - a real risk of getting that IP
rate-limited/blocked, which would degrade invidious-companion too, not just
yt-dlp. A worker that always awaits one prefetch fully before starting the
next never has more than one download in flight, no matter how long the
queue is - that reads as ordinary one-track-at-a-time usage regardless of how
far ahead it gets, so there's no need to artificially cap how far it looks.
`_prefetch_next_direct_track()` is a no-op while a worker is already
running, trusting that worker to pick up anything newly added on its own -
which only holds because `_prefetch_worker`'s loop re-reads
`queue.tracks()` fresh on *every iteration*, not once at the start. Queue 3
songs in quick succession while the first is still downloading and the
other two add_track() calls both correctly no-op this way; if the worker
had looped over a one-time snapshot instead, it would finish after the
first song and exit without ever having seen the other two (confirmed in
production - see Patch 1 in `local-patches.md` for the full incident).
Re-reading each iteration needs an `attempted` per-pass guard alongside it:
`prefetch_direct_track()` doesn't cache a failed resolve, so without
tracking "already tried this pass," a video that just failed would look
identical to a brand-new track on the next iteration and loop forever
instead of the pass ever finishing.

Once a pass finishes, the *next* trigger (e.g. the next `add_track()`)
starts a fresh pass over the whole queue again - so `_prefetch_worker` also
explicitly skips anything `Node.is_direct_resolve_cached()` already has
fresh, via a plain cache+TTL check with no side effects. This isn't just to
avoid a redundant download (`prefetch_direct_track()`'s own
cache check already makes that a cheap no-op) - without it, every fresh pass
called `_prefetch_and_notify()` for the whole queue again, and that method
unconditionally sends a `loading` IPC status before checking anything, so
the dashboard flashed every already-ready track's indicator back to loading
on every single `add_track()` call - looking exactly like the entire queue
was being re-prefetched from scratch each time a user added one more song
(reported 2026-10-11, same session this worker shipped in).

It also runs from `Player.add_track()` whenever a track is added while
something is already playing (`self.is_playing`). This matters because a
`TrackStartEvent`-only trigger is a single snapshot in time: if the queue was
empty at that instant, there was nothing to prefetch, full stop - it never
gets re-checked later. In practice people queue songs one at a time *while
something is already playing* far more often than they queue a full batch up
front, so the queue is routinely still empty at `TrackStartEvent` and only
gets its next entry seconds or minutes into the current track. Without this
second trigger, that newly-added "next" track would never get prefetched at
all and every single track would fall back to the on-demand, un-cached,
full-throttle path - i.e. none of the rest of this document's mitigation
would actually engage for that server's real usage pattern. (Found 2026-10-08:
a user reported persistent lag after this feature had already shipped;
tracing one real session's Lavalink `loadtracks` payloads showed every track
being sent the raw, unproxied CDN url, and the queue logs showed each next
track being added 30+ seconds after the current one had already started.)
Calling the same prefetch check again here is safe/idempotent - it just
re-derives the current next-track, and `resolve_direct_track()`'s own
cache+lock dedups a redundant resolve if a prefetch for that video somehow
already ran.

Cache details (`Node._direct_resolve_cache`, keyed by video id):
- **TTL**: 30 minutes (`Node._DIRECT_RESOLVE_CACHE_TTL`), checked lazily on read.
- **Eviction**: every successful write calls `_prune_direct_resolve_cache()`,
  sweeping out anything older than the TTL — so the cache stays bounded to
  "recently played/queued videos," not "every video this process has ever
  played." No separate background task needed.
- **Dedup**: a per-video-id `asyncio.Lock` (`Node._direct_resolve_locks`)
  ensures a prefetch racing with an on-demand resolve for the same video only
  hits the network once; the second caller gets the first caller's result.

A cache miss still happens for: the very first track played after joining a
channel (nothing was playing yet for `add_track()`'s trigger to fire from
either), or a track jumped to directly (skip-to/play-now) that bypassed the
normal queue progression, or a track added and then played too close together
for the background prefetch to finish in time.

## CDN startup-throttle mitigation: yt-dlp prefetch to disk

Symptom observed 2026-09-27: every track stutters for the first ~15-20
seconds, then plays smoothly for the rest of its duration. This matches a
known YouTube CDN behavior — throttling the *initial* delivery rate of a
stream for non-browser client contexts (like the ones invidious-companion
uses to dodge bot detection) before ramping up to full speed after several
seconds. A larger Lavalink `bufferDurationMs` (see
[`../../docs/local-patches.md`](../../docs/local-patches.md)) only absorbs
brief gaps, not a sustained below-real-time download rate, so it didn't fully
resolve this.

Root-causing this cleanly by disabling the resolver to A/B test isn't
possible: without it, Lavalink's native youtube-plugin is fully blocked by
YouTube's bot detection and nothing plays at all — confirmed directly.

**First attempt — prewarming (superseded).** `Node.resolve_direct_track(...)`
used to fire a background read of the first few MiB of the resolved URL
during prefetch, then discard the bytes, hoping to "use up" the CDN's
ramp-up window on a disposable side connection before Lavalink ever touched
the URL for real. Empirically (by ear, not measured) a 1MiB warmup shortened
the stutter from ~15-20s down to a few seconds — evidence the throttle is at
least partly scoped to the URL/token rather than the TCP connection — but a
10MiB warmup didn't close the remaining gap, because Lavalink still had to
redownload the *entire* track on its own fresh connection after the prewarm
connection closed; prewarming could only ever narrow the window, not remove
it, and it couldn't help at all if the throttle turned out to be
connection-scoped instead.

**Second attempt — in-memory relay proxy (superseded).** `resolve_direct_track()`
started handing Lavalink a URL on a small local HTTP server
(`stream_cache.get_local_url()`) that background-downloaded the raw CDN URL
into an in-memory buffer during `prefetch_direct_track()`, and served
Lavalink from whatever was already buffered. This worked *when there was a
head start* (the previous track's duration gave the background download
real time to get ahead), but had two sharp edges. First (production
incident, 2026-10-04): the initial version routed *every* resolve through
it, including `play()`'s no-head-start on-demand path, where the relay ends
up "live" - waiting on the still-throttled upstream exactly when Lavalink's
own HTTP client is waiting on the relay - and Lavalink's short, fixed
per-read socket timeout killed the track outright
(`SocketTimeoutException`) where a direct CDN connection would have just
stuttered through the same stall. The fix was a `via_prefetch` flag so only
the background path used the relay. Second: the relay buffered everything
in memory, capped at 150MiB/entry and 300MiB total - long videos ran out
mid-track past that cap.

**Current approach — download the whole thing to disk first, with `yt-dlp`.**
`voicelink/stream_cache.py` now uses `yt-dlp` (not invidious-companion) to
fully download a track's audio to a local disk file *before* it's ever
handed to Lavalink - `Node.prefetch_direct_track()` awaits that download in
the background while the previous track plays. `resolve_direct_track()`
(play()'s on-demand path) never *starts* a yt-dlp download itself - no head
start, no upper bound on how long a fresh one could take - but it does
always ask "is there already a *finished* disk download for this video?"
(`stream_cache.get_local_url()`) first, regardless of whether that finished
download happened just now or hours ago. If yes, Lavalink gets pointed at
the local file - a pure local read, no network call, and no risk of
ever serving a half-downloaded file (`get_local_url()` only returns a url
for an entry that's already `is_ready()`). If no (never prefetched, still
downloading, or yt-dlp failed), it falls straight back to
invidious-companion's raw CDN url exactly as before any of this existed.
(Fixed 2026-10-11: this used to skip the disk-cache check entirely and
rely on the short-lived `_direct_resolve_cache` instead, so a video cached
hours ago would get re-resolved via invidious-companion the moment that
30-minute window lapsed - see `docs/local-patches.md`'s Patch 7, fifth
follow-up.)

This removes the sharp edge the relay proxy had: there's no more "live,
racing a still-downloading buffer" state for Lavalink to ever observe -
the proxy only ever serves a complete, static file (`aiohttp.web.FileResponse`),
so the SocketTimeoutException failure mode above cannot recur for a
prefetched track. The two resolvers are kept deliberately independent:
invidious-companion is a dedicated, actively-maintained bot-detection dodge
and remains the only path for anything needed *right now* (no head start to
risk on an alternate extractor); yt-dlp only ever runs in the background,
where a failure (blocked, timed out, video unavailable) just means no disk
entry gets populated and the on-demand path is used instead.

Measured directly against the production network (2026-10-11, same stable
test video used by the integration suite): a full-file download of the
resolved CDN url took anywhere from **1.8 seconds to 112 seconds** for the
same ~3.5-minute track across different attempts - the throttle isn't a
fixed rate, it's genuinely inconsistent run to run. This is why the on-demand
path is left completely untouched rather than also switched to a full
download: a full download has no guaranteed upper bound, so it's only safe
to do in the background, where "however long it takes" doesn't block
playback - and in practice it's frequently much faster than the
previous-track's-duration budget the old relay proxy needed to assume.

If a track isn't fully downloaded by the time it's needed, the result is
the pre-proxy stutter-but-don't-die behavior (same as the no-head-start
on-demand path always had), not a failure - `stream_cache` doesn't serve a
partial file, so there's nothing to race against here.

## Disk cache eviction

A finished download has no TTL - there's no correctness reason to ever
re-fetch a video once it's on disk (unlike the signed CDN url itself, raw
bytes don't expire), so it just stays in `voicelink/stream_cache.py`'s cache
indefinitely, saving a real (possibly slow/throttled) download the next
time that video gets queued again, for as long as there's room under
`STREAM_CACHE_MAX_BYTES`.

When there isn't room, `StreamCache._prune()` evicts in this order:

1. **Never evict a video that's currently playing or upcoming in any
   connected player's queue, anywhere.** `voicelink/pool.py`'s
   `NodePool.is_video_queued()` checks every node's every player's
   `queue.tracks(incTrack=True)`, and registers itself with `stream_cache`
   once at import time (`set_queued_checker()`) - `stream_cache.py` never
   imports `pool.py` directly, keeping the dependency one-directional and
   the module trivially testable on its own.
2. **Among what's left, evict the least-viewed entry first** - `view_count`
   on each cache entry, bumped once when it's first downloaded and again
   every time `download()` is called for a video that's already cached. A
   video queued often survives longer than one only ever played once,
   regardless of which happened to be downloaded first (plain oldest-first
   would get this backwards).

If everything still over budget is actively queued somewhere, `_prune()`
just leaves it over budget rather than evict something actually needed -
`STREAM_CACHE_MAX_BYTES` is a soft cap in that sense. In practice this needs
the sum of every *distinct* video currently queued across every guild to
exceed the budget on its own, which is rare: a typical opus track is
~1MB/minute, so 5GB holds hundreds of average tracks.

A *failed* download is a separate case - `prefetch_direct_track()` doesn't
cache failures as something worth keeping around, but a persistently broken
video (blocked, region-locked, whatever) also shouldn't get hammered on
every single prefetch attempt. `_FAILED_RETRY_COOLDOWN` (30 min) blocks an
immediate retry for the same video; once it elapses, the next prefetch
attempt tries again from scratch - or an admin can manually delete the
failed entry from the dashboard's cache manager (see `local-patches.md`'s
Patch 7) to force an immediate retry instead of waiting it out.

Beyond automatic eviction, the dashboard's cache manager (Patch 7, DJ-or-admin
gated - same privilege level as any other playback control) lets a DJ or
admin see every entry currently in the cache - size, view count, status,
whether it's queued anywhere - and delete one manually. Unlike `_prune()`,
a manual delete does *not* check whether the video is currently queued
first: it's an explicit action, so the user's own call wins, with the same
zero-risk-to-playback fallback as everywhere else in this feature if they
do delete something still in use.

## Dashboard visibility

Both the loading gap and the background prefetch are surfaced in the web
dashboard — `do_next()` sends a `trackLoading` IPC message right before
calling `play()` (shown as a small spinner), and `Player._prefetch_and_notify()`
sends `prefetchStatus: loading`/`ready`/`unavailable` around each background
prefetch. While `loading`, it also polls and forwards live download progress
(`stream_cache`'s yt-dlp `--progress-template` output, via
`Node.get_prefetch_progress()`) roughly once a second, so the queue list can
show a real fill bar under a track's title instead of just a spinner; `ready`
renders as a small checkmark once the track is fully cached. See
[`../../docs/local-patches.md`](../../docs/local-patches.md), Patch 5, for
exactly how that's wired into the dashboard frontend.

## Configuration

Set via environment variables on the `vocard` container (see
`docker-compose.yml`):

| Variable | Purpose |
|---|---|
| `INVIDIOUS_COMPANION_URL` | Base URL of the invidious-companion service (default `http://invidious-companion:8282`) |
| `INVIDIOUS_COMPANION_SECRET` | Shared secret for the companion's `Authorization: Bearer` header. If unset, `resolve_youtube_direct_url()` returns `None` immediately and every track falls back to native youtube-plugin resolution — the feature degrades to "off," it does not error. |
| `STREAM_CACHE_PORT` | Port the local proxy (`voicelink/stream_cache.py`) binds to inside the `vocard` container (default `2334`). |
| `STREAM_CACHE_BASE_URL` | URL Lavalink should use to reach the proxy (default `http://vocard:2334`, i.e. the `vocard` container's name on the `vocard` docker-compose network). Only needs overriding when the bot isn't reachable at that hostname — e.g. the integration test suite, which runs in its own ad-hoc container; see `testing.md`. |
| `STREAM_CACHE_DIR` | Where yt-dlp downloads land inside the `vocard` container (default `/tmp/vocard_stream_cache`, but set to `/app/stream_cache` in `docker-compose.yml`, bind-mounted to `./stream_cache` on the host). Must be a bind-mounted volume now that finished downloads have no TTL and are meant to survive indefinitely (see `STREAM_CACHE_MAX_BYTES` below) - otherwise every container recreate (any image rebuild + `docker compose up`, not just a host reboot) silently wipes the entire cache, which is exactly what happened in production before this was caught (2026-10-11: "why is nothing cached" turned out to be every redeploy nuking `/tmp`). Only safe to leave unmounted if the cache is deliberately meant to be scratch space again. |
| `STREAM_CACHE_MAX_BYTES` | Total disk budget across all cached downloads (default `5000000000`, 5GB). Finished downloads have no TTL and are kept indefinitely under this budget; over it, the least-viewed one not currently queued anywhere is evicted first (see "Disk cache eviction" below). An in-progress download is never evicted. |

## Recovering from mid-stream failures

Signed `googlevideo.com` URLs carry an `expire=` timestamp and are otherwise
rate/format-locked. Observed in production (2026-10-04, on a ~42-minute
track, ~23 seconds in — not only on very long videos as first thought):
Lavalink can hit a `302`/"Not success status code" partway through playback
if the signature or CDN edge changes mid-stream. This can only happen on the
on-demand path (invidious-companion's raw CDN url) - a track served from a
finished yt-dlp disk download needs no further signed-url fetches once
playback starts, so this failure mode is structurally impossible for a
successfully-prefetched track. Lavaplayer treats this as a
hard track error (`TrackExceptionEvent` + `TrackEndEvent` with
`reason: "loadFailed"`) rather than transparently re-resolving, and
previously the bot just treated that like any other track end and moved on
to whatever was next in queue — discarding the rest of the track, or just
going idle if nothing else was queued.

**Fix:** `cogs/listeners.py`'s `on_voicelink_track_end` now checks for
`reason == "loadFailed"` on an eligible track (YouTube source, has an
`identifier`, hasn't exhausted `Player.MAX_DIRECT_RESOLVE_RETRIES` — 2 — for
this video yet) and calls `Player._retry_direct_resolve()` instead of
`do_next()`. That evicts the stale cached url (`Node.invalidate_direct_resolve_cache`,
so the next resolve is forced to ask invidious-companion for a *fresh* signed
url instead of handing Lavalink the exact url that just failed) and resumes
`play()` from roughly where it died. "Roughly" because the resume position
comes from Lavalink's last periodic player-state push
(`playerUpdateInterval`, 5s by default), captured at the moment
`TrackEndEvent` is constructed — see `events.py`'s `TrackEndEvent.position`
and the comment there about why it can't just read `Player.position` at
listener-run time (by then `Player._current` has already been cleared, and
that property depends on it).

The retry budget is per-video and resets on a successful `TrackStartEvent` for
that video, so a video that fails twice in a row falls through to the normal
`do_next()` advance (cheap insurance against a persistently broken url - e.g.
invidious-companion itself down - looping instead of just moving on), but a
video that only fails occasionally gets a fresh two-retry budget each time it
plays. The exception handler (`on_voicelink_track_exception`) mirrors the
same eligibility check purely to pick its user-facing message ("Reconnecting..."
vs. "The next song will begin...") — the actual retry-vs-advance decision
lives solely in `on_voicelink_track_end`, so there's no race between the two
handlers over who decides what happens.

This doesn't make the underlying signed-url expiry go away — it's not rare
enough to ignore, but it IS rare enough (and self-healing enough with this
fix) that spending more effort eliminating it outright isn't worth it right
now.

## Known limitations

- A single download can take anywhere from under 2 seconds to nearly 2
  minutes for the same video (see the measurement above) - there's no hard
  guarantee it finishes before the track it's for actually starts playing.
  When it doesn't, the track just falls back to the on-demand path (stutter,
  not failure) - see "yt-dlp prefetch to disk" above.
- yt-dlp does its own independent YouTube extraction (not through
  invidious-companion), and it's a cat-and-mouse tool that can update-lag a
  YouTube countermeasure change. Because it's only ever used in the
  background with a zero-risk fallback, a yt-dlp break degrades this
  specific optimization back to "every track takes the on-demand path," not
  a playback failure.
- No JS runtime (`deno`/`node`) is installed in the image. yt-dlp logs a
  warning that some formats may be unavailable without one for certain
  videos' signature deciphering; not currently an issue for the preferred
  itags (`251, 250, 249, 140`), but if yt-dlp prefetches start failing more
  often than expected, this is the first thing to check.
- Disk usage is capped at `STREAM_CACHE_MAX_BYTES` (default 5GB) but
  finished downloads have no TTL - see "Disk cache eviction" above for the
  full eviction order (never-queued-elsewhere, then least-viewed first) and
  why 5GB comfortably covers even a sequential whole-queue prefetch.
- `Node._direct_resolve_cache` (the small *metadata* cache - resolved
  URL/encoded-track strings, not audio) still has its own independent
  30-minute TTL, unrelated to the disk cache above. Its expiry just means
  the next `play()`/prefetch for that video re-asks Lavalink to build an
  encoded track for the (still on-disk, still valid) local url - a cheap,
  local operation, not a re-download.

## Verifying it's active

```bash
docker logs vocard --since 24h | grep "DirectResolver\|StreamCache"
```

No `[DirectResolver]` warnings and normal playback means it's resolving
successfully and silently (success isn't logged at INFO level, only failures
are — see `voicelink/direct_resolver.py` and `Node.resolve_direct_track`). A
`[StreamCache] listening on 0.0.0.0:2334, cache dir ...` line at startup
(logged once, the first time any track is prefetched) confirms the local
proxy came up.
