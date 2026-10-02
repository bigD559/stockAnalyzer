from __future__ import annotations

import asyncio
import json
import pickle
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd
from sklearn.ensemble import RandomForestClassifier, GradientBoostingClassifier
from sklearn.preprocessing import StandardScaler
from sklearn.model_selection import train_test_split
from sklearn.metrics import classification_report, roc_auc_score

from src.utils.config import get_config
from src.utils.logging import get_logger
from src.data.universe import UniverseManager
from src.data.historical import HistoricalDataFetcher
from src.data.models import BarData, Signal, SignalType
from src.engine.patterns import PatternEngine
from src.engine.volatility import VolatilityCalculator
from src.engine.state import StateStore

log = get_logger(__name__)


@dataclass
class MLFeatures:
    """Feature set for ML model."""
    # Price features
    returns_1: float = 0.0
    returns_5: float = 0.0
    returns_15: float = 0.0
    
    # Volatility features
    realized_vol_1m: float = 0.0
    realized_vol_5m: float = 0.0
    realized_vol_15m: float = 0.0
    vol_ratio_fast_slow: float = 0.0
    
    # Technical indicators
    zscore: float = 0.0
    bb_percent_b: float = 0.0
    bb_bandwidth: float = 0.0
    rsi: float = 0.0
    macd: float = 0.0
    macd_histogram: float = 0.0
    vwap_deviation: float = 0.0
    
    # Volume features
    volume_ratio: float = 1.0
    obv: float = 0.0
    pv_trend: float = 0.0
    
    # Time features
    minute_of_day: int = 0
    is_morning: int = 0
    is_closing: int = 0
    
    # Signal features
    signal_confidence: float = 0.0
    signal_type_encoded: int = 0
    
    # Target
    forward_return: float = 0.0
    target: int = 0  # 1 = profitable, 0 = loss


class SignalMLModel:
    """ML model for signal filtering and confidence calibration."""
    
    def __init__(self, model_path: str = "models/signal_filter.pkl"):
        self.model_path = Path(model_path)
        self.model_path.parent.mkdir(parents=True, exist_ok=True)
        
        self.model = GradientBoostingClassifier(
            n_estimators=200,
            max_depth=5,
            learning_rate=0.05,
            subsample=0.8,
            random_state=42
        )
        self.scaler = StandardScaler()
        self.feature_names: list[str] = []
        self.is_trained = False
        self.training_history: list[dict] = []

    def extract_features(
        self,
        bar: BarData,
        signal: Signal,
        pattern_engine: PatternEngine,
        vol_calc: VolatilityCalculator,
    ) -> MLFeatures:
        """Extract ML features from bar and signal."""
        features = MLFeatures()
        symbol = bar.symbol
        tf = bar.timeframe
        
        # Time features
        features.minute_of_day = bar.timestamp.hour * 60 + bar.timestamp.minute
        features.is_morning = 1 if bar.timestamp.hour < 11 else 0
        features.is_closing = 1 if bar.timestamp.hour >= 14 else 0
        
        # Signal features
        features.signal_confidence = signal.confidence
        # Encode signal type
        signal_type_map = {
            "MEAN_REVERSION_LONG": 1,
            "MEAN_REVERSION_SHORT": 2,
            "MOMENTUM_UP": 3,
            "MOMENTUM_DOWN": 4,
            "VOL_EXPANSION": 5,
            "VOL_CONTRACTION": 6,
        }
        features.signal_type_encoded = signal_type_map.get(signal.signal_type.value, 0)
        
        # Get volatility
        vol_1m = vol_calc.get_current_volatility(symbol, "1m", 30)
        vol_5m = vol_calc.get_current_volatility(symbol, "5m", 30)
        vol_15m = vol_calc.get_current_volatility(symbol, "15m", 16)
        
        if vol_1m:
            features.realized_vol_1m = vol_1m.realized_vol
        if vol_5m:
            features.realized_vol_5m = vol_5m.realized_vol
        if vol_15m:
            features.realized_vol_15m = vol_15m.realized_vol
        
        # Vol ratio
        if vol_5m and vol_15m and vol_15m.realized_vol > 0:
            features.vol_ratio_fast_slow = vol_5m.realized_vol / vol_15m.realized_vol
        
        # Technical indicators from signal
        if signal.indicators:
            features.zscore = signal.indicators.zscore or 0
            features.bb_percent_b = signal.indicators.bb_percent_b or 0
            features.bb_bandwidth = signal.indicators.bb_bandwidth or 0
            features.rsi = signal.indicators.rsi or 0
            features.macd = signal.indicators.macd or 0
            features.macd_histogram = signal.indicators.macd_histogram or 0
            features.vwap_deviation = signal.indicators.vwap_deviation or 0
        
        # Volume profile
        vol_profile = pattern_engine._detect_volume_profile(symbol, tf)
        features.volume_ratio = vol_profile.get("volume_ratio", 1.0)
        features.obv = vol_profile.get("obv", 0)
        features.pv_trend = vol_profile.get("pv_trend", 0)
        
        # Returns (approximate from bar)
        features.returns_1 = bar.log_returns
        
        return features

    def prepare_training_data(self, signals: list[Signal], bars: list[BarData]) -> pd.DataFrame:
        """Prepare training dataframe from historical signals and outcomes."""
        # This would need historical signal data with forward returns
        # For now, return empty dataframe - implement with actual historical data
        return pd.DataFrame()

    def train(self, X: pd.DataFrame, y: pd.Series) -> dict:
        """Train the ML model."""
        if len(X) < 100:
            log.warning("insufficient_training_data", samples=len(X))
            return {"status": "insufficient_data"}
        
        # Split
        X_train, X_test, y_train, y_test = train_test_split(
            X, y, test_size=0.2, random_state=42, stratify=y
        )
        
        # Scale
        X_train_scaled = self.scaler.fit_transform(X_train)
        X_test_scaled = self.scaler.transform(X_test)
        
        # Train
        self.model.fit(X_train_scaled, y_train)
        
        # Evaluate
        y_pred = self.model.predict(X_test_scaled)
        y_prob = self.model.predict_proba(X_test_scaled)[:, 1]
        
        report = classification_report(y_test, y_pred, output_dict=True)
        auc = roc_auc_score(y_test, y_prob)
        
        self.feature_names = list(X.columns)
        self.is_trained = True
        
        # Save
        self.save()
        
        result = {
            "status": "trained",
            "train_samples": len(X_train),
            "test_samples": len(X_test),
            "auc": auc,
            "precision": report.get("1", {}).get("precision", 0),
            "recall": report.get("1", {}).get("recall", 0),
            "f1": report.get("1", {}).get("f1-score", 0),
        }
        
        self.training_history.append({
            "timestamp": datetime.now().isoformat(),
            **result
        })
        
        log.info("ml_model_trained", **result)
        return result

    def predict_proba(self, features: MLFeatures) -> float:
        """Predict probability of profitable signal."""
        if not self.is_trained:
            return features.signal_confidence  # Fallback to original confidence
        
        # Convert features to array
        feature_dict = features.__dict__
        X = np.array([feature_dict.get(name, 0) for name in self.feature_names]).reshape(1, -1)
        X_scaled = self.scaler.transform(X)
        
        prob = self.model.predict_proba(X_scaled)[0, 1]
        return float(prob)

    def predict(self, features: MLFeatures) -> int:
        """Predict if signal will be profitable (1) or not (0)."""
        prob = self.predict_proba(features)
        return 1 if prob > 0.5 else 0

    def save(self) -> None:
        """Save model to disk."""
        data = {
            "model": self.model,
            "scaler": self.scaler,
            "feature_names": self.feature_names,
            "is_trained": self.is_trained,
            "training_history": self.training_history,
        }
        with open(self.model_path, "wb") as f:
            pickle.dump(data, f)
        log.info("ml_model_saved", path=str(self.model_path))

    def load(self) -> bool:
        """Load model from disk."""
        if not self.model_path.exists():
            return False
        try:
            with open(self.model_path, "rb") as f:
                data = pickle.load(f)
            self.model = data["model"]
            self.scaler = data["scaler"]
            self.feature_names = data["feature_names"]
            self.is_trained = data["is_trained"]
            self.training_history = data.get("training_history", [])
            log.info("ml_model_loaded", path=str(self.model_path))
            return True
        except Exception as e:
            log.warning("ml_model_load_failed", error=str(e))
            return False

    def get_feature_importance(self) -> dict[str, float]:
        """Get feature importance from trained model."""
        if not self.is_trained or not self.feature_names:
            return {}
        importance = self.model.feature_importances_
        return dict(zip(self.feature_names, importance))


class OnlineLearningManager:
    """Manage online learning - update model with new signal outcomes."""
    
    def __init__(self, model: SignalMLModel, state_store: StateStore):
        self.model = model
        self.state_store = state_store
        self.pending_signals: dict[str, dict] = {}  # signal_id -> {features, entry_time, entry_price}
        self.max_holding_bars = 5  # 5 bars = 25 min for 5m

    def register_signal(self, signal: Signal, features: MLFeatures) -> None:
        """Register a new signal for outcome tracking."""
        signal_id = f"{signal.symbol}_{signal.timeframe}_{signal.timestamp.isoformat()}"
        self.pending_signals[signal_id] = {
            "features": features,
            "signal": signal,
            "entry_time": signal.timestamp,
            "entry_price": signal.price,
            "bars_held": 0,
        }

    def update_outcomes(self, bars: list[BarData]) -> None:
        """Update outcomes for pending signals based on new bars."""
        completed = []
        
        for signal_id, data in self.pending_signals.items():
            data["bars_held"] += 1
            
            if data["bars_held"] >= self.max_holding_bars:
                # Find exit bar
                entry_idx = None
                for i, bar in enumerate(bars):
                    if bar.symbol == data["signal"].symbol and bar.timestamp >= data["entry_time"]:
                        entry_idx = i
                        break
                
                if entry_idx is not None and entry_idx + self.max_holding_bars < len(bars):
                    exit_bar = bars[entry_idx + self.max_holding_bars]
                    entry_price = data["entry_price"]
                    exit_price = exit_bar.close
                    
                    # Calculate return
                    signal = data["signal"]
                    if signal.signal_type in (SignalType.MEAN_REVERSION_LONG, SignalType.MOMENTUM_UP):
                        ret = (exit_price - entry_price) / entry_price
                    else:
                        ret = (entry_price - exit_price) / entry_price
                    
                    # Target: 1 if profitable, 0 if loss
                    target = 1 if ret > 0 else 0
                    
                    # Update feature target
                    features = data["features"]
                    features.forward_return = ret
                    features.target = target
                    
                    completed.append(signal_id)
        
        # Remove completed
        for sid in completed:
            del self.pending_signals[sid]
        
        log.debug("ml_outcomes_updated", completed=len(completed), pending=len(self.pending_signals))

    def retrain_if_needed(self, min_new_samples: int = 50) -> dict | None:
        """Retrain model if enough new samples accumulated."""
        # This would need access to stored features and outcomes
        # For now, return None
        return None


# Convenience functions
def create_ml_model() -> SignalMLModel:
    return SignalMLModel()


async def train_initial_model(days: int = 30) -> SignalMLModel:
    """Train initial model from historical data."""
    model = SignalMLModel()
    # Would load historical signals and outcomes from DB
    # For now, return untrained model
    return model