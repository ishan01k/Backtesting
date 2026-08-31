#!/usr/bin/env python3
"""
Nifty 50 At-The-Money (ATM) Options Slope Selling Strategy Backtest
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import datetime
from collections import defaultdict
from pathlib import Path
import pandas as pd

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--capital",      type=float, default=100000.0)
    p.add_argument("--lots",         type=int,   default=1)
    p.add_argument("--lot-size",     type=int,   default=65, dest="lot_size") # Nifty standard lot size
    p.add_argument("--sl",           type=float, default=0.25, help="Stop Loss percentage on premium (e.g. 0.25 = 25% increase)")
    p.add_argument("--tp",           type=float, default=0.80, help="Take Profit percentage on premium (e.g. 0.80 = 80% decay)")
    p.add_argument("--disable-tp",   action="store_true", help="Disable Take Profit exit")
    p.add_argument("--mode",         choices=["trend", "reversion"], default="trend", help="Strategy mode: trend (sell lower slope) or reversion (sell higher slope)")
    p.add_argument("--slope-method", choices=["linear", "simple", "percent"], default="linear", help="Slope calculation method")
    p.add_argument("--settle",       default="15:15", help="Time of day to settle remaining trades (HH:MM)")
    p.add_argument("--reentries",    type=int,   default=0, help="Number of allowed trade reentries on the same day (0 to 4)")
    p.add_argument("--slope-tf",     type=int,   choices=[1, 3, 5], default=1, help="Slope calculation timeframe (1, 3, or 5 minutes)")
    return p.parse_args()

def load_csv(path):
    ce_prices, pe_prices, spot_prices = defaultdict(dict), defaultdict(dict), {}
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            t = ""
            if "datetime" in row and row["datetime"]:
                parts = row["datetime"].split(" ")
                if len(parts) > 1:
                    t = parts[1][:5]
            if not t:
                t = row.get("time", "").strip()
            if len(t) > 5 and ":" in t:
                t = t[:5]
                
            try:
                strike = int(float(row.get("api_strike", 0)))
                otype = row.get("option_type", "").strip().upper()
                price = float(row.get("close", 0))
                spot = float(row.get("spot", 0))
            except (ValueError, KeyError):
                continue
            
            if spot > 0 and t not in spot_prices:
                spot_prices[t] = spot
            
            if otype in ("CALL", "CE"):
                ce_prices[t][strike] = price
            elif otype in ("PUT", "PE"):
                pe_prices[t][strike] = price
    return ce_prices, pe_prices, spot_prices

def get_price(prices, t, strike):
    row = prices.get(t, {})
    # Search for strike, or nearest available strike (with small offset check)
    for d in [0, 50, -50, 100, -100]:
        v = row.get(strike + d)
        if v and v > 0:
            return round(v, 2)
    return None

def nearest_strike(spot):
    return int(round(spot / 50) * 50)

def generate_time_list(start_str, end_str):
    """Generate HH:MM strings from start to end (inclusive)"""
    start_dt = datetime.datetime.strptime(start_str, "%H:%M")
    end_dt = datetime.datetime.strptime(end_str, "%H:%M")
    curr = start_dt
    times = []
    while curr <= end_dt:
        times.append(curr.strftime("%H:%M"))
        curr += datetime.timedelta(minutes=1)
    return times

def get_filled_series(prices_dict, times, strike):
    series = []
    last_val = None
    for t in times:
        val = get_price(prices_dict, t, strike)
        if val is not None:
            last_val = val
        series.append(last_val)
        
    # Lead-fill first valid value backward if start of series has missing data
    first_valid = None
    for v in series:
        if v is not None:
            first_valid = v
            break
            
    if first_valid is not None:
        series = [v if v is not None else first_valid for v in series]
    return series

def compute_slope(prices, method="linear"):
    N = len(prices)
    if N < 2 or any(p is None for p in prices):
        return 0.0
        
    if method == "simple":
        return prices[-1] - prices[0]
    elif method == "percent":
        return (prices[-1] - prices[0]) / prices[0] if prices[0] > 0 else 0.0
    else: # linear regression slope (returns coefficient m of y = mx + c)
        x = list(range(N))
        mean_x = sum(x) / N
        mean_y = sum(prices) / N
        numerator = sum((xi - mean_x) * (yi - mean_y) for xi, yi in zip(x, prices))
        denominator = sum((xi - mean_x) ** 2 for xi in x)
        return numerator / denominator if denominator != 0 else 0.0

def get_sorted_files(data_dir):
    files = []
    pattern = re.compile(r'nifty_options_(\d{4})-(\d{2})-(\d{2})\.csv')
    for p in Path(data_dir).rglob('nifty_options_*.csv'):
        m = pattern.search(p.name)
        if m:
            y, m_month, d = int(m.group(1)), int(m.group(2)), int(m.group(3))
            dt = datetime.date(y, m_month, d)
            files.append((dt, p))
    files.sort(key=lambda x: x[0])
    return files

def main():
    args = parse_args()
    script_dir = Path(__file__).parent
    
    # Locate Data folder (lives at backtests root)
    data_dir = script_dir.parent / "Data"
    if not data_dir.exists():
        data_dir = script_dir / "Data"

    if not data_dir.exists():
        print(f"ERROR: {data_dir} not found")
        sys.exit(1)

    files = get_sorted_files(data_dir)
    print(f"Found {len(files)} daily data files.")
    
    daily_records = []
    all_trade_records = []
    capital = args.capital
    qty = args.lots * args.lot_size
    
    print(f"Starting Capital: ₹{capital:,.2f}")
    print(f"Lots: {args.lots}, Lot Size: {args.lot_size}, Total Qty: {qty}")
    tp_str = "Disabled" if args.disable_tp else f"{args.tp*100:.1f}%"
    print(f"Settings: SL={args.sl*100:.1f}%, TP={tp_str}, Mode={args.mode}, Method={args.slope_method}, Slope TF={args.slope_tf}m")
    
    times_915_945 = generate_time_list("09:15", "09:45")
    
    for idx, (dt, path) in enumerate(files):
        if idx % 50 == 0:
            print(f"Processing day {idx+1}/{len(files)}: {dt} ...")
            
        ce, pe, spot_prices = load_csv(path)
        
        # Verify we have data at 09:45
        spot_945 = spot_prices.get("09:45")
        if not spot_945:
            # Look for closest spot in spot_prices around 09:45
            valid_times = [t for t in spot_prices.keys() if t <= "09:45"]
            if valid_times:
                closest_t = max(valid_times)
                spot_945 = spot_prices[closest_t]
            else:
                continue
                
        strike = nearest_strike(spot_945)
        
        # Get filled premium series from 09:15 to 09:45
        ce_series = get_filled_series(ce, times_915_945, strike)
        pe_series = get_filled_series(pe, times_915_945, strike)
        
        # Ensure we have valid prices
        if not ce_series or not pe_series or ce_series[-1] is None or pe_series[-1] is None:
            continue
            
        # Compute slopes
        slope_ce = compute_slope(ce_series[::args.slope_tf], args.slope_method)
        slope_pe = compute_slope(pe_series[::args.slope_tf], args.slope_method)
        
        # Decide which one to sell
        # Trend mode: sell lower (more negative/declining) slope
        # Reversion mode: sell higher (more positive/increasing) slope
        sell_ce = False
        if args.mode == "trend":
            if slope_ce < slope_pe:
                sell_ce = True
        else: # reversion
            if slope_ce > slope_pe:
                sell_ce = True
                
        sold_leg = "CE" if sell_ce else "PE"
        entry_price = ce_series[-1] if sell_ce else pe_series[-1]
        active_prices = ce if sell_ce else pe
        
        # Position tracking with reentries
        day_trades = []
        entries_allowed = 1 + args.reentries
        entry_count = 0
        
        curr_entry_time = "09:45"
        curr_entry_price = ce_series[-1] if sell_ce else pe_series[-1]
        
        monitoring_times = generate_time_list("09:46", args.settle)
        start_monitor_idx = 0
        day_start_cap = capital
        
        while entry_count < entries_allowed:
            entry_count += 1
            
            sl_level = curr_entry_price * (1.0 + args.sl)
            tp_level = curr_entry_price * (1.0 - args.tp)
            
            trade_exited = False
            exit_time = args.settle
            exit_price = curr_entry_price
            exit_reason = "Settlement 15:15"
            
            last_valid_price = curr_entry_price
            
            for idx_t in range(start_monitor_idx, len(monitoring_times)):
                t = monitoring_times[idx_t]
                curr_price = get_price(active_prices, t, strike)
                if curr_price is not None:
                    last_valid_price = curr_price
                    
                # Check Stop Loss (premium increased above threshold)
                if last_valid_price >= sl_level:
                    exit_time = t
                    exit_price = last_valid_price
                    exit_reason = f"SL Hit ({last_valid_price:.2f} >= {sl_level:.2f})"
                    trade_exited = True
                    start_monitor_idx = idx_t + 1
                    break
                    
                # Check Target (premium decayed below threshold)
                if not args.disable_tp and last_valid_price <= tp_level:
                    exit_time = t
                    exit_price = last_valid_price
                    exit_reason = f"Target Hit ({last_valid_price:.2f} <= {tp_level:.2f})"
                    trade_exited = True
                    start_monitor_idx = idx_t + 1
                    break
            else:
                # Exited via Settlement 15:15
                settle_price = get_price(active_prices, args.settle, strike)
                if settle_price is None:
                    settle_price = last_valid_price
                exit_price = settle_price
                trade_exited = False
                
            pnl_val = round((curr_entry_price - exit_price) * qty, 2)
            capital = round(capital + pnl_val, 2)
            
            day_trades.append({
                "entry_time": curr_entry_time,
                "entry_price": curr_entry_price,
                "exit_time": exit_time,
                "exit_price": exit_price,
                "reason": exit_reason,
                "pnl": pnl_val,
                "capital_after": capital
            })
            
            if not trade_exited or entry_count >= entries_allowed or start_monitor_idx >= len(monitoring_times):
                break
                
            # Search for reentry minute and price
            reentry_found = False
            for idx_t in range(start_monitor_idx, len(monitoring_times)):
                t = monitoring_times[idx_t]
                price = get_price(active_prices, t, strike)
                if price is not None:
                    curr_entry_time = t
                    curr_entry_price = price
                    start_monitor_idx = idx_t + 1
                    reentry_found = True
                    break
            
            if not reentry_found:
                break
                
        dt_str = dt.strftime('%Y-%m-%d')
        
        # Append trade details
        for trade in day_trades:
            all_trade_records.append({
                "Date": dt_str,
                "Leg": sold_leg,
                "Entry_Time": trade["entry_time"],
                "Strike": strike,
                "Sell_Price": trade["entry_price"],
                "Exit_Time": trade["exit_time"],
                "Exit_Price": trade["exit_price"],
                "Qty": qty,
                "Premium_Collected": round(trade["entry_price"] * qty, 2),
                "PnL": trade["pnl"],
                "Reason": trade["reason"],
                "Capital_After": trade["capital_after"],
                "Spot_945": spot_945,
                "Slope_CE": round(slope_ce, 4),
                "Slope_PE": round(slope_pe, 4)
            })
            
        day_total_pnl = round(sum(t["pnl"] for t in day_trades), 2)
        day_ce_pnl = day_total_pnl if sold_leg == "CE" else 0.0
        day_pe_pnl = day_total_pnl if sold_leg == "PE" else 0.0
        ret_pct = (day_total_pnl / day_start_cap) * 100 if day_start_cap > 0 else 0
        
        daily_records.append({
            "Date": dt_str,
            "Starting_Capital": day_start_cap,
            "Ending_Capital": capital,
            "CE_PnL": day_ce_pnl,
            "PE_PnL": day_pe_pnl,
            "Total_PnL": day_total_pnl,
            "Return_Pct": ret_pct,
            "Trades_Count": len(day_trades),
            "Sold_Leg": sold_leg,
            "Slope_CE": round(slope_ce, 4),
            "Slope_PE": round(slope_pe, 4),
            "Strike": strike
        })
        
    print(f"Finished processing. Final Capital: ₹{capital:,.2f}")
    
    # Calculate Max Drawdown
    peak = args.capital
    max_dd_pct = 0.0
    for tr in all_trade_records:
        current_cap = tr["Capital_After"]
        if current_cap > peak:
            peak = current_cap
        dd = (peak - current_cap) / peak * 100
        if dd > max_dd_pct:
            max_dd_pct = dd
    print(f"Max Drawdown: {max_dd_pct:.2f}%")
    
    # Save Daily Returns CSV
    df = pd.DataFrame(daily_records)
    out_csv = script_dir / "slope_strategy_daily_returns.csv"
    if not df.empty:
        df.to_csv(out_csv, index=False)
        print(f"Saved daily returns to {out_csv}")
    
    # Save Trade-level Details CSV
    df_trades = pd.DataFrame(all_trade_records)
    out_trades_csv = script_dir / "slope_all_trades_details.csv"
    if not df_trades.empty:
        df_trades.to_csv(out_trades_csv, index=False)
        print(f"Saved detailed trade log to {out_trades_csv}")
        
if __name__ == "__main__":
    main()
