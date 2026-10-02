#!/usr/bin/env python3
"""Fetch historical data from yfinance and store in DuckDB."""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.config import load_config
from src.utils.logging import setup_logging, get_logger
from src.data.universe import UniverseManager
from src.data.historical import HistoricalDataFetcher

log = get_logger(__name__)


async def main():
    load_config()
    setup_logging()

    log.info("starting_historical_fetch")

    universe = UniverseManager()
    universe.load()

    fetcher = HistoricalDataFetcher()

    symbols = universe.get_yfinance_tickers()
    log.info("fetching_for_symbols", count=len(symbols))

    for tf in ["1m", "5m", "15m", "1h", "1d"]:
        log.info("fetching_timeframe", timeframe=tf)
        count = await fetcher.fetch_and_store(symbols, timeframe=tf)
        log.info("timeframe_complete", timeframe=tf, bars=count)

    fetcher.close()
    log.info("historical_fetch_complete")


if __name__ == "__main__":
    asyncio.run(main())