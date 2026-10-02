from __future__ import annotations

import asyncio
import ssl
from datetime import datetime
from typing import Any
from urllib.parse import quote

import aiohttp

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone

log = get_logger(__name__)

# Working NSE API endpoints (verified)
NSE_BASE = "https://www.nseindia.com"
NSE_ENDPOINTS = {
    "market_status": "/api/marketStatus",
    "all_indices": "/api/allIndices",
    "market_pre_open": "/api/market-data-pre-open",
    "chart_data": "/api/chart-databyindex",
}


class NSEPublicClient:
    """
    NSE Public Data Client using working NSE API endpoints.
    Provides fallback data when SmartAPI is unavailable.
    """
    
    def __init__(self):
        self._session: aiohttp.ClientSession | None = None
        self._rate_limit = asyncio.Semaphore(10)
        self._last_request = 0.0
        self._min_interval = 0.2
        self._initialized = False
        self._ssl_context = ssl.create_default_context()
        self._ssl_context.check_hostname = False
        self._ssl_context.verify_mode = ssl.CERT_NONE

    async def __aenter__(self):
        connector = aiohttp.TCPConnector(ssl=self._ssl_context, limit=20)
        self._session = aiohttp.ClientSession(
            connector=connector,
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "gzip, deflate",
                "Referer": NSE_BASE,
                "X-Requested-With": "XMLHttpRequest",
                "Host": "www.nseindia.com",
            },
            timeout=aiohttp.ClientTimeout(total=15),
            cookie_jar=aiohttp.CookieJar(unsafe=True),
        )
        await self._initialize()
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def _initialize(self):
        """Establish session cookies by visiting NSE pages."""
        if self._initialized:
            return
        try:
            await self._session.get(NSE_BASE)
            await self._session.get(f"{NSE_BASE}/market-data/live-equity-market")
            await asyncio.sleep(0.5)
            self._initialized = True
            log.debug("nse_session_initialized")
        except Exception as e:
            log.warning("nse_init_failed", error=str(e))

    async def _request(self, method: str, url: str, **kwargs) -> Any:
        async with self._rate_limit:
            now = asyncio.get_event_loop().time()
            wait = self._min_interval - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = asyncio.get_event_loop().time()

            try:
                async with self._session.request(method, url, **kwargs) as resp:
                    if resp.status == 429:
                        await asyncio.sleep(2)
                        return await self._request(method, url, **kwargs)
                    if resp.status != 200:
                        log.debug("nse_request_failed", url=url, status=resp.status)
                        return None
                    content_type = resp.headers.get("Content-Type", "")
                    if "application/json" in content_type:
                        return await resp.json()
                    if "text/xml" in content_type or "application/xml" in content_type:
                        return await resp.text()
                    return await resp.text()
            except asyncio.TimeoutError:
                log.warning("nse_timeout", url=url)
                return None
            except Exception as e:
                log.warning("nse_error", url=url, error=str(e))
                return None

    # --- Market Status ---
    async def get_market_status(self) -> dict | None:
        """Get overall market status."""
        url = NSE_BASE + NSE_ENDPOINTS["market_status"]
        data = await self._request("GET", url)
        if data and "marketState" in data:
            for m in data["marketState"]:
                if m.get("market") == "Capital Market":
                    return {
                        "status": m.get("marketStatus", "UNKNOWN"),
                        "trade_date": m.get("tradeDate"),
                        "nifty_last": m.get("index", {}).get("last") if isinstance(m.get("index"), dict) else m.get("last"),
                        "nifty_change": m.get("variation"),
                        "nifty_pct_change": m.get("percentChange"),
                    }
        return {"status": "UNKNOWN"}

    # --- Indices ---
    async def get_all_indices(self) -> list[dict]:
        """Get all indices with last price, change, etc."""
        url = NSE_BASE + NSE_ENDPOINTS["all_indices"]
        data = await self._request("GET", url)
        if data and "data" in data:
            return data["data"]
        return []

    # --- Pre-open / Live Market Data ---
    async def get_pre_open_market(self, key: str = "ALL") -> list[dict]:
        """
        Get pre-open / live market data for all stocks or specific index.
        key: 'ALL', 'NIFTY', 'NIFTY BANK', 'NIFTY FINANCIAL SERVICES', etc.
        Returns list of dicts with symbol, last_price, change, p_change, etc.
        """
        url = NSE_BASE + NSE_ENDPOINTS["market_pre_open"]
        params = {"key": key}
        data = await self._request("GET", url, params=params)
        if data and "data" in data:
            results = []
            for item in data["data"]:
                meta = item.get("metadata", {})
                if meta.get("symbol"):
                    results.append({
                        "symbol": meta.get("symbol"),
                        "identifier": meta.get("identifier"),
                        "series": meta.get("series"),
                        "last_price": meta.get("lastPrice"),
                        "change": meta.get("change"),
                        "p_change": meta.get("pChange"),
                        "prev_close": meta.get("previousClose"),
                        "year_high": meta.get("yearHigh"),
                        "year_low": meta.get("yearLow"),
                        "total_turnover": meta.get("totalTurnover"),
                        "iep": meta.get("iep"),
                        "detail": item.get("detail", {}),
                    })
            return results
        return []

    async def get_quote(self, symbol: str) -> dict | None:
        """
        Get quote for a specific symbol from pre-open data.
        Note: Only works for stocks in the pre-open dataset.
        """
        # Search in ALL pre-open data
        all_data = await self.get_pre_open_market("ALL")
        for item in all_data:
            if item.get("symbol", "").upper() == symbol.upper():
                return item
        return None

    async def get_multiple_quotes(self, symbols: list[str]) -> dict[str, dict]:
        """Get quotes for multiple symbols."""
        all_data = await self.get_pre_open_market("ALL")
        quote_map = {item["symbol"].upper(): item for item in all_data}
        return {s: quote_map.get(s.upper()) for s in symbols if s.upper() in quote_map}

    # --- Chart Data ---
    async def get_chart_data(
        self,
        index: str,
    ) -> list[dict] | None:
        """
        Get intraday chart data for index or security.
        index: 'NIFTY 50', 'NIFTY BANK', 'SECURITY:RELIANCE', etc.
        Returns list of OHLCV points (empty when market closed).
        """
        url = NSE_BASE + NSE_ENDPOINTS["chart_data"]
        params = {"index": index}
        data = await self._request("GET", url, params=params)
        if data and "grapthData" in data:
            return data["grapthData"]
        return None

    # --- Advances/Declines ---
    async def get_advances_declines(self) -> dict | None:
        """Get market breadth from all indices data."""
        data = await self._request("GET", NSE_BASE + NSE_ENDPOINTS["all_indices"])
        if data:
            return {
                "advances": data.get("advances"),
                "declines": data.get("declines"),
                "unchanged": data.get("unchanged"),
                "timestamp": data.get("timestamp"),
            }
        return None


# Convenience functions
async def quick_market_status() -> dict | None:
    async with NSEPublicClient() as client:
        return await client.get_market_status()


async def quick_indices() -> list[dict]:
    async with NSEPublicClient() as client:
        return await client.get_all_indices()


async def quick_pre_open(key: str = "ALL") -> list[dict]:
    async with NSEPublicClient() as client:
        return await client.get_pre_open_market(key)


async def quick_chart(index: str) -> list[dict] | None:
    async with NSEPublicClient() as client:
        return await client.get_chart_data(index)


async def quick_quote(symbol: str) -> dict | None:
    async with NSEPublicClient() as client:
        return await client.get_quote(symbol)


async def quick_multiple_quotes(symbols: list[str]) -> dict[str, dict]:
    async with NSEPublicClient() as client:
        return await client.get_multiple_quotes(symbols)