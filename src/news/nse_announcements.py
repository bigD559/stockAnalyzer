from __future__ import annotations

import asyncio
import hashlib
from datetime import datetime
from typing import Any
from urllib.parse import urljoin

import aiohttp

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
        self.name = "nse"

    async def __aenter__(self):
        self._session = aiohttp.ClientSession(
            headers={
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                "Accept": "application/json, text/plain, */*",
                "Accept-Encoding": "gzip, deflate",
                "Referer": "https://www.nseindia.com/",
            }
        )
        return self

    async def __aexit__(self, *args):
        if self._session:
            await self._session.close()

    async def fetch(self, symbols: list[str] | None = None) -> list[NewsItem]:
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