"""Unit tests for voicelink.pool.NodePool.is_video_queued() - the check
voicelink/stream_cache.py's eviction uses (via set_queued_checker) to avoid
deleting a cached download that's still current/upcoming in some player's
queue. Nodes/players are duck-typed SimpleNamespaces, not real Node/Player
instances - is_video_queued() only ever touches `.players` and
`.queue.tracks(incTrack=True)`.
"""
from __future__ import annotations

import types

import pytest

from voicelink.pool import NodePool
from tests.conftest import make_track


@pytest.fixture(autouse=True)
def reset_nodepool():
    """NodePool._nodes is a class-level dict, shared across the whole
    process - tests must not leak fake nodes into each other (or into any
    other test file that happens to run in the same session).
    """
    original = dict(NodePool._nodes)
    NodePool._nodes.clear()
    yield
    NodePool._nodes.clear()
    NodePool._nodes.update(original)


def _fake_node(players: dict) -> types.SimpleNamespace:
    return types.SimpleNamespace(players=players)


def _fake_player(tracks: list) -> types.SimpleNamespace:
    queue = types.SimpleNamespace(tracks=lambda incTrack=False: list(tracks))
    return types.SimpleNamespace(queue=queue)


def test_is_video_queued_false_when_no_nodes_exist():
    assert NodePool.is_video_queued("vid1") is False


def test_is_video_queued_false_when_nothing_matches():
    player = _fake_player([make_track(identifier="vidOther", source="youtube")])
    NodePool._nodes["n1"] = _fake_node({111: player})

    assert NodePool.is_video_queued("vid1") is False


def test_is_video_queued_true_for_the_currently_playing_track():
    player = _fake_player([make_track(identifier="vid1", source="youtube")])
    NodePool._nodes["n1"] = _fake_node({111: player})

    assert NodePool.is_video_queued("vid1") is True


def test_is_video_queued_true_for_an_upcoming_track_in_a_different_guild():
    player_a = _fake_player([make_track(identifier="vidOther", source="youtube")])
    player_b = _fake_player([
        make_track(identifier="vidCurrent", source="youtube"),
        make_track(identifier="vid1", source="youtube"),
    ])
    NodePool._nodes["n1"] = _fake_node({111: player_a, 222: player_b})

    assert NodePool.is_video_queued("vid1") is True


def test_is_video_queued_false_for_a_non_youtube_track_with_the_same_identifier():
    player = _fake_player([make_track(identifier="vid1", source="soundcloud")])
    NodePool._nodes["n1"] = _fake_node({111: player})

    assert NodePool.is_video_queued("vid1") is False


def test_is_video_queued_checks_across_multiple_nodes():
    player = _fake_player([make_track(identifier="vid1", source="youtube")])
    NodePool._nodes["n1"] = _fake_node({})
    NodePool._nodes["n2"] = _fake_node({111: player})

    assert NodePool.is_video_queued("vid1") is True
