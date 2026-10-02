from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from typing import Any

from src.utils.time import get_timezone


class Exchange(str, Enum):
    NSE = "NSE"
    BSE = "BSE"


class InstrumentType(str, Enum):
    EQUITY = "EQ"
    FUTURE = "FUT"
    OPTION = "OPT"
    INDEX = "IDX"


class SignalType(str, Enum):
    MEAN_REVERSION_LONG = "MEAN_REVERSION_LONG"
    MEAN_REVERSION_SHORT = "MEAN_REVERSION_SHORT"
    MOMENTUM_UP = "MOMENTUM_UP"
    MOMENTUM_DOWN = "MOMENTUM_DOWN"
    VOL_EXPANSION = "VOL_EXPANSION"
    VOL_CONTRACTION = "VOL_CONTRACTION"


class SignalPriority(str, Enum):
    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


@dataclass(slots=True)
class SymbolInfo:
    symbol: str
    token: int
    exchange: Exchange = Exchange.NSE
    instrument_type: InstrumentType = InstrumentType.EQUITY
    yfinance_ticker: str = ""
    lot_size: int = 1
    tick_size: float = 0.05
    is_fno: bool = False

    def __post_init__(self):
        if not self.yfinance_ticker and self.exchange == Exchange.NSE:
            self.yfinance_ticker = f"{self.symbol}.NS"


@dataclass(slots=True)
class TickData:
    symbol: str
    token: int
    timestamp: datetime
    ltp: float
    volume: int = 0
    open: float = 0.0
    high: float = 0.0
    low: float = 0.0
    close: float = 0.0
    oi: int = 0  # Open interest for F&O

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            self.timestamp = get_timezone().localize(self.timestamp)


@dataclass(slots=True)
class BarData:
    symbol: str
    timeframe: str  # "1m", "5m", "15m", "1h", "1d"
    timestamp: datetime  # Bar start time
    open: float
    high: float
    low: float
    close: float
    volume: int
    vwap: float = 0.0
    tick_count: int = 0

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            self.timestamp = get_timezone().localize(self.timestamp)

    @property
    def returns(self) -> float:
        if self.open > 0:
            return (self.close - self.open) / self.open
        return 0.0

    @property
    def log_returns(self) -> float:
        if self.open > 0 and self.close > 0:
            import math
            return math.log(self.close / self.open)
        return 0.0


@dataclass(slots=True)
class VolatilitySnapshot:
    symbol: str
    timeframe: str
    timestamp: datetime
    window_bars: int
    realized_vol: float  # Standard deviation of log returns
    annualized_vol: float
    mean_return: float = 0.0

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            self.timestamp = get_timezone().localize(self.timestamp)


@dataclass(slots=True)
class TechnicalIndicators:
    symbol: str
    timeframe: str
    timestamp: datetime
    zscore: float | None = None
    bb_upper: float | None = None
    bb_middle: float | None = None
    bb_lower: float | None = None
    bb_percent_b: float | None = None
    bb_bandwidth: float | None = None
    rsi: float | None = None
    macd: float | None = None
    macd_signal: float | None = None
    macd_histogram: float | None = None
    vwap: float | None = None
    vwap_deviation: float | None = None

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            self.timestamp = get_timezone().localize(self.timestamp)


@dataclass(slots=True)
class Signal:
    symbol: str
    timeframe: str
    timestamp: datetime
    signal_type: SignalType
    confidence: float  # 0.0 to 1.0
    price: float
    indicators: TechnicalIndicators | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    priority: SignalPriority = SignalPriority.MEDIUM

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            self.timestamp = get_timezone().localize(self.timestamp)


@dataclass(slots=True)
class NewsItem:
    symbol: str
    timestamp: datetime
    source: str
    headline: str
    url: str
    sentiment: float = 0.0  # -1 to 1
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            self.timestamp = get_timezone().localize(self.timestamp)


@dataclass(slots=True)
class Alert:
    id: str
    timestamp: datetime
    symbol: str
    alert_type: str
    message: str
    priority: SignalPriority = SignalPriority.MEDIUM
    acknowledged: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if self.timestamp.tzinfo is None:
            self.timestamp = get_timezone().localize(self.timestamp)


@dataclass(slots=True)
class EODSummary:
    date: datetime
    symbol: str
    close: float
    open: float
    high: float
    low: float
    volume: int
    avg_volume_20d: float | None = None
    realized_vol_1m: float | None = None
    realized_vol_5m: float | None = None
    realized_vol_15m: float | None = None
    signals_count: dict[str, int] = field(default_factory=dict)
    max_confidence: float = 0.0
    news_count: int = 0
    top_news: list[str] = field(default_factory=list)
    gap_pct: float | None = None

    def __post_init__(self):
        if self.date.tzinfo is None:
            self.date = get_timezone().localize(self.date)