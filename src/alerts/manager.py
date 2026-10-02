from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime
from typing import Any

import plyer

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import format_ist
from src.data.models import Signal, SignalPriority, Alert, NewsItem

log = get_logger(__name__)


class AlertManager:
    def __init__(self):
        self.config = get_config().alerts
        self._throttle: dict[str, float] = {}  # key -> last_alert_time
        self._telegram_task: asyncio.Task | None = None
        self._telegram_queue: asyncio.Queue[str] = asyncio.Queue(maxsize=100)

    async def start(self) -> None:
        if self.config.telegram_bot_token and self.config.telegram_chat_id:
            self._telegram_task = asyncio.create_task(self._telegram_worker())
        log.info("alert_manager_started", desktop=self.config.desktop, telegram=bool(self.config.telegram_bot_token))

    async def stop(self) -> None:
        if self._telegram_task:
            self._telegram_task.cancel()
            try:
                await self._telegram_task
            except asyncio.CancelledError:
                pass
        log.info("alert_manager_stopped")

    def send_signal_alert(self, signal: Signal) -> None:
        key = f"{signal.symbol}:{signal.signal_type.value}:{signal.timeframe}"
        now = time.time()

        if key in self._throttle and now - self._throttle[key] < self.config.throttle_sec:
            return

        self._throttle[key] = now

        message = self._format_signal_alert(signal)

        # Console (structured JSON)
        print(json.dumps({
            "type": "SIGNAL",
            "timestamp": format_ist(signal.timestamp),
            "symbol": signal.symbol,
            "signal_type": signal.signal_type.value,
            "confidence": signal.confidence,
            "price": signal.price,
            "timeframe": signal.timeframe,
            "priority": signal.priority.value,
        }))

        # Desktop notification
        if self.config.desktop:
            self._desktop_notify(
                f"{signal.symbol} {signal.signal_type.value}",
                f"Confidence: {signal.confidence:.0%} @ {signal.price:.2f}",
            )

        # Telegram
        if self.config.telegram_bot_token:
            try:
                self._telegram_queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def send_news_alert(self, news: NewsItem) -> None:
        key = f"news:{news.symbol}:{hash(news.headline) % 10000}"
        now = time.time()

        if key in self._throttle and now - self._throttle[key] < self.config.throttle_sec:
            return

        self._throttle[key] = now

        sentiment_str = "🟢" if news.sentiment > 0.2 else "🔴" if news.sentiment < -0.2 else "⚪"
        message = f"{sentiment_str} {news.symbol} | {news.source} | {news.headline[:100]}"

        print(json.dumps({
            "type": "NEWS",
            "timestamp": format_ist(news.timestamp),
            "symbol": news.symbol,
            "source": news.source,
            "headline": news.headline,
            "url": news.url,
            "sentiment": news.sentiment,
        }))

        if self.config.desktop:
            self._desktop_notify(f"{news.symbol} News ({news.source})", news.headline[:100])

        if self.config.telegram_bot_token:
            try:
                self._telegram_queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def send_vol_alert(self, symbol: str, timeframe: str, vol: float, threshold: float, expanding: bool) -> None:
        key = f"vol:{symbol}:{timeframe}:{'expand' if expanding else 'contract'}"
        now = time.time()

        if key in self._throttle and now - self._throttle[key] < self.config.throttle_sec:
            return

        self._throttle[key] = now

        direction = "EXPANDING" if expanding else "CONTRACTING"
        message = f"⚡ {symbol} {timeframe} Vol {direction}: {vol:.2%} (threshold: {threshold:.2%})"

        print(json.dumps({
            "type": "VOLATILITY",
            "timestamp": format_ist(datetime.now()),
            "symbol": symbol,
            "timeframe": timeframe,
            "direction": direction,
            "volatility": vol,
            "threshold": threshold,
        }))

        if self.config.desktop:
            self._desktop_notify(f"{symbol} Vol {direction}", f"{vol:.2%} vs {threshold:.2%}")

        if self.config.telegram_bot_token:
            try:
                self._telegram_queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def send_eod_alert(self, summary: dict) -> None:
        message = self._format_eod_alert(summary)

        print(json.dumps({
            "type": "EOD",
            "timestamp": format_ist(datetime.now()),
            **summary,
        }))

        if self.config.desktop:
            self._desktop_notify("EOD Summary", f"{summary.get('date')} - {summary.get('advancers', 0)} up / {summary.get('decliners', 0)} down")

        if self.config.telegram_bot_token:
            try:
                self._telegram_queue.put_nowait(message)
            except asyncio.QueueFull:
                pass

    def _format_signal_alert(self, signal: Signal) -> str:
        emoji = {
            "MEAN_REVERSION_LONG": "📈",
            "MEAN_REVERSION_SHORT": "📉",
            "MOMENTUM_UP": "🚀",
            "MOMENTUM_DOWN": "💥",
            "VOL_EXPANSION": "⚡",
            "VOL_CONTRACTION": "😴",
        }.get(signal.signal_type.value, "📊")

        priority_emoji = {
            "HIGH": "🔴",
            "MEDIUM": "🟡",
            "LOW": "🟢",
        }.get(signal.priority.value, "")

        return (
            f"{emoji} {priority_emoji} {signal.symbol} {signal.timeframe}\n"
            f"{signal.signal_type.value}\n"
            f"Confidence: {signal.confidence:.0%} | Price: {signal.price:.2f}\n"
            f"Time: {format_ist(signal.timestamp)}"
        )

    def _format_eod_alert(self, summary: dict) -> str:
        lines = [
            f"📊 EOD Summary - {summary.get('date', 'N/A')}",
            f"Advancers: {summary.get('advancers', 0)} | Decliners: {summary.get('decliners', 0)}",
            f"Vol Leaders: {', '.join(summary.get('vol_leaders', [])[:5])}",
            f"Top Signals: {', '.join(summary.get('top_signals', [])[:3])}",
        ]
        return "\n".join(lines)

    def _desktop_notify(self, title: str, message: str) -> None:
        try:
            plyer.notification.notify(
                title=title,
                message=message,
                app_name="Stock Analyzer",
                timeout=5,
            )
        except Exception as e:
            log.debug("desktop_notify_failed", error=str(e))

    async def _telegram_worker(self) -> None:
        import aiohttp
        url = f"https://api.telegram.org/bot{self.config.telegram_bot_token}/sendMessage"

        async with aiohttp.ClientSession() as session:
            while True:
                try:
                    message = await self._telegram_queue.get()
                    payload = {
                        "chat_id": self.config.telegram_chat_id,
                        "text": message,
                        "parse_mode": "HTML",
                    }
                    async with session.post(url, json=payload) as resp:
                        if resp.status != 200:
                            log.warning("telegram_send_failed", status=resp.status)
                    await asyncio.sleep(1)  # Rate limit
                except asyncio.CancelledError:
                    break
                except Exception as e:
                    log.warning("telegram_worker_error", error=str(e))
                    await asyncio.sleep(5)