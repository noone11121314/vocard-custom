"""Unit tests for voicelink/stream_cache.py: the local HTTP proxy that serves
yt-dlp-downloaded, disk-cached YouTube audio. Covers the yt-dlp subprocess
hand-off, download dedup, disk-cache pruning/eviction, the HTTP handler, and
the prefetch_to_disk()/get_local_url() singleton entry points. No real
network access, yt-dlp binary, or Lavalink/invidious-companion involved -
asyncio.create_subprocess_exec is monkeypatched throughout.
"""
from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

import pytest
from unittest.mock import AsyncMock
from aiohttp.test_utils import TestClient, TestServer
from aiohttp import web

from voicelink import stream_cache
from voicelink.stream_cache import StreamCache, _CacheEntry


async def _stop_and_clear_cache() -> None:
    if stream_cache._cache is not None:
        await stream_cache._cache.stop()
        stream_cache._cache = None


@pytest.fixture(autouse=True)
async def reset_singleton(monkeypatch, tmp_path):
    """get_local_url()/prefetch_to_disk() keep a module-level singleton; make
    sure one test's cache instance (and its on-disk files) never leaks into
    the next. Also points CACHE_DIR at a per-test tmp dir so nothing touches
    the real /tmp/vocard_stream_cache.

    Stopping the previous instance (not just discarding the reference) is
    required, not cosmetic: StreamCache.start() binds a real TCP listener on
    LISTEN_PORT, and simply overwriting `_cache` with None leaves that
    listener bound in the background - the next test that starts its own
    instance then intermittently fails with "address already in use" on
    whichever test happens to run while the leaked listener is still alive.

    `_cache` is reset via plain attribute assignment (`_stop_and_clear_cache`),
    not `monkeypatch.setattr` - the code under test reassigns this module
    global directly (see `_get_cache()`), which monkeypatch's own restore
    tracking doesn't see; using monkeypatch here would make teardown restore
    a stale reference from an earlier test instead of the one this test
    actually created, which is the opposite of what this fixture is for.
    """
    await _stop_and_clear_cache()
    monkeypatch.setattr(stream_cache, "CACHE_DIR", tmp_path)
    # pool.py registers the real invidious-companion lookup at import time;
    # tests opt in to a fake one explicitly (Patch 36).
    monkeypatch.setattr(stream_cache, "_url_resolver", None)
    yield
    await _stop_and_clear_cache()


class _FakeStream:
    """Stand-in for a subprocess's StreamReader - just an async iterator over
    a fixed list of lines, matching what `async for line in proc.stdout`
    needs.
    """

    def __init__(self, lines=None):
        self._lines = list(lines or [])

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class _FakeProcess:
    """Stand-in for asyncio.subprocess.Process. `on_communicate` runs once
    `wait()` is reached, to simulate yt-dlp actually producing the output
    file right before exiting. `delay` (applied in `wait()`, since
    _drain_progress reads stdout to EOF before waiting) simulates a
    hung/slow process for the timeout test.
    """

    def __init__(self, returncode=0, stdout_lines=None, stderr_lines=None, delay=0.0, on_communicate=None):
        self.returncode = returncode
        self.stdout = _FakeStream(stdout_lines)
        self.stderr = _FakeStream(stderr_lines)
        self._delay = delay
        self._on_communicate = on_communicate
        self.killed = False

    async def wait(self):
        if self.killed:
            return self.returncode  # a real OS process returns promptly once reaped after kill()
        if self._delay:
            await asyncio.sleep(self._delay)
        if self._on_communicate:
            self._on_communicate()
        return self.returncode

    def kill(self):
        self.killed = True


def _patch_subprocess(monkeypatch, proc: _FakeProcess):
    calls = []

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return proc

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return calls


def _ebur128(i=None, tp=None) -> bytes:
    """ffmpeg ebur128=peak=true stderr summary (Patch 33), as _measure_gain parses it."""
    parts = ["[Parsed_ebur128_0 @ 0x1] Summary:", ""]
    if i is not None:
        parts += ["  Integrated loudness:", f"    I:         {i} LUFS", "    Threshold: -18.2 LUFS", ""]
    parts += ["  Loudness range:", "    LRA:        16.2 LU", ""]
    if tp is not None:
        parts += ["  True peak:", f"    Peak:       {tp} dBFS"]
    return "\n".join(parts).encode()


class _FakeMeasureProcess:
    """Stand-in for the ffmpeg subprocess _measure_gain() runs (Patch 10) -
    unlike _FakeProcess (yt-dlp's stand-in, draining stdout/stderr line by
    line), this one only needs `communicate()`, since _measure_gain reads
    all of stderr at once rather than streaming it.
    """

    def __init__(self, stderr: bytes = b""):
        self._stderr = stderr

    async def communicate(self):
        return b"", self._stderr


def _patch_subprocess_sequence(monkeypatch, procs: list):
    """Like _patch_subprocess, but hands out a different fake process per
    call to asyncio.create_subprocess_exec, in order - for tests that care
    about both the yt-dlp download (_FakeProcess) and its follow-up
    loudness-measurement pass (_FakeMeasureProcess) succeeding. The last
    process is reused for any call beyond the given list.
    """
    calls = []
    remaining = list(procs)

    async def fake_create_subprocess_exec(*args, **kwargs):
        calls.append(args)
        return remaining.pop(0) if len(remaining) > 1 else remaining[0]

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_create_subprocess_exec)
    return calls


# --- StreamCache.download() / _run_ytdlp() --------------------------------


async def test_download_succeeds_when_ytdlp_exits_zero_and_writes_the_file(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid1.audio"

    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"audio-bytes"))
    _patch_subprocess(monkeypatch, proc)

    ok = await c.download("vid1", "https://www.youtube.com/watch?v=vid1")

    assert ok is True
    assert c.is_ready("vid1")
    entry = c._entries["vid1"]
    assert entry.path == dest
    assert entry.size == len(b"audio-bytes")


async def test_download_fails_on_nonzero_exit_and_cleans_up(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid2.audio"

    proc = _FakeProcess(returncode=1, stderr_lines=[b"ERROR: Sign in to confirm you're not a bot"])
    _patch_subprocess(monkeypatch, proc)

    ok = await c.download("vid2", "https://www.youtube.com/watch?v=vid2")

    assert ok is False
    assert not c.is_ready("vid2")
    assert "yt-dlp exited 1" in c._entries["vid2"].error
    assert not dest.exists()


async def test_download_fails_if_file_missing_despite_zero_exit(tmp_path, monkeypatch):
    c = StreamCache()
    proc = _FakeProcess(returncode=0)  # exits clean but never actually wrote anything
    _patch_subprocess(monkeypatch, proc)

    ok = await c.download("vid3", "https://www.youtube.com/watch?v=vid3")

    assert ok is False
    assert not c.is_ready("vid3")


async def test_download_times_out_and_kills_the_process(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_cache, "_YTDLP_TIMEOUT", 0.05)
    c = StreamCache()
    proc = _FakeProcess(delay=10)  # never finishes within the timeout
    _patch_subprocess(monkeypatch, proc)

    ok = await c.download("vid4", "https://www.youtube.com/watch?v=vid4")

    assert ok is False
    assert proc.killed is True
    assert c._entries["vid4"].error == "yt-dlp timed out"


async def test_download_dedups_concurrent_calls_for_the_same_key(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid5.audio"
    proc = _FakeProcess(returncode=0, delay=0.02, on_communicate=lambda: dest.write_bytes(b"x"))
    calls = _patch_subprocess(monkeypatch, proc)

    first, second = await asyncio.gather(
        c.download("vid5", "http://y"),
        c.download("vid5", "http://y"),
    )

    assert first is True and second is True
    # One yt-dlp process, plus its one loudness-measurement pass (Patch 10) -
    # both happen inside the single _run_ytdlp task that concurrent callers
    # dedup onto, so still only 2 total, not 4.
    assert len(calls) == 2


async def test_download_reuses_a_fresh_finished_entry_without_respawning(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid6.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    calls = _patch_subprocess(monkeypatch, proc)

    await c.download("vid6", "http://y")
    await c.download("vid6", "http://y")

    # yt-dlp + its one loudness-measurement pass (Patch 10) on the first,
    # successful download only - the reuse doesn't re-run either.
    assert len(calls) == 2
    assert c._entries["vid6"].view_count == 2  # one for the initial download, one for the reuse


async def test_download_stores_the_title_on_first_download(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid_title.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    _patch_subprocess(monkeypatch, proc)

    await c.download("vid_title", "http://y", title="My Song")

    assert c._entries["vid_title"].title == "My Song"


async def test_download_backfills_a_missing_title_on_reuse(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid_backfill.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    _patch_subprocess(monkeypatch, proc)

    await c.download("vid_backfill", "http://y")  # no title the first time
    await c.download("vid_backfill", "http://y", title="Late Title")

    assert c._entries["vid_backfill"].title == "Late Title"


async def test_download_does_not_overwrite_an_existing_title_on_reuse(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid_keep.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    _patch_subprocess(monkeypatch, proc)

    await c.download("vid_keep", "http://y", title="Original Title")
    await c.download("vid_keep", "http://y", title="Different Title")

    assert c._entries["vid_keep"].title == "Original Title"


async def test_download_writes_a_title_sidecar_so_it_survives_a_restart(tmp_path, monkeypatch):
    """Regression guard (2026-10-11): the audio file alone only carries the
    video id - without a sidecar, a restart's _adopt_existing_files() would
    permanently blank every title, no matter how many times a video had
    been downloaded with one.
    """
    c = StreamCache()
    dest = tmp_path / "vid_sidecar.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    _patch_subprocess(monkeypatch, proc)

    await c.download("vid_sidecar", "http://y", title="Sidecar Song")

    assert (tmp_path / "vid_sidecar.meta.json").read_text() == '{"title": "Sidecar Song"}'


async def test_download_does_not_write_a_sidecar_without_a_title(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid_notitle.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    _patch_subprocess(monkeypatch, proc)

    await c.download("vid_notitle", "http://y")

    assert not (tmp_path / "vid_notitle.meta.json").exists()


async def test_download_writes_a_title_sidecar_on_a_backfilled_reuse_too(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid_backfill2.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    _patch_subprocess(monkeypatch, proc)

    await c.download("vid_backfill2", "http://y")  # no title the first time
    await c.download("vid_backfill2", "http://y", title="Backfilled Song")

    assert (tmp_path / "vid_backfill2.meta.json").read_text() == '{"title": "Backfilled Song"}'


# --- StreamCache._measure_gain (Patch 10: per-track loudness normalization) -


async def test_measure_gain_computes_a_boost_for_a_quiet_track(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=_FakeMeasureProcess(_ebur128("-22.0"))),
    )

    gain = await StreamCache._measure_gain(Path("/fake/path.audio"))

    # -22 LUFS measured, -14 LUFS target = +8dB, exactly at the clamp ceiling.
    assert gain == pytest.approx(10 ** (8 / 20))


async def test_measure_gain_computes_a_cut_for_a_loud_track(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=_FakeMeasureProcess(_ebur128("-6.0"))),
    )

    gain = await StreamCache._measure_gain(Path("/fake/path.audio"))

    # -6 LUFS measured, -14 LUFS target = -8dB, exactly at the clamp floor.
    assert gain == pytest.approx(10 ** (-8 / 20))


async def test_measure_gain_clamps_a_measurement_beyond_the_db_limit(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=_FakeMeasureProcess(_ebur128("-40.0"))),
    )

    gain = await StreamCache._measure_gain(Path("/fake/path.audio"))

    # -40 LUFS would need +26dB to hit -14 - clamped to the +8dB ceiling
    # instead, never wildly over-amplified.
    assert gain == pytest.approx(10 ** (8 / 20))


async def test_measure_gain_returns_none_when_ffmpeg_is_missing(monkeypatch):
    async def raise_not_found(*args, **kwargs):
        raise FileNotFoundError("ffmpeg not found")

    monkeypatch.setattr(asyncio, "create_subprocess_exec", raise_not_found)

    assert await StreamCache._measure_gain(Path("/fake/path.audio")) is None


async def test_measure_gain_returns_none_on_unparseable_output(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=_FakeMeasureProcess(b"not json at all, ffmpeg's usual chatter")),
    )

    assert await StreamCache._measure_gain(Path("/fake/path.audio")) is None


async def test_measure_gain_returns_none_when_input_i_is_missing(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=_FakeMeasureProcess(_ebur128(None, "-1.0"))),
    )

    assert await StreamCache._measure_gain(Path("/fake/path.audio")) is None


async def test_measure_gain_returns_none_on_timeout(monkeypatch):
    class _HangingProcess:
        async def communicate(self):
            await asyncio.sleep(10)

    monkeypatch.setattr(stream_cache, "_LOUDNORM_TIMEOUT", 0.01)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_HangingProcess()))

    assert await StreamCache._measure_gain(Path("/fake/path.audio")) is None


async def test_download_measures_and_persists_gain_on_a_successful_download(tmp_path, monkeypatch):
    dest = tmp_path / "vid_gain.audio"
    ytdlp_proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    measure_proc = _FakeMeasureProcess(_ebur128("-20.0"))
    _patch_subprocess_sequence(monkeypatch, [ytdlp_proc, measure_proc])

    c = StreamCache()
    await c.download("vid_gain", "http://y", title="Gain Song")

    entry = c._entries["vid_gain"]
    assert entry.gain == pytest.approx(10 ** (6 / 20))  # -20 -> -14 is +6dB
    meta = json.loads((tmp_path / "vid_gain.meta.json").read_text())
    assert meta["title"] == "Gain Song"
    assert meta["gain"] == pytest.approx(10 ** (6 / 20))


async def test_download_leaves_gain_unmeasured_when_the_measurement_pass_fails(tmp_path, monkeypatch):
    """The shared default _FakeProcess has no communicate() method (only
    yt-dlp's own stdout/stderr-draining interface), so re-using it for the
    measurement pass simulates ffmpeg being unavailable - gain must stay
    None, and the download itself must still succeed regardless.
    """
    dest = tmp_path / "vid_nogain.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    _patch_subprocess(monkeypatch, proc)

    c = StreamCache()
    ok = await c.download("vid_nogain", "http://y", title="No Gain Song")

    assert ok is True
    assert c._entries["vid_nogain"].gain is None
    meta = json.loads((tmp_path / "vid_nogain.meta.json").read_text())
    assert "gain" not in meta
    assert meta["title"] == "No Gain Song"


async def test_download_reuses_an_entry_indefinitely_no_ttl(tmp_path, monkeypatch):
    """Regression guard: successful entries used to expire after _CACHE_TTL
    (30 min) and get silently re-downloaded. There's no TTL any more - see
    the module docstring - so even a very old entry must still be reused.
    """
    c = StreamCache()
    dest = tmp_path / "vid_old.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    calls = _patch_subprocess(monkeypatch, proc)

    await c.download("vid_old", "http://y")
    c._entries["vid_old"].created_at = time.monotonic() - 100_000  # long past the old TTL

    ok = await c.download("vid_old", "http://y")

    assert ok is True
    assert len(calls) == 2  # still no second yt-dlp invocation - just the one download's own yt-dlp + measurement pass


async def test_download_does_not_retry_a_failure_within_the_cooldown(tmp_path, monkeypatch):
    c = StreamCache()
    proc = _FakeProcess(returncode=1, stderr_lines=[b"blocked"])
    calls = _patch_subprocess(monkeypatch, proc)

    first = await c.download("vid_bad", "http://y")
    second = await c.download("vid_bad", "http://y")

    assert first is False and second is False
    assert len(calls) == 1  # the second call didn't hammer yt-dlp again


async def test_download_retries_a_failure_once_the_cooldown_elapses(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid_retry.audio"
    proc = _FakeProcess(returncode=1, stderr_lines=[b"blocked"])
    calls = _patch_subprocess(monkeypatch, proc)

    await c.download("vid_retry", "http://y")
    c._entries["vid_retry"].created_at = time.monotonic() - stream_cache._FAILED_RETRY_COOLDOWN - 1

    # Second attempt succeeds now that yt-dlp "unblocks" - a fresh process.
    proc2 = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    _patch_subprocess(monkeypatch, proc2)

    ok = await c.download("vid_retry", "http://y")

    assert ok is True
    assert len(calls) == 1  # the original failed call
    assert c._entries["vid_retry"].view_count == 1  # a fresh entry, not a reuse of the failed one


async def test_download_passes_the_preferred_format_string_and_output_path(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid7.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    calls = _patch_subprocess(monkeypatch, proc)

    await c.download("vid7", "https://www.youtube.com/watch?v=vid7")

    # calls[0] is yt-dlp itself; calls[1] is its follow-up loudness-measurement
    # pass (Patch 10) - this test only cares about the yt-dlp invocation.
    args = calls[0]
    assert "-f" in args and args[args.index("-f") + 1] == stream_cache._YTDLP_FORMAT
    assert "-o" in args and args[args.index("-o") + 1] == str(dest)
    assert args[-1] == "https://www.youtube.com/watch?v=vid7"


async def test_download_parses_progress_lines_while_ignoring_other_ytdlp_output(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid8.audio"
    proc = _FakeProcess(
        returncode=0,
        stdout_lines=[
            b"[youtube] Extracting URL: https://...\n",
            f"{stream_cache._PROGRESS_PREFIX}100/1000\n".encode(),
            b"[download] Destination: vid8.audio\n",
            f"{stream_cache._PROGRESS_PREFIX}1000/1000\n".encode(),
        ],
        on_communicate=lambda: dest.write_bytes(b"x"),
    )
    _patch_subprocess(monkeypatch, proc)

    await c.download("vid8", "https://www.youtube.com/watch?v=vid8")

    entry = c._entries["vid8"]
    assert entry.downloaded_bytes == 1000
    assert entry.total_bytes == 1000


async def test_download_tolerates_an_unparseable_progress_line(tmp_path, monkeypatch):
    c = StreamCache()
    dest = tmp_path / "vid9.audio"
    proc = _FakeProcess(
        returncode=0,
        stdout_lines=[
            f"{stream_cache._PROGRESS_PREFIX}garbage\n".encode(),
            f"{stream_cache._PROGRESS_PREFIX}500/NA\n".encode(),
        ],
        on_communicate=lambda: dest.write_bytes(b"x"),
    )
    _patch_subprocess(monkeypatch, proc)

    ok = await c.download("vid9", "http://y")  # must not raise

    assert ok is True
    entry = c._entries["vid9"]
    assert entry.downloaded_bytes == 500  # the parseable line still took effect
    assert entry.total_bytes is None  # "NA" total is left unset, not crashed on


# --- StreamCache.progress ---------------------------------------------------


def test_progress_is_none_for_an_unknown_key():
    c = StreamCache()
    assert c.progress("never-started") is None


def test_progress_is_none_once_the_entry_is_ready():
    c = StreamCache()
    entry = _CacheEntry(downloaded_bytes=1000, total_bytes=1000)
    entry.ready.set()
    c._entries["k"] = entry

    assert c.progress("k") is None


def test_progress_is_none_when_total_bytes_is_unknown():
    c = StreamCache()
    c._entries["k"] = _CacheEntry(downloaded_bytes=100, total_bytes=None)

    assert c.progress("k") is None


def test_progress_is_the_downloaded_over_total_fraction():
    c = StreamCache()
    c._entries["k"] = _CacheEntry(downloaded_bytes=250, total_bytes=1000)

    assert c.progress("k") == 0.25


def test_progress_is_clamped_to_one_if_downloaded_overshoots_total():
    c = StreamCache()
    c._entries["k"] = _CacheEntry(downloaded_bytes=1200, total_bytes=1000)

    assert c.progress("k") == 1.0


# --- StreamCache._adopt_existing_files (recovering files across a restart) ---


def test_adopt_existing_files_rebuilds_entries_from_the_cache_dir(tmp_path):
    (tmp_path / "vidOld.audio").write_bytes(b"audio-bytes")
    c = StreamCache()

    c._adopt_existing_files()

    assert c.is_ready("vidOld")
    entry = c._entries["vidOld"]
    assert entry.path == tmp_path / "vidOld.audio"
    assert entry.size == len(b"audio-bytes")
    assert entry.title == ""  # no sidecar for this file - falls back to the video id


def test_adopt_existing_files_recovers_the_title_from_its_sidecar(tmp_path):
    (tmp_path / "vidWithTitle.audio").write_bytes(b"audio-bytes")
    (tmp_path / "vidWithTitle.meta.json").write_text('{"title": "Recovered Song"}')
    c = StreamCache()

    c._adopt_existing_files()

    assert c._entries["vidWithTitle"].title == "Recovered Song"


def test_adopt_existing_files_tolerates_a_corrupt_sidecar(tmp_path):
    (tmp_path / "vidCorrupt.audio").write_bytes(b"audio-bytes")
    (tmp_path / "vidCorrupt.meta.json").write_text("not valid json{{{")
    c = StreamCache()

    c._adopt_existing_files()  # must not raise

    assert c._entries["vidCorrupt"].title == ""


def test_adopt_existing_files_recovers_the_gain_from_its_sidecar(tmp_path):
    (tmp_path / "vidWithGain.audio").write_bytes(b"audio-bytes")
    (tmp_path / "vidWithGain.meta.json").write_text('{"title": "Song", "gain": 1.25}')
    c = StreamCache()

    c._adopt_existing_files()

    assert c._entries["vidWithGain"].gain == 1.25
    assert c._entries["vidWithGain"].title == "Song"


def test_adopt_existing_files_defaults_gain_to_none_without_a_sidecar(tmp_path):
    """A file cached before Patch 10 existed (or whose measurement pass
    failed) has no "gain" key at all - must not retroactively invent one,
    just play unnormalized until the next re-download re-measures it.
    """
    (tmp_path / "vidNoGain.audio").write_bytes(b"audio-bytes")
    c = StreamCache()

    c._adopt_existing_files()

    assert c._entries["vidNoGain"].gain is None


def test_adopt_existing_files_ignores_non_audio_files(tmp_path):
    (tmp_path / "not-a-cache-file.txt").write_bytes(b"whatever")
    c = StreamCache()

    c._adopt_existing_files()

    assert c.list_entries() == []


def test_adopt_existing_files_does_not_clobber_an_already_known_entry(tmp_path):
    (tmp_path / "vid1.audio").write_bytes(b"new-bytes-on-disk")
    c = StreamCache()
    c._entries["vid1"] = _ready_entry(size=999, view_count=7, title="Kept")

    c._adopt_existing_files()

    assert c._entries["vid1"].size == 999
    assert c._entries["vid1"].view_count == 7
    assert c._entries["vid1"].title == "Kept"


def test_adopt_existing_files_prunes_if_over_budget(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_cache, "_MAX_TOTAL_BYTES", 10)
    (tmp_path / "vidBig.audio").write_bytes(b"x" * 100)
    c = StreamCache()

    c._adopt_existing_files()

    assert c.list_entries() == []  # over budget and not queued anywhere - evicted
    assert not (tmp_path / "vidBig.audio").exists()


# --- StreamCache.list_entries / delete_entry / stats (dashboard cache manager) ---


def test_list_entries_is_empty_for_a_fresh_cache():
    c = StreamCache()
    assert c.list_entries() == []


def test_list_entries_reports_status_size_and_view_count():
    c = StreamCache()
    c._entries["vidReady"] = _ready_entry(size=100, view_count=3, title="My Song")

    [entry] = c.list_entries()

    assert entry == {
        "videoId": "vidReady", "title": "My Song", "size": 100, "viewCount": 3,
        "status": "ready", "queued": False, "progress": None,
    }


def test_list_entries_reports_live_progress_for_a_downloading_entry():
    c = StreamCache()
    c._entries["vidLoading"] = _CacheEntry(view_count=1, downloaded_bytes=50, total_bytes=200)

    [entry] = c.list_entries()

    assert entry["status"] == "downloading"
    assert entry["progress"] == 0.25


def test_list_entries_reports_downloading_status_for_an_in_progress_entry():
    c = StreamCache()
    c._entries["vidLoading"] = _CacheEntry(view_count=1)  # ready Event not set

    [entry] = c.list_entries()

    assert entry["status"] == "downloading"


def test_list_entries_reports_failed_status_for_an_errored_entry():
    c = StreamCache()
    entry = _CacheEntry(error="blocked", view_count=1)
    entry.ready.set()
    c._entries["vidFailed"] = entry

    [out] = c.list_entries()

    assert out["status"] == "failed"


def test_list_entries_reflects_the_queued_elsewhere_check(monkeypatch):
    monkeypatch.setattr(stream_cache, "_is_queued_elsewhere", lambda key: key == "vidQueued")
    c = StreamCache()
    c._entries["vidQueued"] = _ready_entry()
    c._entries["vidNotQueued"] = _ready_entry()

    entries = {e["videoId"]: e["queued"] for e in c.list_entries()}

    assert entries == {"vidQueued": True, "vidNotQueued": False}


def test_delete_entry_removes_a_ready_entry_and_its_file(tmp_path):
    c = StreamCache()
    f = tmp_path / "x.audio"
    f.write_bytes(b"data")
    entry = _CacheEntry(path=f, size=4)
    entry.ready.set()
    c._entries["vid1"] = entry

    deleted = c.delete_entry("vid1")

    assert deleted is True
    assert "vid1" not in c._entries
    assert not f.exists()


def test_delete_entry_also_removes_the_title_sidecar(tmp_path):
    c = StreamCache()
    f = tmp_path / "vid1.audio"
    f.write_bytes(b"data")
    (tmp_path / "vid1.meta.json").write_text('{"title": "Gone Soon"}')
    entry = _CacheEntry(path=f, size=4, title="Gone Soon")
    entry.ready.set()
    c._entries["vid1"] = entry

    c.delete_entry("vid1")

    assert not (tmp_path / "vid1.meta.json").exists()


def test_delete_entry_removes_a_failed_entry_too():
    """A stuck/failed entry can be manually cleared to force an immediate
    retry instead of waiting out _FAILED_RETRY_COOLDOWN.
    """
    c = StreamCache()
    entry = _CacheEntry(error="blocked")
    entry.ready.set()
    c._entries["vid1"] = entry

    assert c.delete_entry("vid1") is True
    assert "vid1" not in c._entries


def test_delete_entry_ignores_the_queued_elsewhere_check(monkeypatch):
    """Unlike _prune(), a manual delete is the admin's own explicit call -
    it must go through even if the video is currently queued somewhere.
    """
    monkeypatch.setattr(stream_cache, "_is_queued_elsewhere", lambda key: True)
    c = StreamCache()
    c._entries["vid1"] = _ready_entry()

    assert c.delete_entry("vid1") is True
    assert "vid1" not in c._entries


def test_delete_entry_returns_false_for_an_unknown_key():
    c = StreamCache()
    assert c.delete_entry("never-cached") is False


def test_delete_entry_returns_false_for_an_in_progress_entry():
    c = StreamCache()
    c._entries["vid1"] = _CacheEntry()  # not ready yet

    assert c.delete_entry("vid1") is False
    assert "vid1" in c._entries  # left alone - still downloading


def test_stats_on_an_empty_cache():
    c = StreamCache()
    assert c.stats() == {"totalBytes": 0, "maxBytes": stream_cache._MAX_TOTAL_BYTES}


def test_stats_sums_only_ready_entries(monkeypatch):
    monkeypatch.setattr(stream_cache, "_MAX_TOTAL_BYTES", 999)
    c = StreamCache()
    c._entries["ready1"] = _ready_entry(size=100)
    c._entries["ready2"] = _ready_entry(size=50)
    c._entries["still_downloading"] = _CacheEntry(size=0)  # not ready - excluded

    assert c.stats() == {"totalBytes": 150, "maxBytes": 999}


# --- StreamCache._prune ---------------------------------------------------


def _ready_entry(size: int = 0, view_count: int = 1, created_at: float | None = None, title: str = "", gain: float | None = None) -> _CacheEntry:
    e = _CacheEntry(path=Path("/nonexistent"), size=size, view_count=view_count, title=title, gain=gain)
    if created_at is not None:
        e.created_at = created_at
    e.ready.set()
    return e


def test_prune_leaves_everything_alone_when_under_budget(monkeypatch):
    """No TTL any more - an old, rarely-viewed entry survives as long as
    there's room. See the module docstring for why: once a video is on
    disk, there's no correctness reason to ever re-fetch it.
    """
    monkeypatch.setattr(stream_cache, "_MAX_TOTAL_BYTES", 1_000_000)
    c = StreamCache()
    old_and_unpopular = _ready_entry(size=10, view_count=1, created_at=time.monotonic() - 100_000)
    c._entries["old"] = old_and_unpopular

    c._prune()

    assert c._entries["old"] is old_and_unpopular


def test_prune_never_evicts_in_progress_entries(monkeypatch):
    monkeypatch.setattr(stream_cache, "_MAX_TOTAL_BYTES", 0)  # always "over budget"
    c = StreamCache()
    in_progress = _CacheEntry(created_at=time.monotonic() - 1000)  # not ready

    c._entries["active"] = in_progress

    c._prune()

    assert c._entries["active"] is in_progress


def test_prune_evicts_the_least_viewed_entry_first_when_over_budget(monkeypatch):
    monkeypatch.setattr(stream_cache, "_MAX_TOTAL_BYTES", 15)
    c = StreamCache()
    # "popular" is older but viewed far more - it must survive over "new",
    # which is newer but barely viewed. Oldest-first would get this backwards.
    c._entries["popular"] = _ready_entry(10, view_count=50, created_at=time.monotonic() - 1000)
    c._entries["new"] = _ready_entry(10, view_count=1, created_at=time.monotonic())

    c._prune()

    assert "new" not in c._entries
    assert "popular" in c._entries


def test_prune_never_evicts_an_entry_that_is_queued_elsewhere(monkeypatch):
    """Even the least-viewed entry is protected if it's current/upcoming in
    some player's queue right now - see set_queued_checker. Eviction should
    fall through to the next-least-viewed entry that isn't.
    """
    monkeypatch.setattr(stream_cache, "_MAX_TOTAL_BYTES", 15)
    monkeypatch.setattr(stream_cache, "_is_queued_elsewhere", lambda key: key == "queued_but_unpopular")
    c = StreamCache()
    c._entries["queued_but_unpopular"] = _ready_entry(10, view_count=1)
    c._entries["unqueued_but_popular"] = _ready_entry(10, view_count=99)

    c._prune()

    assert "queued_but_unpopular" in c._entries  # protected despite being the least-viewed
    assert "unqueued_but_popular" not in c._entries  # evicted instead, to get under budget


def test_prune_stays_over_budget_if_everything_left_is_queued_elsewhere(monkeypatch):
    monkeypatch.setattr(stream_cache, "_MAX_TOTAL_BYTES", 5)
    monkeypatch.setattr(stream_cache, "_is_queued_elsewhere", lambda key: True)
    c = StreamCache()
    c._entries["a"] = _ready_entry(10, view_count=1)
    c._entries["b"] = _ready_entry(10, view_count=2)

    c._prune()  # must not raise or evict anything actually needed

    assert set(c._entries) == {"a", "b"}


def test_evict_deletes_the_backing_file(tmp_path):
    c = StreamCache()
    f = tmp_path / "x.audio"
    f.write_bytes(b"data")
    c._entries["k"] = _CacheEntry(path=f, size=4)
    c._entries["k"].ready.set()

    c._evict("k")

    assert "k" not in c._entries
    assert not f.exists()


# --- StreamCache._handle (real HTTP semantics via aiohttp test client) ---


@pytest.fixture
async def http_client():
    c = StreamCache()
    app = web.Application()
    app.router.add_route("GET", "/stream/{key}", c._handle)
    app.router.add_route("HEAD", "/stream/{key}", c._handle)
    client = TestClient(TestServer(app))
    await client.start_server()
    client.cache = c
    yield client
    await client.close()


async def test_handle_404s_for_an_unknown_key(http_client):
    resp = await http_client.get("/stream/missing")
    assert resp.status == 404


async def test_handle_404s_while_still_downloading(http_client):
    http_client.cache._entries["k"] = _CacheEntry()  # not ready yet

    resp = await http_client.get("/stream/k")

    assert resp.status == 404


async def test_handle_404s_on_a_failed_download(http_client):
    entry = _CacheEntry(error="boom")
    entry.ready.set()
    http_client.cache._entries["k"] = entry

    resp = await http_client.get("/stream/k")

    assert resp.status == 404


async def test_handle_serves_the_full_file(http_client, tmp_path):
    f = tmp_path / "k.audio"
    f.write_bytes(b"hello world")
    entry = _CacheEntry(path=f, size=11)
    entry.ready.set()
    http_client.cache._entries["k"] = entry

    resp = await http_client.get("/stream/k")

    assert resp.status == 200
    assert await resp.read() == b"hello world"


async def test_handle_serves_a_byte_range(http_client, tmp_path):
    f = tmp_path / "k.audio"
    f.write_bytes(b"hello world")
    entry = _CacheEntry(path=f, size=11)
    entry.ready.set()
    http_client.cache._entries["k"] = entry

    resp = await http_client.get("/stream/k", headers={"Range": "bytes=6-"})

    assert resp.status == 206
    assert await resp.read() == b"world"


async def test_handle_head_request_has_no_body(http_client, tmp_path):
    f = tmp_path / "k.audio"
    f.write_bytes(b"hello world")
    entry = _CacheEntry(path=f, size=11)
    entry.ready.set()
    http_client.cache._entries["k"] = entry

    resp = await http_client.head("/stream/k")

    assert resp.status == 200
    assert await resp.read() == b""


# --- prefetch_to_disk() / get_local_url() (module-level singleton) -------


async def test_get_local_url_returns_none_when_nothing_cached():
    assert await stream_cache.get_local_url("never-prefetched") is None


async def test_prefetch_to_disk_then_get_local_url_round_trip(tmp_path, monkeypatch):
    start_mock = AsyncMock()
    monkeypatch.setattr(StreamCache, "start", start_mock)
    dest = tmp_path / "vid1.audio"
    proc = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    _patch_subprocess(monkeypatch, proc)

    ok = await stream_cache.prefetch_to_disk("vid1", "http://y")
    url = await stream_cache.get_local_url("vid1")

    assert ok is True
    assert url == f"{stream_cache.PUBLIC_BASE_URL}/stream/vid1"
    start_mock.assert_awaited_once()  # server only bound once


async def test_get_local_url_is_none_for_a_failed_prefetch(tmp_path, monkeypatch):
    proc = _FakeProcess(returncode=1, stderr_lines=[b"blocked"])
    _patch_subprocess(monkeypatch, proc)

    await stream_cache.prefetch_to_disk("vid2", "http://y")
    url = await stream_cache.get_local_url("vid2")

    assert url is None


async def test_get_progress_reflects_the_live_download_state(tmp_path, monkeypatch):
    cache = await stream_cache._get_cache()
    cache._entries["vid3"] = _CacheEntry(downloaded_bytes=300, total_bytes=1000)

    assert await stream_cache.get_progress("vid3") == 0.3


async def test_get_progress_is_none_for_an_unknown_key():
    assert await stream_cache.get_progress("never-started") is None


async def test_get_gain_returns_none_when_nothing_measured():
    assert await stream_cache.get_gain("never-measured") is None


async def test_get_gain_returns_the_measured_value():
    cache = await stream_cache._get_cache()
    cache._entries["vidGain"] = _ready_entry(gain=1.4)

    assert await stream_cache.get_gain("vidGain") == 1.4


async def test_get_cache_adopts_files_left_by_a_previous_process(tmp_path):
    """End-to-end at the module-wrapper level: a file already sitting in
    CACHE_DIR (e.g. from before a restart, now that it's a persistent
    volume - see docs/local-patches.md's third Patch 7 follow-up) must be
    visible the moment the singleton starts, not just after that video is
    queued again.
    """
    (tmp_path / "vidRestarted.audio").write_bytes(b"leftover-bytes")

    cache = await stream_cache._get_cache()

    assert cache.is_ready("vidRestarted")


async def test_module_level_list_entries_delete_entry_get_stats_round_trip():
    """The dashboard cache-manager IPC methods go through these module-level
    wrappers, not StreamCache directly - exercise the whole round trip once.
    """
    cache = await stream_cache._get_cache()
    cache._entries["vid1"] = _ready_entry(size=100)

    entries = await stream_cache.list_entries()
    assert entries == [{
        "videoId": "vid1", "title": "", "size": 100, "viewCount": 1,
        "status": "ready", "queued": False, "progress": None,
    }]

    stats = await stream_cache.get_stats()
    assert stats == {"totalBytes": 100, "maxBytes": stream_cache._MAX_TOTAL_BYTES}

    deleted = await stream_cache.delete_entry("vid1")
    assert deleted is True
    assert await stream_cache.list_entries() == []


# --- Patch 26: true-peak cap on boosts, and the startup gain backfill -------


async def test_measure_gain_caps_a_boost_so_the_true_peak_stays_below_the_ceiling(monkeypatch):
    # -20 LUFS wants +6dB, but a -4 dBTP peak only has 3dB of headroom
    # before the -1 dBTP ceiling - boosting further would clip.
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=_FakeMeasureProcess(_ebur128("-20.0", "-4.0"))),
    )

    gain = await StreamCache._measure_gain(Path("/fake/path.audio"))

    assert gain == pytest.approx(10 ** (3 / 20))


async def test_measure_gain_never_boosts_a_track_already_peaking_at_the_ceiling(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=_FakeMeasureProcess(_ebur128("-20.0", "-0.5"))),
    )

    assert await StreamCache._measure_gain(Path("/fake/path.audio")) == pytest.approx(1.0)


async def test_measure_gain_peak_cap_never_affects_a_cut(monkeypatch):
    monkeypatch.setattr(
        asyncio, "create_subprocess_exec",
        AsyncMock(return_value=_FakeMeasureProcess(_ebur128("-8.0", "0.5"))),
    )

    assert await StreamCache._measure_gain(Path("/fake/path.audio")) == pytest.approx(10 ** (-6 / 20))


async def test_backfill_measures_only_files_without_a_current_gain(tmp_path, monkeypatch):
    (tmp_path / "unmeasured.audio").write_bytes(b"a")
    (tmp_path / "measured.audio").write_bytes(b"a")
    (tmp_path / "measured.meta.json").write_text('{"gain": 0.5}')
    (tmp_path / "oldBoost.audio").write_bytes(b"a")
    (tmp_path / "oldBoost.meta.json").write_text('{"title": "Quiet", "gain": 2.0}')
    (tmp_path / "newBoost.audio").write_bytes(b"a")
    (tmp_path / "newBoost.meta.json").write_text('{"gain": 1.5, "gain_version": 2}')
    c = StreamCache()
    c._adopt_existing_files()
    measured = []

    async def fake_measure(path, low_priority=False):
        measured.append((path.stem, low_priority))
        return 0.7

    monkeypatch.setattr(StreamCache, "_measure_gain", staticmethod(fake_measure))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())

    await c._backfill_gains()

    assert sorted(measured) == [("oldBoost", True), ("unmeasured", True)]
    assert c.gain("unmeasured") == pytest.approx(0.7)
    assert c.gain("measured") == pytest.approx(0.5)
    assert c._read_meta_sidecar("oldBoost") == {"title": "Quiet", "gain": 0.7, "gain_version": 2}


async def test_backfill_measures_queued_tracks_first(tmp_path, monkeypatch):
    for key in ("a", "b", "queuedOne"):
        (tmp_path / f"{key}.audio").write_bytes(b"a")
    c = StreamCache()
    c._adopt_existing_files()
    order = []

    async def fake_measure(path, low_priority=False):
        order.append(path.stem)
        return 1.0

    monkeypatch.setattr(StreamCache, "_measure_gain", staticmethod(fake_measure))
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(stream_cache, "_is_queued_elsewhere", lambda key: key == "queuedOne")

    await c._backfill_gains()

    assert order[0] == "queuedOne"


async def test_backfill_never_raises_when_measurement_fails(tmp_path, monkeypatch):
    (tmp_path / "x.audio").write_bytes(b"a")
    c = StreamCache()
    c._adopt_existing_files()
    monkeypatch.setattr(StreamCache, "_measure_gain", staticmethod(AsyncMock(side_effect=RuntimeError("boom"))))

    await c._backfill_gains()  # must not raise

    assert c.gain("x") is None


# --- Patch 33: gentle downloads (rate caps, one background at a time, low priority)


def _ytdlp_call(calls):
    return next(c for c in calls if "yt_dlp" in c)


async def test_background_download_is_rate_capped_and_low_priority(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_cache, "_BACKGROUND_RATE", "1M")
    monkeypatch.setattr(stream_cache.shutil, "which", lambda name: f"/usr/bin/{name}")
    c = StreamCache()
    dest = tmp_path / "vidbg.audio"
    calls = _patch_subprocess(monkeypatch, _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x")))

    await c.download("vidbg", "https://www.youtube.com/watch?v=vidbg")

    args = _ytdlp_call(calls)
    assert args[:6] == ("nice", "-n", "15", "ionice", "-c2", "-n7")
    assert args[args.index("--limit-rate") + 1] == "1M"
    assert args[-1] == "https://www.youtube.com/watch?v=vidbg"
    measure = next(c for c in calls if "ffmpeg" in c)
    assert measure[0] == "nice"  # the loudness pass after a download is low priority too


async def test_urgent_download_uses_the_faster_cap(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_cache, "_URGENT_RATE", "4M")
    c = StreamCache()
    dest = tmp_path / "vidnow.audio"
    calls = _patch_subprocess(monkeypatch, _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x")))

    await c.download("vidnow", "https://www.youtube.com/watch?v=vidnow", urgent=True)

    args = _ytdlp_call(calls)
    assert args[args.index("--limit-rate") + 1] == "4M"


async def test_a_zero_rate_means_unlimited(tmp_path, monkeypatch):
    monkeypatch.setattr(stream_cache, "_BACKGROUND_RATE", "0")
    c = StreamCache()
    dest = tmp_path / "vidfree.audio"
    calls = _patch_subprocess(monkeypatch, _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x")))

    await c.download("vidfree", "https://www.youtube.com/watch?v=vidfree")

    assert "--limit-rate" not in _ytdlp_call(calls)


async def test_background_downloads_run_one_at_a_time_and_urgent_ones_skip_the_queue(tmp_path, monkeypatch):
    c = StreamCache()
    release = asyncio.Event()
    started = []

    class _Blocking(_FakeProcess):
        def __init__(self, key):
            super().__init__(returncode=0, on_communicate=lambda: (tmp_path / f"{key}.audio").write_bytes(b"x"))
            self.key = key

        async def wait(self):
            if self.key == "bg1":
                await release.wait()
            return await super().wait()

    async def fake_exec(*args, **kwargs):
        if "yt_dlp" in args:
            key = args[args.index("-o") + 1].rsplit("/", 1)[-1].split(".")[0]
            started.append(key)
            return _Blocking(key)
        return _FakeMeasureProcess(_ebur128("-14.0"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    bg1 = asyncio.create_task(c.download("bg1", "https://y/bg1"))
    bg2 = asyncio.create_task(c.download("bg2", "https://y/bg2"))
    await asyncio.sleep(0.05)
    assert started == ["bg1"]  # bg2 waits for the single background slot

    assert await c.download("now1", "https://y/now1", urgent=True) is True
    assert started == ["bg1", "now1"]  # urgent didn't wait behind the background download

    release.set()
    assert await bg1 and await bg2
    assert started == ["bg1", "now1", "bg2"]


async def test_measure_gain_uses_the_fast_ebur128_filter(monkeypatch):
    calls = []

    async def fake_exec(*args, **kwargs):
        calls.append(args)
        return _FakeMeasureProcess(_ebur128("-14.0", "-3.0"))

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    assert await StreamCache._measure_gain(Path("/fake/path.audio")) == pytest.approx(1.0)
    args = calls[0]
    assert args[args.index("-af") + 1] == "ebur128=peak=true"


async def test_measure_gain_handles_a_silent_track(monkeypatch):
    # ffmpeg reports -70 LUFS and a -inf true peak for silence; the boost is
    # clamped to the +8dB limit and the missing peak never blocks it.
    monkeypatch.setattr(asyncio, "create_subprocess_exec", AsyncMock(return_value=_FakeMeasureProcess(_ebur128("-70.0", "-inf"))))

    assert await StreamCache._measure_gain(Path("/fake/path.audio")) == pytest.approx(10 ** (8 / 20))


# --- Patch 35: random YouTube 403s, failed-download cooldown, unmeasured songs


async def test_a_random_403_is_retried_and_then_succeeds(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    dest = tmp_path / "vid403.audio"
    refused = _FakeProcess(returncode=1, stderr_lines=[b"ERROR: unable to download video data: HTTP Error 403: Forbidden\n"])
    ok = _FakeProcess(returncode=0, on_communicate=lambda: dest.write_bytes(b"x"))
    calls = _patch_subprocess_sequence(monkeypatch, [refused, ok, _FakeMeasureProcess(_ebur128("-14.0"))])
    c = StreamCache()

    assert await c.download("vid403", "https://y/vid403") is True
    assert sum(1 for a in calls if "yt_dlp" in a) == 2
    assert c.is_ready("vid403")


async def test_gives_up_after_the_403_retries(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    calls = []

    async def always_refused(*args, **kwargs):
        calls.append(args)
        return _FakeProcess(returncode=1, stderr_lines=[b"ERROR: HTTP Error 403: Forbidden\n"])

    monkeypatch.setattr(asyncio, "create_subprocess_exec", always_refused)
    c = StreamCache()

    assert await c.download("vidNo", "https://y/vidNo") is False
    assert sum(1 for a in calls if "yt_dlp" in a) == len(stream_cache._RETRY_DELAYS_403) + 1


async def test_other_failures_are_not_retried(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    gone = _FakeProcess(returncode=1, stderr_lines=[b"ERROR: Video unavailable\n"])
    calls = _patch_subprocess(monkeypatch, gone)

    assert await StreamCache().download("vidGone", "https://y/vidGone") is False
    assert sum(1 for a in calls if "yt_dlp" in a) == 1


async def test_the_failure_cooldown_blocks_background_retries_but_not_urgent_ones(tmp_path, monkeypatch):
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    c = StreamCache()
    _patch_subprocess(monkeypatch, _FakeProcess(returncode=1, stderr_lines=[b"ERROR: Video unavailable\n"]))
    assert await c.download("vidC", "https://y/vidC") is False

    calls = _patch_subprocess(monkeypatch, _FakeProcess(returncode=0, on_communicate=lambda: (tmp_path / "vidC.audio").write_bytes(b"x")))
    assert await c.download("vidC", "https://y/vidC") is False  # background: still cooling down
    assert calls == []

    assert await c.download("vidC", "https://y/vidC", urgent=True) is True  # someone is waiting: try again


def test_typical_gain_is_the_median_of_measured_songs(tmp_path):
    for key, gain in (("a", 0.4), ("b", 0.5), ("c", 0.6), ("d", None)):
        (tmp_path / f"{key}.audio").write_bytes(b"x")
        if gain is not None:
            (tmp_path / f"{key}.meta.json").write_text(f'{{"gain": {gain}}}')
    c = StreamCache()
    c._adopt_existing_files()

    assert c.typical_gain() == pytest.approx(0.5)


def test_typical_gain_is_none_with_nothing_measured():
    assert StreamCache().typical_gain() is None


# --- Patch 36: invidious-companion fallback when yt-dlp is blocked


def test_rate_strings_are_parsed_like_yt_dlp():
    assert stream_cache._rate_bytes_per_sec("1M") == 1024 ** 2
    assert stream_cache._rate_bytes_per_sec("500K") == 500 * 1024
    assert stream_cache._rate_bytes_per_sec("2.5M") == 2.5 * 1024 ** 2
    assert stream_cache._rate_bytes_per_sec("0") is None
    assert stream_cache._rate_bytes_per_sec("") is None


async def test_no_resolver_keeps_the_yt_dlp_error(tmp_path, monkeypatch):
    _patch_subprocess(monkeypatch, _FakeProcess(returncode=1, stderr_lines=[b"ERROR: Sign in to confirm you're not a bot\n"]))
    c = StreamCache()

    assert await c.download("vidBlocked", "https://y/vidBlocked") is False
    assert "not a bot" in c._entries["vidBlocked"].error


async def test_companion_gives_no_link_keeps_both_errors(tmp_path, monkeypatch):
    _patch_subprocess(monkeypatch, _FakeProcess(returncode=1, stderr_lines=[b"ERROR: Sign in to confirm you're not a bot\n"]))
    monkeypatch.setattr(stream_cache, "_url_resolver", AsyncMock(return_value=None))
    c = StreamCache()

    assert await c.download("vidB2", "https://y/vidB2") is False
    error = c._entries["vidB2"].error
    assert "not a bot" in error and "companion" in error
    assert not (tmp_path / "vidB2.audio").exists() and not (tmp_path / "vidB2.audio.part").exists()
