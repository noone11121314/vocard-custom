"""Integration tests for voicelink/ipc/client.py: the real IPCClient against
a real (in-process) websocket server standing in for the dashboard.

Guards against the 2026-10-18 outage (Patch 29): the bot sent a blank
settings.json version as Client-Version, the dashboard rejected it, and the
client never retried - the website couldn't reach either bot until they
were restarted. Also covers reconnecting after the dashboard drops the
connection (e.g. a dashboard restart).
"""
from __future__ import annotations

import asyncio
import json
import socket
from unittest.mock import MagicMock

import pytest
from aiohttp import WSMsgType, web

import update
from voicelink.ipc import client as ipc_client_module
from voicelink.ipc.client import IPCClient

pytestmark = pytest.mark.integration

_real_sleep = asyncio.sleep


@pytest.fixture(autouse=True)
def fast_retries(monkeypatch):
    """The client waits 10s between attempts; keep the real behaviour but
    shrink the waits so the test runs in well under a second."""
    async def quick_sleep(delay, *args, **kwargs):
        return await _real_sleep(min(delay, 0.05), *args, **kwargs)

    monkeypatch.setattr(ipc_client_module.asyncio, "sleep", quick_sleep)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeDashboard:
    """Minimal /ws_bot endpoint: records each handshake's headers, rejects
    the first `reject_first` attempts with 403 (like a version mismatch),
    then accepts and sends one message."""

    def __init__(self, reject_first: int = 0):
        self.reject_first = reject_first
        self.attempts = []
        self.connections = 0
        self.received = []
        self.sockets = []
        self.connected = asyncio.Event()

    async def handler(self, request):
        self.attempts.append(dict(request.headers))
        if len(self.attempts) <= self.reject_first:
            return web.Response(status=403)
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        self.connections += 1
        self.sockets.append(ws)
        self.connected.set()
        await ws.send_json({"op": "initBot", "userId": "42"})
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                self.received.append(json.loads(msg.data))
        return ws


async def _start(dashboard: FakeDashboard):
    app = web.Application()
    app.router.add_get("/ws_bot", dashboard.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    port = _free_port()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    return runner, port


def _client(port: int) -> IPCClient:
    bot = MagicMock()
    bot.user.id = 1480921697505906758
    bot.loop = asyncio.get_running_loop()
    return IPCClient(bot, "127.0.0.1", port, "secret-pw")


async def _wait_for(condition, timeout=3.0):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while not condition():
        if loop.time() > end:
            raise AssertionError("condition not met in time")
        await _real_sleep(0.02)


async def test_handshake_sends_the_running_code_version_and_password(monkeypatch):
    dispatched = []
    monkeypatch.setattr(ipc_client_module, "process_methods", lambda c, b, data: _record(dispatched, data))
    dashboard = FakeDashboard()
    runner, port = await _start(dashboard)
    client = _client(port)
    try:
        await client.connect()
        await _wait_for(lambda: dispatched)
        headers = dashboard.attempts[0]
        assert headers["Client-Version"] == update.__version__
        assert headers["Authorization"] == "secret-pw"
        assert headers["User-Id"] == "1480921697505906758"
        assert dispatched == [{"op": "initBot", "userId": "42"}]

        await client.send({"op": "hello"})
        await _wait_for(lambda: dashboard.received)
        assert dashboard.received == [{"op": "hello"}]
    finally:
        await client.disconnect()
        await runner.cleanup()


async def test_keeps_retrying_after_rejected_handshakes_until_accepted(monkeypatch):
    monkeypatch.setattr(ipc_client_module, "process_methods", lambda c, b, data: _record([], data))
    dashboard = FakeDashboard(reject_first=2)
    runner, port = await _start(dashboard)
    client = _client(port)
    try:
        await client.connect()  # rejected - must schedule retries, not give up
        await _wait_for(lambda: client.is_connected)
        assert len(dashboard.attempts) == 3
    finally:
        await client.disconnect()
        await runner.cleanup()


async def test_reconnects_after_the_dashboard_drops_the_connection(monkeypatch):
    monkeypatch.setattr(ipc_client_module, "process_methods", lambda c, b, data: _record([], data))
    dashboard = FakeDashboard()
    runner, port = await _start(dashboard)
    client = _client(port)
    try:
        await client.connect()
        await _wait_for(lambda: dashboard.connections == 1)
        await dashboard.sockets[0].close()  # e.g. the dashboard restarting
        await _wait_for(lambda: dashboard.connections == 2)
        await _wait_for(lambda: client.is_connected)
    finally:
        await client.disconnect()
        await runner.cleanup()


async def test_retries_when_the_dashboard_is_not_up_yet(monkeypatch):
    monkeypatch.setattr(ipc_client_module, "process_methods", lambda c, b, data: _record([], data))
    port = _free_port()
    client = _client(port)
    with pytest.raises(Exception):
        await client.connect()  # nothing listening yet
    dashboard = FakeDashboard()
    app = web.Application()
    app.router.add_get("/ws_bot", dashboard.handler)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", port).start()
    try:
        await _wait_for(lambda: client.is_connected)
        assert dashboard.connections == 1
    finally:
        await client.disconnect()
        await runner.cleanup()


async def _record(sink, data):
    sink.append(data)


async def test_a_deliberate_disconnect_closes_the_socket_and_does_not_reconnect(monkeypatch):
    """Found by this suite (Patch 34): disconnect() used to leave the socket
    open on the dashboard's side, and the listener treated its own
    cancellation as a dropped connection and reconnected straight away."""
    monkeypatch.setattr(ipc_client_module, "process_methods", lambda c, b, data: _record([], data))
    dashboard = FakeDashboard()
    runner, port = await _start(dashboard)
    client = _client(port)
    try:
        await client.connect()
        await _wait_for(lambda: dashboard.connections == 1)
        await client.disconnect()
        await _wait_for(lambda: dashboard.sockets[0].closed)
        await _real_sleep(0.3)  # several retry intervals at the shortened wait
        assert dashboard.connections == 1
        assert not client.is_connected
    finally:
        await runner.cleanup()
