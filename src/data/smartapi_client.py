from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlencode

import aiohttp
import pyotp

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone
from src.data.models import TickData

log = get_logger(__name__)


class SmartAPIClient:
    BASE_URL = "https://apiconnect.angelone.in"
    SANDBOX_URL = "https://apiconnect.angelone.in"  # Same base, different endpoints

    def __init__(self):
        self.config = get_config().smartapi
        self.session: aiohttp.ClientSession | None = None
        self.ws: aiohttp.ClientWebSocketResponse | None = None
        self.auth_token: str | None = None
        self.refresh_token: str | None = None
        self.feed_token: str | None = None
        self._running = False
        self._tick_queue: asyncio.Queue[TickData] | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._heartbeat_task: asyncio.Task | None = None

    async def __aenter__(self):
        await self.connect()
        return self

    async def __aexit__(self, *args):
        await self.disconnect()

    async def connect(self) -> None:
        if self.session is None:
            self.session = aiohttp.ClientSession()
        await self._login()
        self._tick_queue = asyncio.Queue(maxsize=10000)
        self._running = True
        asyncio.create_task(self._ws_loop())

    async def disconnect(self) -> None:
        self._running = False
        if self._heartbeat_task:
            self._heartbeat_task.cancel()
        if self.ws:
            await self.ws.close()
        if self.session:
            await self.session.close()
        log.info("smartapi_disconnected")

    async def _login(self) -> None:
        url = f"{self.BASE_URL}/rest/auth/angelbroking/user/v1/loginByPassword"
        totp = pyotp.TOTP(self.config.totp_secret).now()

        payload = {
            "clientcode": self.config.client_id,
            "password": self.config.password,
            "totp": totp,
        }
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-PrivateKey": self.config.api_key,
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
            "X-UserType": "USER",
        }

        async with self.session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json()
            if not data.get("status"):
                raise Exception(f"Login failed: {data.get('message')}")
            self.auth_token = data["data"]["jwtToken"]
            self.refresh_token = data["data"]["refreshToken"]
            self.feed_token = data["data"].get("feedToken") or data["data"].get("feed_token")
            log.info("smartapi_login_success")

    def _auth_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.auth_token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "X-PrivateKey": self.config.api_key,
            "X-UserType": "USER",
            "X-SourceID": "WEB",
            "X-ClientLocalIP": "127.0.0.1",
            "X-ClientPublicIP": "127.0.0.1",
            "X-MACAddress": "00:00:00:00:00:00",
        }

    async def _ws_loop(self) -> None:
        while self._running:
            try:
                await self._ws_connect()
                await self._ws_listen()
            except Exception as e:
                log.warning("ws_error", error=str(e))
                if self._running:
                    await asyncio.sleep(5)  # Reconnect delay
            finally:
                if self.ws:
                    await self.ws.close()
                    self.ws = None

    async def _ws_connect(self) -> None:
        ws_url = "wss://smartapisocket.angelone.in/smart-stream"
        headers = {
            "Authorization": self.auth_token,
            "x-api-key": self.config.api_key,
            "x-client-code": self.config.client_id,
            "x-feed-token": self.feed_token,
        }
        self.ws = await self.session.ws_connect(ws_url, headers=headers, heartbeat=30)
        log.info("ws_connected")

        # Subscribe to tokens
        await self._subscribe_tokens()

        # Start heartbeat
        self._heartbeat_task = asyncio.create_task(self._ws_heartbeat())

    async def _ws_heartbeat(self) -> None:
        while self._running and self.ws:
            try:
                await self.ws.ping()
                await asyncio.sleep(30)
            except Exception:
                break

    async def _subscribe_tokens(self) -> None:
        if not self.ws:
            return
        tokens = self._get_subscription_tokens()
        if not tokens:
            return

        # SmartAPI expects subscription in chunks
        for i in range(0, len(tokens), 50):
            chunk = tokens[i:i+50]
            msg = {
                "action": "subscribe",
                "params": {"mode": "FULL", "tokenList": [{"exchangeType": 1, "tokens": chunk}]},
            }
            await self.ws.send_str(json.dumps(msg))
            await asyncio.sleep(0.1)
        log.info("ws_subscribed", token_count=len(tokens))

    def _get_subscription_tokens(self) -> list[int]:
        from src.data.universe import UniverseManager
        universe = UniverseManager()
        universe.load()
        return universe.get_subscribable_tokens()

    async def _ws_listen(self) -> None:
        async for msg in self.ws:
            if msg.type == aiohttp.WSMsgType.TEXT:
                await self._handle_ws_message(msg.data)
            elif msg.type == aiohttp.WSMsgType.ERROR:
                log.error("ws_error", error=self.ws.exception())
                break

    async def _handle_ws_message(self, data: str) -> None:
        try:
            msg = json.loads(data)
            if "token" in msg and "last_traded_price" in msg:
                tick = self._parse_tick(msg)
                if tick and self._tick_queue:
                    try:
                        self._tick_queue.put_nowait(tick)
                    except asyncio.QueueFull:
                        log.warning("tick_queue_full", dropped=1)
        except json.JSONDecodeError:
            pass

    def _parse_tick(self, msg: dict[str, Any]) -> TickData | None:
        try:
            token = int(msg.get("token", 0))
            ltp = float(msg.get("last_traded_price", 0)) / 100  # SmartAPI sends price * 100
            volume = int(msg.get("volume_trade_for_the_day", 0))
            open_price = float(msg.get("open_price_of_the_day", 0)) / 100
            high_price = float(msg.get("high_price_of_the_day", 0)) / 100
            low_price = float(msg.get("low_price_of_the_day", 0)) / 100
            close_price = float(msg.get("close_price", 0)) / 100
            oi = int(msg.get("open_interest", 0))

            ts_str = msg.get("exchange_timestamp") or msg.get("last_traded_time")
            if ts_str:
                timestamp = datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
            else:
                timestamp = datetime.now(get_timezone())

            return TickData(
                symbol="",  # Will be resolved via token map
                token=token,
                timestamp=timestamp,
                ltp=ltp,
                volume=volume,
                open=open_price,
                high=high_price,
                low=low_price,
                close=close_price,
                oi=oi,
            )
        except Exception as e:
            log.debug("tick_parse_error", error=str(e), msg=msg)
            return None

    def get_tick_queue(self) -> asyncio.Queue[TickData]:
        if self._tick_queue is None:
            raise RuntimeError("Not connected")
        return self._tick_queue

    async def get_historical_data(
        self,
        token: int,
        exchange: str,
        interval: str,
        from_date: str,
        to_date: str,
    ) -> list[dict]:
        url = f"{self.BASE_URL}/rest/secure/angelbroking/historical/v1/getCandleData"
        payload = {
            "exchange": exchange,
            "symboltoken": str(token),
            "interval": interval,
            "fromdate": from_date,
            "todate": to_date,
        }
        headers = self._auth_headers()
        async with self.session.post(url, json=payload, headers=headers) as resp:
            data = await resp.json()
            if data.get("status"):
                return data["data"]
            return []

    async def get_ltp(self, tokens: list[tuple[str, int]]) -> dict[int, float]:
        """Get LTP for multiple tokens via REST."""
        url = f"{self.BASE_URL}/rest/secure/angelbroking/marketdata/v1/quote"
        # Group tokens by exchange
        exchange_tokens = {}
        for exch, tok in tokens:
            if exch not in exchange_tokens:
                exchange_tokens[exch] = []
            exchange_tokens[exch].append(str(tok))
        
        payload = {
            "mode": "FULL",
            "exchangeTokens": exchange_tokens,
        }
        headers = self._auth_headers()
        async with self.session.post(url, json=payload, headers=headers) as resp:
            # Handle both JSON and HTML responses
            content_type = resp.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                text = await resp.text()
                log.debug("ltp_non_json_response", status=resp.status, content_type=content_type, preview=text[:200])
                return {}
            data = await resp.json()
            result = {}
            if data.get("status"):
                for exch, tok_data in data["data"]["fetched"].items():
                    for tok, info in tok_data.items():
                        result[int(tok)] = float(info.get("ltp", 0)) / 100
            return result

    async def get_option_chain(self, symbol: str, expiry: str) -> dict | None:
        """Get option chain for index."""
        url = f"{self.BASE_URL}/rest/secure/angelbroking/marketdata/v1/getOptionChain"
        payload = {
            "symbol": symbol,
            "expiry": expiry,
        }
        headers = self._auth_headers()
        async with self.session.post(url, json=payload, headers=headers) as resp:
            content_type = resp.headers.get("Content-Type", "")
            if "application/json" not in content_type:
                text = await resp.text()
                log.debug("option_chain_non_json_response", status=resp.status, content_type=content_type, preview=text[:200])
                return None
            data = await resp.json()
            if data.get("status"):
                return data.get("data", {})
            return None

    async def place_order(self, order_params: dict) -> dict:
        url = f"{self.BASE_URL}/rest/secure/angelbroking/order/v1/placeOrder"
        headers = self._auth_headers()
        async with self.session.post(url, json=order_params, headers=headers) as resp:
            return await resp.json()

    async def get_positions(self) -> dict:
        url = f"{self.BASE_URL}/rest/secure/angelbroking/order/v1/getPosition"
        headers = self._auth_headers()
        async with self.session.get(url, headers=headers) as resp:
            return await resp.json()

    async def get_holdings(self) -> dict:
        url = f"{self.BASE_URL}/rest/secure/angelbroking/portfolio/v1/getHolding"
        headers = self._auth_headers()
        async with self.session.get(url, headers=headers) as resp:
            return await resp.json()