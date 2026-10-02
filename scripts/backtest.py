#!/usr/bin/env python3
"""Simple backtesting framework for signal validation."""

import asyncio
import sys
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.config import load_config
from src.utils.logging import setup_logging, get_logger
from src.data.universe import UniverseManager
from src.data.historical import HistoricalDataFetcher
from src.engine.volatility import VolatilityCalculator
from src.engine.patterns import PatternEngine
from src.data.models import BarData

log = get_logger(__name__)


async def main():
    load_config()
    setup_logging()

    universe = UniverseManager()
    universe.load()

    fetcher = HistoricalDataFetcher()
    vol_calc = VolatilityCalculator()
    pattern_engine = PatternEngine()

    symbols = universe.get_all_symbols()[:20]  # Test with 20 symbols
    end_date = datetime.now()
    start_date = end_date - timedelta(days=30)

    results = []

    for symbol in symbols:
        try:
            # Fetch 5m bars for backtest
            bars = fetcher.get_bars(symbol, "5m", start=start_date, end=end_date)
            if len(bars) < 100:
                continue

            signals_generated = []
            forward_returns = []

            for i, bar in enumerate(bars):
                # Add to engines
                vol_snapshots = vol_calc.add_bar(bar)
                signals = pattern_engine.add_bar(bar)

                if signals:
                    for sig in signals:
                        signals_generated.append({
                            "symbol": symbol,
                            "timestamp": bar.timestamp,
                            "signal": sig.signal_type.value,
                            "confidence": sig.confidence,
                            "price": sig.price,
                        })

                        # Calculate forward return (next 5 bars = 25 min)
                        if i + 5 < len(bars):
                            entry = sig.price
                            exit_price = bars[i + 5].close
                            ret = (exit_price - entry) / entry
                            if sig.signal_type.value.endswith("_SHORT") or sig.signal_type.value.endswith("_DOWN"):
                                ret = -ret
                            forward_returns.append({
                                "symbol": symbol,
                                "signal": sig.signal_type.value,
                                "confidence": sig.confidence,
                                "return": ret,
                            })

            if signals_generated:
                df = pd.DataFrame(forward_returns)
                if not df.empty:
                    for sig_type in df["signal"].unique():
                        subset = df[df["signal"] == sig_type]
                        mean_ret = subset["return"].mean()
                        win_rate = (subset["return"] > 0).mean()
                        sharpe = subset["return"].mean() / subset["return"].std() if subset["return"].std() > 0 else 0

                        results.append({
                            "symbol": symbol,
                            "signal": sig_type,
                            "count": len(subset),
                            "mean_return": mean_ret,
                            "win_rate": win_rate,
                            "sharpe": sharpe,
                        })
                        log.info("backtest_result",
                            symbol=symbol, signal=sig_type,
                            count=len(subset), mean_ret=f"{mean_ret:.4f}",
                            win_rate=f"{win_rate:.2%}", sharpe=f"{sharpe:.2f}")

        except Exception as e:
            log.warning("backtest_error", symbol=symbol, error=str(e))

    # Summary
    if results:
        df = pd.DataFrame(results)
        print("\n=== BACKTEST SUMMARY ===")
        for sig_type in df["signal"].unique():
            subset = df[df["signal"] == sig_type]
            print(f"\n{sig_type}:")
            print(f"  Total signals: {subset['count'].sum()}")
            print(f"  Mean return: {subset['mean_return'].mean():.4f}")
            print(f"  Win rate: {(subset['win_rate'] * subset['count']).sum() / subset['count'].sum():.2%}")
            print(f"  Avg Sharpe: {subset['sharpe'].mean():.2f}")

    fetcher.close()


if __name__ == "__main__":
    asyncio.run(main())