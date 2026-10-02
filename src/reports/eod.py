from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone, market_close_time, today, format_ist
from src.data.models import EODSummary, BarData, VolatilitySnapshot, Signal, SignalType
from src.engine.state import StateStore
from src.data.universe import UniverseManager

log = get_logger(__name__)


class EODAnalyzer:
    def __init__(self, state_store: StateStore, universe: UniverseManager):
        self.config = get_config()
        self.state_store = state_store
        self.universe = universe
        self.report_dir = Path("reports")
        self.report_dir.mkdir(parents=True, exist_ok=True)

    def generate(self, date: datetime | None = None) -> dict[str, Any]:
        if date is None:
            date = today()

        symbols = self.universe.get_all_symbols()
        summaries = []

        for symbol in symbols:
            summary = self._analyze_symbol(symbol, date)
            if summary:
                summaries.append(summary)
                self.state_store.insert_eod_summary(summary)

        # Portfolio-level stats
        portfolio = self._compute_portfolio_stats(summaries, date)

        # Save reports
        self._save_json_report(portfolio, date)
        self._save_markdown_report(portfolio, summaries, date)

        log.info("eod_report_generated", date=date.date().isoformat(), symbols=len(summaries))
        return portfolio

    def _analyze_symbol(self, symbol: str, date: datetime) -> EODSummary | None:
        # Get day's bars
        bars_1m = self.state_store.conn.execute("""
            SELECT timestamp, open, high, low, close, volume, vwap
            FROM bars_1m WHERE symbol = ? AND DATE(timestamp) = ?
            ORDER BY timestamp
        """, [symbol, date.date()]).fetchall()

        if not bars_1m:
            return None

        # Basic OHLC
        open_price = bars_1m[0][1]
        high_price = max(r[2] for r in bars_1m)
        low_price = min(r[3] for r in bars_1m)
        close_price = bars_1m[-1][4]
        total_volume = sum(r[5] for r in bars_1m)

        # Avg volume 20d (approximate from stored data)
        avg_vol_row = self.state_store.conn.execute("""
            SELECT AVG(volume) FROM (
                SELECT DATE(timestamp) as dt, SUM(volume) as volume
                FROM bars_1m WHERE symbol = ? AND timestamp < ?
                GROUP BY DATE(timestamp)
                ORDER BY dt DESC LIMIT 20
            )
        """, [symbol, date]).fetchone()
        avg_volume_20d = avg_vol_row[0] if avg_vol_row and avg_vol_row[0] else None

        # Realized volatility at close (last bar of each timeframe)
        vol_1m = self._get_close_vol(symbol, "1m", 30, date)
        vol_5m = self._get_close_vol(symbol, "5m", 30, date)
        vol_15m = self._get_close_vol(symbol, "15m", 16, date)

        # Signals count
        signals = self.state_store.get_recent_signals(symbol, hours=8)  # Market hours
        signals_count = {}
        max_confidence = 0.0
        for s in signals:
            signals_count[s.signal_type.value] = signals_count.get(s.signal_type.value, 0) + 1
            max_confidence = max(max_confidence, s.confidence)

        # News count
        news_count = 0
        top_news = []
        if hasattr(self.state_store.conn, 'execute'):
            news_rows = self.state_store.conn.execute("""
                SELECT headline FROM news
                WHERE symbol = ? AND DATE(timestamp) = ?
                ORDER BY timestamp DESC LIMIT 5
            """, [symbol, date.date()]).fetchall()
            news_count = len(news_rows)
            top_news = [r[0] for r in news_rows]

        # Gap from previous close
        prev_close_row = self.state_store.conn.execute("""
            SELECT close FROM bars_1d WHERE symbol = ? AND timestamp < ?
            ORDER BY timestamp DESC LIMIT 1
        """, [symbol, date]).fetchone()
        gap_pct = None
        if prev_close_row and prev_close_row[0]:
            gap_pct = (close_price - prev_close_row[0]) / prev_close_row[0]

        return EODSummary(
            date=date,
            symbol=symbol,
            close=close_price,
            open=open_price,
            high=high_price,
            low=low_price,
            volume=total_volume,
            avg_volume_20d=avg_volume_20d,
            realized_vol_1m=vol_1m,
            realized_vol_5m=vol_5m,
            realized_vol_15m=vol_15m,
            signals_count=signals_count,
            max_confidence=max_confidence,
            news_count=news_count,
            top_news=top_news,
            gap_pct=gap_pct,
        )

    def _get_close_vol(self, symbol: str, timeframe: str, window: int, date: datetime) -> float | None:
        table = f"bars_{timeframe}"
        rows = self.state_store.conn.execute(f"""
            SELECT close FROM {table}
            WHERE symbol = ? AND DATE(timestamp) = ?
            ORDER BY timestamp
        """, [symbol, date.date()]).fetchall()

        if len(rows) < window:
            return None

        closes = [r[0] for r in rows[-window:]]
        import numpy as np
        log_returns = np.diff(np.log(closes))
        if len(log_returns) == 0:
            return None
        realized_vol = float(np.std(log_returns, ddof=1))

        # Annualize
        tf_minutes = {"1m": 1, "5m": 5, "15m": 15}.get(timeframe, 1)
        annualized = realized_vol * (945000 / tf_minutes) ** 0.5
        return annualized

    def _compute_portfolio_stats(self, summaries: list[EODSummary], date: datetime) -> dict[str, Any]:
        advancers = sum(1 for s in summaries if s.gap_pct is not None and s.gap_pct > 0)
        decliners = sum(1 for s in summaries if s.gap_pct is not None and s.gap_pct < 0)
        unchanged = sum(1 for s in summaries if s.gap_pct is not None and s.gap_pct == 0)

        # Top volume
        vol_leaders = sorted(summaries, key=lambda x: x.volume, reverse=True)[:10]
        vol_leader_names = [f"{s.symbol}({s.volume/1e5:.1f}L)" for s in vol_leaders]

        # Top volatility
        vol_stocks = [s for s in summaries if s.realized_vol_15m is not None]
        vol_leaders_15m = sorted(vol_stocks, key=lambda x: x.realized_vol_15m or 0, reverse=True)[:10]
        vol_leader_vols = [f"{s.symbol}({s.realized_vol_15m:.1%})" for s in vol_leaders_15m]

        # Signal summary
        signal_totals = {}
        for s in summaries:
            for sig_type, count in s.signals_count.items():
                signal_totals[sig_type] = signal_totals.get(sig_type, 0) + count

        top_signals = sorted(signal_totals.items(), key=lambda x: x[1], reverse=True)[:5]

        return {
            "date": date.date().isoformat(),
            "timestamp": format_ist(datetime.now()),
            "total_symbols": len(summaries),
            "advancers": advancers,
            "decliners": decliners,
            "unchanged": unchanged,
            "advance_decline_ratio": advancers / decliners if decliners > 0 else float('inf'),
            "vol_leaders": vol_leader_names,
            "volatility_leaders_15m": vol_leader_vols,
            "signal_summary": signal_totals,
            "top_signals": [f"{k}({v})" for k, v in top_signals],
            "total_news": sum(s.news_count for s in summaries),
            "summaries": [self._summary_to_dict(s) for s in summaries],
        }

    def _summary_to_dict(self, s: EODSummary) -> dict:
        return {
            "symbol": s.symbol,
            "close": s.close,
            "open": s.open,
            "high": s.high,
            "low": s.low,
            "change_pct": s.gap_pct * 100 if s.gap_pct else None,
            "volume": s.volume,
            "avg_vol_20d": s.avg_volume_20d,
            "vol_ratio": s.volume / s.avg_volume_20d if s.avg_volume_20d else None,
            "realized_vol_1m": s.realized_vol_1m,
            "realized_vol_5m": s.realized_vol_5m,
            "realized_vol_15m": s.realized_vol_15m,
            "signals": s.signals_count,
            "max_confidence": s.max_confidence,
            "news_count": s.news_count,
            "top_news": s.top_news[:3],
        }

    def _save_json_report(self, portfolio: dict, date: datetime) -> None:
        filepath = self.report_dir / f"eod_{date.strftime('%Y%m%d')}.json"
        with open(filepath, "w") as f:
            json.dump(portfolio, f, indent=2, default=str)
        log.info("eod_json_saved", path=str(filepath))

    def _save_markdown_report(self, portfolio: dict, summaries: list[EODSummary], date: datetime) -> None:
        filepath = self.report_dir / f"eod_{date.strftime('%Y%m%d')}.md"

        lines = [
            f"# EOD Report - {date.strftime('%Y-%m-%d')}",
            f"Generated: {portfolio['timestamp']}",
            "",
            f"## Market Breadth",
            f"- **Advancers**: {portfolio['advancers']}",
            f"- **Decliners**: {portfolio['decliners']}",
            f"- **Unchanged**: {portfolio['unchanged']}",
            f"- **A/D Ratio**: {portfolio['advance_decline_ratio']:.2f}",
            f"- **Total Symbols Tracked**: {portfolio['total_symbols']}",
            f"- **Total News Items**: {portfolio['total_news']}",
            "",
            f"## Volume Leaders",
            "| Symbol | Volume (L) | vs 20D Avg |",
            "|--------|------------|------------|",
        ]

        for s in summaries[:10]:
            vol_l = s.volume / 1e5
            ratio = s.volume / s.avg_volume_20d if s.avg_volume_20d else 0
            lines.append(f"| {s.symbol} | {vol_l:.1f} | {ratio:.2f}x |")

        lines.extend([
            "",
            f"## Volatility Leaders (15m Realized Vol)",
            "| Symbol | Annualized Vol |",
            "|--------|----------------|",
        ])

        vol_stocks = [s for s in summaries if s.realized_vol_15m is not None]
        for s in sorted(vol_stocks, key=lambda x: x.realized_vol_15m or 0, reverse=True)[:10]:
            lines.append(f"| {s.symbol} | {s.realized_vol_15m:.2%} |")

        lines.extend([
            "",
            f"## Signal Summary",
            "| Signal Type | Count |",
            "|-------------|-------|",
        ])

        for sig_type, count in sorted(portfolio['signal_summary'].items(), key=lambda x: -x[1]):
            lines.append(f"| {sig_type} | {count} |")

        lines.extend([
            "",
            f"## Individual Summaries",
        ])

        for s in summaries:
            lines.append(f"\n### {s.symbol}")
            lines.append(f"- Close: {s.close:.2f} | Change: {s.gap_pct*100:.2f}%" if s.gap_pct else f"- Close: {s.close:.2f}")
            lines.append(f"- Range: {s.low:.2f} - {s.high:.2f}")
            lines.append(f"- Volume: {s.volume/1e5:.1f}L (20D avg: {s.avg_volume_20d/1e5:.1f}L)" if s.avg_volume_20d else f"- Volume: {s.volume/1e5:.1f}L")
            if s.realized_vol_15m:
                lines.append(f"- Realized Vol (15m): {s.realized_vol_15m:.2%}")
            if s.signals_count:
                lines.append(f"- Signals: {', '.join(f'{k}({v})' for k,v in s.signals_count.items())}")
            if s.top_news:
                lines.append(f"- News: {'; '.join(s.top_news[:3])}")

        with open(filepath, "w") as f:
            f.write("\n".join(lines))

        log.info("eod_markdown_saved", path=str(filepath))