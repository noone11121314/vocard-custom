"""Issue #3: the bot must keep trying a Lavalink node that isn't up yet at
startup (after a reboot the bot can start before Lavalink), and the
last-session restore must wait until a node is connected."""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest

import voicelink
from cogs import listeners as listeners_mod
from cogs.listeners import Listeners
from voicelink.pool import NodePool

NODE = {"identifier": "DEFAULT", "host": "lavalink", "port": 2333, "password": "x"}


def make_cog(create_node) -> Listeners:
    cog = Listeners.__new__(Listeners)
    cog.bot = MagicMock()
    cog.voicelink = MagicMock()
    cog.voicelink.create_node = create_node
    cog._node_ready = asyncio.Event()
    return cog


@pytest.fixture
def no_sleep():
    sleeps = []
    real_sleep = asyncio.sleep

    async def fake_sleep(s):
        sleeps.append(s)
        await real_sleep(0)  # let other tasks run, like a real wait would

    with patch.object(listeners_mod.asyncio, "sleep", fake_sleep):
        yield sleeps


async def test_retries_until_node_connects(no_sleep):
    fail = voicelink.NodeConnectionFailure("The connection to node 'DEFAULT' failed.")
    create = AsyncMock(side_effect=[fail, fail, fail, MagicMock()])
    cog = make_cog(create)

    await cog._start_node(NODE)

    assert create.await_count == 4
    assert cog._node_ready.is_set()
    assert no_sleep == [2, 4, 8]


async def test_backoff_is_capped(no_sleep):
    fail = voicelink.NodeConnectionFailure("down")
    create = AsyncMock(side_effect=[fail] * 6 + [MagicMock()])
    cog = make_cog(create)

    await cog._start_node(NODE)

    assert no_sleep == [2, 4, 8, 16, 30, 30]


async def test_connects_first_time_without_waiting(no_sleep):
    cog = make_cog(AsyncMock(return_value=MagicMock()))

    await cog._start_node(NODE)

    assert no_sleep == []
    assert cog._node_ready.is_set()


async def test_node_already_in_pool_counts_as_ready(no_sleep):
    cog = make_cog(AsyncMock(side_effect=voicelink.NodeCreationError("exists")))

    await cog._start_node(NODE)

    assert cog._node_ready.is_set()
    assert no_sleep == []


async def test_one_down_node_does_not_hold_up_another(no_sleep):
    calls = []

    async def create(bot, **n):
        calls.append(n["identifier"])
        if n["identifier"] == "A" and calls.count("A") < 3:
            raise voicelink.NodeConnectionFailure("down")
        return MagicMock()

    cog = make_cog(create)
    nodes = {"A": dict(NODE, identifier="A"), "B": dict(NODE, identifier="B")}
    with patch.object(listeners_mod, "Config", return_value=MagicMock(nodes=nodes)):
        await cog.start_nodes()

    assert calls.index("B") < len(calls) - 1  # B tried before A finally made it
    assert calls.count("A") == 3


async def test_restore_waits_for_a_node():
    cog = make_cog(AsyncMock())
    cog.bot.wait_until_ready = AsyncMock()
    opened = []
    with patch.object(listeners_mod.func, "open_json", side_effect=lambda *_: opened.append(1) or []):
        task = asyncio.create_task(cog.restore_last_session_players())
        await asyncio.sleep(0.01)
        assert opened == []  # still waiting for a node
        cog._node_ready.set()
        await asyncio.wait_for(task, 1)
    assert opened == [1]


async def test_failed_create_node_closes_its_session():
    NodePool._nodes.pop("RETRY_TEST", None)
    with patch.object(voicelink.pool.Node, "connect", AsyncMock(side_effect=voicelink.NodeConnectionFailure("down"))):
        with patch("voicelink.pool.aiohttp.ClientSession") as session_cls:
            session_cls.return_value.close = AsyncMock()
            with pytest.raises(voicelink.NodeConnectionFailure):
                await NodePool.create_node(bot=MagicMock(), host="h", port=1, password="p", identifier="RETRY_TEST")
    session_cls.return_value.close.assert_awaited_once()
    assert "RETRY_TEST" not in NodePool._nodes
