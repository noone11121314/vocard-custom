"""Tests for the dashboard server override dashboard/overrides/objects.py
(Patch 43): a user may have several tabs/devices connected at once.

Run inside the stock dashboard image with the override mounted over
/app/objects.py (see run_sync_test.sh):
    python /tests/test_dashboard_user.py
"""
import asyncio
import json
import sys

sys.path.insert(0, "/app")
import main  # noqa: F401  (imports utils/objects in the order the app does)
import objects
from objects import User


class FakeWebsocket:
    def __init__(self, name):
        self.name, self.sent, self.closed = name, [], None
        self._inbox = asyncio.Queue()

    async def send_json(self, payload):
        if self.closed is not None:
            raise ConnectionError("closed")
        self.sent.append(payload)

    async def receive(self):
        item = await self._inbox.get()
        if item is None:
            raise asyncio.CancelledError()  # what a closed socket's receive does to the handler
        return json.dumps(item)

    async def close(self, code=1000):
        self.closed = code
        await self._inbox.put(None)

    def message(self, payload):
        self._inbox.put_nowait(payload)


class FakeGuild:
    def __init__(self):
        self.removed = []

    async def remove_user(self, user):
        self.removed.append(user.id)
        user.guild = None


async def run():
    broadcasts = []

    async def fake_broadcast(data):
        broadcasts.append(data)

    objects.BotPool.broadcast = fake_broadcast
    user = User(pool=None, data={"id": "111", "global_name": "me", "avatar": None})
    failures = 0

    def check(ok, text):
        nonlocal failures
        failures += not ok
        print(("PASS  " if ok else "FAIL  ") + text)

    a, b = FakeWebsocket("A"), FakeWebsocket("B")
    task_a = asyncio.create_task(user.connect(a))
    await asyncio.sleep(0.05)
    check(user.is_connected and broadcasts == [{"op": "initBot", "userId": "111"}], "first tab connects and the bots are asked to introduce themselves")

    user.bot = object()  # the first tab picked a bot
    broadcasts.clear()
    task_b = asyncio.create_task(user.connect(b))
    await asyncio.sleep(0.05)
    check(a.closed is None, "a second tab does NOT close the first (upstream closed it with 1004)")
    check(broadcasts == [{"op": "initBot", "userId": "111"}], "the second tab gets the bot introduction too")

    await user.send({"op": "initUser"})
    check(a.sent[-1:] == [{"op": "initUser"}] and b.sent[-1:] == [{"op": "initUser"}], "messages for the user reach every tab")

    guild = FakeGuild()
    user.guild = guild
    await a.close()
    await asyncio.sleep(0.05)
    check(task_a.done() and user.is_connected and guild.removed == [] and user.bot is not None,
          "closing one tab keeps the user in the server's player (other tab still open)")

    await user.send({"op": "trackUpdate"})
    check(b.sent[-1:] == [{"op": "trackUpdate"}], "the remaining tab still receives messages")

    await b.close()
    await asyncio.sleep(0.05)
    check(task_b.done() and not user.is_connected and guild.removed == ["111"] and user.bot is None,
          "closing the last tab removes the user from the player and resets the bot choice")

    c = FakeWebsocket("C")
    c.closed = 1000  # a tab whose socket died mid-send
    user._websockets.append(c)
    d = FakeWebsocket("D")
    user._websockets.append(d)
    await user.send({"op": "x"})
    check(d.sent == [{"op": "x"}], "one broken tab never stops messages reaching the others")
    return failures


if __name__ == "__main__":
    sys.exit(1 if asyncio.run(run()) else 0)
