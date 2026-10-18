"""Unit tests for voicelink/session.py's save_last_session() (Patch 18:
queue/playback persistence across a bot restart).

Only the save side is unit tested here - see cogs/listeners.py's
restore_last_session_players() for the read-back side, which is one large,
untestably-entangled async method (real discord.py voice connect, Mongo
settings fetch, Track reconstruction, do_next()) in the same vein as
Player.do_next() itself; it's covered instead by manual verification after
deploy (see docs/local-patches.md's Patch 18).
"""
from __future__ import annotations

import json

from unittest.mock import MagicMock

from voicelink.pool import NodePool
from voicelink import session


def make_player(*, current=None, queue_is_empty=True, data=None, raise_on_data=False):
    player = MagicMock()
    player.current = current
    player.queue.is_empty = queue_is_empty
    player.guild = MagicMock(name="SomeGuild")
    if raise_on_data:
        type(player).data = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    else:
        player.data = data or {"guild_id": 1}
    return player


def make_node(players: dict):
    node = MagicMock()
    node._players = players
    return node


def test_saves_a_player_with_a_current_track(tmp_path, monkeypatch):
    dest = tmp_path / "last-session.json"
    monkeypatch.setattr(session.Config, "LAST_SESSION_FILE_DIR", str(dest))
    player = make_player(current=MagicMock(), queue_is_empty=True, data={"guild_id": 111})
    monkeypatch.setattr(NodePool, "_nodes", {"DEFAULT": make_node({111: player})})

    session.save_last_session()

    assert json.loads(dest.read_text()) == [{"guild_id": 111}]


def test_saves_a_player_with_a_nonempty_queue_even_without_a_current_track(tmp_path, monkeypatch):
    dest = tmp_path / "last-session.json"
    monkeypatch.setattr(session.Config, "LAST_SESSION_FILE_DIR", str(dest))
    player = make_player(current=None, queue_is_empty=False, data={"guild_id": 222})
    monkeypatch.setattr(NodePool, "_nodes", {"DEFAULT": make_node({222: player})})

    session.save_last_session()

    assert json.loads(dest.read_text()) == [{"guild_id": 222}]


def test_skips_a_player_with_neither_a_current_track_nor_a_queue(tmp_path, monkeypatch):
    dest = tmp_path / "last-session.json"
    monkeypatch.setattr(session.Config, "LAST_SESSION_FILE_DIR", str(dest))
    player = make_player(current=None, queue_is_empty=True)
    monkeypatch.setattr(NodePool, "_nodes", {"DEFAULT": make_node({333: player})})

    session.save_last_session()

    assert json.loads(dest.read_text()) == []


def test_saves_players_across_multiple_nodes(tmp_path, monkeypatch):
    dest = tmp_path / "last-session.json"
    monkeypatch.setattr(session.Config, "LAST_SESSION_FILE_DIR", str(dest))
    player_a = make_player(current=MagicMock(), data={"guild_id": 1})
    player_b = make_player(current=MagicMock(), data={"guild_id": 2})
    monkeypatch.setattr(NodePool, "_nodes", {
        "A": make_node({1: player_a}),
        "B": make_node({2: player_b}),
    })

    session.save_last_session()

    assert sorted(json.loads(dest.read_text()), key=lambda d: d["guild_id"]) == [{"guild_id": 1}, {"guild_id": 2}]


def test_continues_past_a_player_whose_data_property_raises(tmp_path, monkeypatch):
    dest = tmp_path / "last-session.json"
    monkeypatch.setattr(session.Config, "LAST_SESSION_FILE_DIR", str(dest))
    broken_player = make_player(current=MagicMock(), raise_on_data=True)
    good_player = make_player(current=MagicMock(), data={"guild_id": 99})
    monkeypatch.setattr(NodePool, "_nodes", {"DEFAULT": make_node({1: broken_player, 2: good_player})})

    session.save_last_session()  # must not raise

    assert json.loads(dest.read_text()) == [{"guild_id": 99}]


def test_never_raises_when_the_file_write_fails(monkeypatch):
    monkeypatch.setattr(session.Config, "LAST_SESSION_FILE_DIR", "/this/path/does/not/exist/last-session.json")
    player = make_player(current=MagicMock(), data={"guild_id": 1})
    monkeypatch.setattr(NodePool, "_nodes", {"DEFAULT": make_node({1: player})})

    session.save_last_session()  # must not raise


def test_writes_an_empty_list_when_there_are_no_players_at_all(tmp_path, monkeypatch):
    dest = tmp_path / "last-session.json"
    monkeypatch.setattr(session.Config, "LAST_SESSION_FILE_DIR", str(dest))
    monkeypatch.setattr(NodePool, "_nodes", {})

    session.save_last_session()

    assert json.loads(dest.read_text()) == []
