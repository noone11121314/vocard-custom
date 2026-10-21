"""Patch 49: the voice channel status (and every other {{ ?? // }}
template) came out empty for a song title with an apostrophe, because the
value was wrapped in '...' before the condition was eval'd."""
from __future__ import annotations

import pytest

from voicelink.placeholders import PlayerPlaceholder

STATUS = "{{@@track_name@@ != 'None' ?? @@track_source_emoji@@ Now Playing: @@track_name@@ // Waiting for song requests}}"


@pytest.fixture
def ph() -> PlayerPlaceholder:
    ph = PlayerPlaceholder.__new__(PlayerPlaceholder)
    ph.regexes = {}
    return ph


@pytest.mark.parametrize("title", [
    "Normal Song",
    "Kiss the rails, girls, I'm going home",
    'The "Quoted" One',
    "Rock 'n' Roll \"Mix\"",
    "Back\\slash",
    "Целуйте бабы рельсы, я еду домой",
])
def test_status_shows_any_title(ph, title):
    out = ph.replace(STATUS, {"track_name": title, "track_source_emoji": "🎵"})
    assert out == f"🎵 Now Playing: {title}"


def test_status_when_nothing_plays(ph):
    assert ph.replace(STATUS, {"track_name": "None", "track_source_emoji": "None"}) == "Waiting for song requests"


def test_numeric_comparisons_still_work(ph):
    tpl = "{{@@volume@@ > 50 ?? loud // quiet}}"
    assert ph.replace(tpl, {"volume": 80}) == "loud"
    assert ph.replace(tpl, {"volume": "20"}) == "quiet"


def test_title_cannot_inject_code(ph):
    out = ph.replace(STATUS, {"track_name": "x' or __import__('os') or '", "track_source_emoji": "🎵"})
    assert out == "🎵 Now Playing: x' or __import__('os') or '"
