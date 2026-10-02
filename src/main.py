from __future__ import annotations

import asyncio
import signal
import sys
from datetime import datetime

from apscheduler.schedulers.asyncio import AsyncIOScheduler

from src.utils.config import load_config
from src.utils.logging import setup_logging, get_logger
from src.utils.time import now, is_market_hours, market_close_time
from src.data.universe import UniverseManager
from src.data.smartapi_client import SmartAPIClient
from src.data.historical import HistoricalDataFetcher
from src.data.models import TickData, BarData
from src.engine.bar_aggregator import BarAggregator
from src.engine.volatility import VolatilityCalculator
from src.engine.patterns import PatternEngine
from src.engine.state import StateStore
from src.news.aggregator import NewsAggregator
from src.alerts.manager import AlertManager
from src.reports.eod import EODAnalyzer

log = get_logger(__name__)


class StockAnalyzer:
    def __init__(self):
        self.config = load_config()
        self.universe = UniverseManager()
        self.smartapi: SmartAPIClient | None = None
        self.historical = HistoricalDataFetcher()
        self.bar_aggregator = BarAggregator()
        self.vol_calc = VolatilityCalculator()
        self.pattern_engine = PatternEngine()
        self.state_store = StateStore()
        self.news_aggregator: NewsAggregator | None = None
        self.alert_manager = AlertManager()
        self.eod_analyzer = EODAnalyzer(self.state_store, self.universe)
        self.scheduler = AsyncIOScheduler(timezone="Asia/Kolkata")
        self._running = False
        self._tasks: list[asyncio.Task] = []

    async def initialize(self) -> None:
        setup_logging()
        log.info("initializing_stock_analyzer")

        # Load universe
        self.universe.load()
        if not self.universe.load_cache():
            # Fetch tokens from SmartAPI instrument list
            if self.smartapi:
                token_map = await self.universe.fetch_instrument_list()
                self.universe.update_tokens(token_map)
            self.universe.save_cache()

        # Connect to DB
        self.historical.connect()

        # Initialize SmartAPI if credentials provided
        if self.config.smartapi.api_key and self.config.smartapi.client_id:
            self.smartapi = SmartAPIClient()
            await self.smartapi.connect()
            # Update universe with actual tokens
            token_map = await self.universe.fetch_instrument_list()
            self.universe.update_tokens(token_map)

        # Initialize news aggregator
        self.news_aggregator = NewsAggregator(self.universe)
        await self.news_aggregator.start()

        # Initialize alert manager
        await self.alert_manager.start()

        # Schedule EOD report
        self.scheduler.add_job(
            self._run_eod,
            "cron",
            hour=15,
            minute=5,
            timezone="Asia/Kolkata",
        )
        self.scheduler.start()

        # Backfill historical data (run in background, non-blocking)
        asyncio.create_task(self._backfill_historical())

        log.info("initialization_complete")

    async def _backfill_historical(self) -> None:
        symbols = self.universe.get_yfinance_tickers()
        log.info("backfill_start", symbols=len(symbols))
        for tf in ["1m", "5m", "15m"]:
            await self.historical.fetch_and_store(symbols, timeframe=tf)
        log.info("backfill_complete")

    async def run(self) -> None:
        self._running = True
        log.info("starting_main_loop")

        # Start consumer tasks
        self._tasks = [
            asyncio.create_task(self._consume_ticks()),
            asyncio.create_task(self._consume_bars()),
            asyncio.create_task(self._consume_news()),
            asyncio.create_task(self._monitor_market_hours()),
        ]

        try:
            await asyncio.gather(*self._tasks)
        except asyncio.CancelledError:
            pass
        except Exception as e:
            log.error("main_loop_error", error=str(e))
        finally:
            await self.shutdown()

    async def _consume_ticks(self) -> None:
        if not self.smartapi:
            log.warning("no_smartapi_client_tick_consumer_idle")
            while self._running:
                await asyncio.sleep(60)
            return

        tick_queue = self.smartapi.get_tick_queue()
        while self._running:
            try:
                tick = await asyncio.wait_for(tick_queue.get(), timeout=1.0)
                await self._process_tick(tick)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error("tick_consumer_error", error=str(e))
                await asyncio.sleep(1)

    async def _process_tick(self, tick: TickData) -> None:
        # Resolve symbol from token
        symbol_info = self.universe.get_by_token(tick.token)
        if symbol_info:
            tick.symbol = symbol_info.symbol
        else:
            tick.symbol = f"TOKEN_{tick.token}"

        # Store tick
        self.state_store.insert_tick(tick)

        # Aggregate bars
        closed_bars = self.bar_aggregator.add_tick(tick)
        for bar in closed_bars:
            await self._process_bar(bar)

    async def _process_bar(self, bar: BarData) -> None:
        # Store bar
        self.state_store.insert_bar(bar)

        # Calculate volatility
        vol_snapshots = self.vol_calc.add_bar(bar)
        for vol in vol_snapshots:
            self.state_store.insert_volatility(vol)

            # Check for vol expansion/contraction
            if self.vol_calc.is_vol_expanding(bar.symbol, bar.timeframe):
                self.alert_manager.send_vol_alert(
                    bar.symbol, bar.timeframe, vol.realized_vol, 0.02, True
                )
            elif self.vol_calc.is_vol_contracting(bar.symbol, bar.timeframe):
                self.alert_manager.send_vol_alert(
                    bar.symbol, bar.timeframe, vol.realized_vol, 0.005, False
                )

        # Pattern detection
        signals = self.pattern_engine.add_bar(bar)
        for signal in signals:
            self.state_store.insert_signal(signal)
            self.alert_manager.send_signal_alert(signal)

    async def _consume_bars(self) -> None:
        # This could consume from bar_aggregator queue directly
        # For now, bars are processed in _process_tick
        while self._running:
            await asyncio.sleep(1)

    async def _consume_news(self) -> None:
        if not self.news_aggregator:
            return

        while self._running:
            try:
                news = await self.news_aggregator.get_latest_news(limit=20)
                for item in news:
                    self.state_store.insert_news(item)
                    self.alert_manager.send_news_alert(item)
            except Exception as e:
                log.error("news_consumer_error", error=str(e))
            await asyncio.sleep(10)

    async def _monitor_market_hours(self) -> None:
        while self._running:
            if is_market_hours():
                log.debug("market_open")
            else:
                log.debug("market_closed")
            await asyncio.sleep(60)

    async def _run_eod(self) -> None:
        if not is_market_hours():
            log.info("running_eod_analysis")
            report = self.eod_analyzer.generate()
            self.alert_manager.send_eod_alert(report)

    async def shutdown(self) -> None:
        log.info("shutting_down")
        self._running = False
        self.scheduler.shutdown()

        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

        if self.smartapi:
            await self.smartapi.disconnect()

        if self.news_aggregator:
            await self.news_aggregator.stop()

        await self.alert_manager.stop()

        self.historical.close()
        self.state_store.close()

        log.info("shutdown_complete")


async def main():
    analyzer = StockAnalyzer()

    # Handle signals (Windows compatible)
    import sys
    if sys.platform != "win32":
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, lambda: asyncio.create_task(analyzer.shutdown()))

    await analyzer.initialize()
    await analyzer.run()


if __name__ == "__main__":
    asyncio.run(main())