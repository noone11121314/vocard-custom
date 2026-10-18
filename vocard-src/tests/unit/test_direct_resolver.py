"""Unit tests for voicelink/direct_resolver.py: the invidious-companion HTTP call
and the audio-format selection logic, in isolation from Node/Lavalink/Player.
"""
from __future__ import annotations

import pytest

from voicelink import direct_resolver


class FakeResponse:
    """Stands in for aiohttp's request context manager."""

    def __init__(self, status: int = 200, payload: dict | None = None, raise_exc: Exception | None = None):
        self.status = status
        self._payload = payload or {}
        self._raise_exc = raise_exc

    async def json(self, content_type=None):
        return self._payload

    async def __aenter__(self):
        if self._raise_exc:
            raise self._raise_exc
        return self

    async def __aexit__(self, *exc_info):
        return False


class FakeSession:
    def __init__(self, response: FakeResponse):
        self._response = response
        self.last_call: dict | None = None

    def post(self, url, *, headers=None, json=None, timeout=None):
        self.last_call = {"url": url, "headers": headers, "json": json, "timeout": timeout}
        return self._response


@pytest.fixture(autouse=True)
def companion_configured(monkeypatch):
    """Every test gets a configured secret/base URL unless it overrides one."""
    monkeypatch.setattr(direct_resolver, "COMPANION_SECRET", "test-secret")
    monkeypatch.setattr(direct_resolver, "COMPANION_BASE_URL", "http://companion-test:8282")


async def test_returns_none_without_secret(monkeypatch):
    monkeypatch.setattr(direct_resolver, "COMPANION_SECRET", "")
    session = FakeSession(FakeResponse())

    result = await direct_resolver.resolve_youtube_direct_url(session, "vid123")

    assert result is None


async def test_returns_none_without_video_id():
    session = FakeSession(FakeResponse())

    result = await direct_resolver.resolve_youtube_direct_url(session, "")

    assert result is None


async def test_returns_none_on_non_200_status():
    session = FakeSession(FakeResponse(status=403))

    result = await direct_resolver.resolve_youtube_direct_url(session, "vid123")

    assert result is None


async def test_returns_none_when_not_playable():
    payload = {"playabilityStatus": {"status": "ERROR", "reason": "This video is blocked"}}
    session = FakeSession(FakeResponse(payload=payload))

    result = await direct_resolver.resolve_youtube_direct_url(session, "vid123")

    assert result is None


async def test_returns_none_when_no_audio_formats_present():
    payload = {
        "playabilityStatus": {"status": "OK"},
        "streamingData": {"adaptiveFormats": [{"mimeType": "video/mp4", "url": "http://video-only"}]},
    }
    session = FakeSession(FakeResponse(payload=payload))

    result = await direct_resolver.resolve_youtube_direct_url(session, "vid123")

    assert result is None


async def test_returns_none_when_audio_format_has_no_url():
    payload = {
        "playabilityStatus": {"status": "OK"},
        "streamingData": {"adaptiveFormats": [{"mimeType": "audio/webm", "itag": 251}]},
    }
    session = FakeSession(FakeResponse(payload=payload))

    result = await direct_resolver.resolve_youtube_direct_url(session, "vid123")

    assert result is None


async def test_prefers_itags_in_configured_priority_order():
    # itag 251 sits before 249 in _PREFERRED_ITAGS, so it must win even though
    # it appears later in the list returned by the companion.
    payload = {
        "playabilityStatus": {"status": "OK"},
        "streamingData": {
            "adaptiveFormats": [
                {"mimeType": "audio/webm", "url": "http://itag-249", "itag": 249, "bitrate": 50_000},
                {"mimeType": "audio/webm", "url": "http://itag-251", "itag": 251, "bitrate": 128_000},
                {"mimeType": "audio/mp4", "url": "http://itag-140", "itag": 140, "bitrate": 128_000},
            ]
        },
    }
    session = FakeSession(FakeResponse(payload=payload))

    result = await direct_resolver.resolve_youtube_direct_url(session, "vid123")

    assert result == "http://itag-251"


async def test_falls_back_to_highest_bitrate_when_no_preferred_itag_present():
    payload = {
        "playabilityStatus": {"status": "OK"},
        "streamingData": {
            "adaptiveFormats": [
                {"mimeType": "audio/webm", "url": "http://mid", "itag": 999, "bitrate": 96_000},
                {"mimeType": "audio/webm", "url": "http://high", "itag": 998, "bitrate": 160_000},
                {"mimeType": "audio/webm", "url": "http://low", "itag": 997, "bitrate": 48_000},
            ]
        },
    }
    session = FakeSession(FakeResponse(payload=payload))

    result = await direct_resolver.resolve_youtube_direct_url(session, "vid123")

    assert result == "http://high"


async def test_returns_none_on_request_exception():
    session = FakeSession(FakeResponse(raise_exc=ConnectionError("companion unreachable")))

    result = await direct_resolver.resolve_youtube_direct_url(session, "vid123")

    assert result is None


async def test_sends_expected_request_shape():
    payload = {
        "playabilityStatus": {"status": "OK"},
        "streamingData": {"adaptiveFormats": [{"mimeType": "audio/webm", "url": "http://ok", "itag": 251, "bitrate": 100_000}]},
    }
    session = FakeSession(FakeResponse(payload=payload))

    await direct_resolver.resolve_youtube_direct_url(session, "vid999")

    assert session.last_call["url"] == "http://companion-test:8282/companion/youtubei/v1/player"
    assert session.last_call["headers"]["Authorization"] == "Bearer test-secret"
    assert session.last_call["json"] == {"videoId": "vid999"}
