"""Unit tests for getUserHistory in voicelink/ipc/methods.py - a separate,
personal "Recently played by you" log (see local-patches.md). Distinct from
getGuildHistory (guild-wide, every member's plays, tested in
test_ipc_guild_history_methods.py): this one just reads back the per-user
`history` field Player.do_next() has always written to each non-bot
requester's own Mongo document.
"""
from __future__ import annotations

from unittest.mock import AsyncMock

from voicelink.ipc.methods import getUserHistory


async def test_returns_the_users_own_history(monkeypatch):
    get_user = AsyncMock(return_value=["ENC_A", "ENC_B"])
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_user", get_user)

    result = await getUserHistory(bot=None, data={"userId": "42"})

    get_user.assert_any_await(42, d_type="history")
    get_user.assert_any_await(42, d_type="addedHistory")  # Patch 44
    assert result["op"] == "getUserHistory" and result["userId"] == "42"
    assert result["history"] == ["ENC_A", "ENC_B"]


async def test_defaults_to_an_empty_list_when_the_user_has_no_history(monkeypatch):
    get_user = AsyncMock(return_value=[])
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_user", get_user)

    result = await getUserHistory(bot=None, data={"userId": "42"})

    assert result["history"] == []


async def test_defaults_to_an_empty_list_when_the_field_is_falsy_none(monkeypatch):
    """get_user's d_type lookup should never return None for a known field
    (it setdefaults to a deep copy of the base shape, []), but guard against
    it anyway rather than ever send the frontend a bare None to iterate.
    """
    get_user = AsyncMock(return_value=None)
    monkeypatch.setattr("voicelink.ipc.methods.MongoDBHandler.get_user", get_user)

    result = await getUserHistory(bot=None, data={"userId": "42"})

    assert result["history"] == []
