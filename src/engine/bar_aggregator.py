from __future__ import annotations

import asyncio
from collections import defaultdict
from datetime import datetime, timedelta
from typing import Any

from src.utils.logging import get_logger
from src.utils.time import get_timezone, round_to_minute, next_bar_time
from src.data.models import TickData, BarData

log = get_logger(__name__)


def _default_dict_factory():
    return defaultdict(dict)


class BarAggregator:
    def __init__(self, timeframes: list[str] = None, grace_seconds: int = 2):
        self.timeframes = timeframes or ["1m", "5m", "15m"]
        self.grace_seconds = grace_seconds
        self._buffers: dict[str, dict[str, dict]] = defaultdict(_default_dict_factory)
        self._closed_bars: asyncio.Queue[BarData] = asyncio.Queue(maxsize=10000)
        self._last_tick_time: dict[str, datetime] = {}

    def add_tick(self, tick: TickData) -> list[BarData]:
        closed = []
        symbol = tick.symbol
        tick_time = tick.timestamp

        for tf in self.timeframes:
            bar_start = round_to_minute(tick_time, self._tf_to_minutes(tf))
            bar_key = f"{symbol}:{tf}:{bar_start.isoformat()}"

            buffer = self._buffers[symbol][tf]
            if bar_key not in buffer:
                buffer[bar_key] = {
                    "symbol": symbol,
                    "timeframe": tf,
                    "start": bar_start,
                    "end": next_bar_time(bar_start, self._tf_to_minutes(tf)),
                    "open": tick.ltp,
                    "high": tick.ltp,
                    "low": tick.ltp,
                    "close": tick.ltp,
                    "volume": tick.volume,
                    "vwap_sum": tick.ltp * tick.volume if tick.volume > 0 else 0,
                    "total_volume": tick.volume,
                    "tick_count": 1,
                    "first_tick_time": tick_time,
                }
            else:
                bar = buffer[bar_key]
                bar["high"] = max(bar["high"], tick.ltp)
                bar["low"] = min(bar["low"], tick.ltp)
                bar["close"] = tick.ltp
                if tick.volume > bar.get("last_volume", 0):
                    vol_delta = tick.volume - bar.get("last_volume", 0)
                    bar["volume"] += vol_delta
                    bar["vwap_sum"] += tick.ltp * vol_delta
                    bar["total_volume"] += vol_delta
                bar["last_volume"] = tick.volume
                bar["tick_count"] += 1

        self._last_tick_time[symbol] = tick_time
        closed = self._check_closed_bars(tick_time)
        return closed

    def _check_closed_bars(self, current_time: datetime) -> list[BarData]:
        closed = []
        grace_cutoff = current_time - timedelta(seconds=self.grace_seconds)

        for symbol, tf_buffers in self._buffers.items():
            for tf, bars in list(tf_buffers.items()):
                for bar_key, bar in list(bars.items()):
                    if bar["end"] <= grace_cutoff:
                        if bar["total_volume"] > 0:
                            vwap = bar["vwap_sum"] / bar["total_volume"]
                        else:
                            vwap = (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4

                        closed_bar = BarData(
                            symbol=bar["symbol"],
                            timeframe=bar["timeframe"],
                            timestamp=bar["start"],
                            open=bar["open"],
                            high=bar["high"],
                            low=bar["low"],
                            close=bar["close"],
                            volume=bar["volume"],
                            vwap=vwap,
                            tick_count=bar["tick_count"],
                        )
                        closed.append(closed_bar)
                        try:
                            self._closed_bars.put_nowait(closed_bar)
                        except asyncio.QueueFull:
                            pass
                        del bars[bar_key]

                if not bars:
                    del tf_buffers[tf]
            if not tf_buffers:
                del self._buffers[symbol]

        return closed

    def _tf_to_minutes(self, tf: str) -> int:
        if tf.endswith("m"):
            return int(tf[:-1])
        if tf.endswith("h"):
            return int(tf[:-1]) * 60
        if tf.endswith("d"):
            return int(tf[:-1]) * 1440
        return 1

    def get_closed_bars_queue(self) -> asyncio.Queue[BarData]:
        return self._closed_bars

    def get_current_bar(self, symbol: str, timeframe: str) -> BarData | None:
        tf_buffers = self._buffers.get(symbol, {}).get(timeframe, {})
        if not tf_buffers:
            return None
        bar = list(tf_buffers.values())[-1]
        if bar["total_volume"] > 0:
            vwap = bar["vwap_sum"] / bar["total_volume"]
        else:
            vwap = (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4
        return BarData(
            symbol=bar["symbol"],
            timeframe=bar["timeframe"],
            timestamp=bar["start"],
            open=bar["open"],
            high=bar["high"],
            low=bar["low"],
            close=bar["close"],
            volume=bar["volume"],
            vwap=vwap,
            tick_count=bar["tick_count"],
        )

    def flush_all(self) -> list[BarData]:
        closed = []
        now = datetime.now(get_timezone())
        for symbol, tf_buffers in list(self._buffers.items()):
            for tf, bars in list(tf_buffers.items()):
                for bar_key, bar in list(bars.items()):
                    if bar["total_volume"] > 0:
                        vwap = bar["vwap_sum"] / bar["total_volume"]
                    else:
                        vwap = (bar["open"] + bar["high"] + bar["low"] + bar["close"]) / 4
                    closed_bar = BarData(
                        symbol=bar["symbol"],
                        timeframe=bar["timeframe"],
                        timestamp=bar["start"],
                        open=bar["open"],
                        high=bar["high"],
                        low=bar["low"],
                        close=bar["close"],
                        volume=bar["volume"],
                        vwap=vwap,
                        tick_count=bar["tick_count"],
                    )
                    closed.append(closed_bar)
        self._buffers.clear()
        return closed