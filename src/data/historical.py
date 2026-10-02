from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import duckdb
import pandas as pd
import yfinance as yf

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone, parse_iso_datetime
from src.data.models import BarData

log = get_logger(__name__)

INTERVAL_MAP = {
    "1m": "1m",
    "5m": "5m",
    "15m": "15m",
    "1h": "60m",
    "1d": "1d",
}

YFINANCE_LIMITS = {
    "1m": 7,      # days
    "5m": 60,
    "15m": 60,
    "60m": 730,
    "1d": 7300,
}


class HistoricalDataFetcher:
    def __init__(self, db_path: str | None = None):
        self.config = get_config()
        self.db_path = db_path or self.config.storage.db_path
        self.conn: duckdb.DuckDBPyConnection | None = None

    def connect(self) -> None:
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(self.db_path)
        self._init_schema()

    def _init_schema(self) -> None:
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS bars_1m (
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
            CREATE INDEX IF NOT EXISTS idx_bars_1m_symbol_time 
            ON bars_1m(symbol, timestamp)
        """)
        
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS bars_5m (
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
            CREATE INDEX IF NOT EXISTS idx_bars_5m_symbol_time 
            ON bars_5m(symbol, timestamp)
        """)
        
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS bars_15m (
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
            CREATE INDEX IF NOT EXISTS idx_bars_15m_symbol_time 
            ON bars_15m(symbol, timestamp)
        """)
        
        self.conn.execute("""
            CREATE TABLE IF NOT EXISTS bars_1h (
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
            CREATE TABLE IF NOT EXISTS bars_1d (
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

    def close(self) -> None:
        if self.conn:
            self.conn.close()
            self.conn = None

    async def fetch_and_store(
        self,
        symbols: list[str],
        timeframe: str = "1m",
        days: int | None = None,
    ) -> int:
        if self.conn is None:
            self.connect()

        yf_interval = INTERVAL_MAP.get(timeframe, "1m")
        limit_days = days or YFINANCE_LIMITS.get(yf_interval, 7)
        end_date = datetime.now(get_timezone())
        start_date = end_date - timedelta(days=limit_days)

        total_stored = 0
        for symbol in symbols:
            try:
                stored = await self._fetch_symbol(symbol, yf_interval, start_date, end_date, timeframe)
                total_stored += stored
                await asyncio.sleep(0.1)  # Rate limiting
            except Exception as e:
                log.warning("historical_fetch_failed", symbol=symbol, error=str(e))

        log.info("historical_fetch_complete", timeframe=timeframe, bars_stored=total_stored)
        return total_stored

    async def _fetch_symbol(
        self,
        symbol: str,
        yf_interval: str,
        start_date: datetime,
        end_date: datetime,
        timeframe: str,
    ) -> int:
        ticker = yf.Ticker(symbol)
        loop = asyncio.get_event_loop()
        df = await loop.run_in_executor(
            None,
            lambda: ticker.history(
                interval=yf_interval,
                start=start_date.strftime("%Y-%m-%d"),
                end=end_date.strftime("%Y-%m-%d"),
                prepost=False,
                auto_adjust=True,
            ),
        )

        if df.empty:
            return 0

        df = df.reset_index()
        df.columns = [c.lower().replace(" ", "_") for c in df.columns]
        df = df.rename(columns={"datetime": "timestamp", "date": "timestamp"})
        df["timestamp"] = pd.to_datetime(df["timestamp"])
        if df["timestamp"].dt.tz is None:
            df["timestamp"] = df["timestamp"].dt.tz_localize("UTC").dt.tz_convert(get_timezone())
        else:
            df["timestamp"] = df["timestamp"].dt.tz_convert(get_timezone())

        df["symbol"] = symbol.replace(".NS", "")
        df["vwap"] = (df["high"] + df["low"] + df["close"]) / 3
        df["tick_count"] = 0

        records = df[["symbol", "timestamp", "open", "high", "low", "close", "volume", "vwap", "tick_count"]]

        table_name = f"bars_{timeframe}"
        
        self.conn.execute("BEGIN TRANSACTION")
        try:
            self.conn.register("temp_bars", records)
            self.conn.execute(f"""
                INSERT OR REPLACE INTO {table_name}
                SELECT * FROM temp_bars
            """)
            self.conn.execute("COMMIT")
            count = len(records)
        except Exception as e:
            self.conn.execute("ROLLBACK")
            raise e

        return count

    def get_bars(
        self,
        symbol: str,
        timeframe: str,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[BarData]:
        if self.conn is None:
            self.connect()

        table_name = f"bars_{timeframe}"
        
        query = f"""
            SELECT symbol, '{timeframe}' as timeframe, timestamp, open, high, low, close, volume, vwap, tick_count
            FROM {table_name}
            WHERE symbol = ?
        """
        params = [symbol]

        if start:
            query += " AND timestamp >= ?"
            params.append(start)
        if end:
            query += " AND timestamp <= ?"
            params.append(end)

        query += " ORDER BY timestamp ASC"
        if limit:
            query += f" LIMIT {limit}"

        rows = self.conn.execute(query, params).fetchall()
        return [
            BarData(
                symbol=row[0],
                timeframe=row[1],
                timestamp=row[2],
                open=row[3],
                high=row[4],
                low=row[5],
                close=row[6],
                volume=row[7],
                vwap=row[8],
                tick_count=row[9],
            )
            for row in rows
        ]

    def get_latest_bar(self, symbol: str, timeframe: str) -> BarData | None:
        if self.conn is None:
            self.connect()
        table_name = f"bars_{timeframe}"
        row = self.conn.execute(f"""
            SELECT symbol, '{timeframe}' as timeframe, timestamp, open, high, low, close, volume, vwap, tick_count
            FROM {table_name}
            WHERE symbol = ?
            ORDER BY timestamp DESC
            LIMIT 1
        """, [symbol]).fetchone()
        if row:
            return BarData(
                symbol=row[0], timeframe=row[1], timestamp=row[2],
                open=row[3], high=row[4], low=row[5], close=row[6],
                volume=row[7], vwap=row[8], tick_count=row[9],
            )
        return None