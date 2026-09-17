import streamlit as st
import pandas as pd
import plotly.graph_objects as go
from plotly.subplots import make_subplots
from pathlib import Path
import re
from datetime import datetime

st.set_page_config(page_title="Nifty Straddle Inspector Dashboard", layout="wide", initial_sidebar_state="expanded")

# --- Custom CSS for Premium Look ---
st.markdown("""
<style>
    .main {
        background-color: #0E1117;
        color: #FAFAFA;
    }
    .metric-card {
        background: rgba(255, 255, 255, 0.05);
        border-radius: 12px;
        padding: 20px;
        box-shadow: 0 4px 6px rgba(0, 0, 0, 0.1);
        backdrop-filter: blur(10px);
        border: 1px solid rgba(255, 255, 255, 0.1);
        text-align: center;
    }
    .metric-value {
        font-size: 24px;
        font-weight: 700;
        color: #00E676;
    }
    .metric-value.negative {
        color: #FF5252;
    }
    .metric-title {
        font-size: 14px;
        color: #BDBDBD;
        text-transform: uppercase;
        letter-spacing: 1px;
        margin-bottom: 5px;
    }
</style>
""", unsafe_allow_html=True)

st.title("⚡ Nifty 50 Intraday Straddle Inspector")
st.markdown("Inspect the premium decay of intraday Straddles (Call + Put) alongside Nifty 50 Spot price movements.")

@st.cache_data
def get_available_dates():
    script_dir = Path(__file__).resolve().parent
    data_dir = script_dir.parent / "Data"
    if not data_dir.exists():
        return []
    files = data_dir.rglob("nifty_options_*.csv")
    dates = []
    for f in files:
        match = re.search(r"nifty_options_(\d{4}-\d{2}-\d{2})\.csv", f.name)
        if match:
            dates.append(match.group(1))
    return sorted(dates, reverse=True)

@st.cache_data
def load_straddle_data(date_str, selected_strike=None, timeframe="1m"):
    script_dir = Path(__file__).resolve().parent
    data_dir = script_dir.parent / "Data"
    year = date_str.split('-')[0]
    csv_path = data_dir / year / f"nifty_options_{date_str}.csv"
    
    if not csv_path.exists():
        for p in data_dir.rglob(f"nifty_options_{date_str}.csv"):
            csv_path = p
            break
            
    if not csv_path.exists():
        return None, [], None
        
    df = pd.read_csv(csv_path)
    df['Time'] = df['datetime'].apply(lambda x: x.split(' ')[1][:5] if isinstance(x, str) and ' ' in x else '')
    df = df[df['spot'] > 0]
    
    if df.empty:
        return None, [], None
        
    # Use api_strike (fixed option contract strike) to prevent dynamic relative label switching spikes
    strike_col = 'api_strike' if 'api_strike' in df.columns else 'strike'
    strikes = sorted([int(x) for x in df[strike_col].dropna().unique().tolist()])
    first_spot = df['spot'].iloc[0]
    atm_strike = int(round(first_spot / 50.0) * 50.0)
    
    if atm_strike not in strikes and strikes:
        atm_strike = min(strikes, key=lambda x: abs(x - atm_strike))
        
    if selected_strike is None:
        selected_strike = atm_strike
        
    df_strike = df[df[strike_col] == selected_strike]
    if df_strike.empty:
        return None, strikes, atm_strike
        
    # Pivot for OHLC columns to compute combined straddle candles
    df_open = df_strike.pivot_table(index='Time', columns='option_type', values='open', aggfunc='first').reset_index()
    df_high = df_strike.pivot_table(index='Time', columns='option_type', values='high', aggfunc='first').reset_index()
    df_low = df_strike.pivot_table(index='Time', columns='option_type', values='low', aggfunc='first').reset_index()
    df_close = df_strike.pivot_table(index='Time', columns='option_type', values='close', aggfunc='first').reset_index()
    df_spot = df_strike.groupby('Time')['spot'].first().reset_index()
    
    def clean_pivot(p_df, value_name):
        if 'CALL' in p_df.columns:
            p_df.rename(columns={'CALL': f'CE_{value_name}'}, inplace=True)
        if 'PUT' in p_df.columns:
            p_df.rename(columns={'PUT': f'PE_{value_name}'}, inplace=True)
        if f'CE_{value_name}' not in p_df.columns:
            p_df[f'CE_{value_name}'] = 0.0
        if f'PE_{value_name}' not in p_df.columns:
            p_df[f'PE_{value_name}'] = 0.0
        p_df[f'CE_{value_name}'] = p_df[f'CE_{value_name}'].fillna(0.0)
        p_df[f'PE_{value_name}'] = p_df[f'PE_{value_name}'].fillna(0.0)
        return p_df[['Time', f'CE_{value_name}', f'PE_{value_name}']]
        
    df_open = clean_pivot(df_open, 'open')
    df_high = clean_pivot(df_high, 'high')
    df_low = clean_pivot(df_low, 'low')
    df_close = clean_pivot(df_close, 'close')
    
    m_df = pd.merge(df_open, df_high, on='Time', how='inner')
    m_df = pd.merge(m_df, df_low, on='Time', how='inner')
    m_df = pd.merge(m_df, df_close, on='Time', how='inner')
    m_df = pd.merge(m_df, df_spot, on='Time', how='inner')
    
    m_df['Straddle_Open'] = m_df['CE_open'] + m_df['PE_open']
    m_df['Straddle_High'] = m_df['CE_high'] + m_df['PE_high']
    m_df['Straddle_Low'] = m_df['CE_low'] + m_df['PE_low']
    m_df['Straddle_Close'] = m_df['CE_close'] + m_df['PE_close']
    
    m_df.rename(columns={'CE_close': 'CE', 'PE_close': 'PE', 'Straddle_Close': 'Straddle'}, inplace=True)
    m_df['Synthetic_Future'] = selected_strike + (m_df['CE'] - m_df['PE'])
    
    # Resample if timeframe is 3m or 5m
    if timeframe in ["3m", "5m"]:
        freq_map = {"3m": "3min", "5m": "5min"}
        freq = freq_map[timeframe]
        
        m_df['dt'] = pd.to_datetime(date_str + ' ' + m_df['Time'])
        m_df = m_df.set_index('dt')
        
        m_df_resampled = m_df.resample(freq).agg({
            'Straddle_Open': 'first',
            'Straddle_High': 'max',
            'Straddle_Low': 'min',
            'Straddle': 'last',
            'CE_open': 'first',
            'CE_high': 'max',
            'CE_low': 'min',
            'CE': 'last',
            'PE_open': 'first',
            'PE_high': 'max',
            'PE_low': 'min',
            'PE': 'last',
            'spot': 'last',
            'Synthetic_Future': 'last'
        }).dropna().reset_index()
        
        m_df_resampled['Time'] = m_df_resampled['dt'].dt.strftime('%H:%M')
        m_df = m_df_resampled
        
    return m_df, strikes, atm_strike

# --- Sidebar Controls ---
st.sidebar.markdown("### Settings")
available_dates = get_available_dates()

if not available_dates:
    st.error("No nifty option CSV data files found in the parent directory Data folder.")
    st.stop()

selected_date = st.sidebar.selectbox("Select Date", options=available_dates)
date_obj = datetime.strptime(selected_date, "%Y-%m-%d")
day_name = date_obj.strftime("%A")

st.sidebar.caption(f"🗓️ **Day of Week:** **{day_name}**")

timeframe = st.sidebar.radio("Timeframe (X-Axis)", options=["1m", "3m", "5m"], index=0, horizontal=True)
chart_style = st.sidebar.selectbox("Straddle Style", options=["Line Chart", "Candlestick Chart"])
underlying_overlay = st.sidebar.multiselect(
    "Underlying Overlay (Right Y-Axis)",
    options=["Nifty 50 Spot", "Synthetic Future"],
    default=["Nifty 50 Spot", "Synthetic Future"]
)

# Initial load to get strikes
df_straddle, strikes, atm_strike = load_straddle_data(selected_date, timeframe=timeframe)

if df_straddle is not None and strikes:
    selected_strike = st.sidebar.selectbox(
        "Select Strike Price",
        options=strikes,
        index=strikes.index(atm_strike) if atm_strike in strikes else 0
    )
    
    # Reload if different strike or timeframe
    df_straddle, _, _ = load_straddle_data(selected_date, selected_strike, timeframe=timeframe)
        
    st.sidebar.markdown("---")
    st.sidebar.markdown(f"📅 **Date:** `{selected_date}` (**{day_name}**)")
    st.sidebar.markdown(f"🎯 **ATM Strike:** `{atm_strike}`")
    st.sidebar.markdown(f"📌 **Selected Strike:** `{selected_strike}`")
    st.sidebar.markdown(f"⏱️ **Candle Timeframe:** `{timeframe}`")
    
    if df_straddle is not None and not df_straddle.empty:
        # Calculate clean tick spacing for x-axis
        total_bars = len(df_straddle)
        step = max(1, total_bars // 15)
        tick_vals = df_straddle['Time'].iloc[::step].tolist()
        if df_straddle['Time'].iloc[-1] not in tick_vals:
            tick_vals.append(df_straddle['Time'].iloc[-1])

        # Chart 1: Straddle and Nifty Spot / Synthetic Future
        fig_straddle = make_subplots(specs=[[{"secondary_y": True}]])
        
        # Plot Straddle based on visual style
        if chart_style == "Line Chart":
            fig_straddle.add_trace(
                go.Scatter(
                    x=df_straddle['Time'],
                    y=df_straddle['Straddle'],
                    name="Straddle (CE+PE)",
                    line=dict(color="#FF1744", width=3)
                ),
                secondary_y=False,
            )
        else:
            fig_straddle.add_trace(
                go.Candlestick(
                    x=df_straddle['Time'],
                    open=df_straddle['Straddle_Open'],
                    high=df_straddle['Straddle_High'],
                    low=df_straddle['Straddle_Low'],
                    close=df_straddle['Straddle'],
                    name="Straddle Candlestick",
                    increasing_line_color='#26a69a',
                    decreasing_line_color='#ef5350'
                ),
                secondary_y=False,
            )
            
        # Nifty 50 Spot Overlay
        if "Nifty 50 Spot" in underlying_overlay:
            fig_straddle.add_trace(
                go.Scatter(
                    x=df_straddle['Time'],
                    y=df_straddle['spot'],
                    name="Nifty 50 Spot",
                    line=dict(color="#2196F3", width=2)
                ),
                secondary_y=True,
            )

        # Synthetic Future Overlay (Strike + CE - PE)
        if "Synthetic Future" in underlying_overlay:
            fig_straddle.add_trace(
                go.Scatter(
                    x=df_straddle['Time'],
                    y=df_straddle['Synthetic_Future'],
                    name="Synthetic Future (K + CE - PE)",
                    line=dict(color="#FFD600", width=2, dash="dash")
                ),
                secondary_y=True,
            )
        
        fig_straddle.update_layout(
            title_text=f"Intraday Straddle ({timeframe}) vs Underlying for {selected_date} ({day_name}) (Strike: {selected_strike})",
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#E0E0E0",
            xaxis=dict(
                showgrid=False,
                rangeslider=dict(visible=False),
                type='category',
                tickmode='array',
                tickvals=tick_vals,
                tickangle=0
            ),
            yaxis=dict(title="Straddle Price (₹)", showgrid=True, gridcolor='rgba(255,255,255,0.1)'),
            yaxis2=dict(title="Underlying Price (Spot / Synthetic)", showgrid=False),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        
        st.plotly_chart(fig_straddle, use_container_width=True)
        
        # Chart 2: PE Close, CE Close
        fig_legs = go.Figure()
        
        # CE Close Line
        fig_legs.add_trace(
            go.Scatter(
                x=df_straddle['Time'],
                y=df_straddle['CE'],
                name="CE Close",
                line=dict(color="#00E5FF", width=2)
            )
        )
        
        # PE Close Line
        fig_legs.add_trace(
            go.Scatter(
                x=df_straddle['Time'],
                y=df_straddle['PE'],
                name="PE Close",
                line=dict(color="#FF9100", width=2)
            )
        )
        
        fig_legs.update_layout(
            title_text=f"Intraday Option Leg Prices ({timeframe} CE vs PE) for {selected_date} ({day_name}) (Strike: {selected_strike})",
            plot_bgcolor="rgba(0,0,0,0)",
            paper_bgcolor="rgba(0,0,0,0)",
            font_color="#E0E0E0",
            xaxis=dict(
                showgrid=False,
                type='category',
                tickmode='array',
                tickvals=tick_vals,
                tickangle=0
            ),
            yaxis=dict(title="Option Price (₹)", showgrid=True, gridcolor='rgba(255,255,255,0.1)'),
            legend=dict(orientation="h", yanchor="bottom", y=1.02, xanchor="right", x=1)
        )
        
        st.plotly_chart(fig_legs, use_container_width=True)
        
        # --- Metrics Cards ---
        st.markdown("### Performance Statistics")
        open_val = df_straddle['Straddle'].iloc[0]
        close_val = df_straddle['Straddle'].iloc[-1]
        decay_pts = open_val - close_val
        decay_pct = (decay_pts / open_val) * 100 if open_val > 0 else 0
        max_val = df_straddle['Straddle'].max()
        min_val = df_straddle['Straddle'].min()
        
        def render_metric(title, value, prefix="", suffix="", delta=None, delta_suffix=""):
            color_class = "negative" if isinstance(delta, (int, float)) and delta < 0 else ""
            formatted_val = f"{prefix}{value:,.2f}{suffix}" if isinstance(value, (int, float)) else f"{prefix}{value}{suffix}"
            
            delta_html = ""
            if delta is not None:
                delta_color = "#FF5252" if delta < 0 else "#00E676"
                arrow = "▼" if delta < 0 else "▲"
                delta_html = f"<div style='font-size: 14px; color: {delta_color}; margin-top: 5px;'>{arrow} {abs(delta):,.2f}{delta_suffix}</div>"
                
            st.markdown(f"""
            <div class="metric-card">
                <div class="metric-title">{title}</div>
                <div class="metric-value {color_class}">{formatted_val}</div>
                {delta_html}
            </div>
            """, unsafe_allow_html=True)
            
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            render_metric("Open Straddle Value", open_val, prefix="₹ ")
        with col2:
            render_metric("Close Straddle Value", close_val, prefix="₹ ")
        with col3:
            render_metric("Total Decay (Premium Melt)", decay_pts, prefix="₹ ", delta=decay_pct, delta_suffix="%")
        with col4:
            render_metric("Daily Straddle Range", f"₹ {min_val:.2f} - ₹ {max_val:.2f}")
            
    else:
        st.error("No option records found for the selected strike.")
else:
    st.error("Could not load option strikes for the selected day.")
