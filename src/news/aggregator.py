from __future__ import annotations

import asyncio
from datetime import datetime
from typing import Any

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone, now
from src.data.models import NewsItem
from src.news import (
    NSEAnnouncementsScraper,
    MoneycontrolScraper,
    EconomicTimesScraper,
    BusinessStandardScraper,
)
from src.data.universe import UniverseManager

log = get_logger(__name__)


class NewsAggregator:
    def __init__(self, universe: UniverseManager):
        self.config = get_config().news
        self.universe = universe
        self._running = False
        self._task: asyncio.Task | None = None
        self._news_queue: asyncio.Queue[NewsItem] = asyncio.Queue(maxsize=1000)
        self._scrapers: list[Any] = []
        self._symbol_map: dict[str, str] = {}  # symbol -> company name

    async def start(self) -> None:
        self._build_symbol_map()
        self._init_scrapers()
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        log.info("news_aggregator_started")

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        for scraper in self._scrapers:
            if hasattr(scraper, '__aexit__'):
                await scraper.__aexit__(None, None, None)
        log.info("news_aggregator_stopped")

    def _build_symbol_map(self) -> None:
        # Build symbol -> company name mapping for better matching
        # In production, fetch from NSE or maintain a static map
        self._symbol_map = {
            "RELIANCE": "Reliance Industries",
            "TCS": "Tata Consultancy Services",
            "HDFCBANK": "HDFC Bank",
            "ICICIBANK": "ICICI Bank",
            "HINDUNILVR": "Hindustan Unilever",
            "INFY": "Infosys",
            "ITC": "ITC",
            "SBIN": "State Bank of India",
            "BHARTIARTL": "Bharti Airtel",
            "BAJFINANCE": "Bajaj Finance",
            "KOTAKBANK": "Kotak Mahindra Bank",
            "LT": "Larsen & Toubro",
            "ASIANPAINT": "Asian Paints",
            "AXISBANK": "Axis Bank",
            "MARUTI": "Maruti Suzuki",
            "SUNPHARMA": "Sun Pharma",
            "TITAN": "Titan Company",
            "ULTRACEMCO": "UltraTech Cement",
            "NESTLEIND": "Nestle India",
            "WIPRO": "Wipro",
            "POWERGRID": "Power Grid",
            "NTPC": "NTPC",
            "TECHM": "Tech Mahindra",
            "TATAMOTORS": "Tata Motors",
            "BAJAJFINSV": "Bajaj Finserv",
            "HCLTECH": "HCL Technologies",
            "ADANIENT": "Adani Enterprises",
            "JSWSTEEL": "JSW Steel",
            "ONGC": "ONGC",
            "GRASIM": "Grasim Industries",
            "INDUSINDBK": "IndusInd Bank",
            "CIPLA": "Cipla",
            "COALINDIA": "Coal India",
            "BRITANNIA": "Britannia Industries",
            "DRREDDY": "Dr Reddy's Labs",
            "EICHERMOT": "Eicher Motors",
            "APOLLOHOSP": "Apollo Hospitals",
            "DIVISLAB": "Divi's Labs",
            "HEROMOTOCO": "Hero MotoCorp",
            "BPCL": "Bharat Petroleum",
            "TATACONSUM": "Tata Consumer",
            "HINDALCO": "Hindalco",
            "SBILIFE": "SBI Life",
            "BAJAJ-AUTO": "Bajaj Auto",
            "SHRIRAMFIN": "Shriram Finance",
            "M&M": "Mahindra & Mahindra",
            "TRENT": "Trent",
            "BEL": "Bharat Electronics",
            "ZOMATO": "Zomato",
            "JIOFIN": "Jio Financial",
            "DMART": "Avenue Supermarts",
        }

    def _init_scrapers(self) -> None:
        self._scrapers = []

        if "nse" in self.config.sources:
            self._scrapers.append(NSEAnnouncementsScraper(self._symbol_map))

        if "moneycontrol" in self.config.sources:
            self._scrapers.append(MoneycontrolScraper(self._symbol_map))

        if "economictimes" in self.config.sources:
            self._scrapers.append(EconomicTimesScraper(self._symbol_map))

        if "businessstandard" in self.config.sources:
            self._scrapers.append(BusinessStandardScraper(self._symbol_map))

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._fetch_all()
            except Exception as e:
                log.error("news_poll_error", error=str(e))

            await asyncio.sleep(self.config.poll_interval_sec)

    async def _fetch_all(self) -> None:
        symbols = self.universe.get_all_symbols()
        all_news = []

        for scraper in self._scrapers:
            try:
                async with scraper:
                    news = await scraper.fetch(symbols)
                    all_news.extend(news)
            except Exception as e:
                log.warning("scraper_failed", scraper=scraper.name, error=str(e))

        # Sort by timestamp, newest first
        all_news.sort(key=lambda x: x.timestamp, reverse=True)

        for item in all_news:
            try:
                self._news_queue.put_nowait(item)
            except asyncio.QueueFull:
                log.warning("news_queue_full")
                break

        log.info("news_fetched", total=len(all_news), queue_size=self._news_queue.qsize())

    def get_news_queue(self) -> asyncio.Queue[NewsItem]:
        return self._news_queue

    async def get_latest_news(self, symbol: str | None = None, limit: int = 50) -> list[NewsItem]:
        # Drain queue and filter
        items = []
        while not self._news_queue.empty() and len(items) < limit * 2:
            try:
                item = self._news_queue.get_nowait()
                if symbol is None or item.symbol == symbol:
                    items.append(item)
                else:
                    # Put back for other consumers
                    try:
                        self._news_queue.put_nowait(item)
                    except asyncio.QueueFull:
                        pass
            except asyncio.QueueEmpty:
                break
        return items[:limit]

    def get_news_for_symbol(self, symbol: str, since: datetime | None = None) -> list[NewsItem]:
        # This would query persistent storage; for now return empty
        return []