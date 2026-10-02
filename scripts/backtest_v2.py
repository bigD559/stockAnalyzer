from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone
from src.data.universe import UniverseManager
from src.data.historical import HistoricalDataFetcher
from src.data.models import BarData, Signal, SignalType, SignalPriority
from src.engine.volatility import VolatilityCalculator
from src.engine.patterns import PatternEngine
from src.engine.state import StateStore

log = get_logger(__name__)


@dataclass
class BacktestResult:
    symbol: str
    signal_type: str
    entry_time: datetime
    entry_price: float
    exit_time: datetime | None
    exit_price: float | None
    return_pct: float | None
    holding_bars: int
    confidence: float
    max_favorable: float = 0.0
    max_adverse: float = 0.0


@dataclass
class BacktestSummary:
    total_signals: int = 0
    winning_signals: int = 0
    losing_signals: int = 0
    win_rate: float = 0.0
    avg_return: float = 0.0
    avg_win: float = 0.0
    avg_loss: float = 0.0
    profit_factor: float = 0.0
    sharpe_ratio: float = 0.0
    max_drawdown: float = 0.0
    expectancy: float = 0.0
    by_signal_type: dict[str, dict] = field(default_factory=dict)


class BacktestEngine:
    def __init__(
        self,
        universe: UniverseManager,
        fetcher: HistoricalDataFetcher,
        holding_bars: int = 5,
        lookback_days: int = 30,
    ):
        self.universe = universe
        self.fetcher = fetcher
        self.holding_bars = holding_bars
        self.lookback_days = lookback_days
        self.vol_calc = VolatilityCalculator()
        self.pattern_engine = PatternEngine()
        self.results: list[BacktestResult] = []

    async def run(
        self,
        symbols: list[str] | None = None,
        timeframes: list[str] | None = None,
        start_date: datetime | None = None,
        end_date: datetime | None = None,
    ) -> BacktestSummary:
        if symbols is None:
            symbols = self.universe.get_all_symbols()[:50]  # Limit for speed
        if timeframes is None:
            timeframes = ["1m"]  # Use 1m data (yfinance has best availability)
        if end_date is None:
            end_date = datetime.now(get_timezone())
        if start_date is None:
            start_date = end_date - timedelta(days=min(self.lookback_days, 7))  # yfinance 1m limit

        log.info("backtest_start", symbols=len(symbols), timeframes=timeframes)

        self.results = []
        
        for symbol in symbols:
            for tf in timeframes:
                await self._backtest_symbol(symbol, tf, start_date, end_date)

        return self._compute_summary()

    async def _backtest_symbol(
        self,
        symbol: str,
        timeframe: str,
        start: datetime,
        end: datetime,
    ) -> None:
        # For 1m data, we can also test resampled timeframes
        bars = self.fetcher.get_bars(symbol, timeframe, start=start, end=end)
        
        if len(bars) < 50:
            return

        # Reset engines for each symbol/timeframe
        self.vol_calc = VolatilityCalculator()
        self.pattern_engine = PatternEngine()

        for i, bar in enumerate(bars):
            self.vol_calc.add_bar(bar)
            signals = self.pattern_engine.add_bar(bar)

            for signal in signals:
                result = self._evaluate_signal(signal, bars, i, timeframe)
                if result:
                    self.results.append(result)

    def _evaluate_signal(
        self,
        signal: Signal,
        bars: list[BarData],
        entry_idx: int,
        timeframe: str,
    ) -> BacktestResult | None:
        exit_idx = min(entry_idx + self.holding_bars, len(bars) - 1)
        
        if exit_idx <= entry_idx:
            return None

        entry_price = signal.price
        exit_price = bars[exit_idx].close
        
        # Calculate return
        if signal.signal_type in (SignalType.MEAN_REVERSION_LONG, SignalType.MOMENTUM_UP):
            return_pct = (exit_price - entry_price) / entry_price
        else:
            return_pct = (entry_price - exit_price) / entry_price

        # Max favorable/adverse excursion
        max_fav = 0.0
        max_adv = 0.0
        
        for j in range(entry_idx + 1, exit_idx + 1):
            high = bars[j].high
            low = bars[j].low
            
            if signal.signal_type in (SignalType.MEAN_REVERSION_LONG, SignalType.MOMENTUM_UP):
                fav = (high - entry_price) / entry_price
                adv = (entry_price - low) / entry_price
            else:
                fav = (entry_price - low) / entry_price
                adv = (high - entry_price) / entry_price
            
            max_fav = max(max_fav, fav)
            max_adv = max(max_adv, adv)

        return BacktestResult(
            symbol=signal.symbol,
            signal_type=signal.signal_type.value,
            entry_time=bars[entry_idx].timestamp,
            entry_price=entry_price,
            exit_time=bars[exit_idx].timestamp,
            exit_price=exit_price,
            return_pct=return_pct,
            holding_bars=exit_idx - entry_idx,
            confidence=signal.confidence,
            max_favorable=max_fav,
            max_adverse=max_adv,
        )

    def _compute_summary(self) -> BacktestSummary:
        if not self.results:
            return BacktestSummary()

        df = pd.DataFrame([{
            "symbol": r.symbol,
            "signal_type": r.signal_type,
            "return_pct": r.return_pct,
            "confidence": r.confidence,
            "max_fav": r.max_favorable,
            "max_adv": r.max_adverse,
        } for r in self.results])

        summary = BacktestSummary()
        summary.total_signals = len(df)
        summary.winning_signals = len(df[df["return_pct"] > 0])
        summary.losing_signals = len(df[df["return_pct"] <= 0])
        summary.win_rate = summary.winning_signals / summary.total_signals if summary.total_signals > 0 else 0
        summary.avg_return = df["return_pct"].mean()
        summary.avg_win = df[df["return_pct"] > 0]["return_pct"].mean() if summary.winning_signals > 0 else 0
        summary.avg_loss = df[df["return_pct"] <= 0]["return_pct"].mean() if summary.losing_signals > 0 else 0
        
        gross_profit = df[df["return_pct"] > 0]["return_pct"].sum()
        gross_loss = abs(df[df["return_pct"] <= 0]["return_pct"].sum())
        summary.profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')
        
        returns = df["return_pct"].values
        summary.sharpe_ratio = np.mean(returns) / np.std(returns) if np.std(returns) > 0 else 0
        summary.max_drawdown = self._calc_max_drawdown(returns)
        summary.expectancy = (summary.win_rate * summary.avg_win) - ((1 - summary.win_rate) * abs(summary.avg_loss))

        # By signal type
        for sig_type in df["signal_type"].unique():
            subset = df[df["signal_type"] == sig_type]
            summary.by_signal_type[sig_type] = {
                "count": len(subset),
                "win_rate": len(subset[subset["return_pct"] > 0]) / len(subset),
                "avg_return": subset["return_pct"].mean(),
                "avg_confidence": subset["confidence"].mean(),
                "profit_factor": (
                    subset[subset["return_pct"] > 0]["return_pct"].sum() /
                    abs(subset[subset["return_pct"] <= 0]["return_pct"].sum())
                    if (subset["return_pct"] <= 0).any() else float('inf')
                ),
            }

        log.info("backtest_complete", **summary.__dict__)
        return summary

    def _calc_max_drawdown(self, returns: np.ndarray) -> float:
        cumulative = np.cumprod(1 + returns)
        running_max = np.maximum.accumulate(cumulative)
        drawdown = (cumulative - running_max) / running_max
        return abs(drawdown.min()) if len(drawdown) > 0 else 0

    def save_results(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        
        df = pd.DataFrame([{
            "symbol": r.symbol,
            "signal_type": r.signal_type,
            "entry_time": r.entry_time,
            "entry_price": r.entry_price,
            "exit_time": r.exit_time,
            "exit_price": r.exit_price,
            "return_pct": r.return_pct,
            "holding_bars": r.holding_bars,
            "confidence": r.confidence,
            "max_favorable": r.max_favorable,
            "max_adverse": r.max_adverse,
        } for r in self.results])
        
        df.to_parquet(path)
        log.info("backtest_results_saved", path=str(path))


async def run_backtest(
    symbols: list[str] | None = None,
    timeframes: list[str] = None,
    days: int = 30,
    holding_bars: int = 5,
) -> BacktestSummary:
    config = get_config()
    universe = UniverseManager()
    universe.load()
    fetcher = HistoricalDataFetcher()
    
    engine = BacktestEngine(universe, fetcher, holding_bars=holding_bars, lookback_days=days)
    summary = await engine.run(symbols, timeframes)
    engine.save_results(f"reports/backtest_{datetime.now().strftime('%Y%m%d_%H%M%S')}.parquet")
    fetcher.close()
    
    return summary


if __name__ == "__main__":
    summary = asyncio.run(run_backtest(days=30, holding_bars=5))
    print(f"Total signals: {summary.total_signals}")
    print(f"Win rate: {summary.win_rate:.2%}")
    print(f"Avg return: {summary.avg_return:.4f}")
    print(f"Profit factor: {summary.profit_factor:.2f}")
    print(f"Sharpe: {summary.sharpe_ratio:.2f}")
    print(f"Max DD: {summary.max_drawdown:.2%}")
    print(f"Expectancy: {summary.expectancy:.4f}")
    for sig_type, stats in summary.by_signal_type.items():
        print(f"  {sig_type}: {stats}")