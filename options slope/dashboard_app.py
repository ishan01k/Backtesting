import subprocess
import sys
import streamlit as st
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
from pathlib import Path
import viewer_server

# Start options slope visualizer server on port 8503 (avoid conflict with port 8502 of the other strategy)
viewer_server.start_server_in_thread(8503)

st.set_page_config(page_title="Nifty Options Slope Dashboard", layout="wide", initial_sidebar_state="expanded")

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
        color: #4CAF50;
    }
    .metric-value.negative {
        color: #F44336;
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

st.title("📈 Nifty Options Slope Strategy Dashboard")
st.markdown("Analyzing intraday premium decay based on At-The-Money Call/Put premium slope calculated at 09:45 AM.")

@st.cache_data
def load_data():
    script_dir = Path(__file__).parent
    daily_returns_path = script_dir / "slope_strategy_daily_returns.csv"
    trades_details_path = script_dir / "slope_all_trades_details.csv"
    
    df_daily = pd.DataFrame()
    df_trades = pd.DataFrame()
    
    if daily_returns_path.exists():
        df_daily = pd.read_csv(daily_returns_path)
        if not df_daily.empty:
            df_daily['Date'] = pd.to_datetime(df_daily['Date'])
            
    if trades_details_path.exists():
        df_trades = pd.read_csv(trades_details_path)
        if not df_trades.empty:
            df_trades['Date'] = pd.to_datetime(df_trades['Date'])
            
    return df_daily, df_trades

df_daily, df_trades = load_data()

st.sidebar.markdown("### Backtest Settings")
with st.sidebar.form("backtest_form"):
    # Load defaults from existing run if available
    default_cap = float(df_daily['Starting_Capital'].iloc[0]) if not df_daily.empty else 100000.0
    
    form_capital = st.number_input("Starting Capital (₹)", value=default_cap, step=50000.0)
    form_lot_size = st.number_input("Lot Size / Quantity", value=65, step=1)
    form_lots = st.number_input("Number of Lots", value=1, step=1)
    
    st.markdown("---")
    st.markdown("**Risk Management**")
    form_sl = st.slider("Stop Loss (%)", min_value=10, max_value=200, value=25, step=5, help="Option price increase percentage to trigger stop loss")
    form_tp_enabled = st.checkbox("Enable Take Profit (Target)", value=True, help="Exit trade early when target option decay is achieved")
    form_tp = st.slider("Target/Take Profit (%)", min_value=10, max_value=100, value=80, step=5, disabled=not form_tp_enabled, help="Option premium decay percentage to trigger profit taking")
    form_reentries = st.slider("Reentries per Day", min_value=0, max_value=4, value=0, step=1, help="Number of allowed trade reentries on the same day after hitting SL or Target")
    
    st.markdown("---")
    st.markdown("**Strategy Configuration**")
    form_mode = st.selectbox("Strategy Mode", options=["trend", "reversion"], index=0, format_func=lambda x: "Trend-Following (Sell Lower Slope)" if x == "trend" else "Mean-Reversion (Sell Higher Slope)")
    form_slope_method = st.selectbox("Slope Method", options=["linear", "simple", "percent"], index=0, format_func=lambda x: "Linear Regression (y=mx+c)" if x == "linear" else ("Simple Difference" if x == "simple" else "Percentage Change"))
    form_slope_tf = st.selectbox("Slope Timeframe", options=[1, 3, 5], index=0, format_func=lambda x: f"{x} Minute Candles", help="Candle interval used to compute the slope from 09:15 to 09:45 AM")

    run_button = st.form_submit_button("Run Backtest")

if run_button:
    with st.spinner("Running multi-year backtest... This will take about a minute."):
        script_dir = Path(__file__).parent
        cmd = [
            sys.executable, "run_backtest_slope.py",
            "--capital", str(form_capital),
            "--lot-size", str(form_lot_size),
            "--lots", str(form_lots),
            "--sl", str(form_sl / 100.0),
            "--tp", str(form_tp / 100.0),
            "--mode", form_mode,
            "--slope-method", form_slope_method,
            "--reentries", str(form_reentries),
            "--slope-tf", str(form_slope_tf)
        ]
        if not form_tp_enabled:
            cmd.append("--disable-tp")
        subprocess.run(cmd, cwd=script_dir)
        st.cache_data.clear()
        st.rerun()

if df_daily.empty or df_trades.empty:
    st.warning("⚠️ Backtest data not found or empty. Please run the backtest script using the sidebar.")
    st.stop()

# --- Year Selection Filter ---
available_years = sorted(df_daily['Date'].dt.year.unique().tolist())
selected_years = st.sidebar.multiselect("Select Years", options=available_years, default=available_years)

if not selected_years:
    st.warning("⚠️ Please select at least one year to view the data.")
    st.stop()

df_daily = df_daily[df_daily['Date'].dt.year.isin(selected_years)].copy()
df_trades = df_trades[df_trades['Date'].dt.year.isin(selected_years)].copy()

# The actual executed starting capital from the dataset for the selected period
starting_capital = df_daily['Starting_Capital'].iloc[0]

# Recalculate cumulative capital based on the actual PnL for the selected period
df_daily['Ending_Capital'] = starting_capital + df_daily['Total_PnL'].cumsum()
if not df_trades.empty:
    df_trades['Capital_After'] = starting_capital + df_trades['PnL'].cumsum()

# --- Key Metrics ---
st.markdown("### Key Performance Metrics")

final_capital = df_daily['Ending_Capital'].iloc[-1]
total_pnl = final_capital - starting_capital
return_pct = (total_pnl / starting_capital) * 100

total_trades = len(df_trades)
winning_trades = len(df_trades[df_trades['PnL'] > 0])
win_rate = (winning_trades / total_trades) * 100 if total_trades > 0 else 0

avg_win = df_trades[df_trades['PnL'] > 0]['PnL'].mean() if winning_trades > 0 else 0
avg_loss = df_trades[df_trades['PnL'] <= 0]['PnL'].mean() if total_trades - winning_trades > 0 else 0

peak = df_daily['Ending_Capital'].cummax()
drawdown = (peak - df_daily['Ending_Capital']) / peak * 100
max_drawdown = drawdown.max()

total_trading_days = len(df_daily[df_daily['Trades_Count'] > 0])
winning_days = len(df_daily[df_daily['Total_PnL'] > 0])
day_win_rate = (winning_days / total_trading_days) * 100 if total_trading_days > 0 else 0

avg_day_profit = df_daily[df_daily['Total_PnL'] > 0]['Total_PnL'].mean() if winning_days > 0 else 0
avg_day_loss = df_daily[df_daily['Total_PnL'] < 0]['Total_PnL'].mean() if len(df_daily[df_daily['Total_PnL'] < 0]) > 0 else 0

def render_metric(title, value, prefix="", suffix="", is_currency=False):
    color_class = "negative" if isinstance(value, (int, float)) and value < 0 else ""
    formatted_val = f"{prefix}{value:,.2f}{suffix}" if isinstance(value, (int, float)) else f"{prefix}{value}{suffix}"
    if is_currency and isinstance(value, (int, float)):
        formatted_val = f"₹ {value:,.2f}"
    
    st.markdown(f"""
    <div class="metric-card">
        <div class="metric-title">{title}</div>
        <div class="metric-value {color_class}">{formatted_val}</div>
    </div>
    """, unsafe_allow_html=True)

col1, col2, col3, col4 = st.columns(4)
with col1: render_metric("Starting Capital", starting_capital, is_currency=True)
with col2: render_metric("Final Capital", final_capital, is_currency=True)
with col3: render_metric("Total PnL", total_pnl, is_currency=True)
with col4: render_metric("Return (%)", return_pct, suffix="%")

st.markdown("<br>", unsafe_allow_html=True)

col5, col6, col7, col8 = st.columns(4)
with col5: render_metric("Trade Win Rate", win_rate, suffix="%")
with col6: render_metric("Avg Win (Trade)", avg_win, is_currency=True)
with col7: render_metric("Avg Loss (Trade)", avg_loss, is_currency=True)
with col8: render_metric("Max Drawdown", max_drawdown, suffix="%")

st.markdown("<br>", unsafe_allow_html=True)

col9, col10, col11 = st.columns(3)
with col9: render_metric("Day Win Rate", day_win_rate, suffix="%")
with col10: render_metric("Avg Daily Profit", avg_day_profit, is_currency=True)
with col11: render_metric("Avg Daily Loss", avg_day_loss, is_currency=True)

st.markdown("<br><hr><br>", unsafe_allow_html=True)

# --- Year-by-Year Performance Breakdown ---
st.markdown("### 📅 Year-by-Year Performance Breakdown")

if not df_daily.empty:
    df_daily_all, df_trades_all = load_data()
    
    if not df_daily_all.empty:
        df_daily_all['Date'] = pd.to_datetime(df_daily_all['Date'])
        if not df_trades_all.empty:
            df_trades_all['Date'] = pd.to_datetime(df_trades_all['Date'])
            
        all_years = sorted(df_daily_all['Date'].dt.year.unique())
        yearly_records = []
        
        for y in all_years:
            df_y = df_daily_all[df_daily_all['Date'].dt.year == y].sort_values('Date')
            df_t_y = df_trades_all[df_trades_all['Date'].dt.year == y].sort_values('Date') if not df_trades_all.empty else pd.DataFrame()
            
            if df_y.empty:
                continue
                
            start_cap = df_y['Starting_Capital'].iloc[0]
            pnl = df_y['Total_PnL'].sum()
            end_cap = start_cap + pnl
            ret_pct = (pnl / start_cap) * 100 if start_cap > 0 else 0.0
            
            total_trades = len(df_t_y)
            win_trades = len(df_t_y[df_t_y['PnL'] > 0]) if not df_t_y.empty else 0
            win_rate = (win_trades / total_trades) * 100 if total_trades > 0 else 0.0
            
            cap_curve = start_cap + df_y['Total_PnL'].cumsum()
            peak = cap_curve.cummax()
            drawdown = (peak - cap_curve) / peak * 100
            max_dd = drawdown.max()
            
            trading_days = len(df_y[df_y['Trades_Count'] > 0])
            win_days = len(df_y[df_y['Total_PnL'] > 0])
            day_win_rate = (win_days / trading_days) * 100 if trading_days > 0 else 0.0
            
            yearly_records.append({
                "Year": int(y),
                "Starting Capital": start_cap,
                "Ending Capital": end_cap,
                "Total PnL": pnl,
                "Return (%)": ret_pct,
                "Trades Count": total_trades,
                "Win Rate (%)": win_rate,
                "Max Drawdown (%)": max_dd,
                "Daily Win Rate (%)": day_win_rate
            })
            
        df_yearly = pd.DataFrame(yearly_records)
        
        # Display the dataframe with high quality formatting
        st.dataframe(
            df_yearly,
            column_config={
                "Year": st.column_config.NumberColumn("Year", format="%d"),
                "Starting Capital": st.column_config.NumberColumn("Starting Capital", format="₹ %,.2f"),
                "Ending Capital": st.column_config.NumberColumn("Ending Capital", format="₹ %,.2f"),
                "Total PnL": st.column_config.NumberColumn("Total PnL", format="₹ %,.2f"),
                "Return (%)": st.column_config.NumberColumn("Return (%)", format="%.2f%%"),
                "Trades Count": st.column_config.NumberColumn("Trades Count"),
                "Win Rate (%)": st.column_config.NumberColumn("Win Rate (%)", format="%.2f%%"),
                "Max Drawdown (%)": st.column_config.NumberColumn("Max Drawdown (%)", format="%.2f%%"),
                "Daily Win Rate (%)": st.column_config.NumberColumn("Daily Win Rate (%)", format="%.2f%%")
            },
            use_container_width=True,
            hide_index=True
        )
        
        st.markdown("<br>", unsafe_allow_html=True)
        
        col_chrt1, col_chrt2 = st.columns(2)
        with col_chrt1:
            fig_yearly_bar = px.bar(
                df_yearly, 
                x="Year", 
                y="Return (%)", 
                title="Return (%) by Year",
                text=df_yearly["Return (%)"].apply(lambda val: f"{val:.1f}%"),
                color="Return (%)",
                color_continuous_scale=["#FF5252", "#4CAF50"]
            )
            fig_yearly_bar.update_layout(
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font_color="#E0E0E0",
                xaxis=dict(type='category', showgrid=False),
                yaxis=dict(showgrid=True, gridcolor='rgba(255,255,255,0.1)'),
                margin=dict(l=0, r=0, t=30, b=0),
                coloraxis_showscale=False
            )
            fig_yearly_bar.update_traces(textposition='outside')
            st.plotly_chart(fig_yearly_bar, use_container_width=True)
            
        with col_chrt2:
            fig_dd_bar = px.bar(
                df_yearly, 
                x="Year", 
                y="Max Drawdown (%)", 
                title="Max Drawdown (%) by Year",
                text=df_yearly["Max Drawdown (%)"].apply(lambda val: f"{val:.1f}%"),
                color="Max Drawdown (%)",
                color_continuous_scale=["#4CAF50", "#FF5252"]
            )
            fig_dd_bar.update_layout(
                plot_bgcolor="rgba(0,0,0,0)",
                paper_bgcolor="rgba(0,0,0,0)",
                font_color="#E0E0E0",
                xaxis=dict(type='category', showgrid=False),
                yaxis=dict(showgrid=True, gridcolor='rgba(255,255,255,0.1)'),
                margin=dict(l=0, r=0, t=30, b=0),
                coloraxis_showscale=False
            )
            fig_dd_bar.update_traces(textposition='outside')
            st.plotly_chart(fig_dd_bar, use_container_width=True)

st.markdown("<br><hr><br>", unsafe_allow_html=True)

# --- Charts ---
st.markdown("### Strategy Equity Curve")

fig_equity = px.area(df_daily, x='Date', y='Ending_Capital', 
                     color_discrete_sequence=['#00C853'],
                     title="Cumulative Equity Over Time")
fig_equity.update_layout(
    plot_bgcolor="rgba(0,0,0,0)",
    paper_bgcolor="rgba(0,0,0,0)",
    font_color="#E0E0E0",
    xaxis=dict(showgrid=False),
    yaxis=dict(showgrid=True, gridcolor='rgba(255,255,255,0.1)'),
    margin=dict(l=0, r=0, t=40, b=0)
)
fig_equity.update_traces(fillcolor='rgba(0, 200, 83, 0.2)', line=dict(width=2))
st.plotly_chart(fig_equity, use_container_width=True)

col_chart1, col_chart2 = st.columns(2)

with col_chart1:
    st.markdown("### Drawdown (%)")
    df_dd = pd.DataFrame({'Date': df_daily['Date'], 'Drawdown': drawdown})
    fig_dd = px.line(df_dd, x='Date', y='Drawdown', 
                     color_discrete_sequence=['#F44336'])
    fig_dd.update_layout(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font_color="#E0E0E0",
        yaxis=dict(autorange="reversed", showgrid=True, gridcolor='rgba(255,255,255,0.1)'),
        margin=dict(l=0, r=0, t=30, b=0)
    )
    fig_dd.update_traces(fill='tozeroy', fillcolor='rgba(244, 67, 54, 0.2)')
    st.plotly_chart(fig_dd, use_container_width=True)

with col_chart2:
    st.markdown("### PnL by Leg (CE vs PE)")
    pnl_summary = pd.DataFrame({
        'Leg': ['CE', 'PE'],
        'PnL': [df_daily['CE_PnL'].sum(), df_daily['PE_PnL'].sum()]
    })
    fig_bar = px.bar(pnl_summary, x='Leg', y='PnL', color='Leg',
                     color_discrete_map={'CE': '#2196F3', 'PE': '#FF9800'})
    fig_bar.update_layout(
        plot_bgcolor="rgba(0,0,0,0)",
        paper_bgcolor="rgba(0,0,0,0)",
        font_color="#E0E0E0",
        showlegend=False,
        margin=dict(l=0, r=0, t=30, b=0)
    )
    st.plotly_chart(fig_bar, use_container_width=True)

st.markdown("<hr>", unsafe_allow_html=True)

# --- Trade Log ---
st.markdown("### Detailed Trade Log")
st.dataframe(df_trades.sort_values('Date', ascending=False), use_container_width=True, height=400)

# --- Daily Trade Log ---
st.markdown("### Daily Returns Log & Visual Inspector")
df_daily_display = df_daily.copy()
# Generate visual inspection links pointing to the background visualizer server on port 8503
df_daily_display['Inspect'] = "http://localhost:8503/?day=" + df_daily_display['Date'].dt.strftime('%Y-%m-%d')
# Convert date to string for clean layout display
df_daily_display['Date'] = df_daily_display['Date'].dt.strftime('%Y-%m-%d')

st.dataframe(
    df_daily_display.sort_values('Date', ascending=False),
    column_config={
        "Inspect": st.column_config.LinkColumn(
            "Inspect Day",
            help="Click to open the options chain and TradingView chart for this day",
            display_text="🔍 Open Chart"
        ),
        "Starting_Capital": st.column_config.NumberColumn("Starting Capital (₹)", format="₹ %.2f"),
        "Ending_Capital": st.column_config.NumberColumn("Ending Capital (₹)", format="₹ %.2f"),
        "CE_PnL": st.column_config.NumberColumn("CE PnL (₹)", format="₹ %.2f"),
        "PE_PnL": st.column_config.NumberColumn("PE PnL (₹)", format="₹ %.2f"),
        "Total_PnL": st.column_config.NumberColumn("Total PnL (₹)", format="₹ %.2f"),
        "Return_Pct": st.column_config.NumberColumn("Return (%)", format="%.2f%%"),
        "Trades_Count": st.column_config.NumberColumn("Trades Count"),
        "Slope_CE": st.column_config.NumberColumn("Slope CE", format="%.4f"),
        "Slope_PE": st.column_config.NumberColumn("Slope PE", format="%.4f"),
        "Strike": st.column_config.NumberColumn("ATM Strike")
    },
    use_container_width=True,
    height=400
)
