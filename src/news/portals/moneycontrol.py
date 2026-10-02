from __future__ import annotations

import asyncio
import hashlib
import re
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import aiohttp
from bs4 import BeautifulSoup

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone, parse_iso_datetime
from src.data.models import NewsItem

log = get_logger(__name__)


class NSEAnnouncementsScraper:
    BASE_URL = "https://www.nseindia.com"
    API_URL = "https://www.nseindia.com/api/corporate-announcements"

    def __init__(self, symbol_map: dict[str, str] | None = None):
        self.config = get_config()
        self.symbol_map = symbol_map or {}
        self._session: aiohttp.ClientSession | None = None
        self._seen_hashes: set[str] = set()
        self._running = False

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/plain, */*",
                "Referer": "https://www.nseindia.com/",
            }
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def fetch(self) -> list[NewsItem]:
        if not self._session:
            return []

        params = {
            "index": "equities",
            "from_date": "",
            "to_date": "",
        }

        try:
            async with self._session.get(self.API_URL, params=params) as resp:
                if resp.status != 200:
                    log.warning("nse_announcements_failed", status=resp.status)
                    return []
                data = await resp.json()
        except Exception as e:
            log.warning("nse_announcements_error", error=str(e))
            return []

        items = []
        for ann in data:
            try:
                symbol = self._extract_symbol(ann.get("symbol", ""))
                if not symbol:
                    continue

                headline = ann.get("subject", "").strip()
                if not headline:
                    continue

                # Deduplication
                content_hash = hashlib.md5(f"{symbol}{headline}".encode()).hexdigest()
                if content_hash in self._seen_hashes:
                    continue
                self._seen_hashes.add(content_hash)

                timestamp_str = ann.get("dissemDT", "")
                if timestamp_str:
                    timestamp = parse_iso_datetime(timestamp_str)
                else:
                    timestamp = datetime.now(get_timezone())

                url = urljoin(self.BASE_URL, ann.get("attachment", ""))

                items.append(NewsItem(
                    symbol=symbol,
                    timestamp=timestamp,
                    source="nse",
                    headline=headline,
                    url=url,
                    sentiment=self._analyze_sentiment(headline),
                    metadata={"category": ann.get("category", ""), "symbol_raw": ann.get("symbol", "")},
                ))
            except Exception as e:
                log.debug("nse_announcement_parse_error", error=str(e), ann=ann)

        log.info("nse_announcements_fetched", count=len(items))
        return items

    def _extract_symbol(self, raw: str) -> str | None:
        # NSE format: "RELIANCE-EQ" or "RELIANCE"
        if "-" in raw:
            return raw.split("-")[0].strip().upper()
        return raw.strip().upper() if raw else None

    def _analyze_sentiment(self, text: str) -> float:
        positive = ["profit", "gain", "rise", "up", "growth", "bonus", "dividend", "buyback", "strong", "beat", "exceed", "record", "high", "approval", "clear", "launch", "expansion", "acquisition", "merger", "partnership", "contract", "order", "award", "investment", "fund", "raise", "capex"]
        negative = ["loss", "fall", "down", "decline", "drop", "cut", "reduce", "weak", "miss", "below", "low", "concern", "risk", "investigation", "penalty", "fine", "lawsuit", "default", "delay", "cancel", "withdraw", "suspend", "resign", "exit", "sell", "stake sale"]

        text_lower = text.lower()
        pos_score = sum(1 for w in positive if w in text_lower)
        neg_score = sum(1 for w in negative if w in text_lower)

        if pos_score + neg_score == 0:
            return 0.0
        return (pos_score - neg_score) / (pos_score + neg_score)


class PortalScraperBase:
    def __init__(self, name: str, base_url: str, symbol_map: dict[str, str] | None = None):
        self.name = name
        self.base_url = base_url
        self.symbol_map = symbol_map or {}
        self._session: aiohttp.ClientSession | None = None
        self._seen_hashes: set[str] = set()

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            },
            timeout=aiohttp.ClientTimeout(total=30),
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def fetch(self, symbols: list[str] | None = None) -> list[NewsItem]:
        return []

    def _deduplicate(self, symbol: str, headline: str, url: str) -> bool:
        content_hash = hashlib.md5(f"{symbol}{headline}{url}".encode()).hexdigest()
        if content_hash in self._seen_hashes:
            return True
        self._seen_hashes.add(content_hash)
        return False

    def _analyze_sentiment(self, text: str) -> float:
        positive = ["profit", "gain", "rise", "up", "growth", "bonus", "dividend", "buyback", "strong", "beat", "exceed", "record", "high", "approval", "clear", "launch", "expansion", "acquisition", "merger", "partnership", "contract", "order", "award", "investment", "fund", "raise", "capex", "positive", "bullish", "upgrade", "outperform", "target raised"]
        negative = ["loss", "fall", "down", "decline", "drop", "cut", "reduce", "weak", "miss", "below", "low", "concern", "risk", "investigation", "penalty", "fine", "lawsuit", "default", "delay", "cancel", "withdraw", "suspend", "resign", "exit", "sell", "stake sale", "negative", "bearish", "downgrade", "underperform", "target cut"]

        text_lower = text.lower()
        pos_score = sum(1 for w in positive if w in text_lower)
        neg_score = sum(1 for w in negative if w in text_lower)

        if pos_score + neg_score == 0:
            return 0.0
        return (pos_score - neg_score) / (pos_score + neg_score)


class MoneycontrolScraper(PortalScraperBase):
    def __init__(self, symbol_map: dict[str, str] | None = None):
        super().__init__("moneycontrol", "https://www.moneycontrol.com", symbol_map)
        self.rss_url = "https://www.moneycontrol.com/rss/latestnews.xml"

    async def fetch(self, symbols: list[str] | None = None) -> list[NewsItem]:
        if not self._session:
            return []

        items = []
        try:
            async with self._session.get(self.rss_url) as resp:
                if resp.status != 200:
                    return []
                xml = await resp.text()
        except Exception as e:
            log.warning("moneycontrol_rss_failed", error=str(e))
            return []

        try:
            soup = BeautifulSoup(xml, "xml")
            for item in soup.find_all("item")[:50]:
                title = item.title.text.strip() if item.title else ""
                link = item.link.text.strip() if item.link else ""
                pub_date = item.pubDate.text.strip() if item.pubDate else ""

                if not title or not link:
                    continue

                # Try to match symbol from title
                matched_symbol = self._match_symbol(title, symbols)
                if not matched_symbol:
                    continue

                if self._deduplicate(matched_symbol, title, link):
                    continue

                try:
                    timestamp = parse_iso_datetime(pub_date) if pub_date else datetime.now(get_timezone())
                except Exception:
                    timestamp = datetime.now(get_timezone())

                items.append(NewsItem(
                    symbol=matched_symbol,
                    timestamp=timestamp,
                    source=self.name,
                    headline=title,
                    url=link,
                    sentiment=self._analyze_sentiment(title),
                    metadata={},
                ))
        except Exception as e:
            log.warning("moneycontrol_parse_error", error=str(e))

        return items

    def _match_symbol(self, title: str, symbols: list[str] | None = None) -> str | None:
        title_upper = title.upper()
        search_symbols = symbols or list(self.symbol_map.keys())

        # Direct symbol match
        for sym in search_symbols:
            if sym in title_upper:
                return sym

        # Company name mapping
        for sym, name in self.symbol_map.items():
            if name.upper() in title_upper:
                return sym

        return None


class EconomicTimesScraper(PortalScraperBase):
    def __init__(self, symbol_map: dict[str, str] | None = None):
        super().__init__("economictimes", "https://economictimes.indiatimes.com", symbol_map)
        self.rss_urls = [
            "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
            "https://economictimes.indiatimes.com/industry/rssfeeds/13352306.cms",
        ]

    async def fetch(self, symbols: list[str] | None = None) -> list[NewsItem]:
        if not self._session:
            return []

        items = []
        for rss_url in self.rss_urls:
            try:
                async with self._session.get(rss_url) as resp:
                    if resp.status != 200:
                        continue
                    xml = await resp.text()
            except Exception:
                continue

            try:
                soup = BeautifulSoup(xml, "xml")
                for item in soup.find_all("item")[:30]:
                    title = item.title.text.strip() if item.title else ""
                    link = item.link.text.strip() if item.link else ""
                    pub_date = item.pubDate.text.strip() if item.pubDate else ""

                    if not title or not link:
                        continue

                    matched_symbol = self._match_symbol(title, symbols)
                    if not matched_symbol:
                        continue

                    if self._deduplicate(matched_symbol, title, link):
                        continue

                    try:
                        timestamp = parse_iso_datetime(pub_date) if pub_date else datetime.now(get_timezone())
                    except Exception:
                        timestamp = datetime.now(get_timezone())

                    items.append(NewsItem(
                        symbol=matched_symbol,
                        timestamp=timestamp,
                        source=self.name,
                        headline=title,
                        url=link,
                        sentiment=self._analyze_sentiment(title),
                        metadata={},
                    ))
            except Exception as e:
                log.warning("economictimes_parse_error", error=str(e))

        return items

    def _match_symbol(self, title: str, symbols: list[str] | None = None) -> str | None:
        title_upper = title.upper()
        search_symbols = symbols or list(self.symbol_map.keys())

        for sym in search_symbols:
            if sym in title_upper:
                return sym

        for sym, name in self.symbol_map.items():
            if name.upper() in title_upper:
                return sym

        return None


class BusinessStandardScraper(PortalScraperBase):
    def __init__(self, symbol_map: dict[str, str] | None = None):
        super().__init__("businessstandard", "https://www.business-standard.com", symbol_map)
        self.search_url = "https://www.business-standard.com/search"

    async def fetch(self, symbols: list[str] | None = None) -> list[NewsItem]:
        if not self._session or not symbols:
            return []

        items = []
        for symbol in symbols[:10]:  # Limit to avoid too many requests
            try:
                params = {"q": symbol, "page": 1}
                async with self._session.get(self.search_url, params=params) as resp:
                    if resp.status != 200:
                        continue
                    html = await resp.text()
            except Exception:
                continue

            try:
                soup = BeautifulSoup(html, "html.parser")
                articles = soup.find_all("div", class_="article-item")[:5]
                for article in articles:
                    title_elem = article.find("h3") or article.find("h2")
                    link_elem = article.find("a")
                    time_elem = article.find("span", class_="time") or article.find("time")

                    if not title_elem or not link_elem:
                        continue

                    title = title_elem.get_text(strip=True)
                    link = link_elem.get("href", "")
                    if link and not link.startswith("http"):
                        link = urljoin(self.base_url, link)

                    if not title or not link:
                        continue

                    if self._deduplicate(symbol, title, link):
                        continue

                    try:
                        timestamp = parse_iso_datetime(time_elem.get_text(strip=True)) if time_elem else datetime.now(get_timezone())
                    except Exception:
                        timestamp = datetime.now(get_timezone())

                    items.append(NewsItem(
                        symbol=symbol,
                        timestamp=timestamp,
                        source=self.name,
                        headline=title,
                        url=link,
                        sentiment=self._analyze_sentiment(title),
                        metadata={},
                    ))
            except Exception as e:
                log.warning("businessstandard_parse_error", error=str(e))

            await asyncio.sleep(1)  # Rate limit

        return items