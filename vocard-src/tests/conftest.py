"""Shared fixtures for the direct-URL-resolution feature test suite.

Covers voicelink/direct_resolver.py, the resolve/prefetch/cache additions on
voicelink.pool.Node, and the play()/prefetch integration on voicelink.player.Player.
"""
from __future__ import annotations

import types
from unittest.mock import MagicMock

import pytest

from voicelink.pool import Node


def make_track(
    *,
    source: str = "youtube",
    identifier: str | None = "vid123",
    track_id: str = "NATIVE_ENCODED",
    title: str = "Test Track",
    uri: str | None = None,
    length: int = 200_000,
    position: int = 0,
    end_time: int | None = None,
):
    """A duck-typed stand-in for voicelink.objects.Track.

    The real Track requires a loaded Config() singleton (for search_platform /
    source emoji lookups) which unit tests shouldn't need to stand up just to
    exercise play()'s direct-resolve branch. Only the attributes that
    Player.play() and Player._prefetch_next_direct_track() actually read are
    provided here.
    """
    requester = MagicMock()
    requester.bot = True
    requester.id = 111

    return types.SimpleNamespace(
        source=source,
        identifier=identifier,
        track_id=track_id,
        title=title,
        uri=uri or f"https://www.youtube.com/watch?v={identifier}",
        length=length,
        position=position,
        end_time=end_time,
        requester=requester,
    )


@pytest.fixture
def node() -> Node:
    """A real Node instance with its network-facing bits swapped for mocks.

    Node.__init__ does no I/O itself (no websocket connect, no HTTP calls), so
    constructing a real instance and only faking bot/session/logger is enough
    to exercise the real resolve_direct_track / prefetch_direct_track /
    _prune_direct_resolve_cache implementations.
    """
    bot = MagicMock()
    bot.user.id = 999999

    return Node(
        pool=MagicMock(),
        bot=bot,
        host="lavalink-test",
        port=2333,
        password="test-password",
        identifier="TEST",
        session=MagicMock(),
        logger=MagicMock(),
    )
