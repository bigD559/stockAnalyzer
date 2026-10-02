from __future__ import annotations

import math
from collections import deque
from datetime import datetime
from enum import Enum
from typing import Any

import numpy as np

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.utils.time import get_timezone
from src.data.models import BarData, TechnicalIndicators, Signal, SignalType, SignalPriority

log = get_logger(__name__)


class CandlePattern(str, Enum):
    DOJI = "DOJI"
    HAMMER = "HAMMER"
    INVERTED_HAMMER = "INVERTED_HAMMER"
    BULLISH_ENGULFING = "BULLISH_ENGULFING"
    BEARISH_ENGULFING = "BEARISH_ENGULFING"
    MORNING_STAR = "MORNING_STAR"
    EVENING_STAR = "EVENING_STAR"
    THREE_WHITE_SOLDIERS = "THREE_WHITE_SOLDIERS"
    THREE_BLACK_CROWS = "THREE_BLACK_CROWS"
    PIERCING_LINE = "PIERCING_LINE"
    DARK_CLOUD_COVER = "DARK_CLOUD_COVER"
    HANGING_MAN = "HANGING_MAN"
    SHOOTING_STAR = "SHOOTING_STAR"
    MARUBOZU_BULLISH = "MARUBOZU_BULLISH"
    MARUBOZU_BEARISH = "MARUBOZU_BEARISH"


class PatternEngine:
    def __init__(self):
        self.config = get_config().patterns
        self._price_history: dict[str, dict[str, deque]] = {}  # symbol -> tf -> deque of closes
        self._open_history: dict[str, dict[str, deque]] = {}  # symbol -> tf -> deque of opens
        self._volume_history: dict[str, dict[str, deque]] = {}
        self._high_history: dict[str, dict[str, deque]] = {}
        self._low_history: dict[str, dict[str, deque]] = {}
        self._vwap_history: dict[str, dict[str, deque]] = {}
        self._macd_state: dict[str, dict[str, dict]] = {}  # symbol -> tf -> {fast_ema, slow_ema, signal_ema}

    def add_bar(self, bar: BarData) -> list[Signal]:
        signals = []
        symbol = bar.symbol
        tf = bar.timeframe

        self._init_symbol_tf(symbol, tf)

        closes = self._price_history[symbol][tf]
        opens = self._open_history[symbol][tf]
        volumes = self._volume_history[symbol][tf]
        highs = self._high_history[symbol][tf]
        lows = self._low_history[symbol][tf]
        vwaps = self._vwap_history[symbol][tf]

        closes.append(bar.close)
        opens.append(bar.open)
        volumes.append(bar.volume)
        highs.append(bar.high)
        lows.append(bar.low)
        vwaps.append(bar.vwap)

        indicators = self._calculate_indicators(symbol, tf, bar)

        if indicators:
            # Detect candlestick patterns
            candle_patterns = self._detect_candlestick_patterns(symbol, tf, bar)
            if candle_patterns:
                signals = self._generate_candle_signals(symbol, tf, bar, indicators, candle_patterns)
            
            # Detect volume profile
            vol_profile = self._detect_volume_profile(symbol, tf)
            
            new_signals = self._generate_signals(symbol, tf, bar, indicators, vol_profile)
            signals.extend(new_signals)

        return signals

    def _init_symbol_tf(self, symbol: str, tf: str) -> None:
        window = max(self.config.zscore_window, self.config.bb_window, self.config.rsi_window, self.config.macd_slow)
        if symbol not in self._price_history:
            self._price_history[symbol] = {}
            self._open_history[symbol] = {}
            self._volume_history[symbol] = {}
            self._high_history[symbol] = {}
            self._low_history[symbol] = {}
            self._vwap_history[symbol] = {}
            self._macd_state[symbol] = {}

        for d in [self._price_history, self._open_history, self._volume_history, self._high_history, self._low_history, self._vwap_history]:
            if tf not in d[symbol]:
                d[symbol][tf] = deque(maxlen=window + 10)

        if tf not in self._macd_state[symbol]:
            self._macd_state[symbol][tf] = {
                "fast_ema": None,
                "slow_ema": None,
                "signal_ema": None,
                "prev_histogram": None,
            }

    def _calculate_indicators(self, symbol: str, tf: str, bar: BarData) -> TechnicalIndicators | None:
        closes = list(self._price_history[symbol][tf])
        highs = list(self._high_history[symbol][tf])
        lows = list(self._low_history[symbol][tf])
        volumes = list(self._volume_history[symbol][tf])
        vwaps = list(self._vwap_history[symbol][tf])

        if len(closes) < max(self.config.zscore_window, self.config.bb_window, self.config.rsi_window):
            return None

        # Z-score
        zscore_window = min(self.config.zscore_window, len(closes))
        recent_closes = closes[-zscore_window:]
        mean_price = np.mean(recent_closes)
        std_price = np.std(recent_closes, ddof=1)
        zscore = (bar.close - mean_price) / std_price if std_price > 0 else 0

        # Bollinger Bands
        bb_window = min(self.config.bb_window, len(closes))
        bb_closes = closes[-bb_window:]
        bb_mean = np.mean(bb_closes)
        bb_std = np.std(bb_closes, ddof=1)
        bb_upper = bb_mean + self.config.bb_std * bb_std
        bb_lower = bb_mean - self.config.bb_std * bb_std
        bb_percent_b = (bar.close - bb_lower) / (bb_upper - bb_lower) if bb_upper != bb_lower else 0.5
        bb_bandwidth = (bb_upper - bb_lower) / bb_mean if bb_mean > 0 else 0

        # RSI
        rsi = self._calculate_rsi(closes, self.config.rsi_window)

        # MACD
        macd_val, macd_signal, macd_hist = self._calculate_macd(symbol, tf, bar.close)

        # VWAP deviation
        vwap_deviation = (bar.close - bar.vwap) / bar.vwap if bar.vwap > 0 else 0

        return TechnicalIndicators(
            symbol=symbol,
            timeframe=tf,
            timestamp=bar.timestamp,
            zscore=zscore,
            bb_upper=bb_upper,
            bb_middle=bb_mean,
            bb_lower=bb_lower,
            bb_percent_b=bb_percent_b,
            bb_bandwidth=bb_bandwidth,
            rsi=rsi,
            macd=macd_val,
            macd_signal=macd_signal,
            macd_histogram=macd_hist,
            vwap=bar.vwap,
            vwap_deviation=vwap_deviation,
        )

    def _calculate_rsi(self, closes: list[float], window: int) -> float | None:
        if len(closes) < window + 1:
            return None
        deltas = np.diff(closes[-(window+1):])
        gains = np.where(deltas > 0, deltas, 0)
        losses = np.where(deltas < 0, -deltas, 0)
        avg_gain = np.mean(gains)
        avg_loss = np.mean(losses)
        if avg_loss == 0:
            return 100.0
        rs = avg_gain / avg_loss
        return 100 - (100 / (1 + rs))

    def _calculate_macd(self, symbol: str, tf: str, close: float) -> tuple[float | None, float | None, float | None]:
        state = self._macd_state[symbol][tf]
        fast_period = self.config.macd_fast
        slow_period = self.config.macd_slow
        signal_period = self.config.macd_signal

        fast_mult = 2 / (fast_period + 1)
        slow_mult = 2 / (slow_period + 1)
        signal_mult = 2 / (signal_period + 1)

        if state["fast_ema"] is None:
            state["fast_ema"] = close
            state["slow_ema"] = close
            state["signal_ema"] = 0
            return None, None, None

        state["fast_ema"] = close * fast_mult + state["fast_ema"] * (1 - fast_mult)
        state["slow_ema"] = close * slow_mult + state["slow_ema"] * (1 - slow_mult)
        macd = state["fast_ema"] - state["slow_ema"]
        state["signal_ema"] = macd * signal_mult + state["signal_ema"] * (1 - signal_mult)
        histogram = macd - state["signal_ema"]

        # Store histogram for crossover detection
        self._update_macd_histogram(symbol, tf, histogram)

        return macd, state["signal_ema"], histogram

    def _generate_signals(
        self, symbol: str, tf: str, bar: BarData, indicators: TechnicalIndicators,
        vol_profile: dict[str, Any] | None = None
    ) -> list[Signal]:
        signals = []
        threshold = self.config.signal_threshold

        # Mean Reversion Long
        if (indicators.zscore is not None and indicators.zscore < -2 and
            indicators.rsi is not None and indicators.rsi < 30 and
            indicators.bb_percent_b is not None and indicators.bb_percent_b < 0.1):
            confidence = self._calc_confidence([
                ("zscore", min(abs(indicators.zscore) / 3, 1.0)),
                ("rsi", (30 - indicators.rsi) / 30),
                ("bb", 1 - indicators.bb_percent_b),
            ])
            if confidence >= threshold:
                signals.append(Signal(
                    symbol=symbol,
                    timeframe=tf,
                    timestamp=bar.timestamp,
                    signal_type=SignalType.MEAN_REVERSION_LONG,
                    confidence=confidence,
                    price=bar.close,
                    indicators=indicators,
                    priority=SignalPriority.HIGH if confidence > 0.8 else SignalPriority.MEDIUM,
                ))

        # Mean Reversion Short
        if (indicators.zscore is not None and indicators.zscore > 2 and
            indicators.rsi is not None and indicators.rsi > 70 and
            indicators.bb_percent_b is not None and indicators.bb_percent_b > 0.9):
            confidence = self._calc_confidence([
                ("zscore", min(indicators.zscore / 3, 1.0)),
                ("rsi", (indicators.rsi - 70) / 30),
                ("bb", indicators.bb_percent_b),
            ])
            if confidence >= threshold:
                signals.append(Signal(
                    symbol=symbol,
                    timeframe=tf,
                    timestamp=bar.timestamp,
                    signal_type=SignalType.MEAN_REVERSION_SHORT,
                    confidence=confidence,
                    price=bar.close,
                    indicators=indicators,
                    priority=SignalPriority.HIGH if confidence > 0.8 else SignalPriority.MEDIUM,
                ))

        # MACD Momentum Up (crossover)
        if (indicators.macd is not None and indicators.macd_signal is not None and
            indicators.macd_histogram is not None):
            prev_hist = self._get_prev_macd_hist(symbol, tf)
            if prev_hist is not None and prev_hist <= 0 and indicators.macd_histogram > 0:
                # Volume confirmation
                vol_ratio = self._get_volume_ratio(symbol, tf)
                if vol_ratio and vol_ratio > 1.2:
                    confidence = min(0.7 + (vol_ratio - 1.2) * 0.1, 0.95)
                    if confidence >= threshold:
                        signals.append(Signal(
                            symbol=symbol,
                            timeframe=tf,
                            timestamp=bar.timestamp,
                            signal_type=SignalType.MOMENTUM_UP,
                            confidence=confidence,
                            price=bar.close,
                            indicators=indicators,
                            metadata={"volume_ratio": vol_ratio},
                            priority=SignalPriority.MEDIUM,
                        ))

            # MACD Momentum Down (crossunder)
            if prev_hist is not None and prev_hist >= 0 and indicators.macd_histogram < 0:
                vol_ratio = self._get_volume_ratio(symbol, tf)
                if vol_ratio and vol_ratio > 1.2:
                    confidence = min(0.7 + (vol_ratio - 1.2) * 0.1, 0.95)
                    if confidence >= threshold:
                        signals.append(Signal(
                            symbol=symbol,
                            timeframe=tf,
                            timestamp=bar.timestamp,
                            signal_type=SignalType.MOMENTUM_DOWN,
                            confidence=confidence,
                            price=bar.close,
                            indicators=indicators,
                            metadata={"volume_ratio": vol_ratio},
                            priority=SignalPriority.MEDIUM,
                        ))

        return signals

    def _calc_confidence(self, factors: list[tuple[str, float]]) -> float:
        if not factors:
            return 0.0
        weights = {"zscore": 0.4, "rsi": 0.3, "bb": 0.3, "volume": 0.2}
        total_weight = 0.0
        weighted_sum = 0.0
        for name, value in factors:
            w = weights.get(name, 0.2)
            weighted_sum += value * w
            total_weight += w
        return weighted_sum / total_weight if total_weight > 0 else 0.0

    def _get_prev_macd_hist(self, symbol: str, tf: str) -> float | None:
        state = self._macd_state.get(symbol, {}).get(tf, {})
        return state.get("prev_histogram")

    def _update_macd_histogram(self, symbol: str, tf: str, histogram: float) -> None:
        state = self._macd_state.get(symbol, {}).get(tf, {})
        state["prev_histogram"] = histogram

    def _get_volume_ratio(self, symbol: str, tf: str) -> float | None:
        volumes = self._volume_history.get(symbol, {}).get(tf)
        if not volumes or len(volumes) < 20:
            return None
        recent_avg = np.mean(list(volumes)[-5:])
        longer_avg = np.mean(list(volumes)[-20:])
        if longer_avg > 0:
            return recent_avg / longer_avg
        return None

    # --- Candlestick Pattern Detection ---
    def _detect_candlestick_patterns(
        self, symbol: str, tf: str, bar: BarData
    ) -> list[CandlePattern]:
        """Detect candlestick patterns from recent bars."""
        patterns = []
        closes = list(self._price_history[symbol][tf])
        opens = list(self._open_history[symbol][tf])
        highs = list(self._high_history[symbol][tf])
        lows = list(self._low_history[symbol][tf])
        
        # Need at least 3 bars for most patterns
        if len(closes) < 3:
            return patterns
        
        curr = len(closes) - 1
        prev = curr - 1
        prev2 = curr - 2
        
        # Current bar OHLC
        o, h, l, c = bar.open, bar.high, bar.low, bar.close
        body = abs(c - o)
        candle_range = h - l
        
        if candle_range == 0:
            return patterns
        
        body_ratio = body / candle_range
        upper_shadow = h - max(c, o)
        lower_shadow = min(c, o) - l
        
        # --- Single Candle Patterns ---
        
        # Doji: very small body
        if body_ratio < 0.1:
            patterns.append(CandlePattern.DOJI)
        
        # Marubozu: no shadows
        if upper_shadow < 0.01 * candle_range and lower_shadow < 0.01 * candle_range:
            if c > o:
                patterns.append(CandlePattern.MARUBOZU_BULLISH)
            else:
                patterns.append(CandlePattern.MARUBOZU_BEARISH)
        
        # Hammer / Hanging Man: small body, long lower shadow, little upper shadow
        if lower_shadow > 2 * body and upper_shadow < 0.1 * candle_range:
            if c > o:  # Bullish hammer
                patterns.append(CandlePattern.HAMMER)
            else:  # Bearish hanging man
                patterns.append(CandlePattern.HANGING_MAN)
        
        # Inverted Hammer / Shooting Star: small body, long upper shadow, little lower shadow
        if upper_shadow > 2 * body and lower_shadow < 0.1 * candle_range:
            if c > o:
                patterns.append(CandlePattern.INVERTED_HAMMER)
            else:
                patterns.append(CandlePattern.SHOOTING_STAR)
        
        # --- Two Candle Patterns ---
        if prev >= 0:
            po, ph, pl, pc = opens[prev], highs[prev], lows[prev], closes[prev]
            pbody = abs(pc - po)
            prange = ph - pl
            
            # Bullish Engulfing: current bullish candle engulfs previous bearish
            if pc < po and c > o and c > po and o < pc:
                if body > pbody * 1.1:
                    patterns.append(CandlePattern.BULLISH_ENGULFING)
            
            # Bearish Engulfing: current bearish engulfs previous bullish
            if pc > po and c < o and c < po and o > pc:
                if body > pbody * 1.1:
                    patterns.append(CandlePattern.BEARISH_ENGULFING)
            
            # Piercing Line: bearish then bullish, closes above midpoint of prev
            if pc < po and c > o and c > (po + pc) / 2 and c < po:
                patterns.append(CandlePattern.PIERCING_LINE)
            
            # Dark Cloud Cover: bullish then bearish, closes below midpoint
            if pc > po and c < o and c < (po + pc) / 2 and c > po:
                patterns.append(CandlePattern.DARK_CLOUD_COVER)
        
        # --- Three Candle Patterns ---
        if prev2 >= 0:
            p2o, p2h, p2l, p2c = opens[prev2], highs[prev2], lows[prev2], closes[prev2]
            
            # Morning Star: bearish, small body (doji/hammer), bullish
            if (closes[prev2] < opens[prev2] and  # First bearish
                abs(closes[prev] - opens[prev]) < 0.3 * (highs[prev] - lows[prev]) and  # Second small
                c > o and  # Third bullish
                c > (opens[prev2] + closes[prev2]) / 2):  # Closes above midpoint of first
                patterns.append(CandlePattern.MORNING_STAR)
            
            # Evening Star: bullish, small body, bearish
            if (closes[prev2] > opens[prev2] and  # First bullish
                abs(closes[prev] - opens[prev]) < 0.3 * (highs[prev] - lows[prev]) and  # Second small
                c < o and  # Third bearish
                c < (opens[prev2] + closes[prev2]) / 2):  # Closes below midpoint of first
                patterns.append(CandlePattern.EVENING_STAR)
            
            # Three White Soldiers: three consecutive bullish candles
            if (c > o and closes[prev] > opens[prev] and closes[prev2] > opens[prev2] and
                c > closes[prev] and closes[prev] > closes[prev2]):
                patterns.append(CandlePattern.THREE_WHITE_SOLDIERS)
            
            # Three Black Crows: three consecutive bearish candles
            if (c < o and closes[prev] < opens[prev] and closes[prev2] < opens[prev2] and
                c < closes[prev] and closes[prev] < closes[prev2]):
                patterns.append(CandlePattern.THREE_BLACK_CROWS)
        
        return patterns

    def _generate_candle_signals(
        self,
        symbol: str,
        tf: str,
        bar: BarData,
        indicators: TechnicalIndicators,
        patterns: list[CandlePattern],
    ) -> list[Signal]:
        """Generate signals from detected candlestick patterns."""
        signals = []
        threshold = self.config.signal_threshold
        
        for pattern in patterns:
            confidence = 0.0
            signal_type = None
            priority = SignalPriority.MEDIUM
            
            if pattern == CandlePattern.BULLISH_ENGULFING:
                confidence = 0.75
                signal_type = SignalType.MEAN_REVERSION_LONG
                priority = SignalPriority.HIGH
            elif pattern == CandlePattern.PIERCING_LINE:
                confidence = 0.70
                signal_type = SignalType.MEAN_REVERSION_LONG
            elif pattern == CandlePattern.MORNING_STAR:
                confidence = 0.80
                signal_type = SignalType.MEAN_REVERSION_LONG
                priority = SignalPriority.HIGH
            elif pattern == CandlePattern.HAMMER:
                confidence = 0.65
                signal_type = SignalType.MEAN_REVERSION_LONG
            elif pattern == CandlePattern.BULLISH_ENGULFING:
                confidence = 0.75
                signal_type = SignalType.MEAN_REVERSION_LONG
            
            elif pattern == CandlePattern.BEARISH_ENGULFING:
                confidence = 0.75
                signal_type = SignalType.MEAN_REVERSION_SHORT
                priority = SignalPriority.HIGH
            elif pattern == CandlePattern.DARK_CLOUD_COVER:
                confidence = 0.70
                signal_type = SignalType.MEAN_REVERSION_SHORT
            elif pattern == CandlePattern.EVENING_STAR:
                confidence = 0.80
                signal_type = SignalType.MEAN_REVERSION_SHORT
                priority = SignalPriority.HIGH
            elif pattern == CandlePattern.SHOOTING_STAR:
                confidence = 0.65
                signal_type = SignalType.MEAN_REVERSION_SHORT
            elif pattern == CandlePattern.HANGING_MAN:
                confidence = 0.60
                signal_type = SignalType.MEAN_REVERSION_SHORT
            
            elif pattern == CandlePattern.THREE_WHITE_SOLDIERS:
                confidence = 0.75
                signal_type = SignalType.MOMENTUM_UP
                priority = SignalPriority.HIGH
            elif pattern == CandlePattern.THREE_BLACK_CROWS:
                confidence = 0.75
                signal_type = SignalType.MOMENTUM_DOWN
                priority = SignalPriority.HIGH
            
            elif pattern == CandlePattern.DOJI:
                # Doji alone is indecision - low confidence
                confidence = 0.45
                signal_type = SignalType.MEAN_REVERSION_LONG if bar.close > bar.open else SignalType.MEAN_REVERSION_SHORT
            
            if signal_type and confidence >= threshold:
                signals.append(Signal(
                    symbol=symbol,
                    timeframe=tf,
                    timestamp=bar.timestamp,
                    signal_type=signal_type,
                    confidence=confidence,
                    price=bar.close,
                    indicators=indicators,
                    metadata={"candle_pattern": pattern.value},
                    priority=priority,
                ))
        
        return signals

    def _detect_volume_profile(self, symbol: str, tf: str) -> dict[str, Any]:
        """Analyze volume profile for the current session."""
        volumes = list(self._volume_history.get(symbol, {}).get(tf, []))
        closes = list(self._price_history.get(symbol, {}).get(tf, []))
        
        if len(volumes) < 20:
            return {}
        
        recent_volumes = volumes[-20:]
        recent_closes = closes[-20:]
        
        avg_volume = np.mean(recent_volumes)
        current_volume = recent_volumes[-1]
        vol_ratio = current_volume / avg_volume if avg_volume > 0 else 1
        
        # Price-volume trend
        pv_trend = 0
        for i in range(1, len(recent_closes)):
            if recent_closes[i] > recent_closes[i-1]:
                pv_trend += recent_volumes[i]
            else:
                pv_trend -= recent_volumes[i]
        
        # On-Balance Volume
        obv = 0
        for i in range(1, len(recent_closes)):
            if recent_closes[i] > recent_closes[i-1]:
                obv += recent_volumes[i]
            elif recent_closes[i] < recent_closes[i-1]:
                obv -= recent_volumes[i]
        
        return {
            "volume_ratio": vol_ratio,
            "avg_volume": avg_volume,
            "pv_trend": pv_trend,
            "obv": obv,
            "high_volume": vol_ratio > 1.5,
            "low_volume": vol_ratio < 0.5,
        }

    def get_indicators(self, symbol: str, tf: str) -> TechnicalIndicators | None:
        if symbol not in self._price_history or tf not in self._price_history[symbol]:
            return None
        return None