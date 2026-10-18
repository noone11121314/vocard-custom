"""Local HTTP proxy serving fully-downloaded, disk-cached YouTube audio.

Local addition on top of stock Voicelink/Vocard, part of the direct-URL-resolver
feature - see docs/direct-url-resolver.md. Not part of upstream Vocard.

Earlier versions of this module background-downloaded the raw googlevideo.com
URL (resolved via invidious-companion) into an in-memory buffer and served
Lavalink from whatever was already buffered, racing the download. That worked
but had two sharp edges: no head start meant Lavalink could outrun the buffer
and hit a SocketTimeoutException (see docs/direct-url-resolver.md's "no-head-
start" production incident), and the 150MiB in-memory entry cap meant long
videos ran out mid-track.

This version uses `yt-dlp` to fully download a track's audio to a local disk
file *before* it's ever handed to Lavalink. `Node.prefetch_direct_track()`
awaits that download in the background while the previous track plays;
`Node.resolve_direct_track()` (play()'s on-demand path) only ever uses this
proxy if a download already finished - otherwise it falls straight back to
asking invidious-companion for the raw CDN url directly, exactly as if this
module didn't exist. That removes the racing-the-download failure mode
entirely: this proxy now only ever serves a complete, static file.

A finished download has no TTL - there's no correctness reason to ever
re-fetch a video once it's on disk, so it just stays there, saving a real
(possibly slow/throttled) download the next time it's queued again, for as
long as there's room under STREAM_CACHE_MAX_BYTES. When there isn't, _prune()
evicts the least-viewed entry (view_count, bumped on every reuse) that isn't
currently playing/queued in any connected player - see set_queued_checker().

yt-dlp does its own YouTube extraction here - it is not handed the url
invidious-companion would have resolved, it resolves the video itself. The
two resolvers are intentionally kept independent: invidious-companion is a
dedicated, actively-maintained bot-detection dodge and stays the only path for
anything that needs to play *right now* (no head start to risk on an
alternate extractor); yt-dlp only ever runs in the background, where a
failure (blocked, timed out, video unavailable) just means no disk cache
entry gets populated and the on-demand path is used instead - this can never
be the reason a track fails to play.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Optional
from urllib.parse import quote

from aiohttp import web

logger = logging.getLogger("vocard")

LISTEN_PORT: int = int(os.getenv("STREAM_CACHE_PORT", "2334"))
# What Lavalink should use to reach this proxy - the vocard container's
# service name on the shared docker-compose network by default.
PUBLIC_BASE_URL: str = os.getenv("STREAM_CACHE_BASE_URL", f"http://vocard:{LISTEN_PORT}").rstrip("/")
# Ephemeral, container-local scratch space - deliberately not a bind-mounted
# volume. Entries are meant to only ever live as long as this process does,
# same as the old in-memory buffer.
CACHE_DIR: Path = Path(os.getenv("STREAM_CACHE_DIR", "/tmp/vocard_stream_cache"))

# How long a *failed* download blocks a retry for the same video - not a
# cache expiry any more (successful entries have none, see module docstring).
# Keeps a persistently-broken video (blocked, region-locked, whatever) from
# getting hammered on every single prefetch attempt.
_FAILED_RETRY_COOLDOWN: int = 1800
# Total disk budget across all cached audio files, 5GB max. Disk is cheap
# relative to the old 300MiB in-memory cap, and a full track is tens of MB
# at most, so this comfortably holds a healthy number of recently-queued
# videos even with PREFETCH_LOOKAHEAD keeping several tracks warm at once.
_MAX_TOTAL_BYTES: int = int(os.getenv("STREAM_CACHE_MAX_BYTES", str(5_000_000_000)))
# Prefer opus/webm audio-only formats, matching direct_resolver._PREFERRED_ITAGS,
# falling back to yt-dlp's own best-audio selection if none of those exist for
# a given video.
_YTDLP_FORMAT: str = "251/250/249/140/bestaudio"

# Patch 33: go easy on the machine's connection and CPU. Every download used
# to run at full line speed with normal priority - a sharp burst per new song
# that competed with other services on this host. Background pre-caching
# (songs further down the queue) is capped low and runs one at a time across
# every guild; a download someone is actively waiting on (Patch 11's
# first-play wait) gets a higher cap so that wait stays short. yt-dlp
# --limit-rate syntax ("1M" = 1 MiB/s); "0" or "" means unlimited.
_BACKGROUND_RATE: str = os.environ.get("STREAM_CACHE_PREFETCH_RATE", "1M")
_URGENT_RATE: str = os.environ.get("STREAM_CACHE_ONDEMAND_RATE", "4M")
_BACKGROUND_CONCURRENCY: int = max(1, int(os.environ.get("STREAM_CACHE_PREFETCH_CONCURRENCY", "1")))


def _low_priority_prefix() -> list:
    """nice + best-effort/lowest ionice, when available - so downloads and
    loudness analysis always yield CPU and disk to everything else here."""
    prefix = []
    if shutil.which("nice"):
        prefix += ["nice", "-n", "15"]
    if shutil.which("ionice"):
        prefix += ["ionice", "-c2", "-n7"]
    return prefix


def _rate_args(rate: str) -> list:
    return ["--limit-rate", rate] if rate and rate != "0" else []
_YTDLP_TIMEOUT: float = 600.0  # hard cap on a single download; a stuck/throttled
# process just means this track falls back to the on-demand path instead of
# hanging the prefetch task forever.

# Patch 10: per-track loudness normalization. -14 LUFS matches roughly what
# YouTube/Spotify themselves target, so a normalized track doesn't sound
# noticeably quieter/louder than what people are used to elsewhere. The +/-8dB
# clamp exists so one wildly-mismeasured track (e.g. mostly silence, or a
# measurement glitch) can never be boosted or cut to an extreme - worst case
# it's left only partially corrected, never distorted or deafening.
_LOUDNORM_TARGET_LUFS: float = -14.0
_LOUDNORM_MAX_GAIN_DB: float = 8.0
# Patch 26: a boost is also capped so the track's measured true peak stays at
# or below this (Lavalink's volume filter is a plain multiplier with no
# limiter, so boosting past 0 dBFS clips audibly).
_LOUDNORM_PEAK_CEILING_DBTP: float = -1.0
# Sidecar marker for measurements that include the peak cap - older boosted
# measurements without it get re-measured by the startup backfill.
_GAIN_VERSION: int = 2
_LOUDNORM_TIMEOUT: float = 120.0  # analysis-only pass over an already-local
# file (no re-encode), so this is normally much faster than the download
# itself; a timeout/failure here just means the track plays unnormalized,
# same as if ffmpeg weren't installed at all - never blocks or fails the
# download that already succeeded.

# Prefix on a dedicated stdout line yt-dlp emits per progress update (via
# --progress-template/--newline below), used to pull live download progress
# for the dashboard's prefetch progress bar without parsing yt-dlp's normal
# human-readable, non-"download" chatter on the rest of stdout.
_PROGRESS_PREFIX: str = "VOCARD_PROGRESS "
_PROGRESS_TEMPLATE: str = (
    f"download:{_PROGRESS_PREFIX}%(progress.downloaded_bytes)s/"
    "%(progress.total_bytes,progress.total_bytes_estimate)s"
)


@dataclass
class _CacheEntry:
    path: Optional[Path] = None
    size: int = 0
    error: Optional[str] = None
    created_at: float = field(default_factory=time.monotonic)
    ready: asyncio.Event = field(default_factory=asyncio.Event)
    task: Optional[asyncio.Task] = None
    # Live download progress, updated while downloading - see _drain_progress.
    # total_bytes is None until yt-dlp reports a known/estimated size (should
    # be immediate for the preferred itags, but isn't guaranteed for every
    # possible fallback format).
    downloaded_bytes: int = 0
    total_bytes: Optional[int] = None
    # Bumped once on creation and again on every later reuse (download()
    # called again for a video already on disk) - a simple "how popular is
    # this" signal _prune() uses to pick what to evict first under space
    # pressure, instead of oldest-first.
    view_count: int = 0
    # Human-readable track title, for the dashboard's cache manager - purely
    # cosmetic, never used as a cache key (the video id still is). Best-
    # effort: whatever the caller passed at download() time, or "" if it
    # didn't have one handy.
    title: str = ""
    # Patch 10: linear gain multiplier (1.0 = unchanged) from a one-time
    # ffmpeg loudnorm measurement of this file, or None if never measured
    # (ffmpeg missing/failed/not yet run) - callers must treat None as "play
    # at the original volume," never as an error.
    gain: Optional[float] = None


# Set by pool.py once at import time (NodePool.is_video_queued) - lets
# _prune() avoid evicting a video that's current/upcoming in any connected
# player's queue, without this module importing voicelink.pool directly
# (pool.py already imports this module; the callback keeps the dependency
# one-directional and keeps this module trivially testable standalone).
# Defaults to "nothing is ever queued" until something registers a real one.
_is_queued_elsewhere: Callable[[str], bool] = lambda video_id: False


def set_queued_checker(checker: Callable[[str], bool]) -> None:
    global _is_queued_elsewhere
    _is_queued_elsewhere = checker


class StreamCache:
    """Disk-backed download cache, keyed by an arbitrary string - the resolver
    uses the YouTube video id, matching Node._direct_resolve_cache's own
    keying. Unlike that smaller metadata cache, entries here have no TTL -
    see the module docstring and _prune() for why and how eviction works
    instead.
    """

    def __init__(self) -> None:
        self._entries: Dict[str, _CacheEntry] = {}
        self._runner: Optional[web.AppRunner] = None
        self._backfill_task: Optional[asyncio.Task] = None
        self._background_slots = asyncio.Semaphore(_BACKGROUND_CONCURRENCY)

    async def start(self) -> None:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        self._adopt_existing_files()
        app = web.Application()
        app.router.add_route("GET", "/stream/{key}", self._handle)
        app.router.add_route("HEAD", "/stream/{key}", self._handle)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, "0.0.0.0", LISTEN_PORT)
        await site.start()
        logger.info(f"[StreamCache] listening on 0.0.0.0:{LISTEN_PORT}, cache dir {CACHE_DIR}")
        self._backfill_task = asyncio.create_task(self._backfill_gains())

    def _needs_gain_measurement(self, key: str, entry: _CacheEntry) -> bool:
        if not entry.ready.is_set() or not entry.path or not entry.path.exists():
            return False
        if entry.gain is None:
            return True
        # Boosted before the peak cap existed - may clip, re-measure.
        return entry.gain > 1.0 and self._read_meta_sidecar(key).get("gain_version") != _GAIN_VERSION

    async def _backfill_gains(self) -> None:
        """Patch 26: measures loudness for files cached before measurement
        existed (Patch 10) - they otherwise play at full, unadjusted volume
        next to normalized tracks (most music is turned *down* ~6dB, so an
        unmeasured track sounds markedly louder). One file at a time, at low
        CPU priority; queued/upcoming tracks first. Never raises.
        """
        try:
            pending = [k for k, e in list(self._entries.items()) if self._needs_gain_measurement(k, e)]
            if not pending:
                return
            logger.info(f"[StreamCache] measuring loudness for {len(pending)} previously cached file(s) in the background")
            done = 0
            while pending:
                pending.sort(key=lambda k: not _is_queued_elsewhere(k))
                key = pending.pop(0)
                entry = self._entries.get(key)
                if not entry or not self._needs_gain_measurement(key, entry):
                    continue
                gain = await self._measure_gain(entry.path, low_priority=True)
                if gain is not None and self._entries.get(key) is entry:
                    entry.gain = gain
                    self._write_meta_sidecar(key, title=entry.title, gain=gain)
                    done += 1
                await asyncio.sleep(0.5)
            logger.info(f"[StreamCache] loudness backfill finished: {done} file(s) measured")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.debug(f"[StreamCache] loudness backfill stopped: {e}")

    def _adopt_existing_files(self) -> None:
        """Rebuilds `_entries` from whatever's already sitting in CACHE_DIR
        on startup. The files themselves now survive a restart (STREAM_CACHE_DIR
        is a bind-mounted volume - see docs/local-patches.md's third Patch 7
        follow-up), but `_entries` - the in-memory catalog list_entries()/
        is_ready()/download() all actually read - does not; without this, a
        perfectly good cached file from a previous process lifetime is
        invisible to the dashboard's cache manager and to is_ready() (so
        resolve_direct_track() would never serve it, and download() would
        spawn a whole yt-dlp subprocess just to have it no-op "already
        downloaded") until that exact video happens to get queued again.

        view_count can't be recovered this way - it was only ever an
        in-memory popularity counter, not worth persisting on its own - so
        an adopted entry starts with view_count 1, self-healing the next
        time that video is actually reused. Title and loudness gain,
        however, are read back from the small `{key}.meta.json` sidecar
        _write_meta_sidecar() leaves next to a successfully downloaded file,
        if one exists - without it, a restart would otherwise permanently
        blank the cache manager's titles for every file already on disk
        (2026-10-11: this is exactly what was happening - see
        docs/local-patches.md's Patch 7, sixth follow-up). Both fall back to
        "" / None (unmeasured - plays unnormalized) for a file saved before
        this sidecar existed (or before gain measurement existed - Patch
        10), or if the sidecar's missing/corrupt for any other reason -
        never worth failing adoption over just a cosmetic title or a
        loudness tweak that'll simply be measured fresh next re-download.
        """
        for path in CACHE_DIR.glob("*.audio"):
            key = path.stem
            if key in self._entries:
                continue
            meta = self._read_meta_sidecar(key)
            entry = _CacheEntry(
                path=path,
                size=path.stat().st_size,
                view_count=1,
                title=meta.get("title") or "",
                gain=meta.get("gain"),
            )
            entry.ready.set()
            self._entries[key] = entry
        self._prune()

    async def stop(self) -> None:
        if self._backfill_task and not self._backfill_task.done():
            self._backfill_task.cancel()
        if self._runner:
            await self._runner.cleanup()

    @staticmethod
    def _meta_path(key: str) -> Path:
        return CACHE_DIR / f"{key}.meta.json"

    @staticmethod
    def _write_meta_sidecar(key: str, *, title: Optional[str] = None, gain: Optional[float] = None) -> None:
        """Persists `title`/`gain` next to a successfully downloaded
        `{key}.audio` so _adopt_existing_files() can recover them across a
        restart - the audio file alone only carries the video id, neither
        the title nor its measured loudness gain (Patch 10). Merges with
        whatever's already in the sidecar rather than overwriting it, so
        writing one field (e.g. a title backfilled on reuse, well after the
        original download already measured and wrote a gain) never erases
        the other. Omits a field entirely when it's None/empty rather than
        writing it as null - keeps the on-disk JSON (and existing tests
        asserting its exact content) unchanged for whichever fields were
        never provided. Never raises: a lost title or gain is purely
        cosmetic/best-effort, never worth failing or retrying a download over.
        """
        fields = {k: v for k, v in {"title": title, "gain": gain}.items() if v is not None and v != ""}
        if gain is not None:
            fields["gain_version"] = _GAIN_VERSION
        if not fields:
            return
        try:
            existing = StreamCache._read_meta_sidecar(key)
            existing.update(fields)
            StreamCache._meta_path(key).write_text(json.dumps(existing))
        except OSError as e:
            logger.debug(f"[StreamCache] failed to write meta sidecar for {key}: {e}")

    @staticmethod
    def _read_meta_sidecar(key: str) -> dict:
        try:
            data = json.loads(StreamCache._meta_path(key).read_text())
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def is_ready(self, key: str) -> bool:
        entry = self._entries.get(key)
        return bool(entry and entry.ready.is_set() and entry.error is None and entry.path and entry.path.exists())

    def local_url(self, key: str) -> str:
        return f"{PUBLIC_BASE_URL}/stream/{quote(key, safe='')}"

    def gain(self, key: str) -> Optional[float]:
        """Patch 10: `key`'s measured loudness-normalization gain, or None
        if it was never measured (not yet disk-cached, ffmpeg unavailable,
        or the measurement pass itself failed) - callers must treat None
        as "play at the original volume."
        """
        entry = self._entries.get(key)
        return entry.gain if entry else None

    def progress(self, key: str) -> Optional[float]:
        """Fraction (0.0-1.0) of `key`'s download completed so far, for the
        dashboard's prefetch progress bar. None if there's nothing in
        progress for this key, it's already finished (caller should be
        checking is_ready()/get_local_url() by then, not polling progress),
        or yt-dlp hasn't reported a total size yet (e.g. right at startup,
        or for a format whose size yt-dlp can't determine up front).
        """
        entry = self._entries.get(key)
        if entry is None or entry.ready.is_set() or not entry.total_bytes:
            return None
        return max(0.0, min(1.0, entry.downloaded_bytes / entry.total_bytes))

    def list_entries(self) -> list[dict]:
        """Snapshot of every cache entry for the dashboard's cache manager -
        see docs/local-patches.md. `queued` mirrors exactly what _prune()
        itself checks before evicting, so the UI can show *why* something
        survived (or would survive) an eviction pass.
        """
        out = []
        for key, e in self._entries.items():
            if not e.ready.is_set():
                status = "downloading"
            elif e.error is not None:
                status = "failed"
            else:
                status = "ready"
            out.append({
                "videoId": key,
                "title": e.title,
                "size": e.size,
                "viewCount": e.view_count,
                "status": status,
                "queued": _is_queued_elsewhere(key),
                "progress": self.progress(key) if status == "downloading" else None,
            })
        return out

    def delete_entry(self, key: str) -> bool:
        """Manually evicts `key` - an explicit admin action from the
        dashboard's cache manager, not automatic space-pressure eviction, so
        unlike _prune() this *doesn't* check _is_queued_elsewhere: the user's
        choice wins, and worst case is just a fallback resolve next time that
        video is needed, same zero-risk guarantee as everywhere else in this
        feature. Also how a stuck/failed entry gets manually cleared to force
        an immediate retry instead of waiting out _FAILED_RETRY_COOLDOWN.
        Returns False if there's nothing to delete, or it's still actively
        downloading - there's no file to delete yet, and killing the
        in-flight download out from under its own task isn't handled by
        this method (the task will simply finish, successfully or not, on
        its own; delete it again afterward if still unwanted).
        """
        entry = self._entries.get(key)
        if entry is None or not entry.ready.is_set():
            return False
        self._evict(key)
        return True

    def stats(self) -> dict:
        """Total on-disk usage vs. budget, for the dashboard's cache manager
        header. Mirrors _prune()'s own total calculation.
        """
        total = sum(e.size for e in self._entries.values() if e.ready.is_set())
        return {"totalBytes": total, "maxBytes": _MAX_TOTAL_BYTES}

    async def download(self, key: str, youtube_url: str, title: str = "", urgent: bool = False) -> bool:
        """Fully downloads `key`'s audio to disk via yt-dlp. Returns True on
        success. Idempotent/dedup'd: an already-finished (or currently
        in-flight) entry for the same key is reused rather than re-fetched -
        callers just await whichever task is already running. A successful
        entry is reused indefinitely (no TTL - see module docstring); a
        *failed* one still blocks retries for _FAILED_RETRY_COOLDOWN so a
        persistently broken video doesn't get hammered every time it's
        queued.

        `title` is cosmetic only (dashboard display) - stored on first
        download and backfilled on reuse if the entry didn't have one yet,
        never used to key or invalidate the entry.
        """
        existing = self._entries.get(key)
        if existing is not None:
            if existing.task is not None:
                await existing.task
            if existing.error is None:
                existing.view_count += 1
                if title and not existing.title:
                    existing.title = title
                    self._write_meta_sidecar(key, title=title)
                return existing.path is not None and existing.path.exists()
            if time.monotonic() - existing.created_at < _FAILED_RETRY_COOLDOWN:
                return False

        entry = _CacheEntry(view_count=1, title=title)
        self._entries[key] = entry
        entry.task = asyncio.create_task(self._run_ytdlp(key, youtube_url, entry, urgent=urgent))
        await entry.task
        return entry.error is None

    async def _run_ytdlp(self, key: str, youtube_url: str, entry: _CacheEntry, urgent: bool = False) -> None:
        if urgent:
            return await self._run_ytdlp_now(key, youtube_url, entry, _URGENT_RATE)
        # Background: wait for the (global) background slot first.
        async with self._background_slots:
            return await self._run_ytdlp_now(key, youtube_url, entry, _BACKGROUND_RATE)

    async def _run_ytdlp_now(self, key: str, youtube_url: str, entry: _CacheEntry, rate: str) -> None:
        dest = CACHE_DIR / f"{key}.audio"
        try:
            proc = await asyncio.create_subprocess_exec(
                *_low_priority_prefix(),
                sys.executable, "-m", "yt_dlp",
                *_rate_args(rate),
                "-f", _YTDLP_FORMAT,
                "--no-playlist",
                "--no-warnings",
                "--newline",
                "--progress-template", _PROGRESS_TEMPLATE,
                "-o", str(dest),
                youtube_url,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            stderr_chunks: list[bytes] = []
            try:
                await asyncio.wait_for(
                    asyncio.gather(
                        self._drain_progress(proc, entry),
                        self._drain_stderr(proc, stderr_chunks),
                    ),
                    timeout=_YTDLP_TIMEOUT,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                entry.error = "yt-dlp timed out"
                dest.unlink(missing_ok=True)
                logger.debug(f"[StreamCache] yt-dlp timed out downloading {key} ({entry.title or 'no title'})")
                return

            if proc.returncode != 0 or not dest.exists():
                stderr_text = b"".join(stderr_chunks).decode(errors="replace")
                entry.error = f"yt-dlp exited {proc.returncode}: {stderr_text[:300]}"
                dest.unlink(missing_ok=True)
                logger.debug(f"[StreamCache] yt-dlp failed for {key} ({entry.title or 'no title'}): {entry.error}")
                return

            entry.path = dest
            entry.size = dest.stat().st_size
            entry.gain = await self._measure_gain(dest, low_priority=True)
            self._write_meta_sidecar(key, title=entry.title, gain=entry.gain)
            logger.debug(
                f"[StreamCache] cached {key} ({entry.title or 'no title'}), {entry.size} bytes, "
                f"gain={entry.gain if entry.gain is not None else 'unmeasured'}"
            )
        except Exception as e:
            entry.error = str(e)
            dest.unlink(missing_ok=True)
            logger.debug(f"[StreamCache] yt-dlp download failed for {key}: {e}")
        finally:
            entry.ready.set()
            self._prune()

    @staticmethod
    async def _measure_gain(path: Path, low_priority: bool = False) -> Optional[float]:
        """Patch 10: measures `path`'s integrated loudness via a single,
        analysis-only ffmpeg `ebur128` pass (Patch 33: was `loudnorm`, which
        gives the same integrated loudness/true peak but upsamples to 192kHz
        internally - ~4.5x slower, and this runs before an uncached song may
        start playing). Nothing is re-encoded or
        written back - the audio file on disk is never touched) and
        returns the linear gain multiplier that would bring it to
        _LOUDNORM_TARGET_LUFS, clamped to +/-_LOUDNORM_MAX_GAIN_DB.

        Returns None - meaning "play unchanged," never an error - if
        ffmpeg isn't installed, the process fails/times out, or its output
        can't be parsed. A measurement failure must never block or fail a
        download that already succeeded.
        """
        try:
            prefix = _low_priority_prefix() if low_priority else []
            proc = await asyncio.create_subprocess_exec(
                *prefix, "ffmpeg", "-i", str(path),
                "-hide_banner", "-nostats",
                "-af", "ebur128=peak=true",
                "-f", "null", "-",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_LOUDNORM_TIMEOUT)
        except Exception as e:
            logger.debug(f"[StreamCache] loudness measurement failed to start for {path.name}: {e}")
            return None

        text = stderr.decode(errors="replace")
        summary = text[text.rfind("Summary:"):] if "Summary:" in text else ""
        integrated = re.search(r"Integrated loudness:\s*I:\s*(-?(?:inf|[\d.]+))\s*LUFS", summary)
        if not integrated:
            logger.debug(f"[StreamCache] could not parse ebur128 output for {path.name}")
            return None
        measured_lufs = float(integrated.group(1))

        gain_db = max(-_LOUDNORM_MAX_GAIN_DB, min(_LOUDNORM_MAX_GAIN_DB, _LOUDNORM_TARGET_LUFS - measured_lufs))
        peak = re.search(r"True peak:\s*Peak:\s*(-?(?:inf|[\d.]+))\s*dBFS", summary)
        if peak:
            gain_db = min(gain_db, max(0.0, _LOUDNORM_PEAK_CEILING_DBTP - float(peak.group(1))))
        return 10 ** (gain_db / 20)

    @staticmethod
    async def _drain_progress(proc: "asyncio.subprocess.Process", entry: _CacheEntry) -> None:
        """Reads yt-dlp's stdout line by line for the duration of the
        download, updating `entry`'s live progress fields from any
        _PROGRESS_PREFIX lines (see _PROGRESS_TEMPLATE); every other line
        (yt-dlp's own extraction/info chatter) is ignored. Also waits for
        the process to exit, so a caller awaiting this alongside
        _drain_stderr knows the whole subprocess lifecycle is done, not just
        that stdout hit EOF.
        """
        assert proc.stdout is not None
        async for raw in proc.stdout:
            line = raw.decode(errors="ignore").strip()
            if not line.startswith(_PROGRESS_PREFIX):
                continue
            try:
                downloaded_s, total_s = line[len(_PROGRESS_PREFIX):].split("/", 1)
                entry.downloaded_bytes = int(downloaded_s)
                if total_s not in ("NA", ""):
                    entry.total_bytes = int(total_s)
            except (ValueError, IndexError):
                continue
        await proc.wait()

    @staticmethod
    async def _drain_stderr(proc: "asyncio.subprocess.Process", chunks: list) -> None:
        assert proc.stderr is not None
        async for raw in proc.stderr:
            chunks.append(raw)

    def _prune(self) -> None:
        """Evicts entries only when the cache's total on-disk size is over
        budget - a finished entry otherwise lives forever (see module
        docstring). An entry that's still downloading (not yet `ready`) is
        never evicted - there's nothing to delete yet, the download just
        keeps running.

        Eviction order: never touch a video that's current/upcoming in any
        connected player's queue right now (_is_queued_elsewhere), no matter
        how unpopular it is - it's about to be needed. Among what's left,
        evict the *least-viewed* first (view_count), not oldest-first - a
        video queued often should survive longer than one that was only
        ever played once, regardless of which was downloaded first.

        If everything still over budget is actively queued somewhere, this
        is a soft cap: it just stays over budget rather than evict something
        actually needed. That's expected to be rare - it needs the sum of
        every distinct video currently queued across every guild to exceed
        STREAM_CACHE_MAX_BYTES on its own.
        """
        total = sum(e.size for e in self._entries.values() if e.ready.is_set())
        if total <= _MAX_TOTAL_BYTES:
            return

        candidates = sorted(
            (
                (k, e) for k, e in self._entries.items()
                if e.ready.is_set() and not _is_queued_elsewhere(k)
            ),
            key=lambda kv: kv[1].view_count,
        )
        for k, e in candidates:
            total -= e.size
            self._evict(k)
            if total <= _MAX_TOTAL_BYTES:
                break

    def _evict(self, key: str) -> None:
        entry = self._entries.pop(key, None)
        if entry and entry.path:
            entry.path.unlink(missing_ok=True)
        self._meta_path(key).unlink(missing_ok=True)

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        key = request.match_info["key"]
        if not self.is_ready(key):
            raise web.HTTPNotFound()

        entry = self._entries[key]
        return web.FileResponse(entry.path)


_cache: Optional[StreamCache] = None
_cache_lock: asyncio.Lock = asyncio.Lock()


async def _get_cache() -> StreamCache:
    global _cache
    if _cache is None:
        async with _cache_lock:
            if _cache is None:
                cache = StreamCache()
                await cache.start()
                _cache = cache
    return _cache


async def prefetch_to_disk(key: str, youtube_url: str, title: str = "", urgent: bool = False) -> bool:
    """Entry point used by Node.prefetch_direct_track(). Lazily starts the
    local proxy server on first call, then downloads (or reuses an existing
    download of) `key`'s audio to local disk via yt-dlp. Returns True once
    the file is ready to serve. `title` is cosmetic only - see
    StreamCache.download().
    """
    cache = await _get_cache()
    return await cache.download(key, youtube_url, title, urgent=urgent)


async def get_progress(key: str) -> Optional[float]:
    """Entry point used by Player._prefetch_and_notify() to report live
    download progress to the dashboard while a prefetch is in flight. See
    StreamCache.progress() for the None cases.
    """
    cache = await _get_cache()
    return cache.progress(key)


async def get_local_url(key: str) -> Optional[str]:
    """Entry point used by Node.resolve_direct_track(). Returns the local
    proxy url for `key` if (and only if) a completed disk download already
    exists for it - i.e. prefetch_to_disk() already succeeded. Returns None
    otherwise, which callers should treat as "no cached copy; resolve the
    normal on-demand way instead."
    """
    cache = await _get_cache()
    return cache.local_url(key) if cache.is_ready(key) else None


async def get_gain(key: str) -> Optional[float]:
    """Entry point used by Node.get_track_gain() (Patch 10's per-track
    loudness normalization, applied as a Lavalink "volume" filter in
    Player.play()). See StreamCache.gain() for the None case.
    """
    cache = await _get_cache()
    return cache.gain(key)


async def list_entries() -> list[dict]:
    """Entry point used by the dashboard's cache-manager IPC methods
    (voicelink/ipc/methods.py's getCacheEntries/deleteCacheEntry). See
    StreamCache.list_entries().
    """
    cache = await _get_cache()
    return cache.list_entries()


async def delete_entry(key: str) -> bool:
    """Entry point used by the dashboard's cache-manager IPC methods for a
    manual, admin-initiated eviction. See StreamCache.delete_entry().
    """
    cache = await _get_cache()
    return cache.delete_entry(key)


async def get_stats() -> dict:
    """Entry point used by the dashboard's cache-manager IPC methods for the
    usage-vs-budget header. See StreamCache.stats().
    """
    cache = await _get_cache()
    return cache.stats()
