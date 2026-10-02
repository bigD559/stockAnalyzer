# Indian Stock Market Analysis Tool - Implementation Plan

## Project Overview
Real-time intra-day volatility analyzer for NSE stocks (Nifty 50/100/200) with statistical pattern detection, news scraping, and EOD analysis. Deployed locally with Angel One SmartAPI for live data, yfinance for historical.

---

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      LOCAL MACHINE                              │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐          │
│  │ Data Ingestion│  │  Processing  │  │   Output     │          │
│  │  (Workers)   │──▶│  (Engine)    │──▶│  (Alerts/    │          │
│  └──────────────┘  └──────────────┘  │   Reports)   │          │
│       ▲                   ▲          └──────────────┘          │
│       │                   │                  │                  │
│  ┌────┴────┐        ┌────┴────┐      ┌─────┴─────┐            │
│  │SmartAPI │        │  State  │      │ Console   │            │
│  │ WebSocket│       │  Store  │      │ Desktop   │            │
│  └─────────┘        │(SQLite/  │      │ Notif +   │            │
│       ▲             │ DuckDB) │      │ EOD JSON  │            │
│  ┌────┴────┐        └─────────┘      └───────────┘            │
│  │ yfinance│                              ▲                    │
│  │Historical│                             │                    │
│  └─────────┘              ┌──────────────┴──────────────┐      │
│                           │      News Scrapers          │      │
│                           │  (Moneycontrol, ET, NSE)    │      │
│                           └─────────────────────────────┘      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Module Breakdown

### 1. Data Ingestion Layer

#### 1.1 Angel One SmartAPI Client (`src/data/smartapi_client.py`)
- **Authentication**: TOTP-based login, token refresh handling
- **WebSocket**: Subscribe to `NSE_FO`/`NSE_CM` for LTP, OHLC, volume
- **Symbols**: Dynamic subscription for Nifty 200 tokens
- **Rate limits**: Respect 50 req/sec, 500 req/min limits
- **Reconnection**: Exponential backoff, resubscribe on disconnect
- **Output**: Normalized tick → `TickData` dataclass → async queue

#### 1.2 Historical Data Fetcher (`src/data/historical.py`)
- **Source**: yfinance (primary), fallback to SmartAPI historical
- **Intervals**: 1m, 5m, 15m, 1h, 1d
- **Lookback**: yfinance limits (7d for 1m, 60d for 5m, 730d for 1h)
- **Storage**: DuckDB partitioned by symbol/date for fast queries
- **Schema**: `symbol, timestamp, open, high, low, close, volume`

#### 1.3 Symbol Universe Manager (`src/data/universe.py`)
- **Source**: NSE official CSV or static JSON (Nifty 50/100/200)
- **Mapping**: Symbol → token (SmartAPI), yfinance ticker (`.NS`)
- **Filters**: F&O eligible, liquidity thresholds
- **Refresh**: Daily at startup

---

### 2. Processing Engine

#### 2.1 Bar Aggregator (`src/engine/bar_aggregator.py`)
- **Input**: Tick stream from WebSocket
- **Output**: 1m, 5m, 15m OHLCV bars (closed bars only)
- **Logic**: Time-based bucketing, volume-weighted VWAP
- **Late ticks**: Configurable grace period (e.g., 2s)

#### 2.2 Volatility Calculator (`src/engine/volatility.py`)
- **Method**: Realized volatility = rolling std dev of log returns
- **Windows**: 1m (30/60 bars), 5m (30/60 bars), 15m (16/32 bars)
- **Annualization**: √(252 × 375 / window_minutes) for daily equivalent
- **Output**: `VolatilitySnapshot` per symbol per timeframe
- **Storage**: Append to DuckDB `volatility` table

#### 2.3 Statistical Pattern Detector (`src/engine/patterns.py`)
- **Indicators** (per timeframe):
  - Z-score (price vs rolling mean, window=20)
  - Bollinger Bands (20, 2σ) → %B, bandwidth
  - RSI (14) — mean reversion signals
  - MACD (12,26,9) — momentum crossovers
  - VWAP deviation — institutional reference
- **Signals**: 
  - `MEAN_REVERSION_LONG` (Z < -2, RSI < 30, price < lower BB)
  - `MEAN_REVERSION_SHORT` (Z > 2, RSI > 70, price > upper BB)
  - `MOMENTUM_UP` (MACD crossover + volume confirm)
  - `MOMENTUM_DOWN` (MACD crossunder + volume confirm)
  - `VOL_EXPANSION` (vol > 2× rolling median)
  - `VOL_CONTRACTION` (vol < 0.5× rolling median)
- **Confidence**: Composite score 0-1 based on confluence

#### 2.4 State Store (`src/engine/state.py`)
- **Backend**: DuckDB (analytical) + SQLite (metadata/alerts)
- **Tables**:
  - `ticks` (raw, partitioned by date)
  - `bars_1m`, `bars_5m`, `bars_15m`
  - `volatility` (symbol, ts, timeframe, realized_vol, annualized_vol)
  - `signals` (symbol, ts, timeframe, signal_type, confidence, metadata)
  - `alerts` (id, ts, symbol, type, message, acknowledged)
  - `news` (symbol, ts, source, headline, url, sentiment)
  - `eod_summary` (date, symbol, close, vol, signals_count, news_count)

---

### 3. News Scraping Layer

#### 3.1 NSE/BSE Announcements (`src/news/nse_announcements.py`)
- **Source**: `https://www.nseindia.com/api/corporate-announcements`
- **Filter**: By symbol, category (financial results, board meetings, etc.)
- **Frequency**: Poll every 5 min during market hours
- **Parser**: Extract symbol, headline, PDF link, timestamp

#### 3.2 Financial Portal Scrapers (`src/news/portals/`)
- **Moneycontrol**: RSS + article page scraping (company-specific)
- **Economic Times**: RSS feeds for markets/companies
- **Business Standard**: Search API + article scrape
- **Rate limiting**: 1 req/2s per domain, respect robots.txt
- **Deduplication**: Content hash + URL tracking
- **Sentiment**: Simple keyword-based (positive/negative/neutral) or VADER

#### 3.3 News Aggregator (`src/news/aggregator.py`)
- **Merge**: Combine all sources, sort by timestamp
- **Link to symbols**: Map company names → NSE symbols
- **Relevance scoring**: Title mention + recency + source weight
- **Output**: Queue for pattern engine correlation

---

### 4. Alert & Reporting Layer

#### 4.1 Alert Manager (`src/alerts/manager.py`)
- **Channels**:
  - Console (structured JSON lines)
  - Desktop notifications (plyer/notify2)
  - Optional: Telegram bot (async, non-blocking)
- **Throttling**: Max 1 alert/symbol/minute per signal type
- **Priority**: HIGH (vol expansion + signal), MEDIUM (pattern), LOW (news)

#### 4.2 EOD Analyzer (`src/reports/eod.py`)
- **Trigger**: 15:05 IST (after market close + settlement)
- **Computation per symbol**:
  - Close price, day's range, volume vs 20d avg
  - Realized vol (1m, 5m, 15m) at close
  - Signal summary (count by type, max confidence)
  - News count + top headlines
  - Overnight gap risk (prev close vs current close)
- **Output**: JSON + Markdown report → `reports/eod_YYYYMMDD.json/.md`
- **Summary**: Portfolio-level stats (advancers/decliners, vol leaders)

---

### 5. Configuration & Operations

#### 5.1 Config (`config.yaml`)
```yaml
smartapi:
  api_key: ""
  client_id: ""
  password: ""
  totp_secret: ""
  
universe:
  indices: ["NIFTY50", "NIFTY100", "NIFTY200"]
  min_liquidity_lakh: 500  # avg daily volume * price
  
volatility:
  windows:
    "1m": [30, 60]
    "5m": [30, 60]
    "15m": [16, 32]
  annualization_factor: 252 * 375  # trading minutes/year

patterns:
  zscore_window: 20
  bb_window: 20
  bb_std: 2
  rsi_window: 14
  macd_fast: 12
  macd_slow: 26
  macd_signal: 9
  signal_threshold: 0.6

news:
  poll_interval_sec: 300
  sources: ["nse", "moneycontrol", "economictimes", "businessstandard"]
  sentiment_enabled: true

alerts:
  desktop: true
  telegram_bot_token: ""
  telegram_chat_id: ""
  throttle_sec: 60

storage:
  db_path: "data/market.duckdb"
  retention_days: 90

market_hours:
  open: "09:15"
  close: "15:30"
  timezone: "Asia/Kolkata"
  pre_market_start: "09:00"
  post_market_end: "16:00"
```

#### 5.2 Project Structure
```
stock-analyzer/
├── config.yaml
├── requirements.txt
├── pyproject.toml
├── src/
│   ├── __init__.py
│   ├── main.py                 # Entry point, orchestrator
│   ├── data/
│   │   ├── __init__.py
│   │   ├── smartapi_client.py
│   │   ├── historical.py
│   │   └── universe.py
│   ├── engine/
│   │   ├── __init__.py
│   │   ├── bar_aggregator.py
│   │   ├── volatility.py
│   │   ├── patterns.py
│   │   └── state.py
│   ├── news/
│   │   ├── __init__.py
│   │   ├── nse_announcements.py
│   │   ├── portals/
│   │   │   ├── __init__.py
│   │   │   ├── moneycontrol.py
│   │   │   ├── economictimes.py
│   │   │   └── businessstandard.py
│   │   └── aggregator.py
│   ├── alerts/
│   │   ├── __init__.py
│   │   └── manager.py
│   ├── reports/
│   │   ├── __init__.py
│   │   └── eod.py
│   └── utils/
│       ├── __init__.py
│       ├── time.py
│       └── logging.py
├── tests/
│   ├── test_volatility.py
│   ├── test_patterns.py
│   └── test_integration.py
├── scripts/
│   ├── init_db.py
│   ├── fetch_historical.py
│   └── backtest.py
├── reports/                    # EOD outputs (gitignored)
├── data/                       # DuckDB files (gitignored)
└── logs/                       # Structured logs (gitignored)
```

---

## Implementation Phases

### Phase 1: Foundation (Week 1)
- [ ] Project setup: poetry/pip, config.yaml, logging, DuckDB schema
- [ ] SmartAPI auth + WebSocket connection (tick → queue)
- [ ] Symbol universe loader (Nifty 200 tokens + yfinance mapping)
- [ ] Bar aggregator (1m/5m/15m from ticks)
- [ ] Basic volatility calculator (rolling std dev)
- [ ] Console logging of ticks/bars/vol

### Phase 2: Pattern Engine (Week 1-2)
- [ ] Statistical indicators (Z-score, BB, RSI, MACD, VWAP)
- [ ] Signal generation with confidence scoring
- [ ] State persistence (DuckDB writes)
- [ ] Unit tests for indicators against known values

### Phase 3: News & Alerts (Week 2)
- [ ] NSE announcements scraper
- [ ] Moneycontrol/ET/BS scrapers (RSS + article)
- [ ] News aggregator + symbol linking
- [ ] Alert manager (console + desktop + throttle)
- [ ] Integration: correlate news with signals

### Phase 4: EOD & Polish (Week 2-3)
- [ ] EOD report generator (JSON + Markdown)
- [ ] Historical backfill script (yfinance → DuckDB)
- [ ] Backtesting framework (signal → forward returns)
- [ ] Config validation, error handling, graceful shutdown
- [ ] Documentation (README, config guide)

### Phase 5: Optional Enhancements
- [ ] Telegram bot for mobile alerts
- [ ] Simple dashboard (Streamlit/Plotly Dash) for visualization
- [ ] Options chain integration (IV, PCR, max pain)
- [ ] ML model for signal filtering
- [ ] Multi-process architecture (separate ingest/engine/alert)

---

## Key Technical Decisions

| Decision | Choice | Rationale |
|----------|--------|-----------|
| **Language** | Python 3.11+ | Rich ecosystem (pandas, duckdb, asyncio), yfinance support |
| **Async** | asyncio + aiohttp | WebSocket + HTTP scraping concurrent |
| **Time-series DB** | DuckDB | Embedded, columnar, SQL, fast analytics, no server |
| **Scheduling** | apscheduler | Cron-style for EOD, interval for news |
| **Desktop notifications** | plyer | Cross-platform (Windows/Linux/Mac) |
| **Logging** | structlog + stdlib | Structured JSON, context binding |

---

## Risks & Mitigations

| Risk | Mitigation |
|------|------------|
| SmartAPI rate limits / disconnects | Queue buffering, reconnection logic, fallback to REST polling |
| yfinance 1m data limited to 7d | Accept for backtest; rely on SmartAPI for live building history |
| News scraping breaks on site changes | Modular scrapers, graceful degradation, monitoring |
| Local machine sleep/hibernate | Detect market hours, pause/resume, catch-up on wake |
| Data quality (bad ticks) | Outlier filtering (IQR), gap detection in bars |

---

## Next Steps

1. **Confirm**: Review this plan, adjust scope/priorities
2. **Credentials**: Provide SmartAPI credentials (or use sandbox)
3. **Initialize**: Run `scripts/init_db.py` after Phase 1
4. **Test**: Paper trade signals for 1-2 weeks before live use

---

## Reference Implementation
- GitHub: https://github.com/maanavshah/stock-market-india
- SmartAPI docs: https://smartapi.angelone.in/docs
- NSE APIs: https://www.nseindia.com/api-documentation