from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Iterator

import duckdb

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone
from src.data.models import (
    TickData, BarData, VolatilitySnapshot, TechnicalIndicators,
    Signal, NewsItem, Alert, EODSummary
)

log = get_logger(__name__)


class StateStore:
    def __init__(self, db_path: str | None = None):
        self.config = get_config().storage
        self.db_path = db_path or self.config.db_path
        self.conn: duckdb.DuckDBPyConnection | None = None
        self._init_db()

    def _init_db(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(self.db_path)
        self._create_tables()

    def _create_tables(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS ticks (
                symbol VARCHAR,
                token BIGINT,
                timestamp TIMESTAMP WITH TIME ZONE,
                ltp DOUBLE,
                volume BIGINT,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                close DOUBLE,
                oi BIGINT,
                PRIMARY KEY (symbol, timestamp)
            )
        """)

        for tf in ["1m", "5m", "15m", "1h", "1d"]:
            self.conn.execute(f"""
                CREATE TABLE IF NOT EXISTS bars_{tf} (
                    symbol VARCHAR,
                    timestamp TIMESTAMP WITH TIME ZONE,
                    open DOUBLE,
                    high DOUBLE,
                    low DOUBLE,
                    close DOUBLE,
                    volume BIGINT,
                    vwap DOUBLE,
                    tick_count BIGINT,
                    PRIMARY KEY (symbol, timestamp)
                )
            """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS volatility (
                symbol VARCHAR,
                timeframe VARCHAR,
                timestamp TIMESTAMP WITH TIME ZONE,
                window_bars INTEGER,
                realized_vol DOUBLE,
                annualized_vol DOUBLE,
                mean_return DOUBLE,
                PRIMARY KEY (symbol, timeframe, timestamp, window_bars)
            )
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id UUID DEFAULT gen_random_uuid(),
                symbol VARCHAR,
                timeframe VARCHAR,
                timestamp TIMESTAMP WITH TIME ZONE,
                signal_type VARCHAR,
                confidence DOUBLE,
                price DOUBLE,
                indicators_json VARCHAR,
                metadata_json VARCHAR,
                priority VARCHAR,
                PRIMARY KEY (id)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_signals_symbol_time ON signals(symbol, timestamp)")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS alerts (
                id UUID DEFAULT gen_random_uuid(),
                timestamp TIMESTAMP WITH TIME ZONE,
                symbol VARCHAR,
                alert_type VARCHAR,
                message VARCHAR,
                priority VARCHAR,
                acknowledged BOOLEAN DEFAULT FALSE,
                metadata_json VARCHAR,
                PRIMARY KEY (id)
            )
        """)

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS news (
                id UUID DEFAULT gen_random_uuid(),
                symbol VARCHAR,
                timestamp TIMESTAMP WITH TIME ZONE,
                source VARCHAR,
                headline VARCHAR,
                url VARCHAR,
                sentiment DOUBLE,
                metadata_json VARCHAR,
                PRIMARY KEY (id)
            )
        """)
        self.conn.execute("CREATE INDEX IF NOT EXISTS idx_news_symbol_time ON news(symbol, timestamp)")

        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS eod_summary (
                date DATE,
                symbol VARCHAR,
                close DOUBLE,
                open DOUBLE,
                high DOUBLE,
                low DOUBLE,
                volume BIGINT,
                avg_volume_20d DOUBLE,
                realized_vol_1m DOUBLE,
                realized_vol_5m DOUBLE,
                realized_vol_15m DOUBLE,
                signals_json VARCHAR,
                max_confidence DOUBLE,
                news_count INTEGER,
                top_news_json VARCHAR,
                gap_pct DOUBLE,
                PRIMARY KEY (date, symbol)
            )
        """)

    @contextmanager
    def transaction(self) -> Iterator[duckdb.DuckDBPyConnection]:
        self.conn.execute("BEGIN TRANSACTION")
        try:
            yield self.conn
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise

    def insert_tick(self, tick: TickData) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO ticks VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            tick.symbol, tick.token, tick.timestamp, tick.ltp, tick.volume,
            tick.open, tick.high, tick.low, tick.close, tick.oi
        ])

    def insert_bar(self, bar: BarData) -> None:
        table = f"bars_{bar.timeframe}"
        self.conn.execute(f"""
            INSERT OR REPLACE INTO {table} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            bar.symbol, bar.timestamp, bar.open, bar.high, bar.low,
            bar.close, bar.volume, bar.vwap, bar.tick_count
        ])

    def insert_volatility(self, vol: VolatilitySnapshot) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO volatility VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            vol.symbol, vol.timeframe, vol.timestamp, vol.window_bars,
            vol.realized_vol, vol.annualized_vol, vol.mean_return
        ])

    def insert_signal(self, signal: Signal) -> None:
        indicators_json = ""
        if signal.indicators:
            indicators_json = json.dumps({
                "zscore": signal.indicators.zscore,
                "bb_percent_b": signal.indicators.bb_percent_b,
                "rsi": signal.indicators.rsi,
                "macd": signal.indicators.macd,
                "macd_histogram": signal.indicators.macd_histogram,
                "vwap_deviation": signal.indicators.vwap_deviation,
            })

        self.conn.execute("""
            INSERT INTO signals (symbol, timeframe, timestamp, signal_type, confidence, price, indicators_json, metadata_json, priority)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            signal.symbol, signal.timeframe, signal.timestamp,
            signal.signal_type.value, signal.confidence, signal.price,
            indicators_json, json.dumps(signal.metadata), signal.priority.value
        ])

    def insert_alert(self, alert: Alert) -> None:
        self.conn.execute("""
            INSERT INTO alerts (timestamp, symbol, alert_type, message, priority, acknowledged, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            alert.timestamp, alert.symbol, alert.alert_type, alert.message,
            alert.priority.value, alert.acknowledged, json.dumps(alert.metadata)
        ])

    def insert_news(self, news: NewsItem) -> None:
        self.conn.execute("""
            INSERT INTO news (symbol, timestamp, source, headline, url, sentiment, metadata_json)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        """, [
            news.symbol, news.timestamp, news.source, news.headline,
            news.url, news.sentiment, json.dumps(news.metadata)
        ])

    def insert_eod_summary(self, summary: EODSummary) -> None:
        self.conn.execute("""
            INSERT OR REPLACE INTO eod_summary VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, [
            summary.date.date(), summary.symbol, summary.close, summary.open,
            summary.high, summary.low, summary.volume, summary.avg_volume_20d,
            summary.realized_vol_1m, summary.realized_vol_5m, summary.realized_vol_15m,
            json.dumps(summary.signals_count), summary.max_confidence,
            summary.news_count, json.dumps(summary.top_news), summary.gap_pct
        ])

    def get_recent_signals(self, symbol: str, hours: int = 24) -> list[Signal]:
        rows = self.conn.execute("""
            SELECT symbol, timeframe, timestamp, signal_type, confidence, price, indicators_json, metadata_json, priority
            FROM signals
            WHERE symbol = ? AND timestamp >= NOW() - INTERVAL ? HOURS
            ORDER BY timestamp DESC
        """, [symbol, hours]).fetchall()
        return [self._row_to_signal(r) for r in rows]

    def get_volatility_history(
        self, symbol: str, timeframe: str, hours: int = 24
    ) -> list[VolatilitySnapshot]:
        rows = self.conn.execute("""
            SELECT symbol, timeframe, timestamp, window_bars, realized_vol, annualized_vol, mean_return
            FROM volatility
            WHERE symbol = ? AND timeframe = ? AND timestamp >= NOW() - INTERVAL ? HOURS
            ORDER BY timestamp DESC
        """, [symbol, timeframe, hours]).fetchall()
        return [
            VolatilitySnapshot(
                symbol=r[0], timeframe=r[1], timestamp=r[2], window_bars=r[3],
                realized_vol=r[4], annualized_vol=r[5], mean_return=r[6]
            ) for r in rows
        ]

    def get_eod_summary(self, date: datetime, symbol: str | None = None) -> list[EODSummary]:
        if symbol:
            rows = self.conn.execute("""
                SELECT date, symbol, close, open, high, low, volume, avg_volume_20d,
                       realized_vol_1m, realized_vol_5m, realized_vol_15m,
                       signals_json, max_confidence, news_count, top_news_json, gap_pct
                FROM eod_summary WHERE date = ? AND symbol = ?
            """, [date.date(), symbol]).fetchall()
        else:
            rows = self.conn.execute("""
                SELECT date, symbol, close, open, high, low, volume, avg_volume_20d,
                       realized_vol_1m, realized_vol_5m, realized_vol_15m,
                       signals_json, max_confidence, news_count, top_news_json, gap_pct
                FROM eod_summary WHERE date = ?
            """, [date.date()]).fetchall()
        return [self._row_to_eod(r) for r in rows]

    def _row_to_signal(self, row) -> Signal:
        return Signal(
            symbol=row[0], timeframe=row[1], timestamp=row[2],
            signal_type=SignalType(row[3]), confidence=row[4], price=row[5],
            priority=SignalPriority(row[8]) if row[8] else SignalPriority.MEDIUM
        )

    def _row_to_eod(self, row) -> EODSummary:
        return EODSummary(
            date=row[0], symbol=row[1], close=row[2], open=row[3],
            high=row[4], low=row[5], volume=row[6], avg_volume_20d=row[7],
            realized_vol_1m=row[8], realized_vol_5m=row[9], realized_vol_15m=row[10],
            signals_count=json.loads(row[11]) if row[11] else {},
            max_confidence=row[12], news_count=row[13],
            top_news=json.loads(row[14]) if row[14] else [],
            gap_pct=row[15]
        )

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None