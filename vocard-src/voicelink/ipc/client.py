import aiohttp
import asyncio
import logging
import voicelink

from discord.ext import commands
from typing import Optional

from .methods import process_methods

class IPCClient:
    def __init__(
        self,
        bot: commands.Bot,
        host: str,
        port: int,
        password: str,
        heartbeat: int = 30,
        secure: bool = False,
        *arg,
        **kwargs
    ) -> None:
        
        self._bot: commands.Bot = bot
        self._host: str = host
        self._port: int = port
        self._password: str = password
        self._heartbeat: int = heartbeat
        self._is_secure: bool = secure
        self._is_connected: bool = False
        self._is_connecting: bool = False
        self._logger: logging.Logger = logging.getLogger("vocard.ipc_client")
        
        self._websocket_url: str = f"{'wss' if self._is_secure else 'ws'}://{self._host}:{self._port}/ws_bot"
        self._session: Optional[aiohttp.ClientSession] = None
        self._websocket: Optional[aiohttp.ClientWebSocketResponse] = None
        self._task: Optional[asyncio.Task] = None
        self._reconnect_task: Optional[asyncio.Task] = None
        self._closing: bool = False  # set by disconnect(): no auto-reconnect

        self._heanders = {
            "Authorization": self._password,
            "User-Id": str(bot.user.id),
            "Client-Version": self._running_version()
        }

    @staticmethod
    def _running_version() -> str:
        """Patch 29: the version of the code actually running. settings.json's
        "version" is only a last-synced-commands marker (main.py rewrites it
        after a slash-command sync); sending it made the dashboard reject the
        bot whenever it was blank or stale."""
        try:
            import update
            return update.__version__
        except Exception:
            return voicelink.Config().version

    def _schedule_reconnect(self) -> None:
        """Patch 29: keeps retrying every 10s until connected. Before, a failed
        first connect (e.g. dashboard down or a rejected handshake) never
        retried, and a dropped connection got exactly one attempt - if the
        dashboard was still restarting then, the bot stayed off the
        dashboard until it was itself restarted."""
        if self._closing:
            return
        if self._reconnect_task and not self._reconnect_task.done():
            return
        self._reconnect_task = self._bot.loop.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        while not self._is_connected and not self._closing:
            await asyncio.sleep(10)
            if self._closing:
                return
            try:
                await self.connect()
            except Exception:
                pass

    async def _listen(self) -> None:
        while True:
            try:
                msg = await self._websocket.receive()
                self._logger.debug(f"Received Message: {msg}")
            except asyncio.CancelledError:
                raise  # disconnect() cancelling the listener - not a dropped connection
            except:
                self._is_connected = False
                self._schedule_reconnect()
                break

            if msg.type in [aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR]:
                self._is_connected = False
                self._logger.info("Connection closed. Trying to reconnect every 10s.")
                self._schedule_reconnect()
                break
            else:
                self._bot.loop.create_task(process_methods(self, self._bot, msg.json()))

    async def send(self, data: dict):
        if self.is_connected:
            try:
                await self._websocket.send_json(data)
                self._logger.debug(f"Send Message: {data}")
            except ConnectionResetError as _:
                await self.disconnect()
                await self.connect()
                await self._websocket.send_json(data)
                self._logger.debug(f"Send Message: {data}")

    async def send(self, data: dict):
        # Check if the websocket is still open
        if self.is_connected:
            try:
                await self._websocket.send_json(data)
                self._logger.debug(f"Sent Message: {data}")
            except ConnectionResetError:
                self._logger.warning("Connection lost, attempting to reconnect.")
                await self._handle_reconnect(data)
            except Exception as e:
                self._logger.error(f"Failed to send message: {e}")
        else:
            self._logger.warning("WebSocket is not connected or already closed.")

    async def _handle_reconnect(self, data: dict):
        await self.disconnect()
        await self.connect()
        await asyncio.sleep(1)  # Optional delay before retrying
        if self.is_connected:
            try:
                await self._websocket.send_json(data)
                self._logger.debug(f"Sent Message on reconnect: {data}")
            except Exception as e:
                self._logger.error(f"Failed to send message on reconnect: {e}")
        else:
            self._logger.error("Reconnection failed, not connected.")
                    
    async def connect(self):    
        try:
            if not self._session:
                self._session = aiohttp.ClientSession()

            if self._is_connecting or self._is_connected:
                return

            self._closing = False
            
            self._is_connecting = True
            self._websocket = await self._session.ws_connect(
                self._websocket_url, headers=self._heanders, heartbeat=self._heartbeat
            )

            self._task = self._bot.loop.create_task(self._listen())
            self._is_connected = True
            
            self._logger.info("Connected to dashboard!")
        
        except aiohttp.ClientConnectorError:
            self._schedule_reconnect()
            raise Exception("Connection failed.")
            
        except aiohttp.WSServerHandshakeError as e:
            self._logger.error("Access forbidden: Missing bot ID, version mismatch, or invalid password.")
            self._schedule_reconnect()
            
        except Exception as e:
            self._logger.error("Error occurred while connecting to dashboard.", exc_info=e)
            self._schedule_reconnect()
        
        finally:
            self._is_connecting = False
            
        return self

    async def disconnect(self) -> None:
        """Patch 34: also closes the websocket (it used to stay open on the
        dashboard's side) and stops any pending auto-reconnect - before, the
        listener treated its own cancellation as a dropped connection and
        reconnected straight away."""
        self._closing = True
        self._is_connected = False
        if self._reconnect_task and not self._reconnect_task.done():
            self._reconnect_task.cancel()
        if self._task:
            self._task.cancel()
        if self._websocket and not self._websocket.closed:
            await self._websocket.close()
        if self._session and not self._session.closed:
            await self._session.close()
        self._session = None  # connect() makes a fresh one
        self._logger.info("Disconnected to dashboard!")
    
    @property
    def is_connected(self) -> bool:
        return self._is_connected and self._websocket and not self._websocket.closed