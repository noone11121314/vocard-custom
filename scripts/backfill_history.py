"""Rebuilds play/add history from logs into the HistoryEvents collection
(local-patches.md Patch 45). Safe to re-run: events are de-duplicated by a
unique key. Run inside the bot image on the vocard network (see the
comment at the bottom / docs). --dry-run prints what it would insert.

Sources (all times UTC):
  - Lavalink request logs (lavalink/logs, back to 2026-09-24): every song
    start as a PATCH with an encodedTrack - guild id, time, and the cached
    file's URL (which contains the YouTube video id). Restart-resumes start
    mid-song (position > 0) and are skipped. No requester.
  - Bot logs (logs/, since 2026-10-18): dashboard addTracks requests (who
    added which song, where) and trackUpdate messages.
  - The stored per-user "history" (last 25 plays) - used to credit plays.
"""
from __future__ import annotations

import argparse
import ast
import asyncio
import collections
import datetime as dt
import glob
import gzip
import json
import os
import re
import sys
import urllib.parse

sys.path.insert(0, "/app")
from voicelink.transformer import decode  # noqa: E402

UTC = dt.timezone.utc
LAVA_LINE = re.compile(r"^(\d{4}-\d\d-\d\dT[\d:.]+)Z .*PATCH /v4/sessions/\w+/players/(\d+)\S* .*payload=(\{.*\})\s*$")
BOT_LINE = re.compile(r"^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d) \[\s*DEBUG\s*\] vocard\.ipc_client: (Received|Sent) Message: (.*)$")
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def video_id_of(info: dict):
    uri = info.get("uri") or info.get("identifier") or ""
    if "/stream/" in uri:
        return uri.rsplit("/stream/", 1)[1].split("?")[0]
    if info.get("sourceName") == "youtube":
        return info.get("identifier")
    m = re.search(r"[?&]v=([\w-]{11})", uri)
    return m.group(1) if m else None


def lavalink_plays(log_dir, sizes):
    """sizes: cached file size -> video id. A streamed play's direct link
    carries clen (the exact audio file size), which identifies the song when
    the same file was cached later."""
    plays, unresolved = [], 0
    for path in sorted(glob.glob(os.path.join(log_dir, "spring.log*"))):
        opener = gzip.open if path.endswith(".gz") else open
        with opener(path, "rt", errors="replace") as fh:
            for line in fh:
                m = LAVA_LINE.match(line.strip())
                if not m or "encodedTrack" not in m.group(3):
                    continue
                try:
                    payload = json.loads(m.group(3))
                    if not payload.get("encodedTrack"):
                        continue  # a stop command
                    if int(float(payload.get("position") or 0)) > 0:
                        continue  # resume / seek-start, not a new play
                    info = decode(payload["encodedTrack"])
                except Exception:
                    unresolved += 1
                    continue
                vid = video_id_of(info)
                at = dt.datetime.fromisoformat(m.group(1)).replace(tzinfo=UTC)
                if not vid and "googlevideo" in (info.get("uri") or ""):
                    clen = urllib.parse.parse_qs(urllib.parse.urlparse(info["uri"]).query).get("clen", [None])[0]
                    vid = sizes.get(int(clen)) if clen and clen.isdigit() else None
                    if not vid:
                        plays.append({"kind": "played", "guild_id": int(m.group(2)), "video_id": None, "at": at, "user_id": None, "track_id": None, "source": "lavalink-log"})
                        continue
                if not vid:
                    unresolved += 1
                    continue
                plays.append({"kind": "played", "guild_id": int(m.group(2)), "video_id": vid, "at": at, "user_id": None, "track_id": None, "source": "lavalink-log"})
    return plays, unresolved


def bot_log_events(paths):
    adds, known, started = [], {}, []
    for path in paths:
        with open(path, errors="replace") as fh:
            for raw in fh:
                line = ANSI.sub("", raw).strip()
                m = BOT_LINE.match(line)
                if not m:
                    continue
                at = dt.datetime.strptime(m.group(1), "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
                body = m.group(3)
                try:
                    if m.group(2) == "Received":
                        jm = re.search(r"data='(\{.*\})'", body)
                        if not jm or '"addTracks"' not in jm.group(1):
                            continue
                        data = json.loads(jm.group(1))
                        for tid in data.get("tracks", []):
                            info = decode(tid)
                            vid = video_id_of(info)
                            if vid:
                                known.setdefault(vid, tid)
                                adds.append({"kind": "added", "guild_id": int(data["guildId"]), "user_id": int(data["userId"]), "video_id": vid, "track_id": tid, "at": at, "source": "bot-log"})
                    elif "'trackId'" in body:
                        data = ast.literal_eval(body)
                        tid = data.get("trackId")
                        if tid:
                            vid = video_id_of(decode(tid))
                            if vid:
                                known.setdefault(vid, tid)
                                if data.get("op") == "trackLoading" and data.get("guildId"):
                                    started.append((int(data["guildId"]), at, vid, tid))
                except Exception:
                    continue
    return adds, known, started


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--lavalink-logs", default="/lavalink-logs")
    ap.add_argument("--bot-logs", default="/bot-logs/vocard.log*")
    ap.add_argument("--mongo", default=os.environ.get("VOCARD_TEST_MONGO_URI"))
    ap.add_argument("--db", default="Vocard")
    ap.add_argument("--cache-dir", default="/cache")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--until", help="ISO UTC time: skip events from then on (the bot records those itself since Patch 45)")
    args = ap.parse_args()

    from motor.motor_asyncio import AsyncIOMotorClient
    client = AsyncIOMotorClient(args.mongo)
    db = client[args.db]

    sizes = {}
    for f in glob.glob(os.path.join(args.cache_dir, "*.audio")):
        sizes[os.path.getsize(f)] = os.path.basename(f)[:-6]
    plays, unresolved = lavalink_plays(args.lavalink_logs, sizes)
    adds, known, started = bot_log_events(sorted(glob.glob(args.bot_logs)))
    # Streamed plays still unidentified: the bot log's "loading <song>" line
    # in the same server within 30s (only since the bot logs were kept).
    for p in plays:
        if p["video_id"] is None:
            near = [s for s in started if s[0] == p["guild_id"] and abs((s[1] - p["at"]).total_seconds()) <= 30]
            if near:
                p["video_id"], p["track_id"] = near[0][2], near[0][3]
    unidentified = [p for p in plays if p["video_id"] is None]
    plays = [p for p in plays if p["video_id"] is not None]

    # Known YouTube track ids (with title/artwork) for each video id.
    stored_history = {}
    async for s in db.Settings.find({}):
        for key in ("botHistory", "uploadHistory", "addedHistory"):
            for tid in s.get(key, []) or []:
                known.setdefault(video_id_of(decode(tid)) or "", tid)
        for entry in (s.get("botTopCounts") or {}).values():
            if isinstance(entry, dict) and entry.get("trackId"):
                known.setdefault(video_id_of(decode(entry["trackId"])) or "", entry["trackId"])
    async for u in db.Users.find({}):
        stored_history[u["_id"]] = [video_id_of(decode(t)) for t in u.get("history", []) or []]
        for tid in u.get("history", []) or []:
            known.setdefault(video_id_of(decode(tid)) or "", tid)

    # Credit plays: the person who added that song in that server most
    # recently before it played (bot logs), then each user's 25 stored plays.
    adds_by_key = collections.defaultdict(list)
    for a in adds:
        adds_by_key[(a["guild_id"], a["video_id"])].append(a)
    for p in plays:
        prior = [a for a in adds_by_key.get((p["guild_id"], p["video_id"]), []) if a["at"] <= p["at"] and (p["at"] - a["at"]).total_seconds() < 6 * 3600]
        if prior:
            p["user_id"] = prior[-1]["user_id"]
    for user_id, vids in stored_history.items():
        for vid in reversed(vids):
            for p in sorted((p for p in plays if p["video_id"] == vid and p["user_id"] is None), key=lambda p: p["at"], reverse=True)[:1]:
                p["user_id"] = user_id

    events = plays + adds
    if args.until:
        until = dt.datetime.fromisoformat(args.until.replace("Z", "+00:00"))
        if until.tzinfo is None:
            until = until.replace(tzinfo=dt.timezone.utc)
        events = [e for e in events if (e["at"] if e["at"].tzinfo else e["at"].replace(tzinfo=dt.timezone.utc)) < until]
    missing = sorted({e["video_id"] for e in events if e["video_id"] not in known})
    print(f"plays from Lavalink logs: {len(plays)} identified, {len(unidentified)} streamed songs that can't be identified, {unresolved} other")
    print(f"adds from bot logs: {len(adds)}")
    print(f"distinct songs: {len({e['video_id'] for e in events})}, with stored metadata: {len({e['video_id'] for e in events}) - len(missing)}, to look up: {len(missing)}")
    credited = collections.Counter(e["user_id"] for e in events if e["kind"] == "played")
    print("plays credited per user:", dict(credited))
    span = sorted(e["at"] for e in events)
    print(f"time span: {span[0]:%Y-%m-%d %H:%M} .. {span[-1]:%Y-%m-%d %H:%M} UTC")
    if args.dry_run:
        return

    # Look up metadata for the rest through Lavalink (gently).
    import aiohttp
    async with aiohttp.ClientSession() as http:
        for vid in missing:
            q = urllib.parse.quote(f"https://www.youtube.com/watch?v={vid}")
            try:
                async with http.get(f"http://lavalink:2333/v4/loadtracks?identifier={q}", headers={"Authorization": os.environ.get("LAVALINK_PASSWORD", "youshallnotpass")}) as r:
                    data = await r.json()
                if data.get("loadType") == "track":
                    known[vid] = data["data"]["encoded"]
            except Exception:
                pass
            await asyncio.sleep(0.5)

    coll = db.HistoryEvents
    await coll.create_index([("user_id", -1), ("at", -1)])
    await coll.create_index([("guild_id", -1), ("at", -1)])
    await coll.create_index([("guild_id", 1), ("video_id", 1), ("kind", 1), ("at", 1)], unique=True)
    inserted = skipped = 0
    for e in events:
        e["track_id"] = e["track_id"] or known.get(e["video_id"])
        if not e["track_id"]:
            skipped += 1
            continue
        try:
            await coll.insert_one(dict(e))
            inserted += 1
        except Exception:
            skipped += 1  # already there (re-run) or invalid
    print(f"inserted {inserted}, skipped {skipped} (already present or no metadata)")


if __name__ == "__main__":
    asyncio.run(main())
