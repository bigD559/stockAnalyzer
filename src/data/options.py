from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone

log = get_logger(__name__)


@dataclass
class OptionChainData:
    symbol: str
    expiry: str
    spot_price: float
    timestamp: datetime
    calls: dict  # strike -> {iv, oi, volume, ltp, delta, gamma, theta, vega}
    puts: dict
    pcr: float = 0.0
    pcr_volume: float = 0.0
    max_pain: float = 0.0
    iv_percentile: float = 0.0
    atm_iv: float = 0.0
    skew: float = 0.0


@dataclass
class OptionsMetrics:
    symbol: str
    timestamp: datetime
    atm_iv: float
    iv_percentile: float
    iv_rank: float
    skew_25d: float
    pcr_oi: float
    pcr_volume: float
    max_pain: float
    max_pain_distance_pct: float
    total_call_oi: int
    total_put_oi: int
    total_call_volume: int
    total_put_volume: int
    call_resistance: list
    put_support: list


class OptionsClient:
    """Options data client using Angel One SmartAPI.
    
    Uses SmartAPI's option chain and LTP endpoints.
    """
    
    def __init__(self, smartapi_client=None):
        self.config = get_config()
        self.smartapi = smartapi_client
        self._session = None
        self._option_tokens: dict[str, list[dict]] = {}  # symbol -> list of option contracts

    async def __aenter__(self):
        if self.smartapi:
            # Use existing SmartAPI session
            self._session = self.smartapi.session
        else:
            import aiohttp
            self._session = aiohttp.ClientSession(
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Accept": "application/json, text/plain, */*",
                    "Accept-Encoding": "gzip, deflate",
                    "Referer": "https://www.nseindia.com",
                    "X-Requested-With": "XMLHttpRequest",
                    "Host": "www.nseindia.com",
                },
                timeout=aiohttp.ClientTimeout(total=15),
            )
            await self._initialize()
        await self._load_option_tokens()
        return self

    async def __aexit__(self, *args):
        if self._session and not self.smartapi:
            await self._session.close()

    async def _initialize(self):
        """Initialize NSE session for public API fallback."""
        if self.smartapi:
            return
        try:
            await self._session.get("https://www.nseindia.com")
            await asyncio.sleep(0.5)
        except Exception as e:
            log.warning("options_client_init_failed", error=str(e))

    async def _load_option_tokens(self):
        """Load option token mappings from Angel One instrument master."""
        url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
        try:
            import aiohttp
            async with aiohttp.ClientSession() as session:
                async with session.get(url) as resp:
                    data = await resp.json()
                    
            self._option_tokens = {}
            for item in data:
                if item.get("exch_seg") == "NFO" and item.get("instrumenttype") in ("OPTIDX", "FUTIDX"):
                    name = item.get("name", "")
                    if name not in self._option_tokens:
                        self._option_tokens[name] = []
                    self._option_tokens[name].append({
                        "token": int(item.get("token", 0)),
                        "symbol": item.get("symbol", ""),
                        "expiry": item.get("expiry", ""),
                        "strike": float(item.get("strike", 0)),
                        "instrument_type": item.get("instrumenttype", ""),
                        "lot_size": int(item.get("lotsize", 0)),
                    })
            
            log.info("option_tokens_loaded", symbols=list(self._option_tokens.keys()))
        except Exception as e:
            log.warning("option_tokens_load_failed", error=str(e))

    def _get_nearest_expiry_options(self, symbol: str, expiry: str | None = None) -> list[dict]:
        """Get option contracts for symbol, optionally filtered by expiry."""
        options = self._option_tokens.get(symbol, [])
        if not options:
            return []
        
        if expiry:
            return [opt for opt in options if opt["expiry"] == expiry]
        
        # Return nearest expiry
        expiries = sorted(set(opt["expiry"] for opt in options if opt["expiry"]))
        if expiries:
            nearest = expiries[0]
            return [opt for opt in options if opt["expiry"] == nearest]
        return []

    async def get_option_chain(self, symbol: str, expiry: str | None = None) -> OptionChainData | None:
        """Fetch option chain using SmartAPI."""
        if not self.smartapi:
            log.warning("smartapi_required_for_option_chain")
            return None
        
        # Get nearest expiry if not specified
        if not expiry:
            options = self._get_nearest_expiry_options(symbol)
            if options:
                expiry = options[0]["expiry"]
        
        if not expiry:
            log.warning("no_expiry_found", symbol=symbol)
            return None
        
        try:
            # Get option chain from SmartAPI
            chain_data = await self.smartapi.get_option_chain(symbol, expiry)
            if not chain_data:
                return None
            
            # Parse SmartAPI response
            return self._parse_smartapi_option_chain(symbol, expiry, chain_data)
        except Exception as e:
            log.warning("option_chain_fetch_failed", symbol=symbol, error=str(e))
            return None

    def _parse_smartapi_option_chain(self, symbol: str, expiry: str, data: Any) -> OptionChainData | None:
        """Parse SmartAPI option chain response."""
        # SmartAPI returns data in their format
        # This would need to be adapted based on actual response format
        try:
            spot_price = 0
            calls = {}
            puts = {}
            
            # Parse based on actual SmartAPI response format
            # For now, return placeholder
            return OptionChainData(
                symbol=symbol,
                expiry=expiry,
                spot_price=spot_price,
                timestamp=datetime.now(get_timezone()),
                calls=calls,
                puts=puts,
            )
        except Exception as e:
            log.warning("option_chain_parse_failed", symbol=symbol, error=str(e))
            return None

    async def get_ltp_for_options(self, symbol: str, expiry: str | None = None) -> dict[str, dict]:
        """Get LTP for option contracts."""
        if not self.smartapi:
            return {}
        
        options = self._get_nearest_expiry_options(symbol, expiry)
        if not options:
            return {}
        
        # Get tokens
        tokens = [("NFO", opt["token"]) for opt in options[:50]]  # Limit to 50
        ltp_data = await self.smartapi.get_ltp(tokens)
        
        result = {}
        for opt in options:
            token = opt["token"]
            if token in ltp_data:
                result[opt["symbol"]] = {
                    "token": token,
                    "strike": opt["strike"],
                    "instrument_type": opt["instrument_type"],
                    "ltp": ltp_data[token],
                    "expiry": opt["expiry"],
                }
        return result

    async def get_metrics(self, symbol: str, expiry: str | None = None) -> OptionsMetrics | None:
        """Get computed options metrics (PCR, Max Pain, IV, etc.)."""
        chain = await self.get_option_chain(symbol, expiry)
        if not chain:
            return None
        
        # Compute metrics
        calls = chain.calls
        puts = chain.puts
        
        total_call_oi = sum(v.get("oi", 0) for v in calls.values())
        total_put_oi = sum(v.get("oi", 0) for v in puts.values())
        total_call_vol = sum(v.get("volume", 0) for v in calls.values())
        total_put_vol = sum(v.get("volume", 0) for v in puts.values())
        
        pcr_oi = total_put_oi / total_call_oi if total_call_oi > 0 else 0
        pcr_volume = total_put_vol / total_call_vol if total_call_vol > 0 else 0
        
        # Max Pain
        max_pain = self._calculate_max_pain(calls, puts)
        
        # ATM IV
        atm_iv = self._calculate_atm_iv(calls, puts, chain.spot_price)
        
        # Skew
        skew = self._calculate_skew(calls, puts, chain.spot_price)
        
        # High OI strikes
        call_resistance = sorted(calls.items(), key=lambda x: x[1].get("oi", 0), reverse=True)[:5]
        put_support = sorted(puts.items(), key=lambda x: x[1].get("oi", 0), reverse=True)[:5]
        
        return OptionsMetrics(
            symbol=symbol,
            timestamp=chain.timestamp,
            atm_iv=atm_iv,
            iv_percentile=0.5,  # Would need historical data
            iv_rank=0.5,
            skew_25d=skew,
            pcr_oi=pcr_oi,
            pcr_volume=pcr_volume,
            max_pain=max_pain,
            max_pain_distance_pct=(max_pain - chain.spot_price) / chain.spot_price * 100 if chain.spot_price > 0 else 0,
            total_call_oi=total_call_oi,
            total_put_oi=total_put_oi,
            total_call_volume=total_call_vol,
            total_put_volume=total_put_vol,
            call_resistance=[{"strike": s, "oi": v["oi"], "iv": v.get("iv", 0)} for s, v in call_resistance],
            put_support=[{"strike": s, "oi": v["oi"], "iv": v.get("iv", 0)} for s, v in put_support],
        )

    def _calculate_max_pain(self, calls: dict, puts: dict) -> float:
        all_strikes = set(calls.keys()) | set(puts.keys())
        if not all_strikes:
            return 0
        
        min_loss = float('inf')
        max_pain_strike = 0
        
        for strike in all_strikes:
            total_loss = 0
            for k, v in calls.items():
                if k < strike:
                    total_loss += (strike - k) * v.get("oi", 0)
            for k, v in puts.items():
                if k > strike:
                    total_loss += (k - strike) * v.get("oi", 0)
            if total_loss < min_loss:
                min_loss = total_loss
                max_pain_strike = strike
        return max_pain_strike

    def _calculate_atm_iv(self, calls: dict, puts: dict, spot: float) -> float:
        if not calls or not puts or spot == 0:
            return 0
        all_strikes = sorted(set(calls.keys()) | set(puts.keys()))
        atm_strike = min(all_strikes, key=lambda x: abs(x - spot))
        call_iv = calls.get(atm_strike, {}).get("iv", 0)
        put_iv = puts.get(atm_strike, {}).get("iv", 0)
        if call_iv > 0 and put_iv > 0:
            return (call_iv + put_iv) / 2
        elif call_iv > 0:
            return call_iv
        elif put_iv > 0:
            return put_iv
        return 0

    def _calculate_skew(self, calls: dict, puts: dict, spot: float) -> float:
        if not calls or not puts:
            return 0
        all_strikes = sorted(set(calls.keys()) | set(puts.keys()))
        if len(all_strikes) < 3:
            return 0
        atm_strike = min(all_strikes, key=lambda x: abs(x - spot))
        atm_idx = all_strikes.index(atm_strike)
        put_strike = all_strikes[max(0, atm_idx - 2)]
        call_strike = all_strikes[min(len(all_strikes) - 1, atm_idx + 2)]
        put_iv = puts.get(put_strike, {}).get("iv", 0)
        call_iv = calls.get(call_strike, {}).get("iv", 0)
        if call_iv > 0:
            return put_iv - call_iv
        return 0


# Convenience functions
async def get_nifty_options(smartapi_client=None) -> OptionChainData | None:
    async with OptionsClient(smartapi_client) as client:
        return await client.get_option_chain("NIFTY")


async def get_banknifty_options(smartapi_client=None) -> OptionChainData | None:
    async with OptionsClient(smartapi_client) as client:
        return await client.get_option_chain("BANKNIFTY")


async def get_finnifty_options(smartapi_client=None) -> OptionChainData | None:
    async with OptionsClient(smartapi_client) as client:
        return await client.get_option_chain("FINNIFTY")


async def get_stock_options(symbol: str, smartapi_client=None) -> OptionChainData | None:
    async with OptionsClient(smartapi_client) as client:
        return await client.get_option_chain(symbol)


async def get_all_index_options(smartapi_client=None) -> dict[str, OptionChainData]:
    async with OptionsClient(smartapi_client) as client:
        results = {}
        for index in ["NIFTY", "BANKNIFTY", "FINNIFTY"]:
            chain = await client.get_option_chain(index)
            if chain:
                results[index] = chain
            await asyncio.sleep(0.2)
        return results