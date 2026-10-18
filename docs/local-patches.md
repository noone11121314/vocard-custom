# Local Patches

This deployment runs modified code on top of two upstream projects:
[ChocoMeow/Vocard](https://github.com/ChocoMeow/Vocard) (the bot,
`vocard-src/`) and [ChocoMeow/Vocard-Dashboard](https://github.com/ChocoMeow/Vocard-Dashboard)
(`dashboard-src/`, though the running dashboard container currently uses the
stock upstream image plus file overrides — see Patches 4 and 5).

**Why this file exists:** none of this is upstream. If the bot image is ever
rebuilt from a fresh `git clone` of upstream Vocard, or the dashboard's
`docker-compose.yml` image line is ever pointed back at plain
`ghcr.io/chocomeow/vocard-dashboard:latest` without the override volume,
these patches silently disappear with no error — the bot/dashboard just goes
back to stock behavior. Check this list before doing either.

---

## Patch 1 — Direct URL resolution via invidious-companion (+ prefetch/cache/local proxy)

**What:** YouTube tracks are resolved to a direct, signed `googlevideo.com`
URL through a self-hosted invidious-companion instance before being handed to
Lavalink, bypassing Lavalink's own youtube-plugin (which is subject to
YouTube bot detection). The next queue track is pre-resolved in the
background while the current one plays, and results are cached (30 min TTL,
self-pruning) so this doesn't add latency to every track start. On top of
that, *prefetched* resolves (the ones with an actual head start) are handed a
URL on a small local HTTP caching proxy (`voicelink/stream_cache.py`) instead
of the raw CDN URL, which background-downloads the real CDN URL starting at
the prefetch point and buffers it in memory so Lavalink reads from local
memory instead of YouTube's CDN directly. This mitigates an observed CDN
initial-throttle stutter (see `direct-url-resolver.md` for the history of
attempts, including a superseded prewarm-and-discard approach, **and a
production incident where routing the no-head-start/on-demand resolve
through the same proxy killed playback outright via a Lavalink-side socket
timeout** — fixed by gating the proxy to the prefetch path only via a
`via_prefetch` flag on `Node.resolve_direct_track`). Separately, a signed
CDN url can still get redirected/invalidated mid-stream (`reason: "loadFailed"`,
not only on very long tracks as first assumed) — `cogs/listeners.py` now
retries that specific failure (fresh signed url + resume position) up to
`Player.MAX_DIRECT_RESOLVE_RETRIES` times per video instead of just
discarding the track and moving on. **A second production report ("still
laggy" after the above had already shipped) traced to the prefetch trigger
itself: it only ran from `TrackStartEvent`, a single point-in-time check, so
if the queue was still empty at that instant (the common case — people queue
songs one at a time while something is already playing) the next track never
got prefetched at all and silently fell back to the raw, un-cached CDN path
every time**, confirmed by reading the actual `encodedTrack` payloads Lavalink
received straight out of its own request log. Fixed by also triggering the
same prefetch check from `Player.add_track()` whenever a track is added while
one is already playing. Also fixed in the same pass: `StreamCache._handle`
was only catching `ConnectionResetError` around `resp.write()`, not the plain
`ConnectionError` aiohttp actually raises when Lavalink disconnects
mid-stream, so that case escaped as an unhandled exception and spammed
`aiohttp.server ERROR` logs on effectively every track transition (harmless,
but noisy — `docker logs vocard | grep -c "ConnectionError: Connection lost"`
showed 84 occurrences over 4 days before the fix).

**Third pass (2026-10-11) — the in-memory relay proxy replaced with a
yt-dlp-backed disk cache.** The `via_prefetch`/memory-buffer relay above
still had two residual issues: a 150MiB/300MiB memory cap that cut long
videos off mid-track, and a theoretical SocketTimeoutException risk any time
Lavalink's read position caught up to a still-downloading buffer's live
edge. At the user's request, `voicelink/stream_cache.py` was rewritten to use
`yt-dlp` (a separate, independent extractor from invidious-companion) to
fully download a track's audio to local disk *before* handing anything to
Lavalink - `Node.prefetch_direct_track()` now awaits that download
in the background, and `resolve_direct_track()` (the on-demand path) only
ever uses the result if it's already a complete file; otherwise it falls
straight back to invidious-companion's raw CDN url exactly as before, same
zero-risk guarantee as every other part of this patch. This removes the
racing-the-live-edge failure mode entirely (the proxy now only ever serves a
finished, static file via `aiohttp.web.FileResponse` - verified this also
means Lavalink disconnecting mid-send needs no special handling any more;
`FileResponse` already swallows that internally, so the `ConnectionError`
fix above no longer applies to the rewritten `_handle`) and replaces the
150MiB/300MiB memory caps with a single `STREAM_CACHE_MAX_BYTES` disk budget
(default ~2.5GB, requested as "2-3GB"). Measured directly against production
(same stable test video the integration suite uses): a full download of the
same ~3.5-minute track took anywhere from 1.8s to 112s across different
attempts - YouTube's CDN throttle is inconsistent run-to-run, not a fixed
rate, which is why the on-demand path still never attempts a full download
(no upper bound on how long that could take) and only the background/prefetch
path does. The `via_prefetch` flag on `resolve_direct_track` is gone - the
on-demand path no longer takes that parameter at all, since it never touches
`stream_cache` now.

**Fourth pass (2026-10-11) — whole-queue sequential prefetch, and a larger
disk budget.** `_prefetch_next_direct_track` only ever looked one track
ahead; asked whether prefetching the *entire* playlist would be more
efficient. A naive version of that would not be, for a concrete reason:
invidious-companion and yt-dlp both egress to YouTube from the same host IP,
so firing off a download for every track in a long playlist *at once* looks
like a scraping burst from a single IP - a real risk of that IP getting
rate-limited/blocked, which would degrade invidious-companion too, not just
yt-dlp. First attempt settled for a small fixed lookahead window (3 tracks,
staggered) as a middle ground - superseded almost immediately by the actual
ask: prefetch the whole queue, but strictly *sequentially*. Replaced the
lookahead window with `Player._prefetch_worker()`, a single background task
that awaits each upcoming YouTube track's prefetch fully before starting the
next; `_prefetch_next_direct_track()` just (re)starts this worker if one
isn't already running. Never more than one download in flight regardless of
queue length removes the burst signature entirely, so there's no need to cap
how far ahead it looks - it naturally reads as ordinary one-track-at-a-time
usage no matter how deep the queue is. `STREAM_CACHE_MAX_BYTES`'s default
was also raised from ~2.5GB to 5GB, per request - confirmed this is nowhere
close to a binding constraint for whole-queue prefetching: a typical opus
track is ~1MB/minute, so 5GB holds hundreds of average tracks at once. The
30-minute cache TTL, not the disk budget, is what actually limits how far
ahead a deep queue is worth prefetching - a track downloaded far enough
ahead to expire before its turn just falls back to the on-demand path like
any other cache miss.

**Follow-up bug (2026-10-11) — the dashboard appeared to "re-prefetch the
entire list" on every add.** Reported after the whole-queue worker above
shipped. Real cause: `_prefetch_worker`'s fresh pass (one per `add_track()`
call, since a prior pass has usually already finished by the time the user
adds just one more song) walked the *entire* queue again, including tracks
that finished prefetching long ago - `prefetch_direct_track()`'s own cache
check made that a cheap no-op (no second download), but `_prefetch_and_notify()`
unconditionally sends a `prefetchStatus: loading` IPC message *before*
checking anything, so the dashboard flashed every already-ready track's tick
back to a spinner and back again, on every single add - looking exactly
like the whole queue was being re-downloaded even though nothing was.
**Fix:** added `Node.is_direct_resolve_cached()` (a plain cache+TTL check,
no side effects) and `_prefetch_worker` now skips a track entirely if it's
already fresh in the cache - no `_prefetch_and_notify` call at all, so no
IPC send, so the dashboard's indicator for that track is simply never
touched again until it actually expires.

**Second follow-up bug (2026-10-11) — rapidly adding several songs only
prefetched the first one.** Reported right after the fix above. Real cause:
`_prefetch_worker`'s loop (`for track in self.queue.tracks(incTrack=False):`)
evaluated the queue exactly once, at the start of a pass, then iterated over
that fixed snapshot. Queueing 3 songs in quick succession while the first is
still downloading is exactly the case `_prefetch_next_direct_track()`'s
"a worker is already running, it'll pick up anything new on its own"
no-op depends on - but the *running* worker's snapshot was taken before
songs 2 and 3 ever existed, so after finishing song 1 it just exited,
having never seen them. They'd then sit unprefetched until some unrelated
later `TrackStartEvent` happened to fire. **Fix:** rewrote the loop as a
`while True` that re-reads `self.queue.tracks()` fresh every iteration and
picks the next not-yet-attempted, not-yet-cached track - so a song added
mid-pass is simply still there, found, and prefetched on the very next
iteration instead of requiring a whole new pass. Needed an `attempted: set[str]`
guard alongside this: `prefetch_direct_track()` doesn't cache a failed
resolve, so without tracking "already tried this pass," a video that just
failed would look identical to a brand-new track on the next iteration and
get retried in an infinite loop instead of the pass ever finishing.

**Fifth pass (2026-10-11) — keep finished downloads indefinitely, evict by
popularity.** Requested: if the 5GB budget isn't full, keep a downloaded
track around to save a future re-download, and when it IS full, evict
smartly rather than blindly. Two changes to `voicelink/stream_cache.py`:
(1) removed the 30-minute TTL from finished disk entries entirely - there's
no correctness reason to ever re-fetch a video once it's on disk (unlike
the signed CDN url, raw bytes don't go stale), so `download()`'s reuse
check no longer has an age limit, only `_prune()`'s space-based eviction
can ever remove one now. (A *failed* entry is a separate, narrower case:
still blocked from an immediate retry for `_FAILED_RETRY_COOLDOWN`, so a
persistently broken video doesn't get hammered on every prefetch attempt.)
(2) added a `view_count` per entry (bumped on first download and every
later reuse) as the "popularity" signal requested, and a
`NodePool.is_video_queued()` check (new - walks every connected player's
queue across every guild) that `stream_cache` eviction consults via a
registered callback (`set_queued_checker()`, called once from `pool.py` at
import time - keeps `stream_cache.py` from needing to import `pool.py`
directly, which stays one-directional and keeps the module trivially
testable standalone). `_prune()`'s eviction order is now: never touch a
video that's current/upcoming in any queue right now, no matter how
unpopular; among what's left, evict the least-viewed first. If everything
left over budget is actively queued somewhere, it just stays over budget -
a soft cap, same spirit as the 5GB number itself never being a hard
guarantee.

**Why:** Lavalink's youtube-plugin was intermittently getting blocked by
YouTube's bot detection at stream-fetch time; separately, every track
stuttered audibly for the first ~15-20 seconds due to YouTube CDN throttling;
separately again, tracks would occasionally die outright partway through
from a signed-url redirect with nothing queued to recover to. Lavalink's youtube-plugin was intermittently getting blocked by
YouTube's bot detection at stream-fetch time; separately, every track
stuttered audibly for the first ~15-20 seconds due to YouTube CDN throttling;
separately again, tracks would occasionally die outright partway through
from a signed-url redirect with nothing queued to recover to.

**Files:** `vocard-src/voicelink/direct_resolver.py` (new),
`vocard-src/voicelink/stream_cache.py` (new),
`vocard-src/voicelink/pool.py` (`Node.resolve_direct_track`,
`Node.prefetch_direct_track`, `Node._prune_direct_resolve_cache`,
`Node.invalidate_direct_resolve_cache`),
`vocard-src/voicelink/player.py` (`Player.play()`'s resolve branch,
`Player._prefetch_next_direct_track`, `Player._retry_direct_resolve`),
`vocard-src/voicelink/events.py` (`TrackEndEvent.position`),
`vocard-src/cogs/listeners.py` (`on_voicelink_track_end`,
`on_voicelink_track_exception`, `_is_retryable_direct_resolve`).

**Infra:** requires the `invidious-companion` service in `docker-compose.yml`,
the `INVIDIOUS_COMPANION_URL`/`INVIDIOUS_COMPANION_SECRET` env vars on the
`vocard` service, the `vocard` service exposing port 2334 (the local proxy)
with `STREAM_CACHE_PORT`/`STREAM_CACHE_BASE_URL` env vars (defaults assume
Lavalink reaches the bot at `http://vocard:2334`, which holds automatically
on the `vocard` docker-compose network), `STREAM_CACHE_MAX_BYTES` (disk
budget, default 5GB) and `STREAM_CACHE_DIR` (set to `/app/stream_cache`,
bind-mounted to `./stream_cache` on the host - see the second follow-up
below for why that mount is required, not optional), and `yt-dlp` as a production
dependency in `requirements.txt` (invoked via `python -m yt_dlp`, not the
console script, so nothing extra needs copying into the runtime Docker
stage).

**Details / tests:** [`vocard-src/docs/direct-url-resolver.md`](../vocard-src/docs/direct-url-resolver.md),
[`vocard-src/docs/testing.md`](../vocard-src/docs/testing.md).

**To reapply after a rebuild from clean upstream:** copy `direct_resolver.py`
and `stream_cache.py`, reapply the diffs in `pool.py`/`player.py` described in
that doc, and add the `invidious-companion` service, the `STREAM_CACHE_*`
env vars, and the `2334` port exposure back to `docker-compose.yml`.

---

## Patch 2 — `updateVolume` no longer requires admin

**What:** the dashboard's volume-change IPC method now allows anyone the bot
considers DJ-or-admin (`player.is_privileged(member)`), not just members with
`manage_guild`.

**Why:** volume was the one control still gated to full admins while every
other playback control (skip, pause, queue management) already used the
DJ-or-admin check — inconsistent and more restrictive than intended.

**File:** `vocard-src/voicelink/ipc/methods.py`, `updateVolume` (~line 279):

```python
# before
@require_permission(only_admin=True)
async def updateVolume(...): ...

# after
@require_permission()
async def updateVolume(...): ...
```

**To reapply:** change the decorator argument back to the default (remove
`only_admin=True`).

---

## Patch 3 — Music controller button labels fixed

**What:** the music-controller buttons (Back/Pause/Resume/Skip/Stop, and the
track-select dropdown) were rendering the literal string `"Not found!"`
instead of their real labels.

**Why:** `settings.json`'s `default_controller.buttons` referenced flat
translation keys (`buttonBack`, `buttonSkip`, `buttonLeave`,
`buttonPause`, `buttonResume`, `playerDropdown`) that don't exist anywhere in
`vocard-src/langs/EN.json`. The lang file actually nests these under
`player.buttons.*` / `player.dropdown.*`, and `LangHandler._get_lang()`
silently falls back to `"Not found!"` for any key it can't find — so every
button rendered that fallback text instead of erroring loudly.

**File:** `settings.json` (the live, bind-mounted config —
**not** `vocard-src/settings.json`, which is only a build-time template and
was intentionally left as-is). `default_controller.buttons` now points at:
`@@t_player.buttons.back@@`, `@@t_player.buttons.pause@@`,
`@@t_player.buttons.resume@@`, `@@t_player.buttons.skip@@`,
`@@t_player.buttons.leave@@`, `@@t_player.dropdown.trackSelect@@`.

**To reapply:** this is a config fix, not a code patch — if `settings.json`
ever gets regenerated from a template, re-point those six `@@t_...@@` keys at
the nested `player.buttons.*`/`player.dropdown.*` paths shown above. No image
rebuild needed, just `docker compose restart vocard` (the config has no
hot-reload; a restart is required for changes to take effect).

---

## Patch 4 — Dashboard: spacebar no longer toggles play/pause

**What:** removed the dashboard's keyboard shortcut that toggled play/pause on
<kbd>Space</kbd>. The pause **button** and every other keyboard shortcut
(arrow-key seek/skip, `r` repeat, `s` shuffle, `/` shortcuts help, `k` search
focus) are untouched.

**Why:** requested — spacebar-triggers-pause is unwanted behavior on the
dashboard's now-playing view.

**How it's deployed — read this carefully:** the `vocard-dashboard` container
runs the **stock upstream image** (`ghcr.io/chocomeow/vocard-dashboard:latest`),
not something built from `dashboard-src/` (that local clone is several months
stale relative to the deployed image — rebuilding an image from it would have
silently reverted unrelated upstream fixes). Instead, `docker-compose.yml`
bind-mounts a single edited file over the container's copy:

```yaml
volumes:
  - ./dashboard/settings.json:/app/settings.json
  - ./dashboard/overrides/assets/js/action.js:/app/assets/js/action.js
```

The override file lives at `dashboard/overrides/assets/js/action.js` and was
produced by extracting the *live* file from the running container
(`docker cp vocard-dashboard:/app/assets/js/action.js ...`) and removing the
three `playPause`/`" "` references (the `keyMap` entry, the `switch` case, and
the shortcuts-help modal listing) — not by editing the stale `dashboard-src/`
clone directly. The dashboard app minifies `assets/js/*.js` into
`static/js/*.min.js` at container startup, so this override is picked up
fresh on every restart with no build step.

**To reapply:** if the dashboard image is ever upgraded, re-extract the new
image's `action.js`, diff it against `dashboard/overrides/assets/js/action.js`
to see if upstream changed anything nearby, and reapply the same three
removals (search for `playPause`).

**Caution:** `dashboard-src/` is a stale reference clone (last synced
2026-05-11), not what's deployed. Don't build a dashboard image from it
without first diffing against the live container's files, or you'll roll back
months of unrelated upstream fixes — see the note above.

---

## Patch 5 — Dashboard: loading / prefetch status indicators

**What:** the dashboard now shows a small spinner (reusing the existing
`.loader` element already used elsewhere in the UI) in two places:
- next to the current track's title (both the persistent bottom control bar
  and the "Now playing" card in the queue panel) while the bot is between
  songs — i.e. from the moment it starts resolving/handing off the next track
  to Lavalink until playback actually starts.
- next to the **next** item in the queue list while its direct URL is being
  pre-resolved in the background (Patch 1's prefetch mechanism) — clears
  automatically once that resolve finishes (success or failure).

**2026-10-11 addition — tick + progress bar for the whole-queue sequential
prefetch.** Once Patch 1's prefetch became a sequential worker covering the
*entire* upcoming queue (not just the next track), a bare spinner on
whichever track happened to be downloading wasn't enough feedback - requested:
a checkmark once a track is actually ready, and a progress bar showing how
far along an in-flight download is. Two parts:
- **Backend** (`voicelink/stream_cache.py`): yt-dlp is now invoked with
  `--newline --progress-template` instead of `--no-progress --quiet`, and
  `_run_ytdlp` reads its stdout live (a new `_drain_progress` coroutine,
  replacing the old single `communicate()` call) instead of only after it
  exits, updating each `_CacheEntry`'s `downloaded_bytes`/`total_bytes` as
  lines arrive. `StreamCache.progress(key)`/`stream_cache.get_progress(key)`
  expose the resulting fraction; `Node.get_prefetch_progress()` wraps it with
  the same "never raises, swallows errors" guarantee as every other part of
  this feature.
- **Player** (`Player._prefetch_and_notify`): instead of one `await` on
  `prefetch_direct_track()`, the prefetch now runs as a task that's polled
  every `PREFETCH_PROGRESS_POLL_SECONDS` (1s) while in flight, pushing a
  `prefetchStatus: loading` with a `progress` fraction each time one is
  known (omitted/ignored while unknown - e.g. right at download start before
  yt-dlp has reported a size - so the frontend doesn't show a bar stuck at
  0%).
- **Frontend**: `ready` is no longer treated the same as `unavailable`
  (both used to just clear the indicator) - `ready` now renders a small
  green checkmark (`check_circle`) in the same badge slot the spinner used,
  and a `loading` status with a known `progress` renders a thin fill bar
  under the track title/author instead of the spinner (the spinner is kept
  as the fallback for the brief window before a size is known).

**Why:** requested, to make the (usually brief, occasionally not) gap between
songs visible instead of the dashboard just looking frozen, and to give
visibility into whether the prefetch optimization from Patch 1 is doing
anything - extended to show real download progress once prefetching the
whole queue meant downloads could meaningfully overlap in the queue view.

**Bug fix (2026-10-11) — ticks/progress bars disappeared on every track
transition.** Real cause: `objects.js`'s `Player.init()` resets
`prefetchStatuses`/`cacheStats` to empty, and `init()` is called for three
different reasons, only two of which are an actual "forget everything"
moment (the player closing; the user switching to a different guild). The
third is `trackUpdate`'s own resync check (`if (track?.trackId != data["trackId"])
{ player.send({ op: "initPlayer" }) }`) - a lighter, *same-guild* correction
for whenever the dashboard's locally-cached current track doesn't match what
the bot just started playing, which responds with a full `initPlayer`
snapshot that also runs through `init()`. Since `prefetchStatuses`/
`cacheStats` are keyed by a stable track id that survives this resync intact
(the rebuilt queue uses the exact same ids), wiping them on *this* path had
no upside - it just meant every still-upcoming track's indicator flashed
back to nothing whenever the next song started, not just the just-finished
track's own (expected to disappear - it's not upcoming any more). **Fix:**
removed the `prefetchStatuses`/`cacheStats` reset from `init()` entirely -
the constructor already does the real one-time initialization for a
brand-new `Player` object, and a stale leftover entry for a track no longer
in view is simply never looked up again, harmless.

**How it works:** two new IPC ops the bot pushes over the existing bot↔dashboard
websocket, both routed through the dashboard's **generic, unmodified**
broadcast relay in `objects.py`'s `Bot._listen()` (any payload with a
`guildId` gets relayed to every browser client on that guild as-is — no
dashboard backend code changed for this patch, only its frontend):

| Op | Sent by | When |
|---|---|---|
| `trackLoading` | `Player.do_next()` | right before calling `Player.play()` for the next queued track |
| `prefetchStatus` | `Player._prefetch_and_notify()` | `status: "loading"` (with a `progress` fraction when known) roughly once a second while a background prefetch is in flight, `status: "ready"`/`"unavailable"` when it finishes |

The existing `trackUpdate` op (already sent once `play()` completes) doubles
as the "loading finished" signal for `trackLoading` — the frontend just clears
the flag when it arrives.

**Files:**
- Bot: `vocard-src/voicelink/player.py` (`do_next()`'s `trackLoading` send,
  `Player._prefetch_and_notify()`'s progress-poll loop), `vocard-src/voicelink/pool.py`
  (`Node.prefetch_direct_track()` returns the resolved value instead of
  `None` so the caller can report success/failure; `Node.get_prefetch_progress()`
  for the live fraction), `vocard-src/voicelink/stream_cache.py`
  (`StreamCache.progress()`/`_drain_progress`/`_PROGRESS_TEMPLATE`).
- Dashboard overrides (same single-file-override pattern as Patch 4, extracted
  live from the running container before editing):
  `dashboard/overrides/assets/js/objects.js` (`trackLoading`/`prefetchStatus`
  handlers - `prefetchStatuses` entries are now `{status, progress?}` objects,
  not a bare `"loading"` string - `updateInfo()`/`updateQueueList()` wiring),
  `dashboard/overrides/assets/js/action.js` (`buildQueueTrackHtml`'s tick/
  spinner/progress-bar badge), `dashboard/overrides/templates/controller.html`
  and `dashboard/overrides/templates/queue.html` (the loader elements
  themselves). The bug fix above touched only `objects.js`'s `init()`.

**To reapply after a dashboard image upgrade:** re-extract the new image's
`objects.js`/`action.js`/`controller.html`/`queue.html`, diff against the
`dashboard/overrides/` copies, and reapply: the `trackLoading`/`prefetchStatus`
entries in `objects.js`'s `methods` object, the `isTrackLoading`/
`prefetchStatuses` state + `updateInfo()`/`updateQueueList()` toggles, the
`buildQueueTrackHtml` badge parameter in `action.js`, and the two `<div class="loader" id="...">`
elements in the templates.

---

## Patch 6 — Dashboard: resolved-URL cache size display

**What:** a small, muted line at the bottom of the queue panel showing the
current size of the bot's in-memory direct-URL resolve cache from Patch 1
(e.g. "Resolved-URL cache: 2 tracks · 1.3 KB"). Hidden when the cache is
empty.

**Why:** requested, to make Patch 1's cache visible. Worth being explicit
about what this is *not*: it is **not** an audio/song file cache — Lavalink
streams and transcodes on the fly and never writes audio to disk in this
deployment (confirmed by inspecting the Lavalink container's filesystem: no
cache directory exists). The resolve cache only holds small resolved-URL/
metadata strings with a 30-minute TTL and automatic pruning (see Patch 1) —
it's inherently bounded to a few KB and was never at risk of approaching
anything like 500MB, so no size cap or eviction control was added; this patch
is display-only.

**How it works:** `Node.get_direct_resolve_cache_stats()` returns
`{entries, approxBytes}`; `Player._report_cache_stats()` pushes it over IPC as
a `cacheStats` op whenever the cache changes (after `play()`'s resolve call
and after each background prefetch) — same generic dashboard relay as
Patches 1 and 5, no dashboard backend changes.

**Files:**
- Bot: `vocard-src/voicelink/pool.py` (`Node.get_direct_resolve_cache_stats`),
  `vocard-src/voicelink/player.py` (`Player._report_cache_stats`, called from
  `play()` and `_prefetch_and_notify`).
- Dashboard overrides: `dashboard/overrides/assets/js/objects.js` (`cacheStats`
  handler, `updateCacheStatsView()`), `dashboard/overrides/templates/queue.html`
  (the `#cache-stats-line` element).

**Operational gotcha hit while building this:** editing the *contents* of an
already-mounted override file does **not** get picked up by
`docker compose up -d <service>` — compose only recreates a container when its
*config* (image, env, volume list) changes, not when a bind-mounted file's
contents change on disk. The dashboard also only re-minifies JS and (depending
on Jinja's template cache) re-reads templates at process startup. So after
editing any file under `dashboard/overrides/`, you need an explicit
`docker compose restart vocard-dashboard` — `up -d` alone will silently keep
serving the old version.

---

## Patch 7 — Dashboard: audio cache manager

**What:** a window on the dashboard (opened via a new "Manage audio cache"
link under the queue panel, next to Patch 6's resolved-URL stat line) that
lists every video currently sitting in `voicelink/stream_cache.py`'s yt-dlp
disk cache - size, view count, ready/downloading/failed status, and whether
it's current/upcoming in some player's queue right now - against a usage
bar for the `STREAM_CACHE_MAX_BYTES` budget, with a delete button per entry.

**Why:** requested, alongside the indefinite-caching/popularity-eviction work
in Patch 1's fifth pass - once entries can live indefinitely instead of
expiring after 30 minutes, there's real value in being able to see what's
actually sitting there and manually clear something out (free up space
ahead of automatic eviction, or force an immediate retry on a stuck/failed
entry instead of waiting out `_FAILED_RETRY_COOLDOWN`).

**How it works:** two new bot-wide (not guild-scoped) IPC methods, gated on
DJ-or-admin via `_require_cache_manager_permission` - the same privilege
level as every other playback control (`Player.is_privileged()`), not
`manage_guild` alone, which would lock out DJs who aren't full server
admins (first shipped requiring `manage_guild`, same as `getSettings`;
corrected same day after the DJ-should-have-access request). Since the
cache itself is bot-wide, not tied to any one guild, this only needs *a*
guild the requester is a recognized DJ/admin in - if a player happens to be
connected there, it defers straight to that player's own `is_privileged()`
(skipping the voice-channel-membership check, irrelevant to managing a disk
cache); otherwise it replicates the same bot_access_user/manage_guild/
configured-DJ-role checks directly against the guild's stored settings,
since there's no `Player` instance to ask:

| Op | Handler | What it does |
|---|---|---|
| `getCacheEntries` | `voicelink/ipc/methods.py`'s `getCacheEntries` | Returns the full entry list + usage stats |
| `deleteCacheEntry` | `voicelink/ipc/methods.py`'s `deleteCacheEntry` | Evicts one entry by video id, then returns the same payload shape as `getCacheEntries` so the frontend just re-renders |

Both respond with `{"op": "cacheEntries", "entries": [...], "totalBytes",
"maxBytes", "userId"}` (`deleteCacheEntry` adds `"deleted": bool`) - the
`userId` is what makes the dashboard backend's generic relay
(`objects.py`'s `User.send()`, not the `guildId`-based guild broadcast Patch
5 uses) deliver it back to just the requesting browser tab, same pattern as
`getSettings`.

`deleteCacheEntry` deliberately does **not** check whether the video is
currently queued before deleting it, unlike `StreamCache._prune()`'s
automatic eviction - this is an explicit admin action, the user's own call
to make, and worst case (deleting something actually in use) just falls
back to an on-demand resolve next time, the same zero-risk guarantee as
everywhere else in this feature.

**Files:**
- Bot: `vocard-src/voicelink/stream_cache.py` (`StreamCache.list_entries()`/
  `delete_entry()`/`stats()` and their module-level wrappers), `vocard-src/voicelink/ipc/methods.py`
  (`getCacheEntries`, `deleteCacheEntry`, `_require_cache_manager_permission`,
  and their `METHODS` registration).
- Dashboard overrides: `dashboard/overrides/assets/js/action.js`
  (`buildCacheManagerModalHtml`/`formatCacheBytes`, the `#cache-manager-link`/
  `.cache-entry-delete` click handlers - reuses the existing generic
  `.modal-background`/`.modal-container`/`.close` styling and dismiss
  behavior the shortcuts modal (`buildShortcutsModalHtml`) already
  established, so no new CSS was needed), `dashboard/overrides/assets/js/objects.js`
  (`cacheEntries` handler - replaces the modal in place if it's already
  open, so a delete just updates the list instead of flicker-closing it),
  `dashboard/overrides/templates/queue.html` (the `#cache-manager-link`
  element).

**To reapply after a dashboard image upgrade:** re-extract the new image's
`action.js`/`objects.js`/`queue.html`, diff against the `dashboard/overrides/`
copies, and reapply the pieces listed above. Remember the Patch 6 gotcha:
`docker compose restart vocard-dashboard` after editing, `up -d` alone won't
pick up override file changes.

**Follow-up (2026-10-11), three bug reports:**

1. *"when i refresh the web ui, all the tick gone and more missing"* -
   `prefetchStatuses`/`cacheStats` were purely in-memory JS state, populated
   only by change-only `prefetchStatus`/`cacheStats` IPC pushes - a fresh
   websocket connection (page refresh, not just a same-guild resync) started
   with both empty and nothing ever repopulated them. Fixed by having
   `initPlayer` (`voicelink/ipc/methods.py`) compute a one-shot snapshot at
   connect time: it awaits `stream_cache.list_entries()` once, matches each
   `youtube` track in `player.queue._queue` by `identifier` against the disk
   cache (`ready`/`downloading`/`failed`→ skip), and returns
   `"prefetchStatuses": {trackId: {status, progress}}` plus `"cacheStats"`
   (the existing `get_direct_resolve_cache_stats()` shape) alongside the rest
   of the payload. `dashboard/overrides/assets/js/objects.js`'s `initPlayer`
   handler now sets `player.prefetchStatuses`/`player.cacheStats` from this
   snapshot (overwrites, not merge - authoritative as of connect time) before
   `updateCurrentQueuePos()` triggers the first render, and explicitly calls
   `updateCacheStatsView()` since that line isn't part of the queue render.

2. *"the inline stat line and the cache manager show different numbers"* -
   not a bug: `#cache-stats-line` ("Resolved-URL cache: N tracks · X KB") was
   always showing `Node._direct_resolve_cache` (Patch 6's tiny in-memory
   metadata/URL-string cache, a separate thing with its own TTL), while the
   cache manager modal shows `voicelink/stream_cache.py`'s disk cache (the
   actual downloaded audio, up to `STREAM_CACHE_MAX_BYTES`). Two unrelated
   caches that both happened to say "cache" right next to each other with no
   visible distinction. Relabeled rather than unified, since the two numbers
   really do mean different things: the inline line now reads
   `"URL-metadata cache (not audio): N tracks · X KB"`
   (`objects.js`'s `updateCacheStatsView`), and both elements' tooltips in
   `dashboard/overrides/templates/queue.html` spell out explicitly that
   they're different caches.

3. *"show the song name, not the hash id"* - `stream_cache.py`'s
   `_CacheEntry` gained a `title: str = ""` field, set on first `download()`
   call and backfilled (never overwritten) on reuse if it was empty.
   `download()`/`prefetch_to_disk()`/`Node.prefetch_direct_track()` all grew
   an optional `title` parameter threaded through from
   `Player._prefetch_and_notify()`, which already has the full `Track` (so
   `track.title`). `list_entries()` now includes `"title"` (and `"progress"`
   for in-flight downloads, previously only available via the separate
   `get_progress()` call - free to add alongside `title` since `list_entries`
   already builds one dict per entry). The cache manager modal
   (`action.js`'s `buildCacheManagerModalHtml`) now shows
   `entry.title || entry.videoId` as the link text (falls back to the id for
   any entry downloaded before this change, self-heals next time that video
   is queued again) and appends a live `NN%` to the "Downloading…" badge
   when progress is known.

**Files touched by this follow-up:** `vocard-src/voicelink/stream_cache.py`
(`_CacheEntry.title`, `download()`'s `title` param + backfill, `list_entries()`'s
`title`/`progress` fields, `prefetch_to_disk()`'s `title` param),
`vocard-src/voicelink/pool.py` (`prefetch_direct_track()`'s `title` param),
`vocard-src/voicelink/player.py` (`_prefetch_and_notify()` passes
`track.title` through), `vocard-src/voicelink/ipc/methods.py` (`initPlayer`'s
`prefetchStatuses`/`cacheStats` snapshot), `dashboard/overrides/assets/js/objects.js`
(`initPlayer` handler, `updateCacheStatsView` label),
`dashboard/overrides/assets/js/action.js` (`buildCacheManagerModalHtml`'s
title/progress display), `dashboard/overrides/templates/queue.html` (tooltip
wording). Verified: 144 unit + 6 integration tests pass; bot image rebuilt and
redeployed; dashboard restarted and its served `.min.js` bundles confirmed to
contain the new code (not just the bind-mounted source - see Patch 6's
build-step gotcha); all four touched Python files diffed byte-for-byte clean
against the running `vocard` container.

**Second follow-up (2026-10-11), two more reports:**

1. *`int() argument must be a string, a bytes-like object or a real number,
   not 'NoneType'` when opening the cache manager* - `_require_cache_manager_permission`
   did `int(data.get("guildId"))` unguarded; `data["guildId"]` is just
   whatever `player.guildId` happened to be client-side
   (`action.js`'s `#cache-manager-link` handler), which is `null`/unset
   until a guild's player has actually been initialized (e.g. the dashboard
   was just opened with no guild joined yet) - `int(None)` then threw
   straight into the dashboard's error toast instead of a friendly message.
   Rewritten to not hard-depend on `guildId` at all: since the disk cache is
   bot-wide (see Patch 7's own rationale above), it now builds a candidate
   guild list - the given `guildId` first if it resolves to a real guild,
   then every other guild the bot shares with the requester - and accepts
   the first one where they're a recognized DJ/admin, the same per-guild
   check as before just no longer tied to whichever guild the dashboard
   happens to have selected. Falling back to no guildId at all (or a stale
   one) now works as long as the requester is DJ/admin in *any* mutual
   guild, with a distinct "Vocard doesn't share a server with you" error
   only when they're in none of them.

2. *"the first song that play that hasn't been cached yet is played by
   invidious player and not cached... do it both and add the current song
   into the cache?"* - correct at the time: `Player.play()`'s on-demand
   resolve (`Node.resolve_direct_track()`) never *starts* a new download
   (no head start - see its docstring, unchanged by the fourth follow-up
   below), so any track reached with no prior prefetch - most commonly the
   very first song of a session, since nothing was playing before it to
   prefetch it in the background - never got yt-dlp-downloaded at all, only
   ever resolved fresh via invidious-companion every time it was queued
   again. `play()` now also
   fires `Node.prefetch_direct_track(track.identifier, track.title)` for
   the *current* track as a background task (`self._bot.loop.create_task`,
   never awaited) right alongside the existing on-demand resolve - "do
   both", per the request: playback still goes out over whatever
   `resolve_direct_track()` already returned (unchanged, zero added risk to
   the no-head-start Lavalink-timeout concern that path exists to avoid),
   while this ensures a copy also lands in the disk cache for *next* time.
   Cheap/idempotent even for an already-cached or already-in-flight video -
   `StreamCache.download()`'s existing dedup logic (reuse the finished
   entry, or await whichever task is already running) handles it for free.

**Files touched by this second follow-up:**
`vocard-src/voicelink/ipc/methods.py` (`_require_cache_manager_permission`'s
guild-fallback rewrite), `vocard-src/voicelink/player.py` (`play()`'s new
fire-and-forget `prefetch_direct_track` call). Verified: 147 unit + 6
integration tests pass (3 new: a background-download-is-fired test, a
does-not-block-on-it test, a non-YouTube-track-skips-it test); bot image
rebuilt and redeployed; both files diffed byte-for-byte clean against the
running `vocard` container.

**Third follow-up (2026-10-11): the cache was never actually persisting.**

*"why is nothing cached, i thought the song stayed until it reach 5gb... are
the songs deleted when the bot stop or what"* - yes, effectively: `STREAM_CACHE_DIR`
had no bind-mounted volume in `docker-compose.yml`, defaulting to
`/tmp/vocard_stream_cache` inside the container's own writable layer. That
was a deliberate choice back when Patch 1 first shipped this (entries had a
30-minute TTL and were only ever meant to be short-lived scratch space - see
the now-stale comment this follow-up removed from
`vocard-src/docs/direct-url-resolver.md`'s config table) but was never
revisited when Patch 1's fifth pass changed to indefinite caching under the
5GB budget - the whole point of which was to *stop* re-downloading the same
song. A container *recreate* (not just a reboot - `docker compose up -d`
after any image rebuild recreates the container, shown in its own output as
"Container vocard Recreate/Recreated") throws away the old writable layer
and starts from a fresh one, silently wiping every cached file. This
happened on every single redeploy throughout this whole feature's
development, which is almost certainly why nothing was ever observed
sitting in the cache for long.

**Fix:** added `STREAM_CACHE_DIR=/app/stream_cache` to the `vocard`
service's environment and a `./stream_cache:/app/stream_cache` bind mount
in `docker-compose.yml`, plus `mkdir -p ./stream_cache` on the host.
Verified by writing a marker file into the mount, then running
`docker compose up -d --force-recreate vocard` (the same recreate a normal
redeploy does) and confirming the marker survived.

**Files:** `docker-compose.yml` (environment + volumes for the `vocard`
service), `vocard-src/docs/direct-url-resolver.md` (`STREAM_CACHE_DIR`'s
config-table entry, no longer describing it as deliberately unmounted).

**Fourth follow-up (2026-10-11): the volume mount alone wasn't enough.**

*"the cache window still not showing the song name, and some song is not
cached when added"* - follow-up to the third fix above. The files
themselves now survive a restart (bind-mounted volume), but `_entries` -
the in-memory catalog `list_entries()`/`is_ready()`/`download()` all
actually read, holding title/view_count/ready-status - never did, and
nothing rebuilt it from what was already sitting in `CACHE_DIR`. Confirmed
live: five real `.audio` files existed on disk (`ls stream_cache/`showed
~72MB across 5 videos, downloaded in the session before this follow-up's
own restart for the DEBUG-logging change below), but `StreamCache.start()`
never looked at the directory it just created - it only ever learned about
a file through `download()` actually writing it in the current process.
So every restart (not just the destructive kind the third follow-up fixed
- any restart, including routine ones) made every previously-cached file
invisible to the dashboard's cache manager (title and all) until that
exact video happened to get queued again in the new process, and in the
meantime `download()` would spawn a whole new yt-dlp subprocess for it
rather than recognizing the file was already there (yt-dlp itself would
have no-op'd rather than re-fetching, but it's still a wasted subprocess
and a multi-second wait instead of an instant cache hit).

**Fix:** added `StreamCache._adopt_existing_files()`, called from
`start()` right after `CACHE_DIR.mkdir()`: globs `CACHE_DIR` for `*.audio`
files and creates a ready `_CacheEntry` for each one not already known,
then runs `_prune()` once in case the adopted total is already over
budget. Title/view_count aren't recoverable from just a filename (only the
video id is), so an adopted entry starts with an empty title (falls back
to showing the video id in the cache manager, same as any pre-title-feature
entry) and `view_count=1`; both self-heal normally the next time that
video's actually downloaded/reused.

Separately, to help pin down the "some song is not cached" half of this
report (the two most likely explanations: (a) it was a non-YouTube-sourced
track - Spotify/SoundCloud/etc. - which this whole pipeline has always
skipped by design, since it's YouTube-specific, or (b) an actual silent
yt-dlp failure, invisible because `[DirectResolver]`'s own failure logging
is at DEBUG and `settings.json`'s `logging.level.vocard` was `"INFO"`),
bumped that to `"DEBUG"` temporarily in `/root/vocard/settings.json` and
restarted - revert once the cause is confirmed, DEBUG is noisy
project-wide, not just for this feature.

**Files:** `vocard-src/voicelink/stream_cache.py` (`_adopt_existing_files`,
called from `start()`), `/root/vocard/settings.json` (temporary
`logging.level.vocard: DEBUG`, not part of the feature - revert when done
diagnosing). Verified: 152 unit tests pass (4 new for `_adopt_existing_files`
directly, 1 new exercising it end-to-end through `_get_cache()`) + 6
integration; bot image rebuilt and redeployed; the five pre-existing
`.audio` files on disk confirmed to still be present and unchanged across
the redeploy; `stream_cache.py` diffed byte-for-byte clean against the
running container.

**Fifth follow-up (2026-10-11): cached songs weren't reliably played from
cache either.**

*"are you sure that for the song that already cached, it will used the
cached song locally and not the streaming one"* - good question to ask,
because the honest answer at the time was no, not reliably.
`Node.resolve_direct_track()` (play()'s on-demand path, used for whatever's
about to play *right now*) only ever checked `_direct_resolve_cache` - a
30-minute cache of *resolved encoded tracks* - never `stream_cache`'s
actual disk cache, which has no TTL at all by design (see Patch 1's fifth
pass). So a video that had been fully downloaded to disk hours (or days)
ago would still get a fresh invidious-companion resolve every time it was
played, the instant that 30-minute window lapsed - completely ignoring
that the real file was sitting right there on disk, ready to serve
instantly with zero network calls.

**Fix:** `resolve_direct_track()` now checks `stream_cache.get_local_url()`
first - a cheap, local, no-network dict-and-stat read - before ever
touching `_direct_resolve_cache`'s TTL or invidious-companion. This is safe
even for the no-head-start on-demand path despite the "never touches
stream_cache" rule the rest of this doc still describes correctly
elsewhere: that rule is about never *starting* a new download here (the
actual race risk - see Patch 1's third pass), not about refusing to serve
one that's already finished. `get_local_url()` only ever returns a url for
an entry that's already `is_ready()`, so this can never hand Lavalink a
half-downloaded file - there's nothing to race. Also updated
`docs/direct-url-resolver.md` and `docs/testing.md`'s own description of
this (they previously said the on-demand path "never touches stream_cache"
at all, which is no longer true).

Also added debug-level logging for all three of `_run_ytdlp`'s failure
branches (timeout, nonzero exit, unexpected exception) - previously only
the last of the three logged anything, so a failed download (age-
restricted/region-locked/bot-detected video, or any other yt-dlp error)
was completely silent even with DEBUG on, which is exactly the "some song
is not cached" report's other half still needs: with this in place, the
next actual failure will show up in the logs with yt-dlp's real reason.

**Files:** `vocard-src/voicelink/pool.py` (`resolve_direct_track`'s
disk-cache-first check), `vocard-src/voicelink/stream_cache.py`
(`_run_ytdlp`'s three failure-branch debug logs),
`vocard-src/voicelink/player.py` (one stale comment),
`vocard-src/docs/direct-url-resolver.md`, `vocard-src/docs/testing.md`.
Verified: 4 new unit tests for the disk-cache-first behavior (hit with no
`_direct_resolve_cache` entry, fresher `_direct_resolve_cache` entry still
wins, a `stream_cache` error falls through safely) plus the existing
suite - 155 unit + 6 integration, all passing; bot image rebuilt and
redeployed; `pool.py`/`stream_cache.py`/`player.py` diffed byte-for-byte
clean against the running container.

**Sixth follow-up (2026-10-11): titles still weren't showing, because they
genuinely couldn't survive a restart.**

Re-reported after the fourth follow-up's `_adopt_existing_files` fix:
*"the manage cache window only show the music id... not the song name, and
theres some songs thats failed to cache."* Checked the live host mount
(`ls /root/vocard/stream_cache`) and found **50 real cached files, ~230MB
total** - the caching pipeline itself was working, extensively. So the
"not cached" half was likely either a non-YouTube-sourced track (Spotify/
SoundCloud - always skipped by design) or a genuine yt-dlp failure, now
at least visible thanks to the fifth follow-up's new failure logging - but
the "no song name" half had a real, fully-diagnosable cause: `_adopt_existing_files`
(fourth follow-up) only ever recovered path/size from the bare `.audio`
filename - title was never recoverable that way, so *every* restart since
that fix shipped permanently blanked the title for every file already on
disk at that point, no matter how many times it had actually been
downloaded with a real title in a previous process. (Whether an
additional, unlogged restart happened between the fourth follow-up and
this report couldn't be confirmed either way - `vocard`'s own file logging
is `./logs` with no bind mount, so its history doesn't survive a
container recreate any more than the titles did - but it didn't need to:
the adopt-without-title gap was real regardless, and would reproduce on
the very next restart even if it hadn't yet.)

**Fix:** a small `{key}.meta.json` sidecar next to each `.audio` file,
written by `StreamCache._write_title_sidecar()` right after a successful
download (and again if a title gets backfilled on reuse - see Patch 1's
title-threading work), read back by `_adopt_existing_files()`, and removed
by `_evict()` alongside the audio file. Never raises on a write failure or
a missing/corrupt sidecar - a lost title is purely cosmetic, never worth
failing a download or adoption over.

Also fixed the *actual* local-cache-vs-streaming question this same
report asked, which turned out to have a real answer - see the fifth
follow-up above.

**Files:** `vocard-src/voicelink/stream_cache.py`
(`_write_title_sidecar`/`_read_title_sidecar`/`_meta_path`, wired into
`download()`, `_run_ytdlp`, `_adopt_existing_files`, `_evict`). Verified:
6 new unit tests (sidecar written on download/backfill, not written
without a title, recovered on adopt, tolerates a corrupt sidecar, removed
on delete) - 161 unit + 6 integration, all passing; bot image rebuilt and
redeployed; `stream_cache.py` diffed byte-for-byte clean against the
running container. The 50 pre-existing files from before this fix don't
retroactively gain titles (nothing to recover them from) - they'll pick
one up normally the next time each is downloaded/reused.

---

## Patch 8 — Queue reorder and volume open to every member, not just the DJ

**What:** `moveTrack` (drag-and-drop reorder, and the context menu's "Move
to top"/"Move to bottom") and `updateVolume` no longer require
`player.is_privileged(member)` (DJ or admin) - any member in the voice
channel can now reorder the queue or change the volume, the same way
`addTracks` already worked for everyone. `skipTo`/`backTo`/`removeTrack`/
`clearQueue`/`repeatTrack`/`updateFilter`/`updatePosition` are unchanged
and still DJ-or-admin-gated via `@require_permission()`.

**Why:** requested - "i want my friend who is not dj can also add song and
move the order of the song in the queue and set the volumn for the bot."
Adding a song was already open to everyone (`addTracks` has never had a
`@require_permission()` decorator); reordering and volume were the two
gaps.

**Separately investigated: "my friend... see nothing" when logged in.**
Not a permission bug, and not fixed by the above - a different mechanism
entirely. `voicelink/ipc/methods.py`'s `process_methods` (the IPC
dispatcher) resolves *which guild/player* a request without an explicit
`guildId` applies to by scanning the user's mutual guilds for one where
`member.voice and member.voice.channel` is true (methods.py ~line 904),
and separately refuses to hand back a player at all if the requesting
member isn't in the *exact same* voice channel as the bot (~line 922). If
a user isn't currently sitting in a voice channel together with the bot in
any shared server, `initPlayer` (sent automatically right after picking a
bot) matches nothing, returns no response at all, and the dashboard just
stays blank - no error, no "join a channel" message, nothing. This is a
pre-existing, deliberate design (the dashboard mirrors "whoever's in the
channel can use the controls"), not something this patch changes, and
almost certainly the real explanation: DJ-vs-not only ever disables
specific controls once the page has loaded (see Patch 8 above and
Patch 2), it never blanks the whole page. Told the user to have their
friend join the same voice channel as the bot and confirm whether the
dashboard populates then, before assuming anything else is broken.

(Note, unrelated to either of the above: `getMutualGuilds`/`objects.py`'s
settings-page guild list on the *dashboard* service, not the bot, filters
guilds by `guild['permissions'] >= 1275593889` - a numeric-comparison-not-
bitmask check that effectively requires a high-permission role. That's the
*bot admin settings* page's own guild picker, unrelated to the main
queue/player dashboard this report was about, and not touched here.)

**Files:** `vocard-src/voicelink/ipc/methods.py` (`moveTrack`/`updateVolume`'s
decorators removed), `dashboard/overrides/assets/js/objects.js`
(`initPlayer`'s volume-bar/queue-sortable disabled logic),
`dashboard/overrides/assets/js/action.js` (`moveTop`/`moveEnd` context-menu
items' disabled condition).

**To reapply:** remove `@require_permission()` from `moveTrack` and
`updateVolume` in `methods.py`; in `objects.js`'s `initPlayer`, replace the
DJ-conditional volume-bar disable with an unconditional
`removeAttr("disabled")` and set the `#queue` sortable's `disabled` to
`false`; in `action.js`'s `buildContextMenu`, drop the `|| !player.isDJ`
from `moveTop`/`moveEnd`'s disabled conditions.

**Deployment-order gotcha hit while shipping this patch (self-inflicted,
not a code bug):** this patch's own redeploy ran `docker compose up -d
vocard` (recreating the bot) immediately followed by `docker compose
restart vocard-dashboard`. The bot's `voicelink/ipc/client.py` only
retries its connection to the dashboard automatically *after* an
already-established connection later drops (`_listen()`'s "Connection
closed" handler) - the very first connection attempt at startup, if it
fails, just logs `Cannot connected to dashboard! - Reason: Connection
failed.` once and never retries. Restarting the dashboard right after
starting the bot raced that first attempt against the dashboard actually
being back up, lost the race, and left the bot running but permanently
disconnected from the dashboard's IPC channel - which the dashboard
surfaces to *every* connected browser as "Can't Find You on Discord"
(`dashboard-src`'s `Bot.disconnect()` pushes `{"op": "closeConnection"}` to
every user when the bot's own IPC socket drops), not just whichever
guild/user was involved. Fixed by restarting `vocard` again once the
dashboard was confirmed healthy. **Going forward: if both services need a
restart, restart `vocard-dashboard` first and confirm it's healthy, *then*
`vocard` - never the other order, and never both without a pause to check
between them.**

---

## Patch 9 — Home/Explore built from real per-guild play history, split into bot vs. uploaded

**What:** the dashboard's Home page ("Recently played") and Explore page
("Trending Songs") used to come from two things that had nothing to do with
what had actually been played: Home read one user's own personal, 25-track-
capped Mongo history (`Users.history` - whichever tracks *that specific
person* happened to request), and Explore always fetched the exact same
hardcoded YouTube Music playlist regardless of server. Neither reflected
real, shared listening activity.

Both are now built from a new, per-guild (not per-user, not bot-wide) play
history, split into two buckets by `track.source`: `botHistory`/
`botTopCounts` for anything searched and played normally
(youtube/spotify/soundcloud/deezer/applemusic), `uploadHistory`/
`uploadTopCounts` for a track played from a Discord message attachment
(`track.source` is `http`/`local` for those - the only existing signal that
distinguishes a "self-uploaded" play from a searched one; there is no
separate dashboard upload feature). Home shows each bucket's most-recent,
deduped-by-uri tracks (capped at 60 each); Explore shows each bucket's
most-played tracks ranked by a running per-track play count. The "uploaded"
section on either page is hidden entirely for a server where nobody's ever
played an attachment.

**Why:** requested - "the home page and explore page should be based on all
the songs thats has been play, mostly the recent played should be seperate
into the bot and the user self upload." Scope decisions confirmed with the
user: "self upload" means the existing Discord-attachment-play mechanism
(not a new web-upload feature), and history/trending are tracked per-server,
not bot-wide.

**Data model:** stored on the existing per-guild `Settings` Mongo document
(no new collection) as four new fields: `botHistory`/`uploadHistory` (capped
lists of encoded track-id strings, `$push`+`$slice: -60`) and
`botTopCounts`/`uploadTopCounts` (`{sha1(identifier-or-uri): {count,
trackId, title}}` - hashed so an arbitrary identifier/url can never collide
with Mongo's reserved `.`/`$` field-name characters). Written by
`Player._record_guild_play_history()`, called fire-and-forget from
`do_next()` right alongside the existing per-user history push, for every
track that actually starts playing (regardless of requester, including
autoplay/recommendation picks - this intentionally mirrors "all the songs
that's been played," not just user-requested ones). A write failure here
(e.g. Mongo hiccup) is caught and logged at debug level - it must never
affect playback.

**New IPC methods** (both plain `SystemMethod`s, not `PlayerMethod`s - they
work without an active voice-channel player): `getGuildHistory` (Home) and
`getGuildTopTracks` (Explore, replacing the old hardcoded-playlist
`getTracks` call). Both resolve "which guild" via a new
`_resolve_guild_for_user()` helper - best-effort, same `int(data["guildId"])`
+ try/except pattern as `_require_cache_manager_permission` (Patch 7),
falling back across the user's other mutual guilds if `guildId` is
missing/stale (there's no guild-switcher UI on Home/Explore to always have
one in hand) - only checks plain membership, not DJ/admin privilege, since
play history is read-only and not sensitive.

**Files:** `vocard-src/voicelink/player.py` (`_record_guild_play_history`,
called from `do_next()`), `vocard-src/voicelink/ipc/methods.py`
(`_resolve_guild_for_user`, `_top_tracks_from_counts`, `getGuildHistory`,
`getGuildTopTracks`, registered in `METHODS`), `dashboard/overrides/templates/index.html`
(new override - previously unmodified stock file; splits `#history-tracks`/
`#global-top-tracks` each into bot/uploaded pairs), `dashboard/overrides/assets/js/objects.js`
(`initUser` now requests `getGuildHistory` instead of reading
`data.data.history`; new `methods.getGuildHistory`/`methods.getGuildTopTracks`
handlers), `dashboard/overrides/assets/js/action.js` (`buildExplorePage` now
sends `getGuildTopTracks` instead of the hardcoded YouTube Music playlist
query), `docker-compose.yml` (new bind mount for the `index.html` override).

**Known limitation:** only tracks played going forward are counted - this
doesn't backfill history from anything played before this patch shipped,
same precedent as Patch 7's title-sidecar fix.

---

## Patch 10 — Per-track loudness normalization (no song is noticeably louder/quieter than another)

**What:** every track that gets disk-cached via yt-dlp (Patch 1's
`stream_cache.py` - YouTube-sourced tracks only) is now also measured for
integrated loudness, once, via a single analysis-only `ffmpeg loudnorm`
pass over the downloaded file (nothing is re-encoded; the audio file on
disk is never modified). The resulting gain - a linear multiplier that
would bring that track to -14 LUFS (roughly what YouTube/Spotify themselves
target), clamped to +/-8dB so one wildly-mismeasured track can never be
boosted or cut to an extreme - is applied automatically as Lavalink's native
`"volume"` filter (a flat gain, distinct from the player-level
`updateVolume` the dashboard's volume slider controls) every time that
track starts playing. Per the user's explicit choice, this normalizes in
*both* directions - quiet songs get turned up, loud songs get turned down -
not attenuation-only.

**Why:** requested - "i also need a volumn equalizer, so no song will be
ergonormously louder than the other, without affecting the song quality."
A flat gain multiplier changes only volume, never frequency content/EQ, so
it doesn't touch "quality" in that sense; the +/-8dB clamp exists so an
outlier measurement can't introduce the one real risk of boosting (audible
noise-floor amplification) by more than a modest, bounded amount.

**Not exposed as a user-togglable Effect** - it's automatic, not registered
in `Filters.get_available_filters()`, and deliberately kept out of
`player.filters`/the dashboard's Effects panel entirely (tracked instead as
a plain `Player._loudness_gain` float) so hitting "reset all filters" clears
a user's own Effects without also silencing normalization, and so the
Effects UI never shows a "volume" toggle nobody asked for.

**Scope limitations (same cold-start/source tradeoffs as the disk cache
itself):**
- Only ever known for a track that's already been yt-dlp-downloaded and
  measured - a brand-new video's very first play has nothing measured yet
  and plays at its original volume; later replays are normalized.
- Non-YouTube sources (Spotify/SoundCloud/a Discord attachment) are never
  disk-cached at all (by design, since Patch 1), so they're never measured
  and always play at their original volume.
- The ~130 files already on disk from before this patch don't retroactively
  gain a measurement - they'll get one the next time each is re-downloaded
  (i.e. practically never, since successful cache entries have no TTL - see
  Patch 1's module docstring). Same accepted limitation as Patch 7's title-
  sidecar fix.
- Requires `ffmpeg` in the image (added to the Dockerfile's runtime stage -
  the slim Python base doesn't include it, and yt-dlp itself didn't
  previously need it since no postprocessing/extraction happens). If
  `ffmpeg` is ever missing/fails, measurement just silently returns
  "unmeasured" - never fails or blocks the download that already succeeded.

**Why filters needed a new `_filters_payload_with_gain()` helper:** Lavalink
filters are a property of the player session, not the track, and a filters
PATCH is a partial merge-update - `play()` previously never touched
`"filters"` at all, so without explicitly resetting `"volume"` on every
`play()` call, a boosted/cut gain from one track would silently keep
applying to the next track that was never measured. `add_filter`/
`remove_filter`/`reset_filter` (the user's own Effects controls) were also
switched to this helper, so toggling/resetting an Effect mid-track can't
accidentally wipe or leave behind a stale normalization gain either.

**Files:** `vocard-src/Dockerfile` (apt-installs `ffmpeg`),
`vocard-src/voicelink/stream_cache.py` (`_CacheEntry.gain`, `_measure_gain`,
`_write_meta_sidecar`/`_read_meta_sidecar` - generalized from the Patch 7
title-only sidecar to also hold `gain`, `get_gain`/module-level wrapper),
`vocard-src/voicelink/pool.py` (`Node.get_track_gain`, wraps
`stream_cache.get_gain` with a safe `1.0` default), `vocard-src/voicelink/player.py`
(`self._loudness_gain`, `_filters_payload_with_gain`, `play()` computes/
resets the gain and sends it in the same PATCH as `encodedTrack`).

**To reapply:** see the files above; the core idea is
`gain_db = clamp(-8, 8, -14 - measured_LUFS)`, `multiplier = 10**(gain_db/20)`,
sent as `{"filters": {"volume": multiplier, **other_active_effects}}` on
every `play()` call (always explicit, even when `1.0`/unchanged).

---

## Patch 11 — Wait for a full pre-cache download before playing, even the first song

**What:** `resolve_direct_track()` (play()'s on-demand/cache-miss path) used
to fall straight back to invidious-companion's raw CDN url on a disk-cache
miss, never starting a download itself - by design, to avoid racing an
in-progress download against immediate playback (risked tripping Lavalink's
own HTTP read timeout mid-stream; see Patch 1/docs/direct-url-resolver.md).
On a cache miss, it now instead downloads the video via yt-dlp and *waits*
for it to finish completely before telling Lavalink anything, then plays
from that finished local file - including for the very first song of a
session, which previously could only ever stream live through
invidious-companion. Only falls back to streaming if the download itself
fails (blocked, unavailable, timed out).

**Why:** requested - "i want all the song to be pre-cached even the first
song that play, i can happily wait the song to be pre-cached before playing
to prevent lag." An explicit, informed latency-for-reliability tradeoff:
every play now costs however long the download takes (typically single-digit
to tens of seconds) up front, in exchange for always playing from a
complete local file instead of a live, potentially-throttled stream.

**Why this is safe now, when it wasn't before:** the original no-head-start
race was specifically about handing Lavalink a *partially*-downloaded file
and having it try to stream from a growing file concurrently with the
download. Waiting for the download to finish *before* Lavalink is told
anything at all removes that race entirely - there's nothing in flight for
playback to "outrun" once the file is already complete.

**Side benefit:** since `StreamCache._measure_gain` (Patch 10) runs as part
of the same download this now waits for, the very first play of a session
also gets loudness-normalized immediately, not just later replays - closing
that specific limitation from Patch 10 for YouTube tracks.

**Files:** `vocard-src/voicelink/pool.py` (`Node.resolve_direct_track` now
takes a `title` param and attempts+awaits `stream_cache.prefetch_to_disk`
on a cache miss before falling back to the companion/lock path),
`vocard-src/voicelink/player.py` (`play()` passes `track.title` through;
updated comments reflecting that the fire-and-forget backfill call is now
only a safety net for the `_direct_resolve_cache`-TTL-hit path, not the
primary way a track gets cached).

**To reapply:** in `resolve_direct_track`, between the disk-cache check and
the companion-resolver lock block, add a `try`/`except` that calls
`await stream_cache.prefetch_to_disk(video_id, f"https://www.youtube.com/watch?v={video_id}", title)`
and, if it returns truthy, re-checks `get_local_url` and serves that;
any exception or a still-missing url falls through to the existing
companion path unchanged.

---

## Patch 12 — Fixed silent PlayerMethod drops for a member the bot hasn't cached yet (found while setting up staging)

**What:** `process_methods` (the IPC dispatcher for every `PlayerMethod` -
`initPlayer`, `addTracks`, `moveTrack`, etc.) resolved "which guild/member"
a request without an explicit `guildId` belongs to via `bot.get_user()` +
`user.mutual_guilds` + `guild.get_member()` - three pure in-memory cache
reads. `main.py` deliberately sets `chunk_guilds_at_startup=False` (avoids
the cost of eagerly pulling every member of every guild on a bot that may
be in many servers), so that cache only fills in opportunistically as
discord.py happens to observe a user through some other gateway event
(a message, a command, etc.) - for a *newly*-invited bot and a member who
hasn't yet triggered one of those, it may simply never have happened.
Without an API fallback, every single `PlayerMethod` call for that member
silently dropped - no error, no log, indistinguishable from "not in a voice
channel" - until they happened to trigger some other cached event by luck.

**Why/how found:** hit this setting up the staging bot (see below) - a
freshly-invited, freshly-authorized member couldn't add songs or do
anything else from the dashboard, with zero evidence of a cause anywhere
in the logs. Diagnosed by adding temporary debug logging at each resolution
step (now removed except for the final "dropping request" case) and
confirming `bot.get_user()` genuinely returned `None` for an authorized,
in-voice-channel member.

**Fix:** falls back to `bot.fetch_user()`/`guild.fetch_member()` (real
REST API calls, bypassing the cache entirely) whenever the cache misses -
costs nothing extra for the common already-cached case, and makes a brand
new member usable immediately regardless of chunking state. Also switched
from iterating `user.mutual_guilds` (itself dependent on the same
potentially-stale cache) to iterating `bot.guilds` directly.

**Also hardened (same investigation, same file):** `connect_channel()` had
a bare `except: return` silently swallowing *any* voice-channel-join
failure (missing Connect/Speak permission, wrong region, a Lavalink hiccup)
with zero logging - indistinguishable from the bug above from every log.
Now logs the real exception at debug level before still returning `None`
the same way.

**Files:** `vocard-src/voicelink/ipc/methods.py` (`process_methods`'s
member/guild resolution block, `connect_channel`).

---

## Patch 13 — Now Playing shows a caching indicator/progress bar while the current track downloads

**What:** Patch 11 made `resolve_direct_track()` wait for a full yt-dlp
download on a disk-cache miss before playing - including for the very
first song of a session - which previously gave no feedback at all for
however long that download took; the dashboard just looked frozen. Now:
- The Now Playing panel shows the loading track's title/artwork
  immediately (as soon as `trackLoading` arrives), instead of leaving the
  *previous* track's info sitting there until the eventual `trackUpdate`.
- A thin progress bar (reusing the exact same look as the upcoming-queue
  prefetch bar) appears under the title while the download is actually in
  flight, filling left-to-right with real progress - the same
  `prefetchStatus` mechanism already used for upcoming tracks, just keyed
  by the *current* track's id now too.

**Why:** requested - "at least add the now caching song into the now
playing and show the caching bar so the user will know the song is being
cache[d]."

**Files:** `vocard-src/voicelink/player.py` (`_resolve_and_notify_current_track`,
mirrors the existing `_prefetch_and_notify`; `play()` now calls it instead
of `Node.resolve_direct_track` directly), `dashboard/overrides/templates/queue.html`
(new `#now-playing-prefetch-bar` container), `dashboard/overrides/assets/js/objects.js`
(`trackLoading` now decodes and shows the loading track's info eagerly and
tracks `loadingTrackId`; `prefetchStatus`/`trackUpdate` now also drive the
new `updateNowPlayingPrefetchBar()`).

**To reapply:** the core idea is the exact same loading/progress/ready-or-
unavailable `prefetchStatus` send sequence `_prefetch_and_notify` already
used for upcoming tracks, just wrapped around `resolve_direct_track()`
instead of `prefetch_direct_track()`, and a frontend handler that renders
it against `#now-playing-prefetch-bar` keyed by `loadingTrackId` instead of
a queue-row element keyed by queue position.

---

## Patch 14 — Pause/resume button responds instantly (optimistic UI update)

**What:** clicking play/pause used to wait for the full round trip
(browser → dashboard → bot → Lavalink → bot → dashboard → browser) before
the icon changed at all - visibly laggy (~1s). `togglePause()` now flips
`isPaused` and re-renders the icon/position-timer immediately on click,
*then* sends the request; the eventual `updatePause` response still sets
the authoritative state and shows the toast, so a rare disagreement (e.g.
someone else paused/resumed a moment earlier) self-corrects rather than
getting stuck wrong.

**Why:** requested - "i want no delay between the web ui button and the
bot, when i click i kinda have to wait 1 sec for the song to pause."

**Scope note (superseded by Patch 16 below):** this pass only covered
play/pause. Patch 16 extends the same idea to skip/back/repeat/volume too,
with a correctness fix this one didn't have yet - see that patch for why
play/pause alone wasn't actually safe without it.

**Files:** `dashboard/overrides/assets/js/objects.js` (`Player.togglePause`).

---

## Patch 15 — Queue cards show up immediately on add, not after caching starts

**What:** `addTrack`'s dashboard handler called `updateCurrentQueuePos()`
unconditionally the moment a track was added - which reads
`this.queue[this.currentQueuePosition]` to decide what's "now playing".
`currentQueuePosition` defaults to `0`, so the *very first* track(s) ever
added to an idle player (nothing confirmed playing yet) got immediately
misread as "now playing" before the backend had even started resolving
it - which hid it from the queue list (`updateQueueList()`'s slice starts
*after* whatever's "current") instead of showing it there right away like
any other newly-queued song, and only looked right again once
`trackLoading`/`trackUpdate` eventually caught up.

**Fix:** a new `player.hasActiveTrack` flag, true only once the backend
has actually told us there's a current track (`trackLoading`, confirmed
again by `trackUpdate`), false on `init()`/before anything's ever played.
`addTrack` now only calls `updateCurrentQueuePos()` when `hasActiveTrack`
is true; otherwise it just re-renders the queue list directly, so a
freshly-added card shows up immediately as a normal queued item. Once
`trackLoading` actually fires for it moments later, it correctly moves
into the Now Playing panel at exactly that point, not a moment earlier
than the backend actually started on it. `updateQueueList()`'s own
queue/history slicing is also now guarded by the same flag (starts at
index 0, not 1, when nothing's confirmed current yet).

**Why:** requested - "when i click on a song i want its card to be visible
on the queue immediately not after its done caching then it be shown on
the song queue list."

**Also fixed, same investigation (backend):** `Player.add_track()` only
triggered the background prefetch worker (`_prefetch_next_direct_track()`)
`if self.is_playing` - meaning adding several songs at once to an *idle*
player (a playlist import, or just queuing up a few tracks before
anything's played) left every track after the one `do_next()` immediately
consumes with zero caching indicator at all, until that first track's own
`TrackStartEvent` eventually fired - which Patch 11 can now delay by
however long that first track's own pre-cache download takes. Now
unconditional: the worker only ever walks tracks *after* the current
position (`queue.tracks(incTrack=False)`), so it can't collide with
whatever `do_next()`/`play()` is doing for the track about to become
current - worst case (nothing was playing, so this is also the very first
track) it briefly races `StreamCache`'s own per-video download dedup,
which already collapses to one shared download, never a real duplicate.

**Files:** `dashboard/overrides/assets/js/objects.js` (`hasActiveTrack`,
`addTrack`, `updateQueueList`, `trackLoading`, `trackUpdate`),
`vocard-src/voicelink/player.py` (`add_track`'s prefetch trigger,
unconditional now).

---

## Patch 16 — All playback buttons respond instantly, correctly this time

**What:** extends Patch 14's optimistic-UI idea from just play/pause to
skip (next), back (previous), repeat mode, and volume - and fixes a real
correctness gap Patch 14 shipped with.

**The bug Patch 14 had:** `updatePause` (and `skipTo`/`backTo`) aren't
simple DJ-only actions - they're *vote-gated* for anyone who isn't the DJ/
admin or the current track's own requester (`player.required()` votes
needed). Patch 14's optimistic flip ran unconditionally, so a non-DJ
listener in a multi-person channel clicking pause could see the icon flip
to "paused" immediately even though their click only registered one vote
of several needed - actively wrong, not just early, since the bot wouldn't
actually be paused yet.

**The fix:** a new `Player.canActImmediately()` helper, true exactly when
methods.py's own bypass condition is true - `isDJ` (mirrors
`is_privileged`) OR being the current track's own requester - and every
optimistic update below checks it first:
- **togglePause**: now gated on `canActImmediately()` (fixes the bug above).
- **skipTo/backTo**: for the plain "skip/back one" click (not a
  context-menu jump to an arbitrary queue position - that still waits for
  the real response, to avoid guessing unfamiliar index math), shows the
  known next/previous track's info immediately, gated the same way.
- **repeatMode**: `repeatTrack` is `@require_permission()`'d (a flat DJ/
  admin check, no vote fallback), so this is gated on `isDJ` alone. Cycles
  the displayed mode client-side using the exact same order as
  `queue.py`'s `Repeat.next()` (OFF -> TRACK -> QUEUE -> OFF).
- **volume**: `updateVolume` has no gating at all (open to every member -
  Patch 8), so it's always safe; now also updates `player.volume` itself
  immediately on release, not just the slider's own live-drag position
  (which was already instant).

In every case, the eventual real IPC response still sets the authoritative
state and shows any toast/error, so a wrong guess (including "it was
actually just a vote") self-corrects rather than getting stuck wrong.

**Why:** requested - "yeah i want all the button to be instant that
include the volumn too."

**Files:** `dashboard/overrides/assets/js/objects.js`
(`Player.canActImmediately`, `togglePause`, `skipTo`, `backTo`,
`repeatMode`), `dashboard/overrides/assets/js/action.js` (volume slider's
`change` handler).

---

## Patch 17 — "Recently added" history (separate from "Recently played")

**What:** a new Home-page section logging every track the moment it's
*added* to a guild's queue, regardless of whether it ever actually plays -
a broader, separate list from Patch 9's "Recently played" (which only
logs a track once it starts playing, so a skipped/removed-before-its-turn
track never shows up there at all). Capped at 100 entries per guild (a bit
more headroom than play history's 60, since additions can churn faster
than actual plays - e.g. a big playlist import), not split into bot/
upload buckets like Patch 9 - just one simple list regardless of source.

**Why:** requested - "i think we should have a history of list of song
that has been added" - confirmed as a deliberately separate list from
"Recently played", not an extension of it.

**Files:** `vocard-src/voicelink/player.py`
(`_record_guild_added_history`, called from `add_track()`'s `finally`
block), `vocard-src/voicelink/ipc/methods.py` (`getGuildAddedHistory`,
reuses Patch 9's `_resolve_guild_for_user` helper), `dashboard/overrides/templates/index.html`
(new `#added-history-tracks` section), `dashboard/overrides/assets/js/objects.js`
(`methods.getGuildAddedHistory`, requested from `initUser`).

---

## Patch 18 — Queue/playback survives a bot restart

**What:** when the bot process restarts (a deploy, `docker compose
restart`, `docker stop`, even a local Ctrl+C), it now automatically saves
every guild's current track + upcoming queue + repeat mode + autoplay
setting right before exiting, and automatically rejoins the voice channel
and resumes playing all of it on the next startup - restarting the
current track from the beginning (not the exact second it left off,
though it does also attempt a `seek()` back to the saved position once
playback resumes).

**Why:** requested - "the feature for when any restart we wont lost the
list of song currently playing" - confirmed as "auto-rejoin and resume"
rather than just reloading the queue data for someone to resume manually.

**This is almost entirely a bug-fix + wiring patch, not new
infrastructure** - Vocard already shipped a complete version of this
mechanism (JSON file `last-session.json`, `Track`/queue reconstruction,
voice reconnect), it just had three problems that meant it never actually
ran during a normal deploy:

1. **The save only ever happened from a manual "Stop-Bot" debug-panel
   button** (`voicelink/views/debug.py`) - never automatically on a real
   process shutdown, which is how every `docker compose restart`/redeploy
   actually ends this process (a plain SIGTERM, not a button click).
2. **That save had a backwards condition** - `if player.guild.me.voice: continue`
   skipped (did NOT save) exactly the players that WERE connected and
   playing, the opposite of "preserve what's currently playing."
3. **The restore side had an off-by-one** - `queue._position = saved_position - 1`
   made every restored queue start one track earlier than what was
   actually saved (`Player.data` saves the *exact* live `queue._position`,
   with no such offset).

**The fix:**
- The save logic moved to a new standalone module, `voicelink/session.py`'s
  `save_last_session()` (fixing the backwards condition on the way -
  saves whenever a player has a current track or a non-empty queue,
  regardless of voice-state) - a separate module specifically so both
  `cogs/listeners.py` (outside the `voicelink` package) and
  `voicelink/views/debug.py` (inside it, the original Stop-Bot button,
  now just calling this shared function) can import it without a
  circular import.
- `main.py`'s `Vocard.setup_hook()` now registers this function against
  **both SIGTERM and SIGINT** via `asyncio`'s `add_signal_handler` -
  neither signal does anything graceful by default (the process just
  dies), so without this, Docker's SIGTERM on `restart`/`stop` never gave
  the bot a chance to save anything at all, regardless of the button-only
  bug above.
- `cogs/listeners.py`'s `restore_last_session_players()` (already wired
  to run automatically on every startup, unchanged) got the off-by-one
  fix, plus a fallback to the bot's own member (`guild.me`) wherever it
  used to just give up on restoring a guild because the original DJ or a
  track's requester had since left the server.

**Must survive a container *recreate*, not just a restart:** `last-session.json`
lives at `/app/last-session.json` inside the container, which was not
bind-mounted to the host - meaning a save would succeed, then get
silently discarded the moment the container was recreated for a new image
(exactly the kind of gap that already bit `stream_cache`/logs earlier -
see Patch 7). Added `./last-session.json:/app/last-session.json` (and
`./last-session-staging.json:/app/last-session.json` for the staging
bot - each bot needs its *own* file, not a shared one, since restoring
reads purely by channel/guild id with no notion of which bot saved it) to
both compose files.

**Scope note:** this only triggers on a *graceful* shutdown (SIGTERM/
SIGINT reaching the process in time) - it does not protect against an
abrupt crash or `SIGKILL` (e.g. an OOM kill), which skips signal handling
entirely. That covers every normal deploy/restart in this project's actual
workflow; a continuous/periodic save (for crash resilience too) would be
a reasonable follow-up but wasn't asked for here and adds real write
volume for a case that didn't come up.

**Files:** `vocard-src/voicelink/session.py` (new), `vocard-src/main.py`
(`Vocard._handle_shutdown_signal`, registered in `setup_hook`),
`vocard-src/cogs/listeners.py` (`restore_last_session_players` fixes),
`vocard-src/voicelink/views/debug.py` (Stop-Bot button now calls the
shared function), `docker-compose.yml` / `docker-compose.staging.yml`
(new bind mounts).

---

## Patch 19 — Dashboard duplicate-add / wrong-track-removed bugs

User report: adding a first (uncached) song showed its caching card
correctly, but after the UI sat waiting through Patch 11's new blocking
pre-cache download, the same song appeared *twice* in the queue - and
removing what looked like the duplicate instead deleted a different,
unrelated song that had been added afterward. Three independent, compounding
bugs, all in the gap between "the UI felt unresponsive during a long cache
wait" (the thing Patches 13-16 were about) and the queue/removal code that
assumed quick, synchronous round-trips:

1. **A real double-queue, not just a rendering glitch.** Every "add to
   queue" click (search results, cards, context menu - `action.js`) is a
   plain, undebounced click handler with no instant feedback and nothing
   disabling the row while the request is in flight. With nothing visibly
   changing on click, a user waiting through a long first-play cache
   download can easily click again thinking it didn't register - and
   `add_track()`'s duplicate check is a no-op whenever a guild has
   "allow duplicate tracks" enabled, so the second click genuinely queues
   the same song a second time, server-side. Fixed with a tight,
   object-level debounce in `Player.send()` (objects.js): an identical
   `addTracks` payload (same tracks + type) sent again within 1.5s is
   silently dropped. Deliberately re-queuing the same song minutes later is
   never affected - this only catches an accidental back-to-back resend.

2. **`player.isPlaying` referenced the method itself, not its return
   value** (`objects.js`'s `addTrack` handler: `data.position >= 1 &&
   player.isPlaying` - missing `()`). A function reference is always
   truthy in JS, so this check always passed, regardless of whether
   anything was actually playing. That sent `addTrack`'s position-based
   `splice` path (meant only for inserting into an *already playing*
   queue at a precise relative position) during the window where a track
   is merely loading/caching - combined with `currentQueuePosition`
   staying stale during that same window (`trackLoading` never updates it;
   only `trackUpdate`/`initPlayer` do), producing an index that doesn't
   correspond to anything real. Fixed: `player.isPlaying()`.

3. **`removeTrack` trusted a stale index with no server-side check.** The
   dashboard's remove action sends a queue *index* captured at the moment
   the context menu was opened - a snapshot that goes stale if the queue
   changes before the click (another add/remove, or the current track
   advancing) lands. `voicelink/ipc/methods.py`'s `removeTrack` handed that
   index straight to `Player.remove_track()`, which trusted it
   unconditionally - so a stale index silently deleted whatever real track
   now sat at that position, which is exactly how removing a "duplicate"
   card deleted an unrelated song instead. Fixed: `remove_track()` now takes
   an optional `expected_track_id` (the context menu already captures this
   alongside the index); when the track actually at `index` doesn't match,
   it re-locates the real index by scanning the upcoming queue for that
   track id before removing, and does nothing (rather than guessing) if
   the id isn't found at all.

**Files:** `dashboard/overrides/assets/js/objects.js` (`isPlaying()` call
fix, `Player.send`/`_isDuplicateAddTracksSend`), `vocard-src/voicelink/
player.py` (`remove_track`'s `expected_track_id`), `vocard-src/voicelink/
ipc/methods.py` (`removeTrack` passes it through). Tests:
`tests/unit/test_player_remove_track.py` (new, 5 tests).

---

## Patch 20 — Restart-restore regression: wrong track resumed

Found while investigating the Patch 19 report (not itself reported by the
user, but a real bug surfaced during the same read-through): Patch 18's
restart-persistence restore could resume the *wrong* track after a
graceful restart - silently skipping whatever was actually playing at
shutdown and jumping to the next queued song instead, despite still
correctly seeking/pausing to match the skipped track's saved state.

Root cause: `Queue._position` always sits **one past** whatever track is
current - `Queue.get()` (called once per song, from `do_next()`) reads
`self._queue[self._position]` and then increments. Patch 18's restore
removed a `- 1` from `cogs/listeners.py`'s position restore, reasoning
(incorrectly) that `Queue.get()` reads `_queue[self._position]` as "the
next track", so restoring the exact saved value should be correct.

That missed that **restore resumes playback by calling `do_next()`**,
which performs the *exact same* `Queue.get()` call used during normal
playback. For a track that's actually mid-playback at save time,
`_position` has already been advanced past it once (by the original
`do_next()` call that started it) - restoring that same value and calling
`do_next()` again advances a *second* time, landing on the track *after*
the one that was interrupted. The old `- 1` was in fact necessary to
cancel out `do_next()`'s own advance and land back on the right track; it
only looked like a bug in isolation.

Fixed by moving the adjustment to the save side instead, where it's
unambiguous: `Player.data` (used by `save_last_session()`) now saves
`queue._position - 1` whenever there's a current track (`self._current`),
and the raw `queue._position` (always 0) when nothing has ever played yet
from this queue - that second case needs no adjustment since `do_next()`
is meant to start fresh from the first track, not resume anything.
`cogs/listeners.py`'s restore now just uses the saved value directly, with
a comment pointing at `Player.data` instead of re-deriving the reasoning.

**Files:** `vocard-src/voicelink/player.py` (`Player.data`'s
`queue.position`), `vocard-src/cogs/listeners.py` (comment only - no
behavior change on the restore side itself). Tests:
`tests/unit/test_player_data_snapshot.py` (new, 3 tests).

---

## Patch 21 — "Recently played by you": a separate personal history log

User: the Home page's "Recently played" (Patch 9) is guild-wide - every
member's plays - by design; they also wanted a second, personal log
showing just their *own* recent plays. The underlying data already
existed: `Player.do_next()` has always written every non-bot requester's
play to their own Mongo user document (`history`, capped at 25 - the same
field the `/history` Discord command already reads), it just was never
surfaced anywhere in the dashboard.

Added `getUserHistory` (`voicelink/ipc/methods.py`) - no guild resolution
needed, purely keyed by the viewing user's own id - and a new, initially
hidden "Recently played by you" section on the Home page, populated by a
new `getUserHistory` handler in `objects.js` (requested once from
`initUser`, alongside the existing `getGuildHistory`/`getGuildAddedHistory`
requests), shown only once it actually has at least one track.

**Files:** `vocard-src/voicelink/ipc/methods.py` (`getUserHistory`,
registered in `METHODS`), `dashboard/overrides/templates/index.html` (new
`#user-history-section`), `dashboard/overrides/assets/js/objects.js`
(`initUser`'s request, `getUserHistory` handler). Tests:
`tests/unit/test_ipc_user_history_method.py` (new, 3 tests).

**Not changed:** the Queue page's own "History" tab (`#history-queue`) -
that's a different concept (the *live, current session's* queue, letting
you skip back via `backTo`), not a persistent log, and repurposing it would
break that skip-back behavior. It only shows once `currentQueuePosition >=
1` (i.e. at least one track has actually finished/advanced past) - clicking
its toggle before that point does nothing, which is likely what "the
history queue is not showing" was observing on a fresh session, not a bug.
The two *persistent* guild-wide/personal logs live on the Home page instead
(Patch 9's "Recently played" and this patch's "Recently played by you").

---

## Patch 22 — Queue drag-to-reorder glitching during caching ticks

User: moving a song up/down in the queue kept getting interrupted/"refreshed"
whenever the caching bar or green checkmark updated on any track. Cause:
`prefetchStatus` (fired many times a second while a track downloads) called
the same full `updateQueueList()` that structural changes use -
`$("#queue").html(...)`, replacing every card's DOM node. That's exactly the
element Sortable.js (wired up once in `initPlayer`) tracks for drag-and-drop,
so a tick landing mid-drag (or just before/after one) tore down and
recreated the dragged element out from under the user.

Fixed by giving each queue card two small, stable, always-present slots
(`.prefetch-progress-slot`, `.prefetch-indicator-slot`) and having
`prefetchStatus` patch just those two slots on the one affected card
(`Player.updateQueueTrackIndicator`) instead of re-rendering the whole list.
`buildQueueTrackHtml`'s tick/spinner/progress-bar logic was pulled out into
a shared `buildPrefetchPiecesHtml` (action.js) so the full render and the
targeted patch can never drift out of sync. Structural re-renders
(`trackLoading`, `trackUpdate`, `addTrack` - a track actually moving between
sections) are unaffected and still do a full `updateQueueList()`, which is
fine since those are one-off events, not multiple-times-a-second ticks.

**Files:** `dashboard/overrides/assets/js/action.js`
(`buildPrefetchPiecesHtml`, `buildQueueTrackHtml`), `dashboard/overrides/
assets/js/objects.js` (`prefetchStatus` handler, new
`updateQueueTrackIndicator`).

---

## Patch 23 — Current song duplicated in queue after reconnect; hidden-tab disconnects; stale cached JS

User screenshot: the playing song appeared both under "Now playing" and as
the first "Next in queue" card. This was also the real cause of Patch 19's
"removing the duplicate removed a different song".

1. **`initPlayer` never set `hasActiveTrack`.** `player.init()` resets it to
   false, so after every page load or reconnect `updateQueueList()` started
   the queue at the current track instead of after it. That showed the
   playing song twice and shifted every queue card's index by one, so a
   remove on the "duplicate" card sent index 1 and the server removed the
   next real song. It looked intermittent because the next `trackUpdate`
   set the flag correctly again. Fixed: the bot's `initPlayer` now sends
   `hasCurrentTrack` and `loadingTrackId`, and `objects.js` sets
   `hasActiveTrack`/`loadingTrackId` before it re-renders.
2. **Backend: a song that was still caching counted as "nothing playing".**
   During Patch 11's blocking pre-cache, `Queue.get()` has already moved
   `_position` past the song but `_current` isn't set yet. So `initPlayer`
   pointed one song too far, and Patch 20's `Player.data` would skip that
   song on a restart. Fixed: new `Player._loading_track` (set around
   `play()` in `do_next()`) and a `has_dequeued_track` property, used by
   both `initPlayer` and `Player.data`.
3. **The dashboard disconnected hidden tabs on purpose.** The base
   dashboard's `websocket.js` closed the socket 30s after a tab was hidden.
   That caused the "Some errors occurred and the dashboard could not be
   connected" bar and the full reload/lag when the user came back. The
   dashboard server never times out an idle user socket, so the override
   (`dashboard/overrides/assets/js/websocket.js`, new compose mount):
   keeps hidden tabs connected; ignores late events from an old socket
   (they used to wipe player state after a newer socket had already
   re-synced); allows only one pending reconnect timer (`onerror` and
   `onclose` used to start two); also reconnects while the tab is hidden;
   reconnects right away when the tab becomes visible; and sends a
   heartbeat every 30s instead of 60s.
4. **Browsers kept old JS for up to 12 hours.** Static files are served
   with `cache-control: max-age=43200`, so a browser could keep running
   old code for hours after a deploy. `index.html` now adds
   `?v={{ asset_v }}` to every script URL. **Bump `asset_v` in index.html
   on every JS change.**

**Files:** `vocard-src/voicelink/player.py`, `vocard-src/voicelink/ipc/
methods.py` (`initPlayer`), `dashboard/overrides/assets/js/objects.js`
(`initPlayer` handler), `dashboard/overrides/assets/js/websocket.js` (new
override), `dashboard/overrides/templates/index.html` (`asset_v`),
`docker-compose.yml` (websocket.js mount, which needs `up -d`, not just
`restart`). Tests: 2 new in `tests/unit/test_player_data_snapshot.py`.

---

## Patch 24 — Pasted links search immediately

User asked why adding a YouTube playlist took a while. Measured: Lavalink
loads a 25-song mix in about 0.9s and a 120-song playlist in about 0.8s,
and the bot sends the queue update in the same second the add arrives.
Caching the first song starts only after the songs are already in the
queue. The real wait was the dashboard search box: `action.js` waited a
fixed 2s after the last keystroke before sending the search, even for a
pasted link. Pasted `http(s)://` links now search right away, and the
delay for typed keywords is 700ms instead of 2s. `asset_v` bumped to 24.

**Files:** `dashboard/overrides/assets/js/action.js`,
`dashboard/overrides/templates/index.html`.

---

## Patch 25 — Pause/seek delay (Lavalink buffer); restart-restore position and cleanup

User: pause and seeking took noticeable time to be heard.

1. **Lavalink `bufferDurationMs` was 3000** (`lavalink/application.yml`).
   That's how much encoded audio Lavalink keeps queued to send to Discord,
   so up to 3s of audio kept playing after every pause, seek, volume or
   filter change. It was raised earlier to cover stutter from streaming
   live off YouTube's CDN (see `vocard-src/docs/direct-url-resolver.md`).
   Since Patch 11 every song is fully downloaded and served locally (no
   streaming fallback in the last 24h, 392 songs cached), so it's back to
   Lavalink's default of **400**. Backup:
   `lavalink/application.yml.bak-buffer3000`. If stutter comes back, raise
   it gradually (e.g. 800) rather than straight back to 3000. The bot-side
   handling was never the slow part: logs show the bot confirms every
   pause/seek within the same second it arrives. The green popup only
   *reports* that confirmation; it doesn't delay anything.
2. **Seek bar moves instantly for DJs** (`objects.js` `seekTo`).
   `updatePosition` is DJ-only on the bot with no vote fallback, the same
   rule Patch 16 uses for pause.
3. **Restore lost the saved position.** Found by watching a real restore
   while restarting Lavalink for item 1: the song and the paused state came
   back correctly, but the position stayed at 0:00. `restore_last_session_
   players` seeked right after `do_next()`, and Lavalink drops a seek that
   arrives before the track has started. The song now *starts* at the
   saved position instead, via a one-shot `Player._resume_start_ms` that
   `do_next()` uses as `play(start=...)`. The local stream server supports
   HTTP Range (checked: 206), so starting mid-song is fast.
4. **Restore failed to clear the session file** (`OSError: [Errno 16]
   Device or resource busy`). It used `os.remove()`, but since Patch 18
   that file is a bind mount and can't be deleted. It's now overwritten
   with `[]` instead, so a later crash can't restore the same stale
   session again.

**Restart order used for a Lavalink config change:** stop both bots (the
SIGTERM handler saves each queue), restart `lavalink`, restart
`vocard-dashboard`, then start the bots (they restore and resume).
Restarting Lavalink under running bots would just drop their players.

**Files:** `lavalink/application.yml`, `dashboard/overrides/assets/js/
objects.js`, `dashboard/overrides/templates/index.html` (`asset_v` = 25),
`vocard-src/cogs/listeners.py`, `vocard-src/voicelink/player.py`.

---

## Patch 26 — Loudness backfill + peak cap; multi-dashboard sync fixes and test

User asked to re-check volume equalization and that actions stay in sync
across several people's dashboards in the same voice channel.

**Equalization (`voicelink/stream_cache.py`):**
- Only 75 of 217 cached files had a loudness measurement. The other 142
  were downloaded before Patch 10 and played at full volume, and live
  Lavalink logs showed 8 of the last 10 plays at a flat `volume: 1.0`.
  Since most music gets turned *down* (median gain 0.49, about -6dB), an
  unmeasured track sounded clearly louder than its neighbours.
  `StreamCache.start()` now runs `_backfill_gains()`: it measures every
  file without a gain, one at a time under `nice -n 15`, queued/upcoming
  tracks first, about 7s per file. On first deploy that was 146 files,
  about 17 minutes.
- Boosts are now capped so the measured true peak stays at or below -1
  dBTP (`_LOUDNORM_PEAK_CEILING_DBTP`). Lavalink's volume filter is a plain
  multiplier with no limiter, so an uncapped boost could clip. Cuts are
  unaffected. Sidecars record `gain_version: 2`; older boosted
  measurements (4 files) are re-measured by the backfill.

**Sync (routing was already right):** the dashboard server broadcasts
every guild message without a `userId` to all of that guild's users, and
every bot op has a dashboard handler. The handlers had bugs:
1. **`trackLoading` didn't carry or apply the queue position**, so for
   the whole caching window every dashboard showed the loading song in
   both Now Playing and "Next in queue", and every card index was off by
   one for remove/move/skip-to. The bot now sends `currentQueuePosition`
   (same value as `trackUpdate`) and the handler applies it. The bot also
   sets `_loading_track` *before* sending, so an `initPlayer` handled
   during that send already sees the track as current.
2. **Shuffle field-name mismatch:** the bot sends `queue_type` but the
   dashboard read `queueType`, so every queue shuffle was applied as a
   history shuffle (duplicating songs, wrecking indexes) and then threw
   on `capitalize(undefined)`.
3. **Bot bug, history shuffle deleted the current song:**
   `Queue.replace("history")` wrote back over `[:_position]` (which
   includes the current track) instead of `[:_position-1]` (what
   `history()` returns).
4. Volume: `initPlayer`/`updateVolume` now also set `player.volume`.

**Sync test (`dashboard/tests/run_sync_test.sh`):** `generate_sync_stream.py`
drives the *real* `Player`/`Queue`/`initPlayer` code (only Discord,
Lavalink and Mongo stubbed) through a scripted two-user session. The
session covers adds, add-next, remove, move, swap, both shuffles, both
clears, pause, volume, song changes, and actions plus a reconnect *while
a song is caching*. It records each broadcast with the bot's true state.
`sync_harness.js` replays that into two separate jsdom dashboards running
the real dashboard scripts (copied from the running container), each
joining at a different point, and checks queue order, now playing, the
rendered Next-in-queue and history cards, pause and volume after every
message. Result before the fixes: 94 failures; after: 0 of 53 checks
fail. Replaying with the old `trackLoading` format reproduces the
"loading song shown twice" bug, so the test does catch it.
Run: `dashboard/tests/run_sync_test.sh` (needs docker; installs jsdom and
jquery into `dashboard/tests/node_modules` on first run).

**Files:** `vocard-src/voicelink/stream_cache.py`, `vocard-src/voicelink/
player.py`, `vocard-src/voicelink/queue.py`, `dashboard/overrides/assets/
js/objects.js`, `dashboard/overrides/templates/index.html` (`asset_v` =
26), `dashboard/tests/*` (new). Tests: 6 new in `tests/unit/
test_stream_cache.py`, new `tests/unit/test_queue.py` (3).

---

## Patch 27 — Volume number on the slider; dashboard volume always matches the bot

User: wanted to see the number while moving the volume slider, and said
the dashboard's volume sometimes didn't match what the bot actually
played. Three real causes of a mismatch, found by comparing Lavalink's
live player volume with what the dashboards were told:

1. **Restart-restore never sent the volume to Lavalink.** The normal join
   path (`connect_channel`) calls `set_volume()`, but
   `restore_last_session_players` calls `channel.connect()` directly. After
   every restart Lavalink played at its default 100 while the bot and
   every dashboard showed the saved value (24 at the time; Lavalink's log
   showed no volume sent after the 15:19 restart). Fix: `play()` now
   restates `"volume": self._volume` in every track-start PATCH, so
   Lavalink always matches whatever path created the player.
2. **Only the dashboard saved the volume.** `/volume` and the Discord
   controller's +/- buttons changed it without saving, so the next
   restart reverted to the dashboard's last value. `Player.set_volume()`
   now saves it (in memory and in Mongo) for every caller, and the
   dashboard `updateVolume` no longer does its own write. The Discord mute
   button passes `persist=False` so unmute still restores the pre-mute
   level from `settings["volume"]`.
3. **Range mismatch:** `/volume` and the Discord buttons allow up to 150,
   but the slider's max was 100, so anything above 100 showed as 100. The
   slider now goes to 150, its fill is relative to that max, and the
   dashboard's `updateVolume` is clamped to 0–150 on the bot side.

UI (`controller.html`, `action.js`, `objects.js`): a `#volume-value`
number next to the slider updates live while dragging, and whenever
anyone changes the volume (`Player.updateVolumeView`). Incoming changes
are ignored while this user is mid-drag (`isAdjustingVolume`), so another
person's change can't yank the slider out from under them. Their release
sends the final value anyway.

Verified live after deploy: Lavalink at 30, dashboards told 30, restore
sent 30. The sync test (Patch 26) now also checks the shown number and the
slider position, including a value above 100.

**Files:** `vocard-src/voicelink/player.py`, `vocard-src/voicelink/ipc/
methods.py`, `vocard-src/voicelink/views/controller.py`, `dashboard/
overrides/templates/controller.html`, `dashboard/overrides/assets/js/
action.js`, `dashboard/overrides/assets/js/objects.js`, `index.html`
(`asset_v` = 27), `dashboard/tests/*`. Tests: new
`tests/unit/test_player_volume.py` (5).

---

## Patch 28 — One shared bot volume, 0–100

User clarified: each listener's Discord "User Volume" (right-click the bot)
is personal and client-side, and the bot never sees it. The bot's own
volume (the dashboard slider) is shared by everyone in the channel and
should be a conventional 0–100. Patch 27 had raised the slider to 150 to
match `/volume`; instead everything is now capped at 100 so no path can
set a value the slider can't show:
- `Player.set_volume()` clamps to 0–100. It's the single place every path
  goes through: dashboard, `/volume`, the Discord +/- and mute buttons.
- `Player.__init__` clamps a saved guild volume, in case one above 100 was
  saved before the cap.
- `/volume` is `Range[int, 1, 100]`; the Discord + button caps at
  `MAX_VOLUME` (100); the dashboard `updateVolume` clamps to 0–100.
- Slider `max="100"` again (the fill/number code from Patch 27 already
  reads the max from the element).
- Slash commands only re-register when `settings.json`'s `version`
  changes (`main.py`), so `version` was blanked in both `settings.json`
  and `settings.staging.json` to force one re-sync of `/volume`'s new
  range. Both bots wrote it back as `v2.7.3` after syncing.

**Files:** `vocard-src/voicelink/player.py`, `vocard-src/voicelink/views/
controller.py`, `vocard-src/cogs/settings.py`, `vocard-src/voicelink/ipc/
methods.py`, `dashboard/overrides/templates/controller.html`, `objects.js`
/`action.js` (comments), `index.html` (`asset_v` = 28), `dashboard/tests/*`.
Tests: `tests/unit/test_player_volume.py` (now 6).

---

## Patch 29 — Pages reload themselves instead of going half-empty; bot always reconnects to the dashboard

User: "the web seems broken", then asked for the page to hard-refresh and
pick up new code on its own when something changes or the bot reloads,
and said that after being out of focus a lot of things weren't showing.

**What actually broke (caused by Patch 28):** to re-register `/volume`,
`settings.json`'s `version` was blanked. But the IPC client sent *that
field* as `Client-Version`, and it connects to the dashboard before
`main.py` writes the version back. Both bots were rejected ("Access
forbidden: ... version mismatch"), the client never retried, and the
dashboard couldn't reach either bot until they were restarted. **Don't
blank `version` to force a command sync** (it's safe now, see below, but
the bot is off the dashboard for the 10s until it retries).

**Bot side (`voicelink/ipc/client.py`):**
- Sends the running code's version (`update.__version__`), not the
  settings.json marker.
- `_schedule_reconnect()` keeps retrying every 10s until connected, after
  any failed connect (refused, rejected handshake, error) or a dropped
  connection. Before: a failed first connect never retried, and a drop got
  one attempt 10s later, so a slower dashboard restart left the bot off
  the dashboard for good. Verified live: dashboard-only restart, both bots
  back 10s later.

**Page side:**
- Bot restarts: the dashboard server sends `closeConnection` to the
  bot's pages but never re-attaches them when the bot comes back (they sat
  half-empty until a manual refresh). The page now keeps sending
  `updateSelectedBot` every 3s (requests to an offline bot are silently
  dropped) and does a full reload the moment `initBot`/`initUser`/
  `initPlayer` arrives (`objects.js`: `closeConnection`,
  `waitForBotToReturn`, `handleMessage`).
- Lost connection (dashboard restart, network drop, browser-frozen tab):
  reconnecting reloads the page instead of patching state in place
  (`websocket.js` `onopen`, `hasConnected`).
- Back after 10+ minutes hidden: reloads (`HARD_RELOAD_AFTER_HIDDEN_MS`).
- New code always loads: `index.html`'s `asset_v` is now a fingerprint of
  the dashboard JS plus the base image id, set by
  **`dashboard/restart_dashboard.sh`**. Use that script for every
  dashboard restart: it sets the fingerprint, restarts (or recreates if
  mounts changed) and waits until healthy.

Tests: `tests/unit/test_ipc_client_reconnect.py` (5);
`dashboard/tests/reload_test.js` (7 checks: no reload on first connect,
reload on reconnect, waits for the bot then reloads, 2 min away no
reload, 11 min away reloads), now run by `dashboard/tests/
run_sync_test.sh`; `dashboard/tests/page_smoke.js` loads a rendered
`index.html` like a browser and reports any JS error (no errors found,
apart from the expected unauthenticated-WebSocket one).

**Files:** `vocard-src/voicelink/ipc/client.py`, `dashboard/overrides/
assets/js/websocket.js`, `dashboard/overrides/assets/js/objects.js`,
`dashboard/overrides/templates/index.html`, `dashboard/restart_dashboard.sh`
(new), `dashboard/tests/*`.

---

## Patch 30 — Secrets kept out of git and the Docker image; project on GitHub

The project is pushed to `git@github.com:noone11121314/vocard-custom.git`
(**public**). Nothing secret is tracked:

- **Ignored** (`.gitignore`): `settings.json`, `settings.staging.json`,
  `dashboard/settings.json`, `lavalink/application.yml` (+ `.bak*`; it holds
  a YouTube OAuth refresh token), `lavalink/cookies.txt` (YouTube login
  cookies), `.env`, plus runtime data (`stream_cache*`, `mongodb_data`,
  logs, Lavalink plugins, `last-session*.json`), `.venv`, `dashboard-src`
  (an upstream copy that isn't deployed) and test tooling output.
  `vocard-src/settings.json` is ignored by `vocard-src/.gitignore`.
- **Committed instead:** redacted templates `settings.example.json`,
  `settings.staging.example.json`, `dashboard/settings.example.json`,
  `lavalink/application.example.yml`, `.env.example`.
- **Compose secrets** (Lavalink password, invidious-companion secret,
  Mongo root user/password) moved from `docker-compose*.yml` into `.env`
  (mode 600), referenced as `${VAR}`. Compose reads it automatically;
  the resolved config hash was identical before and after, so the running
  stack is unchanged.
- **Docker image:** `vocard-src/settings.json` (holding the real
  production bot token) was being baked into `vocard-patched` by the
  Dockerfile's `COPY . .`. A new `vocard-src/.dockerignore` excludes it
  (and `.env`, logs, caches); verified the rebuilt image has no
  `settings.json` and no token. Running containers are unaffected because
  `settings.json` is bind-mounted. **Never push an image built before this
  patch.**
- Before the first push, every real secret value (27, read from the
  secret files without printing) was searched for in the staged files and
  none were found. This caught the invidious-companion secret pasted into
  `README.md` and `vocard-src/docs/testing.md` (now
  `$INVIDIOUS_COMPANION_SECRET`, loaded from `.env`). Re-run that kind of
  check before pushing anything that touches config or docs.
- Known weak defaults still in use (not secret, but worth changing if any
  of these ports are ever exposed): Lavalink `youshallnotpass`, and
  `admin` for the dashboard/IPC password and the MongoDB root login.

Commits use the GitHub no-reply address
(`noone11121314@users.noreply.github.com`); no global git config is set on
this machine.

---

## Patch 31 — Volume control design; dashboard JS updates without a restart

**Design** (`action.js`: `setupVolumeControl`, `updateVolumeIndicator`):
the Patch 27 number sat on its own, lower than the slider. It's now a
fixed-width rounded pill reading `NN%`, grouped with the slider in a
`.volume-control` flex row so they're centred together. It uses
`tabular-nums` so the width doesn't jump, fills with `--primary` while
dragging, and is muted at 0. The speaker icon next to it follows the level
(`volume_off` at 0, `volume_down` below 50, `volume_up` otherwise). It's
built from JS (DOM regroup plus an injected `<style>`), so no template
change was needed for it. Checked with real-browser screenshots (playwright
+ the live dashboard's CSS, `dashboard/tests/screenshot_controls.js`) at
0, 30 and 75 (dragging).

**JS goes live without a restart:**
- **The problem:** static JS is served with a 12h browser cache, and the
  page template is cached by the dashboard (`TEMPLATES_AUTO_RELOAD` off).
  So re-minifying in place wasn't enough: browsers kept the old JS until
  the dashboard restarted with a new `?v=`.
- **The fix:** `index.html` now loads the five scripts through a small
  inline loader. It sends a `HEAD` for each with `cache: "no-store"` and
  loads the script with its live `ETag` as `?v=`, in order (falls back to a
  timestamp). The fixed `asset_v` is gone.
- **`dashboard/apply_js.sh`:** syntax-checks the override JS and re-runs
  the dashboard's own minifier inside the running container
  (`import main, utils; utils.process_js_files()`; importing `utils` alone
  hits a circular import). Then it checks that every `.min.js` was rebuilt.
  New code reaches each browser on its next page load.
- **Still needs `dashboard/restart_dashboard.sh`:** template (`.html`) or
  compose changes. It no longer sets a fingerprint.
- This patch's template change needed one dashboard restart to switch
  pages to the loader; the bots reconnected on their own (Patch 29) and
  weren't touched.

**Files:** `dashboard/overrides/assets/js/action.js`, `objects.js`
(`updateVolumeView`), `dashboard/overrides/templates/index.html` (loader),
`dashboard/apply_js.sh` (new), `dashboard/restart_dashboard.sh`,
`dashboard/tests/screenshot_controls.js` (new).

---

## Patch 32 — A real DJ refused queue edits; settings changes ignored until restart

User: a friend who has the server's DJ role couldn't edit the queue etc.,
and asked whether DJ settings can apply without a manual restart.

**Findings:**
- The guild has a DJ role configured (`dj` = role "DJ", created 15:36),
  and Discord's API confirms the friend has it. In DJ-role
  mode `Player.is_privileged()` checks `manage_guild` or the role, so the
  bot's *cached* Member for the friend lacked the role. The dashboard
  showed `isDj: False` and every `clearQueue` got "Only the DJ or admins
  may use this function!". The server owner was unaffected (Manage
  Server). The exact reason the cache was stale couldn't be confirmed
  (the audit log isn't readable by the bot, and the running process
  can't be inspected).
- The "rate limited" page the friend saw was a side effect: about 25
  refused clicks in a few seconds plus page reloads exceeded the
  per-user IPC budget (100 credits / 5 min, `process_methods`).
- Separately, players hold a **deep copy** of the guild settings taken
  when the player is created. `/settings dj` (and every other
  `/settings` command, and the dashboard's `updateSettings`) only wrote
  Mongo, so a running player ignored the change until a restart.

**Fixes:**
- `methods._is_privileged_fresh()`: if the check fails, a DJ role is
  configured and the cached member lacks it, fetch the member from
  Discord (one REST call, only on a refusal), re-check, replace the stale
  cached member, and log `DJ check: cached roles ... were stale`. Used by
  every dashboard permission check (`require_permission`, `initPlayer`'s
  `isDj`, the vote-gated controls, the cache-manager check).
- `MongoDBHandler` now notifies registered listeners after each
  successful `update_settings()`. `pool.py` registers
  `NodePool.apply_settings_update`, which mirrors top-level `$set`/`$unset`
  keys into that guild's live `player.settings`. **Settings changes now
  apply to the running bot immediately, no restart.**

Deploying this needed one bot restart (core code). Nothing was playing at
the time. Tests: new `tests/unit/test_dj_permission_and_live_settings.py`
(7).

---

## Patch 33 — Gentler song caching: rate caps, one background download, low priority, faster loudness

User: caching new songs should spare the network so it doesn't hurt
streaming or lag other services on the machine.

**Before:** every yt-dlp download ran at full line speed with normal
priority (about 3.4MB in under 1.4s, a sharp burst per song), and the
loudness measurement after it used a full core for about 6–7s. That
measurement also ran *before* an uncached song was allowed to start: an
uncached song resumed after a restart took about 12s to start.

**Now** (`voicelink/stream_cache.py`):
- Background pre-caching (`Node.prefetch_direct_track`) is capped with
  `--limit-rate` at `STREAM_CACHE_PREFETCH_RATE` (default `1M`, about
  4–5s per song, still well ahead of playback). It runs one at a time
  across **all** guilds (`STREAM_CACHE_PREFETCH_CONCURRENCY`, default 1);
  before, it was one per player.
- A download someone is waiting on (`Node.resolve_direct_track`, Patch
  11's first-play wait) passes `urgent=True`, gets `STREAM_CACHE_ONDEMAND_
  RATE` (default `4M`) and skips the background queue. `0` means
  unlimited for either cap.
- yt-dlp and ffmpeg run under `nice -n 15` + `ionice -c2 -n7`, so other
  services always get CPU and disk first.
- Loudness is measured with ffmpeg `ebur128=peak=true` instead of
  `loudnorm`. Same integrated loudness and true peak (checked on 5 cached
  songs, within 0.04 dB), about 4.5× faster (about 1.5s vs 6–7s), because
  `loudnorm` upsamples to 192kHz internally.
- Live check, background settings in the bot container: the same song
  took 5.5s at about 1 MB/s instead of a 1.4s burst.
- Playback itself is served from local disk, so it never competed with
  downloads for the connection.

**Deploying without interrupting anyone:** `deploy_when_idle.sh` waits
until Lavalink reports no playing players on two checks 60s apart, then
deploys the already-built image (staging, then production). It was used
for this patch's second half, the `ebur128` switch; the rate caps went out
with an earlier restart.

Tests: 6 new in `tests/unit/test_stream_cache.py`; the measurement tests
now feed real `ebur128` summary output.

---

## Patch 34 — Integration/fuzz/browser test suites, and the bugs they found

User asked for more tests, especially integration tests, so future changes
break less. New suites (details in `vocard-src/docs/testing.md`, run all
with `./run_tests.sh`): real-component integration tests for the stream
cache + ffmpeg, the dashboard connection, MongoDB and restart-restore; a
randomized two-dashboard sync fuzz (`dashboard/tests/fuzz_sync.sh`); a
real-Chromium page check of the minified JS (`dashboard/tests/browser_check.sh`).
Each new test was checked to fail with the bug it targets put back.

**Bugs found and fixed by them:**
- **`Queue.remove()`/`move()` removed or moved the wrong copy of a song.**
  They used `list.remove(track)`, and `Track.__eq__` compares `track_id`,
  so with the same song queued twice (or in the history and queued again)
  the *first* copy went: possibly one in the history, or the one playing.
  Both now work by position.
- **Dashboard positions were off by one whenever nothing was current**
  (queue finished). The bot's positions are relative to its "current" slot.
  New `Player.serverBase()` in `objects.js` is used by addTrack (incl.
  "play next" while idle, which was appended at the end), move, swap, both
  shuffles and clear-queue. That was 15 of the 29 failing fuzz sessions.
- **Skipping a paused song** un-pauses it on the bot, but dashboards kept
  showing "paused" until the next song finished loading. `trackLoading`
  now carries `isPaused`.
- **Clearing the history while a song was still caching deleted that
  song** (`history_clear(is_playing)`; now `has_dequeued_track`).
- **"Play next" with several songs reversed their order**, and the
  dashboard appended them at the end. Order is kept (`put_at_index`) and
  `addTrack` reports `position: 1`.
- **Dashboard connection:** `disconnect()` left the websocket and HTTP
  session open, and the listener treated its own cancellation as a drop
  and reconnected right away (`_closing` flag, `CancelledError` re-raised).
- `StreamCache.stop()` now awaits the cancelled loudness backfill.

Fuzz before the fixes: 29 of 30 random sessions desynced; after: 100 of
100 (80 steps each) in sync.

---

## Patch 35 — A song that failed to cache played much louder than the rest

User: the current song was louder than everything else. It was the song
whose download YouTube randomly refused (HTTP 403) earlier. The failed
download blocked retries for 30 minutes (`_FAILED_RETRY_COOLDOWN`), so when
it came up it was **streamed** from YouTube, unmeasured, at full volume
(gain 1.0), while measured songs are mostly turned down about 6–8 dB.

- **Retry on 403:** a 403 is retried after 2s and 5s before giving up
  (`_RETRY_DELAYS_403`). The same video downloaded fine on 7 retries
  right after the failure.
- **Urgent downloads ignore the cooldown:** it now only blocks
  *background* retries. A song someone is waiting for gets a fresh attempt.
- **Better default for unmeasured songs:** if a song still has to be
  streamed, `Node.get_track_gain` uses the cache's median measured gain
  (`StreamCache.typical_gain`) instead of 1.0. Full volume only if nothing
  has been measured at all.

Deployed with `deploy_when_idle.sh` (nothing was playing). Tests: 6 new in
`test_stream_cache.py`, 3 in `test_node_direct_resolve.py`.

---

## Patch 36 — Why some songs wouldn't download, and minimizing streaming

User asked why some songs refuse to download and to minimize streaming.
Lavalink's logs (where every load records cache vs. stream): 2026-10-18 had
413 loads from the cache and 17 streamed from YouTube (about 4%); 10-11 had
53 streamed of 184. A streamed song skips loudness normalization and can
stutter.

**Two causes, confirmed by experiment** (20 cached songs, yt-dlp `--test`
in throwaway containers):
1. **No JavaScript runtime in the image.** yt-dlp now needs one (deno by
   default) plus its challenge solver (`yt-dlp-ejs`) for YouTube. Without
   them every one of the 20 printed "No supported JavaScript runtime ...
   some formats may be missing": incomplete, unsolved links, the likely
   source of the random HTTP 403s.
2. **YouTube's bot check on this server's IP**: "Sign in to confirm you're
   not a bot", about 1 in 20 attempts, at random, with or without a JS
   runtime.

**Fixes:**
- `deno==2.9.7` and `yt-dlp-ejs==0.8.0` added to `requirements.txt`. The
  Dockerfile now also copies `/usr/local/bin/deno` from the build stage:
  pip puts the binary in `bin/`, and the multi-stage build only copied
  `site-packages`, so the first rebuild silently lacked it. An integration
  test now checks the image has deno and the solver.
- **Companion fallback into the cache** (`StreamCache._companion_attempt`,
  registered from `pool.py` via `set_url_resolver`). When yt-dlp fails
  (after the 403 retries), the cache fetches invidious-companion's direct
  link, which gets past the bot check, in 1 MiB Range requests, paced to
  the same speed cap, measures loudness and caches it like any other
  download. Streaming only happens if both routes fail. Chunking matters:
  one long request for these links is throttled to near playback speed
  (2.7MB in 114s), Range requests come at full speed (under a second).
- Live end-to-end check with the new image: 6 real songs downloaded and
  measured, 2 of them with yt-dlp forced to fail so the real companion
  route had to do it (2–5s each).

**Decision (user, 2026-10-18): no YouTube account cookies.** Passing
`lavalink/cookies.txt` to yt-dlp would cut the bot checks further but
risks that Google account being flagged for automated use; the companion
route covers those cases instead. See Patch 37 for how to monitor it.

Tests: 4 new integration tests (fallback with Range, server ignoring Range,
refused fallback leaves no files, speed cap honoured) plus the image check;
4 unit tests.

---

## Patch 37 — Route logging, persisted bot logs, and a cache report

User chose the companion route over YouTube cookies (see Patch 36) and
asked for everything to be logged and documented.

**`[CacheRoute]` log lines** (INFO/WARNING, so they survive if the `vocard`
log level is lowered from DEBUG later):

| Line | Meaning |
|---|---|
| `cached <id> via yt-dlp` | normal download |
| `yt-dlp could not download <id> (<reason>) - trying invidious-companion` | yt-dlp blocked; reason is e.g. `YouTube bot check`, `HTTP 403` |
| `cached <id> via invidious-companion` | the fallback worked |
| `could not cache <id> by either route: ...` (WARNING) | both routes failed |
| `could not cache <id>: yt-dlp <reason>` (WARNING) | failed with no fallback available |
| `streaming <id> from YouTube` (WARNING) | last resort: played uncached and unmeasured |

**Production bot logs are now kept:** `./logs:/app/logs` was added to
`docker-compose.yml` (staging already had `./logs-staging`). Before this,
production's `vocard.log` lived only inside the container and was lost on
every redeploy. That's why earlier diagnoses kept running out of history.
The log rotates daily and keeps 30 days (`settings.json` `logging`). The
`vocard` level is still DEBUG, about 12MB/day, so roughly 360MB at most.

**`./cache_report.sh [days]`:** per day, how many songs played from the
cache vs. streamed (from Lavalink's logs, which already go back weeks),
and which route new downloads took (from the `[CacheRoute]` lines).
Baseline when this shipped: streamed 56% (10-08), 28% (10-11), 3%
(10-18, before Patch 36's fallback).

**Files:** `vocard-src/voicelink/stream_cache.py` (`_short_reason`, route
logs), `vocard-src/voicelink/pool.py` (streaming log), `docker-compose.yml`,
`cache_report.sh` (new). Tests: 4 unit (reason classification, each route's
log line), integration fallback test now checks the route log too.

---

## Patch 38 — History button did nothing on a new server, empty after the queue finished

User: the queue panel's History toggle didn't work after joining a new
server. Reproduced in real Chromium (`dashboard/tests/history_toggle_check.js`:
real page, fake websocket playing the bot, real clicks). Three bugs:

1. **Did nothing until a song had played:** the toggle only opened when
   `currentQueuePosition >= 1`, with no feedback. It now always toggles,
   and an empty history shows "No songs played in this server yet."
2. **Empty after the queue finished:** `updateQueueList()` rendered no
   history whenever no song was current (`historyEnd = hasActiveTrack ?
   cqp : 0`, an unintended side effect of Patch 15). It now always renders
   everything before `currentQueuePosition`. When idle that's every song
   already played; "back to" indexes still line up, since the bot's
   `backto(n)` counts back from the same position.
3. **Snapped shut on the next update:** `updateCurrentQueuePos()` faded the
   History panel out whenever `currentQueuePosition < 1`, so on a new server
   it closed as soon as anything changed (e.g. a song was added). Removed.

Also: the "Next in queue" section's show/hide check was off by one when
nothing was current; it now uses the new `Player.upcomingStart()`, and so
does `updateQueueList()`.

Shipped with `dashboard/apply_js.sh`, so nothing was restarted. Tests: the
History check runs in `dashboard/tests/browser_check.sh` (3 scenarios,
including staying open after an update), and the sync harness now checks
the history cards when no song is current too (60-session fuzz in sync).

---

## Patch 39 — The current song shows at the top of the History list

User asked for the currently playing song to be in the history too. The
current (playing or caching) song is now the first History card, with its
description reading "**Now playing** · artist" and an equalizer icon in
the indicator slot (`buildQueueTrackHtml(..., { nowPlaying: true })`,
`.history-now-playing`).

- It isn't part of the bot's history, so it's excluded from the "back to"
  numbering (`.track:not(.history-now-playing)` in both click handlers).
  Clicking it doesn't restart the song, and its menu has no "back to".
- **Gotcha:** the stock dashboard CSS sets `opacity: 0` on every `<span>`
  inside a queue card until hover (that's how it hides the "..." button).
  The first version's label and icon were invisible for that reason, and a
  third line (a badge) got clipped by the two-line `.track-info` block.
  Both elements set `opacity: 1` inline.
- Tests: `history_toggle_check.js` checks the card is first, its label is
  actually visible (computed opacity), clicking it sends no `backTo`, and
  clicking the most recent played song sends `backTo` 1. The sync harness
  checks every dashboard's now-playing card matches the bot's current song.

Shipped with `dashboard/apply_js.sh` (no restart).

---

## Patch 40 — Clicking a song in History re-adds it at the end of the queue

User: a song re-added from the History list should go to the bottom of the
queue. Before, clicking a History card ran "back to", which rewinds the queue
so that song plays immediately and everything after it replays.

- **Click a played song** -> `addTracks` with that song, `type:
  "addToQueue"`: it's appended at the end, with the usual "added" toast.
  Clicking the "Now playing" card (Patch 39) still does nothing.
- **Its ... menu** (`HISTORY_MENU`) offers Add to queue, Play next and
  Back to (the old rewind), plus the existing items.
- **Upstream bug fixed on the way:** menus passed `"AddToQueue"` but the
  item is keyed `addToQueue`, so "Add to queue" silently never appeared in
  any song menu (search results, track page, playlists). Fixed in all 8
  menu lists.
- **Duplicates (bot, `Player.add_track`):** with "duplicate tracks" turned
  off, the check covered the *whole* queue including history, so a song
  that already played could never be queued again. It now checks only the
  current and upcoming songs (`queue.tracks(incTrack=has_dequeued_track)`).
  Also, `/settings duplicatetrack` treated an unset setting as "off" while
  the player treats it as "on", so its first use on a server did nothing;
  both now default to `True`.
- **Race introduced by Patch 31's script loader, found by this patch's
  browser test** (failed 2 of 5 runs): the loader loaded `action.js`, which
  starts the dashboard, *before* `transformer.js`, which decodes songs. A
  message that arrived in between lost its songs (e.g. the History showed 1
  of 3). `action.js` now loads last, as with the original static tags:
  0 failures in 10 runs.

Deploy: dashboard restart (template change), bot via `deploy_when_idle.sh`.
Tests: new `tests/unit/test_player_duplicate_tracks.py` (4; 2 fail on the
old code); `history_toggle_check.js` checks the click sends `addTracks` and
the menu has the three items; `browser_check.js` checks the new load order.

---

## Patch 41 — Shuffle button in the queue panel

User asked for a button to shuffle the current queue. The bottom player bar
already had one; the queue panel's "Next in queue" header now has a
**Shuffle** text button next to "Clear queue" (`queue.html`,
`#shuffle-queue-btn` -> `player.shuffle()`), matching that header's
text-button style. A shuffle icon was tried first, but the stock CSS hid it.

`Player.shuffle()` had an off-by-one: it wanted `length -
currentQueuePosition > 3`, i.e. 4 upcoming songs when nothing was current,
while the bot shuffles 3+. It now uses `upcomingStart()`. Fewer than 3
upcoming still shows the "not enough songs to shuffle" pop-up instead of
sending. Non-DJs still go through the bot's shuffle vote as before.

Tests: `dashboard/tests/queue_panel_check.js` (renamed from
`history_toggle_check.js`, setup shared via `openPage()`) adds three
real-Chromium cases: 4 upcoming sends `shuffleTrack`; 3 queued before
anything played sends it (the off-by-one); 2 upcoming sends nothing and
shows the pop-up. Deployed with `dashboard/restart_dashboard.sh` (template
change; music not affected).

---

## Staging environment

A second bot (`vocard-staging`, `docker-compose.staging.yml`) runs
alongside production so changes can be built, tested, and tried live in
Discord without ever restarting the production bot - see
`docs/staging-deployment.md` for the full protocol (one-time setup,
the build/test/try loop, and how to promote a validated change to
production). Both bots currently run from the exact same image (`vocard-patched:latest`
and `vocard-patched:staging` tagged to the same build) - keep them in sync
by building once and tagging both, as in that doc's promotion step,
whenever a change is confirmed good on staging.

---

## Quick verification after any redeploy

```bash
cd /root/vocard

# Patch 1: direct resolver active (no warnings = healthy)
docker logs vocard --since 1h | grep DirectResolver

# Patch 1: local caching proxy came up (logged once, at the first resolved track)
docker logs vocard --since 1h | grep StreamCache

# Patch 1: loadFailed retry code present
docker exec vocard grep -c "_retry_direct_resolve\|invalidate_direct_resolve_cache" voicelink/player.py voicelink/pool.py cogs/listeners.py

# Patch 2: updateVolume decorator
docker exec vocard grep -A1 "async def updateVolume" voicelink/ipc/methods.py

# Patch 3: button labels resolve correctly
docker exec vocard python3 -c "import json; b=json.load(open('/app/settings.json'))['default_controller']['buttons'][0]; print(b['skip']['label'])"
# should print: @@t_player.buttons.skip@@   (not @@t_buttonSkip@@)

# Patch 4: dashboard override mounted and stripped
docker exec vocard-dashboard grep -o "playPause" /app/static/js/action.min.js || echo "OK: no playPause references"

# Patch 5: loading/prefetch indicators present
docker exec vocard-dashboard grep -c "prefetchStatus\|trackLoading" /app/static/js/objects.min.js
docker exec vocard-dashboard grep -c "now-playing-loader" /app/templates/queue.html

# Patch 6: cache-size display present
docker exec vocard-dashboard grep -c "cacheStats\|updateCacheStatsView" /app/static/js/objects.min.js
docker exec vocard-dashboard grep -c "cache-stats-line" /app/templates/queue.html

# Patch 7: audio cache manager present
docker exec vocard-dashboard grep -c "cacheEntries" /app/static/js/objects.min.js
docker exec vocard-dashboard grep -c "getCacheEntries" /app/static/js/action.min.js
docker exec vocard-dashboard grep -c "cache-manager-link" /app/templates/queue.html
docker exec vocard grep -c "getCacheEntries\|deleteCacheEntry" voicelink/ipc/methods.py

# Patch 9: Home/Explore built from real per-guild history, not a hardcoded playlist
docker exec vocard grep -c "getGuildHistory\|getGuildTopTracks" voicelink/ipc/methods.py
docker exec vocard-dashboard grep -c "history-tracks-bot\|top-tracks-bot" /app/templates/index.html

# Patch 10: ffmpeg present, loudness-normalization code present
docker exec vocard ffmpeg -version
docker exec vocard grep -c "_measure_gain\|_loudness_gain" voicelink/stream_cache.py voicelink/player.py

# Patch 11: on-demand resolve waits for a pre-cache download
docker exec vocard grep -c "prefetch_to_disk" voicelink/pool.py

# Patch 12: fetch_user/fetch_member fallback present
docker exec vocard grep -c "fetch_member\|fetch_user" voicelink/ipc/methods.py

# Patch 13: now-playing caching bar present
docker exec vocard grep -c "_resolve_and_notify_current_track" voicelink/player.py
docker exec vocard-dashboard grep -c "updateNowPlayingPrefetchBar" /app/static/js/objects.min.js
docker exec vocard-dashboard grep -c "now-playing-prefetch-bar" /app/templates/queue.html

# Patch 14: optimistic pause toggle present (minified, no spaces around =/!)
docker exec vocard-dashboard grep -c "isPaused=!this.isPaused" /app/static/js/objects.min.js

# Patch 15: hasActiveTrack flag + unconditional prefetch trigger present
docker exec vocard-dashboard grep -c "hasActiveTrack" /app/static/js/objects.min.js
docker exec vocard grep -c "_prefetch_next_direct_track()" voicelink/player.py

# Patch 16: canActImmediately present
docker exec vocard-dashboard grep -c "canActImmediately" /app/static/js/objects.min.js

# Patch 17: added-history code + section present
docker exec vocard grep -c "_record_guild_added_history\|getGuildAddedHistory" voicelink/player.py voicelink/ipc/methods.py
docker exec vocard-dashboard grep -c "added-history-tracks" /app/templates/index.html

# Patch 18: signal handler registered, last-session.json bind-mounted
docker exec vocard grep -c "add_signal_handler" main.py
docker logs vocard --since 1h | grep -i "Saved last-session\|Restoring\|Received a shutdown signal"
cat /root/vocard/last-session.json   # survives a recreate - should exist with real content once something's playing during a restart

# Staging/production parity - both should print the same sha256
docker inspect --format='{{.Image}}' vocard vocard-staging
```
