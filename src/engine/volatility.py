from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from typing import Any

import numpy as np

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone
from src.data.models import BarData, VolatilitySnapshot

log = get_logger(__name__)


class VolatilityCalculator:
    def __init__(self):
        self.config = get_config().volatility
        self._windows: dict[str, dict[str, deque]] = {}  # symbol -> timeframe -> deque of log returns
        self._bars_cache: dict[str, dict[str, deque]] = {}  # symbol -> timeframe -> deque of bars

    def add_bar(self, bar: BarData) -> list[VolatilitySnapshot]:
        snapshots = []
        symbol = bar.symbol
        tf = bar.timeframe

        if symbol not in self._bars_cache:
            self._bars_cache[symbol] = {}
            self._windows[symbol] = {}

        if tf not in self._bars_cache[symbol]:
            self._bars_cache[symbol][tf] = deque(maxlen=max(self.config.windows.get(tf, [60])))
            self._windows[symbol][tf] = deque(maxlen=max(self.config.windows.get(tf, [60])))

        bars_deque = self._bars_cache[symbol][tf]
        returns_deque = self._windows[symbol][tf]

        bars_deque.append(bar)
        if bar.log_returns != 0:
            returns_deque.append(bar.log_returns)

        for window in self.config.windows.get(tf, []):
            if len(returns_deque) >= window:
                recent_returns = list(returns_deque)[-window:]
                realized_vol = float(np.std(recent_returns, ddof=1))
                mean_return = float(np.mean(recent_returns))

                minutes_per_bar = self._tf_to_minutes(tf)
                annualization = math.sqrt(self.config.annualization_factor / minutes_per_bar)
                annualized_vol = realized_vol * annualization

                snapshot = VolatilitySnapshot(
                    symbol=symbol,
                    timeframe=tf,
                    timestamp=bar.timestamp,
                    window_bars=window,
                    realized_vol=realized_vol,
                    annualized_vol=annualized_vol,
                    mean_return=mean_return,
                )
                snapshots.append(snapshot)

        return snapshots

    def get_current_volatility(
        self, symbol: str, timeframe: str, window: int
    ) -> VolatilitySnapshot | None:
        if symbol not in self._windows or timeframe not in self._windows[symbol]:
            return None

        returns_deque = self._windows[symbol][timeframe]
        if len(returns_deque) < window:
            return None

        recent_returns = list(returns_deque)[-window:]
        realized_vol = float(np.std(recent_returns, ddof=1))
        mean_return = float(np.mean(recent_returns))

        minutes_per_bar = self._tf_to_minutes(timeframe)
        annualization = math.sqrt(self.config.annualization_factor / minutes_per_bar)
        annualized_vol = realized_vol * annualization

        bars_deque = self._bars_cache[symbol][timeframe]
        latest_bar = bars_deque[-1] if bars_deque else None
        timestamp = latest_bar.timestamp if latest_bar else datetime.now(get_timezone())

        return VolatilitySnapshot(
            symbol=symbol,
            timeframe=timeframe,
            timestamp=timestamp,
            window_bars=window,
            realized_vol=realized_vol,
            annualized_vol=annualized_vol,
            mean_return=mean_return,
        )

    def get_volatility_series(
        self, symbol: str, timeframe: str, window: int, lookback: int
    ) -> list[VolatilitySnapshot]:
        if symbol not in self._bars_cache or timeframe not in self._bars_cache[symbol]:
            return []

        bars_deque = self._bars_cache[symbol][timeframe]
        if len(bars_deque) < window + lookback:
            return []

        bars = list(bars_deque)
        results = []

        for i in range(lookback, 0, -1):
            idx = len(bars) - i
            window_bars = bars[idx - window:idx]
            if len(window_bars) < window:
                continue

            returns = [b.log_returns for b in window_bars if b.log_returns != 0]
            if len(returns) < window:
                continue

            realized_vol = float(np.std(returns, ddof=1))
            mean_return = float(np.mean(returns))

            minutes_per_bar = self._tf_to_minutes(timeframe)
            annualization = math.sqrt(self.config.annualization_factor / minutes_per_bar)
            annualized_vol = realized_vol * annualization

            results.append(VolatilitySnapshot(
                symbol=symbol,
                timeframe=timeframe,
                timestamp=window_bars[-1].timestamp,
                window_bars=window,
                realized_vol=realized_vol,
                annualized_vol=annualized_vol,
                mean_return=mean_return,
            ))

        return results

    def _tf_to_minutes(self, tf: str) -> int:
        if tf.endswith("m"):
            return int(tf[:-1])
        if tf.endswith("h"):
            return int(tf[:-1]) * 60
        if tf.endswith("d"):
            return int(tf[:-1]) * 1440
        return 1

    def get_vol_ratio(
        self, symbol: str, timeframe: str, fast_window: int, slow_window: int
    ) -> float | None:
        fast = self.get_current_volatility(symbol, timeframe, fast_window)
        slow = self.get_current_volatility(symbol, timeframe, slow_window)
        if fast and slow and slow.realized_vol > 0:
            return fast.realized_vol / slow.realized_vol
        return None

    def is_vol_expanding(self, symbol: str, timeframe: str, threshold: float = 2.0) -> bool:
        ratio = self.get_vol_ratio(symbol, timeframe, 16, 60)  # 15min vs 1hr equivalent
        return ratio is not None and ratio > threshold

    def is_vol_contracting(self, symbol: str, timeframe: str, threshold: float = 0.5) -> bool:
        ratio = self.get_vol_ratio(symbol, timeframe, 16, 60)
        return ratio is not None and ratio < threshold