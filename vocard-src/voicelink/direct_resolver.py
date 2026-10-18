"""Resolves direct, playable googlevideo.com URLs for YouTube tracks via
an invidious-companion instance, bypassing Lavalink's youtube-plugin bot
detection at stream-fetch time.

This is a local addition on top of stock Voicelink, not part of upstream Vocard.
"""

from __future__ import annotations

import os
import logging
from typing import Optional

import aiohttp

logger = logging.getLogger("vocard")

COMPANION_BASE_URL: str = os.getenv("INVIDIOUS_COMPANION_URL", "http://invidious-companion:8282").rstrip("/")
COMPANION_SECRET: str = os.getenv("INVIDIOUS_COMPANION_SECRET", "")

# Prefer opus/webm audio-only formats (matches what Lavalink's http source
# probes cleanly), falling back to highest-bitrate audio track available.
_PREFERRED_ITAGS = (251, 250, 249, 140)


async def resolve_youtube_direct_url(session: aiohttp.ClientSession, video_id: str) -> Optional[str]:
    """Returns a direct, directly-streamable audio URL for a YouTube video id,
    or None if resolution isn't possible/configured.
    """
    if not COMPANION_SECRET or not video_id:
        return None

    try:
        async with session.post(
            f"{COMPANION_BASE_URL}/companion/youtubei/v1/player",
            headers={"Authorization": f"Bearer {COMPANION_SECRET}"},
            json={"videoId": video_id},
            timeout=aiohttp.ClientTimeout(total=10),
        ) as resp:
            if resp.status != 200:
                logger.warning(f"[DirectResolver] companion returned HTTP {resp.status} for {video_id}")
                return None
            data = await resp.json(content_type=None)
    except Exception as e:
        logger.warning(f"[DirectResolver] companion request failed for {video_id}: {e}")
        return None

    if data.get("playabilityStatus", {}).get("status") != "OK":
        logger.warning(
            f"[DirectResolver] video {video_id} not playable via companion: "
            f"{data.get('playabilityStatus', {}).get('reason')}"
        )
        return None

    formats = data.get("streamingData", {}).get("adaptiveFormats", [])
    audio_formats = [f for f in formats if str(f.get("mimeType", "")).startswith("audio/") and f.get("url")]
    if not audio_formats:
        return None

    for itag in _PREFERRED_ITAGS:
        for f in audio_formats:
            if f.get("itag") == itag:
                return f["url"]

    audio_formats.sort(key=lambda f: f.get("bitrate", 0), reverse=True)
    return audio_formats[0]["url"]
