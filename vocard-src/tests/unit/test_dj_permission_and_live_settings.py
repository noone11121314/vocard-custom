"""Unit tests for Patch 32: a real DJ (has the configured DJ role on Discord)
was refused every queue edit because the bot's cached Member lacked the
role, and settings changes (e.g. setting the DJ role) were ignored by an
already-running player until a restart.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, MagicMock

from voicelink.ipc.methods import _is_privileged_fresh
from voicelink.mongodb import MongoDBHandler
from voicelink.pool import NodePool

DJ_ROLE = 999000111


def make_member(role_ids, member_id=222):
    m = MagicMock()
    m.id = member_id
    m.roles = [types.SimpleNamespace(id=r) for r in role_ids]
    return m


def make_player(dj_role=DJ_ROLE):
    player = MagicMock()
    player.settings = {"dj": dj_role} if dj_role else {}
    player.is_privileged = lambda member, check_user_join=True: (
        bool(player.settings.get("dj")) and any(r.id == player.settings["dj"] for r in member.roles)
    )
    return player


async def test_a_stale_cached_member_is_rechecked_against_discord():
    cached = make_member([])
    fresh = make_member([DJ_ROLE])
    cached.guild.fetch_member = AsyncMock(return_value=fresh)
    player = make_player()

    assert await _is_privileged_fresh(player, cached) is True
    cached.guild.fetch_member.assert_awaited_once_with(cached.id)
    cached.guild._add_member.assert_called_once_with(fresh)


async def test_still_refused_when_discord_confirms_no_dj_role():
    cached = make_member([])
    cached.guild.fetch_member = AsyncMock(return_value=make_member([]))

    assert await _is_privileged_fresh(make_player(), cached) is False


async def test_no_extra_call_when_already_privileged():
    member = make_member([DJ_ROLE])
    member.guild.fetch_member = AsyncMock()

    assert await _is_privileged_fresh(make_player(), member) is True
    member.guild.fetch_member.assert_not_awaited()


async def test_no_extra_call_when_no_dj_role_is_configured():
    member = make_member([])
    member.guild.fetch_member = AsyncMock()

    assert await _is_privileged_fresh(make_player(dj_role=None), member) is False
    member.guild.fetch_member.assert_not_awaited()


def test_settings_updates_reach_the_running_player(monkeypatch):
    player = MagicMock(settings={"lang": "EN"})
    other = MagicMock(settings={})
    node = MagicMock(_players={555: player, 777: other})
    monkeypatch.setattr(NodePool, "_nodes", {"DEFAULT": node})

    NodePool.apply_settings_update(555, {"$set": {"dj": DJ_ROLE, "botTopCounts.x.count": 3}})
    assert player.settings == {"lang": "EN", "dj": DJ_ROLE}
    assert other.settings == {}

    NodePool.apply_settings_update(555, {"$unset": {"dj": None}})
    assert "dj" not in player.settings


async def test_update_settings_notifies_listeners(monkeypatch):
    monkeypatch.setattr(MongoDBHandler, "get_settings", AsyncMock(return_value={"_id": 555}))
    monkeypatch.setattr(MongoDBHandler, "_update_db", AsyncMock(return_value=True))
    seen = []
    monkeypatch.setattr(MongoDBHandler, "_settings_listeners", [lambda gid, data: seen.append((gid, data))])

    await MongoDBHandler.update_settings(555, {"$set": {"dj": DJ_ROLE}})

    assert seen == [(555, {"$set": {"dj": DJ_ROLE}})]


async def test_a_failing_listener_never_breaks_the_save(monkeypatch):
    monkeypatch.setattr(MongoDBHandler, "get_settings", AsyncMock(return_value={"_id": 555}))
    monkeypatch.setattr(MongoDBHandler, "_update_db", AsyncMock(return_value=True))
    monkeypatch.setattr(MongoDBHandler, "_settings_listeners", [MagicMock(side_effect=RuntimeError("boom"))])

    assert await MongoDBHandler.update_settings(555, {"$set": {"lang": "VI"}}) is True
