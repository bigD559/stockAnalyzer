# Indian Stock Market Real-time Volatility Analyzer

Real-time intra-day volatility analysis for NSE stocks (Nifty 50/100/200) with statistical pattern detection, news scraping, and EOD reporting.

## Features

- **Real-time Data**: Angel One SmartAPI WebSocket for live ticks
- **Historical Data**: yfinance for backtesting (1m/5m/15m/1h/1d)
- **Volatility**: Realized volatility via rolling std dev of log returns (1m, 5m, 15m windows)
- **Patterns**: Statistical indicators (Z-score, Bollinger Bands, RSI, MACD, VWAP) with confidence scoring
- **News**: NSE announcements + Moneycontrol/ET/Business Standard scraping with sentiment
- **Alerts**: Console JSON + desktop notifications + Telegram (optional)
- **EOD Reports**: JSON + Markdown at 15:05 IST with volatility leaders, signals, news
- **Storage**: DuckDB for time-series analytics, SQLite for metadata
- **Deployment**: Local machine (Python 3.11+)

## Quick Start

### 1. Install Dependencies

```bash
pip install -r requirements.txt
# Or with poetry:
poetry install
```

### 2. Configure

Copy `config.yaml` and fill in your SmartAPI credentials (or leave blank for sandbox/yfinance-only):

```yaml
smartapi:
  api_key: "your_api_key"
  client_id: "your_client_id"
  password: "your_password"
  totp_secret: "BASE32_SECRET"
  sandbox: true
```

### 3. Initialize Database

```bash
python scripts/init_db.py
```

### 4. Fetch Historical Data (for backtesting)

```bash
python scripts/fetch_historical.py
```

### 5. Run Backtest

```bash
python scripts/backtest.py
```

### 6. Run Live Analyzer

```bash
python -m src.main
```

## Project Structure

```
stock-analyzer/
├── config.yaml              # Configuration
├── requirements.txt         # Dependencies
├── pyproject.toml           # Project config
├── scripts/
│   ├── init_db.py           # Initialize DuckDB schema
│   ├── fetch_historical.py  # Backfill yfinance data
│   └── backtest.py          # Signal backtesting
├── src/
│   ├── main.py              # Entry point
│   ├── data/                # Data ingestion
│   │   ├── smartapi_client.py
│   │   ├── historical.py
│   │   └── universe.py
│   ├── engine/              # Processing engine
│   │   ├── bar_aggregator.py
│   │   ├── volatility.py
│   │   ├── patterns.py
│   │   └── state.py
│   ├── news/                # News scraping
│   │   ├── nse_announcements.py
│   │   ├── portals/
│   │   └── aggregator.py
│   ├── alerts/              # Alerting
│   │   └── manager.py
│   ├── reports/             # EOD reports
│   │   └── eod.py
│   └── utils/               # Utilities
│       ├── config.py
│       ├── logging.py
│       └── time.py
├── data/                    # DuckDB files (gitignored)
├── reports/                 # EOD outputs (gitignored)
└── logs/                    # Structured logs (gitignored)
```

## Configuration

Key settings in `config.yaml`:

```yaml
universe:
  indices: ["NIFTY50", "NIFTY100", "NIFTY200"]

volatility:
  windows:
    "1m": [30, 60]
    "5m": [30, 60]
    "15m": [16, 32]

patterns:
  zscore_window: 20
  bb_window: 20
  bb_std: 2.0
  rsi_window: 14
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  signal_threshold: 0.6

alerts:
  desktop: true
  telegram_bot_token: ""
  throttle_sec: 60
```

## Signal Types

| Signal | Description |
|--------|-------------|
| `MEAN_REVERSION_LONG` | Z < -2, RSI < 30, price < lower BB |
| `MEAN_REVERSION_SHORT` | Z > 2, RSI > 70, price > upper BB |
| `MOMENTUM_UP` | MACD crossover + volume confirmation |
| `MOMENTUM_DOWN` | MACD crossunder + volume confirmation |
| `VOL_EXPANSION` | Volatility > 2x rolling median |
| `VOL_CONTRACTION` | Volatility < 0.5x rolling median |

## EOD Report

Generated at 15:05 IST daily in `reports/`:
- `eod_YYYYMMDD.json` - Machine readable
- `eod_YYYYMMDD.md` - Human readable

Contains:
- Market breadth (advancers/decliners)
- Volume & volatility leaders
- Signal summary by type
- Per-symbol OHLC, vol, signals, news

## SmartAPI Setup

1. Login to Angel One → SmartAPI
2. Create App with redirect URL `http://localhost:8080`
3. Enable "Trade" + "Market Data" + "Profile" permissions
4. Get API Key, Client ID, Password, TOTP Secret
5. Add to `config.yaml` or environment variables

## Environment Variables

```bash
export SMARTAPI_API_KEY="your_key"
export SMARTAPI_CLIENT_ID="your_id"
export SMARTAPI_PASSWORD="your_password"
export SMARTAPI_TOTP_SECRET="base32_secret"
export SMARTAPI_SANDBOX="true"
```

## Development

```bash
# Format code
black src/ scripts/

# Lint
ruff src/ scripts/

# Type check
mypy src/

# Test
pytest tests/
```

## Notes

- **Sandbox Mode**: Set `smartapi.sandbox: true` for testing without real orders
- **Rate Limits**: SmartAPI allows 50 req/sec, 500 req/min
- **yfinance Limits**: 1m data only 7 days, 5m data 60 days
- **Market Hours**: 09:15-15:30 IST, pre-market 09:00, post-market 16:00

## License

MIT