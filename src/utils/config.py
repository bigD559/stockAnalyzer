from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field


class SmartAPIConfig(BaseModel):
    api_key: str = ""
    client_id: str = ""
    password: str = ""
    totp_secret: str = ""
    sandbox: bool = True


class UniverseConfig(BaseModel):
    indices: list[str] = Field(default_factory=lambda: ["NIFTY50", "NIFTY100", "NIFTY200"])
    min_liquidity_lakh: int = 500


class VolatilityConfig(BaseModel):
    windows: dict[str, list[int]] = Field(
        default_factory=lambda: {"1m": [30, 60], "5m": [30, 60], "15m": [16, 32]}
    )
    annualization_factor: int = 945000


class PatternsConfig(BaseModel):
    zscore_window: int = 20
    bb_window: int = 20
    bb_std: float = 2.0
    rsi_window: int = 14
    macd_fast: int = 12
    macd_slow: int = 26
    macd_signal: int = 9
    signal_threshold: float = 0.6


class NewsConfig(BaseModel):
    poll_interval_sec: int = 300
    sources: list[str] = Field(
        default_factory=lambda: ["nse", "moneycontrol", "economictimes", "businessstandard"]
    )
    sentiment_enabled: bool = True


class AlertsConfig(BaseModel):
    desktop: bool = True
    telegram_bot_token: str = ""
    telegram_chat_id: str = ""
    throttle_sec: int = 60


class StorageConfig(BaseModel):
    db_path: str = "data/market.duckdb"
    retention_days: int = 90


class MarketHoursConfig(BaseModel):
    open: str = "09:15"
    close: str = "15:30"
    timezone: str = "Asia/Kolkata"
    pre_market_start: str = "09:00"
    post_market_end: str = "16:00"


class LoggingConfig(BaseModel):
    level: str = "INFO"
    file: str = "logs/app.log"
    json: bool = True


class Config(BaseModel):
    smartapi: SmartAPIConfig = Field(default_factory=SmartAPIConfig)
    universe: UniverseConfig = Field(default_factory=UniverseConfig)
    volatility: VolatilityConfig = Field(default_factory=VolatilityConfig)
    patterns: PatternsConfig = Field(default_factory=PatternsConfig)
    news: NewsConfig = Field(default_factory=NewsConfig)
    alerts: AlertsConfig = Field(default_factory=AlertsConfig)
    storage: StorageConfig = Field(default_factory=StorageConfig)
    market_hours: MarketHoursConfig = Field(default_factory=MarketHoursConfig)
    logging: LoggingConfig = Field(default_factory=LoggingConfig)


_config: Config | None = None


def load_config(path: str | Path | None = None) -> Config:
    global _config
    if _config is not None:
        return _config

    config_path = Path(path) if path else Path("config.yaml")
    if not config_path.exists():
        config_path = Path(__file__).parent.parent / "config.yaml"

    if config_path.exists():
        with open(config_path) as f:
            data = yaml.safe_load(f) or {}
    else:
        data = {}

    # Override with environment variables
    env_mappings = {
        "SMARTAPI_API_KEY": ("smartapi", "api_key"),
        "SMARTAPI_CLIENT_ID": ("smartapi", "client_id"),
        "SMARTAPI_PASSWORD": ("smartapi", "password"),
        "SMARTAPI_TOTP_SECRET": ("smartapi", "totp_secret"),
        "SMARTAPI_SANDBOX": ("smartapi", "sandbox"),
    }
    for env_key, (section, key) in env_mappings.items():
        if env_val := os.getenv(env_key):
            if section not in data:
                data[section] = {}
            data[section][key] = env_val

    _config = Config(**data)
    return _config


def get_config() -> Config:
    if _config is None:
        return load_config()
    return _config