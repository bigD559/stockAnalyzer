#!/usr/bin/env python3
"""Streamlit Dashboard for Indian Stock Market Analyzer."""

import streamlit as st
import pandas as pd
import plotly.graph_objects as go
import plotly.express as px
from plotly.subplots import make_subplots
import duckdb
from datetime import datetime, timedelta
from pathlib import Path
import sys

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.utils.config import get_config
from src.utils.time import get_timezone, now, format_ist
from src.data.universe import UniverseManager
from src.data.historical import HistoricalDataFetcher
from src.engine.state import StateStore

st.set_page_config(
    page_title="Indian Stock Market Analyzer",
    page_icon="📈",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Custom CSS
st.markdown("""
<style>
    .metric-card {
        background-color: #f0f2f6;
        padding: 1rem;
        border-radius: 0.5rem;
        margin: 0.5rem 0;
    }
    .signal-buy { color: #00cc00; font-weight: bold; }
    .signal-sell { color: #ff3333; font-weight: bold; }
    .signal-neutral { color: #ffaa00; font-weight: bold; }
    .stTabs [data-baseweb="tab-list"] { gap: 8px; }
    .stTabs [data-baseweb="tab"] { height: 50px; padding: 0 20px; }
</style>
""", unsafe_allow_html=True)


@st.cache_data(ttl=60)
def load_config():
    return get_config()


@st.cache_resource
def get_db_connection():
    config = load_config()
    conn = duckdb.connect(config.storage.db_path, read_only=True)
    return conn


@st.cache_data(ttl=300)
def get_universe():
    u = UniverseManager()
    u.load()
    return u


@st.cache_data(ttl=60)
def get_latest_bars(symbol: str, timeframe: str = "5m", limit: int = 100):
    conn = get_db_connection()
    table = f"bars_{timeframe}"
    query = f"""
        SELECT timestamp, open, high, low, close, volume, vwap
        FROM {table}
        WHERE symbol = ?
        ORDER BY timestamp DESC
        LIMIT ?
    """
    df = conn.execute(query, [symbol, limit]).fetchdf()
    if not df.empty:
        df = df.sort_values("timestamp")
    return df


@st.cache_data(ttl=60)
def get_recent_signals(symbol: str | None = None, hours: int = 24, show_all: bool = False):
    conn = get_db_connection()
    if symbol:
        if show_all:
            query = f"""
                SELECT symbol, timeframe, timestamp, signal_type, confidence, price, priority
                FROM signals
                WHERE symbol = ?
                ORDER BY timestamp DESC
            """
            df = conn.execute(query, [symbol]).fetchdf()
        else:
            query = f"""
                SELECT symbol, timeframe, timestamp, signal_type, confidence, price, priority
                FROM signals
                WHERE symbol = ? AND timestamp >= NOW() - INTERVAL {hours} HOURS
                ORDER BY timestamp DESC
            """
            df = conn.execute(query, [symbol]).fetchdf()
    else:
        if show_all:
            query = f"""
                SELECT symbol, timeframe, timestamp, signal_type, confidence, price, priority
                FROM signals
                ORDER BY timestamp DESC
            """
            df = conn.execute(query).fetchdf()
        else:
            query = f"""
                SELECT symbol, timeframe, timestamp, signal_type, confidence, price, priority
                FROM signals
                WHERE timestamp >= NOW() - INTERVAL {hours} HOURS
                ORDER BY timestamp DESC
            """
            df = conn.execute(query, [hours]).fetchdf()
    return df


@st.cache_data(ttl=60)
def get_volatility_data(symbol: str, timeframe: str = "5m", hours: int = 24, show_all: bool = False):
    conn = get_db_connection()
    if show_all:
        query = f"""
            SELECT timestamp, realized_vol, annualized_vol, window_bars
            FROM volatility
            WHERE symbol = ? AND timeframe = ?
            ORDER BY timestamp
        """
        df = conn.execute(query, [symbol, timeframe]).fetchdf()
    else:
        query = f"""
            SELECT timestamp, realized_vol, annualized_vol, window_bars
            FROM volatility
            WHERE symbol = ? AND timeframe = ? AND timestamp >= NOW() - INTERVAL {hours} HOURS
            ORDER BY timestamp
        """
        df = conn.execute(query, [symbol, timeframe]).fetchdf()
    return df


@st.cache_data(ttl=300)
def get_eod_reports(days: int = 7):
    conn = get_db_connection()
    query = f"""
        SELECT date, symbol, close, high, low, volume, 
               realized_vol_5m, realized_vol_15m,
               signals_json, max_confidence, news_count
        FROM eod_summary
        WHERE date >= CURRENT_DATE - INTERVAL {days} DAYS
        ORDER BY date DESC, symbol
    """
    df = conn.execute(query).fetchdf()
    return df


@st.cache_data(ttl=300)
def get_market_breadth(days: int = 1):
    conn = get_db_connection()
    query = f"""
        SELECT date,
               SUM(CASE WHEN gap_pct > 0 THEN 1 ELSE 0 END) as advancers,
               SUM(CASE WHEN gap_pct < 0 THEN 1 ELSE 0 END) as decliners,
               SUM(CASE WHEN gap_pct = 0 THEN 1 ELSE 0 END) as unchanged
        FROM eod_summary
        WHERE date >= CURRENT_DATE - INTERVAL {days} DAYS
        GROUP BY date
        ORDER BY date DESC
    """
    df = conn.execute(query).fetchdf()
    return df


def create_candlestick_chart(df: pd.DataFrame, title: str):
    if df.empty:
        return go.Figure()
    
    fig = go.Figure(data=[go.Candlestick(
        x=df['timestamp'],
        open=df['open'],
        high=df['high'],
        low=df['low'],
        close=df['close'],
        name='Price'
    )])
    
    # Add volume as bar chart on secondary y
    fig.add_trace(go.Bar(
        x=df['timestamp'],
        y=df['volume'],
        name='Volume',
        marker_color='rgba(0,0,255,0.3)',
        yaxis='y2'
    ))
    
    fig.update_layout(
        title=title,
        xaxis_title='Time',
        yaxis_title='Price (₹)',
        yaxis2=dict(title='Volume', overlaying='y', side='right', showgrid=False),
        xaxis_rangeslider_visible=False,
        height=500,
        template='plotly_white'
    )
    return fig


def create_volatility_chart(df: pd.DataFrame, title: str):
    if df.empty:
        return go.Figure()
    
    fig = go.Figure()
    fig.add_trace(go.Scatter(
        x=df['timestamp'],
        y=df['annualized_vol'] * 100,
        mode='lines',
        name='Annualized Vol (%)',
        line=dict(color='purple', width=1.5)
    ))
    
    # Add rolling average
    df['vol_ma'] = df['annualized_vol'].rolling(20).mean()
    fig.add_trace(go.Scatter(
        x=df['timestamp'],
        y=df['vol_ma'] * 100,
        mode='lines',
        name='Vol MA(20)',
        line=dict(color='orange', width=1, dash='dash')
    ))
    
    fig.update_layout(
        title=title,
        xaxis_title='Time',
        yaxis_title='Volatility (%)',
        height=300,
        template='plotly_white'
    )
    return fig


def create_signals_timeline(df: pd.DataFrame, title: str):
    if df.empty:
        return go.Figure()
    
    # Color map for signal types
    color_map = {
        'MEAN_REVERSION_LONG': 'green',
        'MEAN_REVERSION_SHORT': 'red',
        'MOMENTUM_UP': 'blue',
        'MOMENTUM_DOWN': 'orange',
        'VOL_EXPANSION': 'purple',
        'VOL_CONTRACTION': 'gray',
    }
    
    fig = go.Figure()
    
    for sig_type in df['signal_type'].unique():
        subset = df[df['signal_type'] == sig_type]
        fig.add_trace(go.Scatter(
            x=subset['timestamp'],
            y=subset['price'],
            mode='markers',
            name=sig_type,
            marker=dict(
                color=color_map.get(sig_type, 'gray'),
                size=10,
                symbol='triangle-up' if 'LONG' in sig_type or 'UP' in sig_type else 'triangle-down'
            ),
            hovertemplate=(
                '<b>%{fullData.name}</b><br>' +
                'Time: %{x}<br>' +
                'Price: ₹%{y:.2f}<br>' +
                'Confidence: %{customdata:.0%}<br>' +
                '<extra></extra>'
            ),
            customdata=subset['confidence']
        ))
    
    fig.update_layout(
        title=title,
        xaxis_title='Time',
        yaxis_title='Price (₹)',
        height=400,
        template='plotly_white'
    )
    return fig


# Main App
def main():
    config = load_config()
    universe = get_universe()
    symbols = universe.get_all_symbols()
    
    # Sidebar
    st.sidebar.title("📈 Stock Analyzer")
    st.sidebar.markdown(f"**Market Status**: {get_market_status()}")
    st.sidebar.markdown(f"**Time**: {format_ist(now())}")
    
    # Symbol selector
    selected_symbol = st.sidebar.selectbox(
        "Select Symbol",
        symbols,
        index=0 if "RELIANCE" not in symbols else symbols.index("RELIANCE")
    )
    
    # Timeframe selector
    timeframe = st.sidebar.selectbox(
        "Timeframe",
        ["1m", "5m", "15m", "1h", "1d"],
        index=1
    )
    
    # Lookback period
    lookback_hours = st.sidebar.slider(
        "Lookback Hours",
        1, 48, 8
    )
    
    # Show all historical data (for backtest data)
    show_all_data = st.sidebar.checkbox(
        "Show All Historical Data",
        value=True,
        help="Show all available historical data (not just recent)"
    )
    
    # Main tabs
    tab1, tab2, tab3, tab4, tab5 = st.tabs([
        "📊 Price & Signals", 
        "📈 Volatility", 
        "🎯 Signals", 
        "📰 News & Breadth",
        "📋 EOD Reports"
    ])
    
    with tab1:
        st.header(f"{selected_symbol} - {timeframe} Chart")
        
        # Load data
        bars_df = get_latest_bars(selected_symbol, timeframe, limit=200)
        signals_df = get_recent_signals(selected_symbol, hours=lookback_hours, show_all=show_all_data)
        
        if not bars_df.empty:
            # Main chart
            fig = create_candlestick_chart(bars_df, f"{selected_symbol} {timeframe} Candlesticks")
            
            # Overlay signals on chart
            if not signals_df.empty:
                for sig_type in signals_df['signal_type'].unique():
                    subset = signals_df[signals_df['signal_type'] == sig_type]
                    color = 'green' if 'LONG' in sig_type or 'UP' in sig_type else 'red'
                    symbol_marker = 'triangle-up' if 'LONG' in sig_type or 'UP' in sig_type else 'triangle-down'
                    
                    fig.add_trace(go.Scatter(
                        x=subset['timestamp'],
                        y=subset['price'],
                        mode='markers',
                        name=f'Signal: {sig_type}',
                        marker=dict(color=color, size=12, symbol=symbol_marker),
                        hovertemplate=(
                            '<b>%{fullData.name}</b><br>' +
                            'Time: %{x}<br>' +
                            'Price: ₹%{y:.2f}<br>' +
                            'Confidence: %{customdata:.0%}<br>' +
                            '<extra></extra>'
                        ),
                        customdata=subset['confidence'],
                        showlegend=True
                    ))
            
            st.plotly_chart(fig, use_container_width=True)
            
            # Current metrics
            latest = bars_df.iloc[-1]
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Last Price", f"₹{latest['close']:.2f}")
            col2.metric("Change", f"{latest['close'] - latest['open']:+.2f}")
            col3.metric("Volume", f"{latest['volume']:,.0f}")
            col4.metric("VWAP", f"₹{latest['vwap']:.2f}")
        else:
            st.warning(f"No data available for {selected_symbol} {timeframe}")
    
    with tab2:
        st.header(f"{selected_symbol} - Volatility Analysis")
        
        vol_df = get_volatility_data(selected_symbol, timeframe, hours=lookback_hours, show_all=show_all_data)
        
        if not vol_df.empty:
            fig = create_volatility_chart(vol_df, f"{selected_symbol} {timeframe} Realized Volatility")
            st.plotly_chart(fig, use_container_width=True)
            
            # Vol stats
            col1, col2, col3 = st.columns(3)
            current_vol = vol_df.iloc[-1]['annualized_vol'] * 100
            avg_vol = vol_df['annualized_vol'].mean() * 100
            max_vol = vol_df['annualized_vol'].max() * 100
            
            col1.metric("Current Vol", f"{current_vol:.2f}%")
            col2.metric("Avg Vol (session)", f"{avg_vol:.2f}%")
            col3.metric("Max Vol", f"{max_vol:.2f}%")
            
            # Vol distribution
            st.subheader("Volatility Distribution")
            fig_dist = px.histogram(vol_df, x='annualized_vol', nbins=30, 
                                    title='Annualized Volatility Distribution')
            fig_dist.update_layout(height=300)
            st.plotly_chart(fig_dist, use_container_width=True)
        else:
            st.warning("No volatility data available")
    
    with tab3:
        st.header("Signal Dashboard")
        
        signals_df = get_recent_signals(None, hours=lookback_hours, show_all=show_all_data)
        
        if not signals_df.empty:
            # Summary metrics
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Total Signals", len(signals_df))
            col2.metric("Unique Symbols", signals_df['symbol'].nunique())
            
            buy_signals = len(signals_df[signals_df['signal_type'].str.contains('LONG|UP')])
            sell_signals = len(signals_df[signals_df['signal_type'].str.contains('SHORT|DOWN')])
            col3.metric("Buy Signals", buy_signals)
            col4.metric("Sell Signals", sell_signals)
            
            # Signal timeline
            fig = create_signals_timeline(signals_df, "Recent Signals Timeline")
            st.plotly_chart(fig, use_container_width=True)
            
            # Signal type distribution
            st.subheader("Signal Type Distribution")
            sig_counts = signals_df['signal_type'].value_counts()
            fig_pie = px.pie(values=sig_counts.values, names=sig_counts.index, 
                           title='Signal Types')
            st.plotly_chart(fig_pie, use_container_width=True)
            
            # Top symbols by signal count
            st.subheader("Top Symbols by Signal Count")
            top_symbols = signals_df['symbol'].value_counts().head(15)
            st.bar_chart(top_symbols)
            
            # Signals table
            st.subheader("Recent Signals")
            display_df = signals_df[['timestamp', 'symbol', 'timeframe', 'signal_type', 'confidence', 'price', 'priority']].copy()
            display_df['timestamp'] = display_df['timestamp'].dt.strftime('%H:%M:%S')
            display_df['confidence'] = (display_df['confidence'] * 100).round(1).astype(str) + '%'
            st.dataframe(display_df.head(50), use_container_width=True, hide_index=True)
        else:
            st.info("No signals in the selected time period")
    
    with tab4:
        st.header("Market Breadth & News")
        
        # Market breadth
        breadth_df = get_market_breadth(days=5)
        if not breadth_df.empty:
            st.subheader("Market Breadth (Last 5 Days)")
            fig_breadth = go.Figure()
            fig_breadth.add_trace(go.Bar(x=breadth_df['date'], y=breadth_df['advancers'], name='Advancers', marker_color='green'))
            fig_breadth.add_trace(go.Bar(x=breadth_df['date'], y=breadth_df['decliners'], name='Decliners', marker_color='red'))
            fig_breadth.add_trace(go.Bar(x=breadth_df['date'], y=breadth_df['unchanged'], name='Unchanged', marker_color='gray'))
            fig_breadth.update_layout(barmode='stack', height=300)
            st.plotly_chart(fig_breadth, use_container_width=True)
        
        # News (placeholder)
        st.subheader("Recent News")
        st.info("News feed integration pending - connect to NSE announcements / Moneycontrol / ET")
    
    with tab5:
        st.header("EOD Reports")
        
        eod_df = get_eod_reports(days=7)
        if not eod_df.empty:
            # Date selector
            dates = eod_df['date'].unique()
            selected_date = st.selectbox("Select Date", dates)
            
            day_data = eod_df[eod_df['date'] == selected_date]
            
            # Summary metrics
            col1, col2, col3, col4 = st.columns(4)
            col1.metric("Symbols", len(day_data))
            col2.metric("Avg Close", f"₹{day_data['close'].mean():.2f}")
            col3.metric("Total Volume", f"{day_data['volume'].sum():,.0f}")
            col4.metric("Avg Vol 5m", f"{day_data['realized_vol_5m'].mean()*100:.2f}%")
            
            # Top movers
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("Top Gainers")
                gainers = day_data.nlargest(10, 'close')[['symbol', 'close', 'realized_vol_15m']]
                st.dataframe(gainers, use_container_width=True, hide_index=True)
            
            with col2:
                st.subheader("Top Volume")
                vol_leaders = day_data.nlargest(10, 'volume')[['symbol', 'volume', 'close']]
                st.dataframe(vol_leaders, use_container_width=True, hide_index=True)
            
            # Full table
            st.subheader(f"Full EOD Data - {selected_date}")
            display_cols = ['symbol', 'close', 'high', 'low', 'volume', 'realized_vol_15m', 'max_confidence', 'news_count']
            st.dataframe(day_data[display_cols], use_container_width=True, hide_index=True)
        else:
            st.info("No EOD reports available yet. Run during market hours for live data.")


def get_market_status():
    """Get current market status."""
    tz = get_timezone()
    now_dt = now()
    open_time = tz.localize(datetime.combine(now_dt.date(), datetime.strptime("09:15", "%H:%M").time()))
    close_time = tz.localize(datetime.combine(now_dt.date(), datetime.strptime("15:30", "%H:%M").time()))
    
    if now_dt < open_time:
        return "🔴 Pre-Market"
    elif now_dt > close_time:
        return "🔴 Closed"
    else:
        return "🟢 Live"


if __name__ == "__main__":
    main()