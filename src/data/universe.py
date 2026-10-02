from __future__ import annotations

import csv
import json
from pathlib import Path
from typing import Any

import httpx

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.data.models import SymbolInfo, Exchange, InstrumentType

log = get_logger(__name__)

NIFTY50_SYMBOLS = [
    "RELIANCE", "TCS", "HDFCBANK", "ICICIBANK", "HINDUNILVR", "INFY", "ITC",
    "SBIN", "BHARTIARTL", "BAJFINANCE", "KOTAKBANK", "LT", "ASIANPAINT", "AXISBANK",
    "MARUTI", "SUNPHARMA", "TITAN", "ULTRACEMCO", "NESTLEIND", "WIPRO", "POWERGRID",
    "NTPC", "TECHM", "TATAMOTORS", "BAJAJFINSV", "HCLTECH", "ADANIENT", "JSWSTEEL",
    "ONGC", "GRASIM", "INDUSINDBK", "CIPLA", "COALINDIA", "BRITANNIA", "DRREDDY",
    "EICHERMOT", "APOLLOHOSP", "DIVISLAB", "HEROMOTOCO", "BPCL", "TATACONSUM",
    "HINDALCO", "SBILIFE", "BAJAJ-AUTO", "SHRIRAMFIN", "M&M", "TRENT", "BEL",
    "ZOMATO", "JIOFIN", "DMART"
]

NIFTY100_ADDITIONAL = [
    "ADANIGREEN", "ADANIPORTS", "ADANIPOWER", "AMBUJACEM", "AUROPHARMA", "BANKBARODA",
    "BERGEPAINT", "BIOCON", "BOSCHLTD", "CADILAHC", "CHOLAFIN", "COLPAL", "DABUR",
    "DLF", "GAIL", "GODREJCP", "HAVELLS", "HDFCAMC", "HDFCLIFE", "HINDPETRO",
    "ICICIGI", "ICICIPRULI", "IGL", "INDIGO", "INDUSTOWER", "JINDALSTEL", "JUBLFOOD",
    "LICHSGFIN", "LUPIN", "MARICO", "MCDOWELL-N", "MOTHERSUMI", "MPHASIS", "MRF",
    "MUTHOOTFIN", "NAUKRI", "PAGEIND", "PEL", "PETRONET", "PFC", "PIDILITIND",
    "PIIND", "PNB", "RECLTD", "SAIL", "SHREECEM", "SIEMENS", "SRF", "TORNTPHARM",
    "TORNTPOWER", "TVSMOTOR", "UBL", "UPL", "VEDL", "YESBANK"
]

NIFTY200_ADDITIONAL = [
    "ABB", "ABCAPITAL", "ABFRL", "ACC", "ALKEM", "AMARAJABAT", "AUBANK", "BALKRISIND",
    "BALRAMCHIN", "BATAINDIA", "BDL", "BEL", "BHARATFORG", "BHEL", "BLUEDART", "BSE",
    "CANBK", "CANFINHOME", "CENTRALBK", "CROMPTON", "CUMMINSIND", "DEEPAKNTR", "DELTACORP",
    "DIXON", "EDELWEISS", "EMAMILTD", "ENDURANCE", "ENGINERSIN", "ESCORTS", "EXIDEIND",
    "FEDERALBNK", "FINCABLES", "FORTIS", "GAIL", "GLENMARK", "GMRINFRA", "GODREJIND",
    "GODREJPROP", "GRANULES", "GUJGASLTD", "HAL", "HAPPSTMNDS", "HATSUN", "HINDCOPPER",
    "HONAUT", "IBULHSGFIN", "IDEA", "IDFCFIRSTB", "IEX", "INDHOTEL", "INDIAMART",
    "INDIANB", "INTELLECT", "IPCALAB", "IRCTC", "IRFC", "ITC", "J&KBANK", "JAMNAAUTO",
    "JBCHEPHARM", "JINDALSAW", "JSL", "JUBLINGREA", "JUSTDIAL", "KAJARIACER", "KALYANKJIL",
    "KEI", "KFINTECH", "KPITTECH", "LAURUSLABS", "LEMONTREE", "LICI", "LINDEINDIA",
    "LODHA", "LTIM", "LTTS", "MANAPPURAM", "MARUTI", "MFSL", "MGL", "MOTILALOFS",
    "MPHASIS", "MRF", "MUTHOOTFIN", "NAM-INDIA", "NATCOPHARM", "NAVINFLUOR", "NBCC",
    "NCC", "NHPC", "NMDC", "NTPC", "OBEROIRLTY", "OFSS", "OIL", "PAYTM", "PERSISTENT",
    "PETRONET", "PFIZER", "PHOENIXLTD", "PIRAMAL", "POLICYBZR", "POLYCAB", "POONAWALLA",
    "PRESTIGE", "RADICO", "RAIN", "RBLBANK", "RCF", "REDINGTON", "RELAXO", "RITES",
    "RVNL", "SANOFI", "SCHAEFFLER", "SONATSOFTW", "STARHEALTH", "SUNDARMFIN", "SUNDRMFAST",
    "SUPREMEIND", "SUZLON", "SYNGENE", "TATACHEM", "TATAELXSI", "TATAINVEST", "TATAPOWER",
    "TATASTEEL", "TCS", "TEJASNET", "THERMAX", "TIINDIA", "TMB", "TORNTPHARM", "TRENT",
    "TTKPRESTIG", "TUBEINVEST", "TV18BRDCST", "TVSMOTOR", "UBL", "UNIONBANK", "UNOMINDA",
    "VAIBHAVGBL", "VBL", "VEDL", "VOLTAS", "WELCORP", "WELSPUNIND", "WHIRLPOOL", "WIPRO",
    "ZFCVINDIA", "ZOMATO", "ZYDUSLIFE"
]


class UniverseManager:
    def __init__(self):
        self.config = get_config()
        self._symbols: dict[str, SymbolInfo] = {}
        self._tokens: dict[int, SymbolInfo] = {}
        self._loaded = False

    def load(self) -> dict[str, SymbolInfo]:
        if self._loaded:
            return self._symbols

        indices = self.config.universe.indices
        all_symbols = set()

        if "NIFTY50" in indices:
            all_symbols.update(NIFTY50_SYMBOLS)
        if "NIFTY100" in indices:
            all_symbols.update(NIFTY100_ADDITIONAL)
        if "NIFTY200" in indices:
            all_symbols.update(NIFTY200_ADDITIONAL)

        for symbol in sorted(all_symbols):
            info = SymbolInfo(
                symbol=symbol,
                token=0,  # Will be populated from SmartAPI instrument list
                exchange=Exchange.NSE,
                instrument_type=InstrumentType.EQUITY,
                yfinance_ticker=f"{symbol}.NS",
            )
            self._symbols[symbol] = info

        self._loaded = True
        log.info("universe_loaded", count=len(self._symbols), indices=indices)
        return self._symbols

    def get_symbol(self, symbol: str) -> SymbolInfo | None:
        return self._symbols.get(symbol)

    def get_by_token(self, token: int) -> SymbolInfo | None:
        return self._tokens.get(token)

    def update_tokens(self, token_map: dict[str, int]) -> None:
        for symbol, token in token_map.items():
            if symbol in self._symbols:
                self._symbols[symbol].token = token
                self._tokens[token] = self._symbols[symbol]
        log.info("tokens_updated", count=len(token_map))

    def get_subscribable_tokens(self) -> list[int]:
        return [s.token for s in self._symbols.values() if s.token > 0]

    def get_yfinance_tickers(self) -> list[str]:
        return [s.yfinance_ticker for s in self._symbols.values()]

    def get_all_symbols(self) -> list[str]:
        return list(self._symbols.keys())

    async def fetch_instrument_list(self) -> dict[str, int]:
        """Fetch NSE instrument list from SmartAPI or fallback."""
        url = "https://margincalculator.angelone.in/OpenAPI_File/files/OpenAPIScripMaster.json"
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                data = resp.json()
        except Exception as e:
            log.warning("instrument_fetch_failed", error=str(e))
            return {}

        token_map = {}
        for item in data:
            if item.get("exch_seg") == "NSE" and item.get("instrumenttype", "") in ("EQ", ""):
                symbol = item.get("symbol", "").split("-")[0]
                token = int(item.get("token", 0))
                if symbol in self._symbols and token > 0:
                    token_map[symbol] = token

        log.info("instrument_tokens_fetched", count=len(token_map))
        return token_map

    async def fetch_index_composition(self, index_name: str) -> list[str]:
        """
        Fetch index composition from NSE public API.
        index_name: 'NIFTY 50', 'NIFTY BANK', 'NIFTY FINANCIAL SERVICES', etc.
        Returns list of symbols.
        """
        from src.data.nse_public import NSEPublicClient
        
        async with NSEPublicClient() as client:
            stocks = await client.get_pre_open_market(index_name)
            symbols = [s["symbol"] for s in stocks if s.get("symbol")]
            log.info("index_composition_fetched", index=index_name, count=len(symbols))
            return symbols

    def save_cache(self, path: Path | str = "data/universe_cache.json") -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            symbol: {
                "symbol": info.symbol,
                "token": info.token,
                "exchange": info.exchange.value,
                "instrument_type": info.instrument_type.value,
                "yfinance_ticker": info.yfinance_ticker,
                "lot_size": info.lot_size,
                "tick_size": info.tick_size,
                "is_fno": info.is_fno,
            }
            for symbol, info in self._symbols.items()
        }
        with open(path, "w") as f:
            json.dump(data, f, indent=2)

    def load_cache(self, path: Path | str = "data/universe_cache.json") -> bool:
        path = Path(path)
        if not path.exists():
            return False
        with open(path) as f:
            data = json.load(f)
        for symbol, info_dict in data.items():
            self._symbols[symbol] = SymbolInfo(**info_dict)
            if info_dict["token"] > 0:
                self._tokens[info_dict["token"]] = self._symbols[symbol]
        self._loaded = True
        log.info("universe_cache_loaded", count=len(self._symbols))
        return True