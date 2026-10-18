"""Unit tests for voicelink/ipc/client.py's dashboard reconnect (Patch 29).

Before: a failed first connect never retried, and a dropped connection got
one attempt 10s later. A dashboard restart that took longer than that, or a
rejected handshake (2026-10-18: a blanked settings.json "version"), left the
bot off the dashboard until it was itself restarted.
"""
from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import aiohttp

from voicelink.ipc.client import IPCClient


def make_client() -> IPCClient:
    bot = MagicMock()
    bot.user.id = 42
    bot.loop = asyncio.get_event_loop()
    return IPCClient(bot, "dashboard", 8000, "pw")


def test_sends_the_running_code_version_not_the_settings_marker():
    import update

    client = make_client()

    assert client._heanders["Client-Version"] == update.__version__


async def test_keeps_retrying_until_connected(monkeypatch):
    client = make_client()
    monkeypatch.setattr(asyncio, "sleep", AsyncMock())
    attempts = []

    async def fake_connect():
        attempts.append(1)
        if len(attempts) == 3:
            client._is_connected = True

    client.connect = fake_connect

    client._schedule_reconnect()
    await client._reconnect_task

    assert len(attempts) == 3
    assert client._is_connected


async def test_only_one_reconnect_loop_runs_at_a_time(monkeypatch):
    client = make_client()
    gate = asyncio.Event()

    async def blocked_sleep(_):
        await gate.wait()

    monkeypatch.setattr(asyncio, "sleep", blocked_sleep)
    client.connect = AsyncMock(side_effect=lambda: setattr(client, "_is_connected", True))

    client._schedule_reconnect()
    first = client._reconnect_task
    client._schedule_reconnect()

    assert client._reconnect_task is first
    gate.set()
    await first


async def test_a_rejected_handshake_schedules_a_retry(monkeypatch):
    client = make_client()
    session = MagicMock()
    session.ws_connect = AsyncMock(side_effect=aiohttp.WSServerHandshakeError(MagicMock(), (), status=403))
    client._session = session
    scheduled = MagicMock()
    client._schedule_reconnect = scheduled

    await client.connect()

    scheduled.assert_called_once()
    assert not client._is_connected


async def test_a_closed_connection_schedules_a_retry_and_stops_listening():
    client = make_client()
    client._is_connected = True
    client._websocket = MagicMock()
    client._websocket.receive = AsyncMock(return_value=MagicMock(type=aiohttp.WSMsgType.CLOSED))
    scheduled = MagicMock()
    client._schedule_reconnect = scheduled

    await asyncio.wait_for(client._listen(), timeout=1)

    scheduled.assert_called_once()
    assert not client._is_connected
