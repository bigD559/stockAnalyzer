from __future__ import annotations

import asyncio
import multiprocessing as mp
import signal
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Any
from multiprocessing import Queue, Event

from src.utils.config import load_config
from src.utils.logging import setup_logging, get_logger
from src.utils.time import now, is_market_hours
from src.data.universe import UniverseManager
from src.data.smartapi_client import SmartAPIClient
from src.data.models import TickData, BarData, Signal
from src.engine.bar_aggregator import BarAggregator
from src.engine.volatility import VolatilityCalculator
from src.engine.patterns import PatternEngine
from src.engine.state import StateStore
from src.news.aggregator import NewsAggregator
from src.alerts.manager import AlertManager
from src.reports.eod import EODAnalyzer

log = get_logger(__name__)


@dataclass
class ProcessMessage:
    type: str
    data: Any
    timestamp: datetime = None
    
    def __post_init__(self):
        if self.timestamp is None:
            self.timestamp = datetime.now()


class BaseWorker:
    def __init__(self, name: str, input_queue: Queue, output_queue: Queue, shutdown_event: Event):
        self.name = name
        self.input_queue = input_queue
        self.output_queue = output_queue
        self.shutdown_event = shutdown_event
        self.running = False
        
    def run(self):
        self.running = True
        log.info(f"{self.name} worker started")
        try:
            asyncio.run(self._async_run())
        except Exception as e:
            log.error(f"{self.name} worker error", error=str(e))
        finally:
            self.running = False
            log.info(f"{self.name} worker stopped")
    
    async def _async_run(self):
        raise NotImplementedError


class DataIngestionWorker(BaseWorker):
    """Process: Connects to SmartAPI WebSocket, emits raw ticks."""
    
    def __init__(self, input_queue: Queue, output_queue: Queue, shutdown_event: Event):
        super().__init__("DataIngestion", input_queue, output_queue, shutdown_event)
        
    async def _async_run(self):
        load_config()
        setup_logging()
        
        universe = UniverseManager()
        universe.load()
        if not universe.load_cache():
            smartapi = SmartAPIClient()
            await smartapi.connect()
            token_map = await universe.fetch_instrument_list()
            await smartapi.disconnect()
            universe.update_tokens(token_map)
            universe.save_cache()
        
        smartapi = SmartAPIClient()
        await smartapi.connect()
        
        tick_queue = smartapi.get_tick_queue()
        universe = UniverseManager()
        universe.load()
        
        while not self.shutdown_event.is_set():
            try:
                tick = await asyncio.wait_for(tick_queue.get(), timeout=1.0)
                symbol_info = universe.get_by_token(tick.token)
                if symbol_info:
                    tick.symbol = symbol_info.symbol
                else:
                    tick.symbol = f"TOKEN_{tick.token}"
                
                msg = ProcessMessage(type="TICK", data=tick)
                self.output_queue.put(msg)
            except asyncio.TimeoutError:
                continue
            except Exception as e:
                log.error("tick_processing_error", error=str(e))
                await asyncio.sleep(1)
        
        await smartapi.disconnect()


class BarAggregationWorker(BaseWorker):
    """Process: Aggregates ticks into 1m/5m/15m bars."""
    
    def __init__(self, input_queue: Queue, output_queue: Queue, shutdown_event: Event):
        super().__init__("BarAggregation", input_queue, output_queue, shutdown_event)
        
    async def _async_run(self):
        bar_aggregator = BarAggregator()
        
        while not self.shutdown_event.is_set():
            try:
                msg = self.input_queue.get(timeout=1)
                if msg.type == "TICK":
                    tick = msg.data
                    closed_bars = bar_aggregator.add_tick(tick)
                    for bar in closed_bars:
                        self.output_queue.put(ProcessMessage(type="BAR", data=bar))
            except Exception:
                if not self.shutdown_event.is_set():
                    await asyncio.sleep(0.1)
            await asyncio.sleep(0.01)


class EngineWorker(BaseWorker):
    """Process: Calculates volatility, detects patterns, generates signals."""
    
    def __init__(self, input_queue: Queue, output_queue: Queue, shutdown_event: Event):
        super().__init__("Engine", input_queue, output_queue, shutdown_event)
        
    async def _async_run(self):
        load_config()
        setup_logging()
        
        vol_calc = VolatilityCalculator()
        pattern_engine = PatternEngine()
        state_store = StateStore()
        
        while not self.shutdown_event.is_set():
            try:
                msg = self.input_queue.get(timeout=1)
                if msg.type == "BAR":
                    bar = msg.data
                    
                    state_store.insert_bar(bar)
                    
                    vol_snapshots = vol_calc.add_bar(bar)
                    for vol in vol_snapshots:
                        state_store.insert_volatility(vol)
                    
                    signals = pattern_engine.add_bar(bar)
                    for signal in signals:
                        state_store.insert_signal(signal)
                        self.output_queue.put(ProcessMessage(type="SIGNAL", data=signal))
                        
                        if vol_calc.is_vol_expanding(bar.symbol, bar.timeframe):
                            self.output_queue.put(ProcessMessage(
                                type="VOL_ALERT", 
                                data={"symbol": bar.symbol, "timeframe": bar.timeframe, "expanding": True}
                            ))
            except Exception as e:
                if not self.shutdown_event.is_set():
                    log.error("engine_worker_error", error=str(e))
                    await asyncio.sleep(0.1)
            await asyncio.sleep(0.01)


class AlertWorker(BaseWorker):
    """Process: Handles all alerting (console, desktop, Telegram)."""
    
    def __init__(self, input_queue: Queue, output_queue: Queue, shutdown_event: Event):
        super().__init__("Alert", input_queue, output_queue, shutdown_event)
        
    async def _async_run(self):
        load_config()
        setup_logging()
        
        alert_manager = AlertManager()
        await alert_manager.start()
        
        while not self.shutdown_event.is_set():
            try:
                msg = self.input_queue.get(timeout=1)
                if msg.type == "SIGNAL":
                    alert_manager.send_signal_alert(msg.data)
                elif msg.type == "VOL_ALERT":
                    data = msg.data
                    alert_manager.send_vol_alert(
                        data["symbol"], data["timeframe"], 
                        0.02, 0.02, data["expanding"]
                    )
                elif msg.type == "NEWS":
                    alert_manager.send_news_alert(msg.data)
            except Exception as e:
                if not self.shutdown_event.is_set():
                    log.error("alert_worker_error", error=str(e))
                    await asyncio.sleep(0.1)
            await asyncio.sleep(0.01)
        
        await alert_manager.stop()


class NewsWorker(BaseWorker):
    """Process: Fetches and processes news."""
    
    def __init__(self, input_queue: Queue, output_queue: Queue, shutdown_event: Event):
        super().__init__("News", input_queue, output_queue, shutdown_event)
        
    async def _async_run(self):
        load_config()
        setup_logging()
        
        universe = UniverseManager()
        universe.load()
        news_aggregator = NewsAggregator(universe)
        await news_aggregator.start()
        
        while not self.shutdown_event.is_set():
            try:
                news = await news_aggregator.get_latest_news(limit=20)
                for item in news:
                    self.output_queue.put(ProcessMessage(type="NEWS", data=item))
            except Exception as e:
                log.error("news_worker_error", error=str(e))
            await asyncio.sleep(10)
        
        await news_aggregator.stop()


class EODWorker(BaseWorker):
    """Process: Generates EOD reports at market close."""
    
    def __init__(self, input_queue: Queue, output_queue: Queue, shutdown_event: Event):
        super().__init__("EOD", input_queue, output_queue, shutdown_event)
        
    async def _async_run(self):
        load_config()
        setup_logging()
        
        universe = UniverseManager()
        universe.load()
        state_store = StateStore()
        eod_analyzer = EODAnalyzer(state_store, universe)
        alert_manager = AlertManager()
        await alert_manager.start()
        
        last_eod_date = None
        
        while not self.shutdown_event.is_set():
            if is_market_hours():
                last_eod_date = None
            elif last_eod_date != datetime.now().date():
                log.info("generating_eod_report")
                report = eod_analyzer.generate()
                # Note: alert_manager would need to be passed or recreated
                last_eod_date = datetime.now().date()
            
            await asyncio.sleep(60)
        
        await alert_manager.stop()


class MultiProcessOrchestrator:
    """Orchestrates all worker processes."""
    
    def __init__(self):
        self.shutdown_event = mp.Event()
        self.queues = {
            "ticks": Queue(maxsize=10000),
            "bars": Queue(maxsize=10000),
            "signals": Queue(maxsize=1000),
            "news": Queue(maxsize=1000),
            "alerts": Queue(maxsize=1000),
        }
        self.processes = {}
        
    def start(self):
        log.info("Starting multi-process orchestrator")
        
        workers = {
            "ingestion": DataIngestionWorker,
            "aggregation": BarAggregationWorker,
            "engine": EngineWorker,
            "alerts": AlertWorker,
            "news": NewsWorker,
            "eod": EODWorker,
        }
        
        # Queue mapping for each worker
        queue_map = {
            "ingestion": ("ticks", "ticks"),
            "aggregation": ("ticks", "bars"),
            "engine": ("bars", "signals"),
            "alerts": ("signals", "alerts"),
            "news": ("news", "alerts"),
            "eod": ("news", "alerts"),
        }
        
        self.processes = {}
        
        for name, worker_class in workers.items():
            in_queue_name, out_queue_name = queue_map[name]
            proc = mp.Process(
                target=self._run_worker,
                args=(worker_class, self.queues[in_queue_name], self.queues[out_queue_name], self.shutdown_event),
                name=f"Worker-{name}"
            )
            proc.start()
            self.processes[name] = proc
            log.info(f"Started process: {name} (PID: {proc.pid})")
        
        signal.signal(signal.SIGINT, self._signal_handler)
        signal.signal(signal.SIGTERM, self._signal_handler)
        
    @staticmethod
    def _run_worker(worker_class, input_queue, output_queue, shutdown_event):
        worker = worker_class(input_queue, output_queue, shutdown_event)
        worker.run()
        
    def _signal_handler(self, signum, frame):
        log.info(f"Received signal {signum}, shutting down...")
        self.shutdown()
        sys.exit(0)
        
    def shutdown(self):
        log.info("Shutting down all processes...")
        self.shutdown_event.set()
        
        for name, proc in self.processes.items():
            proc.join(timeout=10)
            if proc.is_alive():
                proc.terminate()
                proc.join(timeout=5)
            log.info(f"Stopped process: {name}")
        
    def wait(self):
        for proc in self.processes.values():
            proc.join()


def run_multiprocess():
    """Entry point for multi-process mode."""
    mp.set_start_method('spawn', force=True)
    
    load_config()
    setup_logging()
    
    log.info("Starting multi-process stock analyzer")
    
    orchestrator = MultiProcessOrchestrator()
    orchestrator.start()
    orchestrator.wait()


if __name__ == "__main__":
    run_multiprocess()