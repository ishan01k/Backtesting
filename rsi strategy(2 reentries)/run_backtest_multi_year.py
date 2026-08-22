#!/usr/bin/env python3
"""
Nifty 50 - Bollinger Band Options Selling Strategy Backtest (All Days, Multi-Year)
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
import matplotlib.pyplot as plt

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--capital",    type=float, default=500_000)
    p.add_argument("--period",     type=int,   default=20)
    p.add_argument("--std",        type=float, default=2.0)
    p.add_argument("--lots",       type=int,   default=1)
    p.add_argument("--lot-size",   type=int,   default=65, dest="lot_size")
    p.add_argument("--settle",     default="15:15")
    p.add_argument("--start-time", default="09:45:00")
    p.add_argument("--max-entries", type=int,  default=3)
    p.add_argument("--settlement-ceiling", type=int, default=50, dest="settlement_ceiling",
                   help="Exit when spot moves this many points in-favour of the short. 0 = disabled.")
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

def get_actual_nifty(spot_prices):
    candles = []
    for t in sorted(spot_prices.keys()):
        candles.append({"time": t, "close": spot_prices[t]})
    return candles

def compute_bb(closes, period, mult):
    out = []
    for i in range(len(closes)):
        if i < period - 1:
            out.append({"mid": None, "upper": None, "lower": None})
            continue
        sl   = closes[i - period + 1: i + 1]
        mean = sum(sl) / period
        std  = math.sqrt(sum((x - mean) ** 2 for x in sl) / period)
        out.append({
            "mid":   round(mean, 2),
            "upper": round(mean + mult * std, 2),
            "lower": round(mean - mult * std, 2),
        })
    return out

def compute_rsi(closes, period=2):
    out = [None] * len(closes)
    if len(closes) <= period: return out
    
    gains = [closes[i] - closes[i-1] if closes[i] > closes[i-1] else 0 for i in range(1, period+1)]
    losses = [closes[i-1] - closes[i] if closes[i] < closes[i-1] else 0 for i in range(1, period+1)]
    
    avg_gain = sum(gains) / period
    avg_loss = sum(losses) / period
    
    if avg_loss == 0:
        out[period] = 100.0 if avg_gain > 0 else None
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))
        
    for i in range(period + 1, len(closes)):
        change = closes[i] - closes[i-1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            out[i] = 100.0 if avg_gain > 0 else 50.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
            
    return out

def nearest_strike(spot):
    return int(round(spot / 50) * 50)

def get_price(prices, t, strike):
    row = prices.get(t, {})
    for d in [0, 50, -50, 100, -100]:
        v = row.get(strike + d)
        if v and v > 0:
            return round(v, 2)
    return None

def run_strategy(candles, bands, rsi_vals, ce_prices, pe_prices,
                 starting_capital, lots, lot_size, settle_hhmm, start_hhmmss, max_entries,
                 settlement_ceiling=50):
    """
    One CE sell + One PE sell per day active at a time.
    Entries only happen on or after `start_hhmmss`.
    Allows multiple trades per day per leg if stop loss hits.
    Includes Stop Loss based on the opposite Bollinger Band at entry time.
    Waits for RSI(2) < 25 to sell PE, and RSI(2) > 75 to sell CE after BB triggers.
    Settlement Ceiling: exit when spot moves `settlement_ceiling` pts in-favour
      (CE: spot drops below entry_spot - ceiling | PE: spot rises above entry_spot + ceiling).
      Set settlement_ceiling=0 to disable.
    """
    qty         = lots * lot_size
    settle_full = settle_hhmm + ":00"

    ce_trade = None
    pe_trade = None
    ce_entry_count = 0
    pe_entry_count = 0
    
    waiting_for_pe_rsi = False
    waiting_for_ce_rsi = False

    capital  = starting_capital
    trades   = []

    for i in range(1, len(candles)):
        t     = candles[i]["time"]
        close = candles[i]["close"]
        B     = bands[i]
        curr_rsi = rsi_vals[i]

        if B["mid"] is None:
            continue

        # ── 1. Settlement: close both legs at 15:15 ──
        if t >= settle_full:
            for trade, piv, label in [
                (ce_trade, ce_prices, "CE"),
                (pe_trade, pe_prices, "PE"),
            ]:
                if trade:
                    ep  = get_price(piv, t, trade["strike"]) or trade["sell_price"]
                    pnl = round((trade["sell_price"] - ep) * qty, 2)
                    capital = round(capital + pnl, 2)
                    trades.append({**trade,
                        "type": label,
                        "exit_time": t, "exit_price": ep,
                        "exit_reason": "Settlement 15:15",
                        "pnl": pnl, "capital_after": capital,
                    })

            ce_trade = pe_trade = None
            waiting_for_pe_rsi = False
            waiting_for_ce_rsi = False
            continue

        # ── 2. Check Stop Loss for active CE trade (Short Call) ──
        if ce_trade and close >= ce_trade["sl_spot"]:
            ep = get_price(ce_prices, t, ce_trade["strike"]) or ce_trade["sell_price"]
            pnl = round((ce_trade["sell_price"] - ep) * qty, 2)
            capital = round(capital + pnl, 2)
            trades.append({**ce_trade,
                "type": "CE",
                "exit_time": t, "exit_price": ep,
                "exit_reason": f"SL Hit ({close:.1f} >= {ce_trade['sl_spot']:.1f})",
                "pnl": pnl, "capital_after": capital,
            })
            ce_trade = None

        # ── 2b. Settlement Ceiling for active CE trade (spot drops in-favour) ──
        if ce_trade and settlement_ceiling > 0 and close <= ce_trade["entry_spot"] - settlement_ceiling:
            opt_price = get_price(ce_prices, t, ce_trade["strike"])
            if opt_price is not None and opt_price < ce_trade["sell_price"]:
                ep = opt_price
                pnl = round((ce_trade["sell_price"] - ep) * qty, 2)
                capital = round(capital + pnl, 2)
                trades.append({**ce_trade,
                    "type": "CE",
                    "exit_time": t, "exit_price": ep,
                    "exit_reason": f"Ceiling Hit (spot {close:.1f} <= entry {ce_trade['entry_spot']:.1f} - {settlement_ceiling} & option {opt_price:.2f} < {ce_trade['sell_price']:.2f})",
                    "pnl": pnl, "capital_after": capital,
                })
                ce_trade = None

        # ── 3. Check Stop Loss for active PE trade (Short Put) ──
        if pe_trade and close <= pe_trade["sl_spot"]:
            ep = get_price(pe_prices, t, pe_trade["strike"]) or pe_trade["sell_price"]
            pnl = round((pe_trade["sell_price"] - ep) * qty, 2)
            capital = round(capital + pnl, 2)
            trades.append({**pe_trade,
                "type": "PE",
                "exit_time": t, "exit_price": ep,
                "exit_reason": f"SL Hit ({close:.1f} <= {pe_trade['sl_spot']:.1f})",
                "pnl": pnl, "capital_after": capital,
            })
            pe_trade = None

        # ── 3b. Settlement Ceiling for active PE trade (spot rises in-favour) ──
        if pe_trade and settlement_ceiling > 0 and close >= pe_trade["entry_spot"] + settlement_ceiling:
            opt_price = get_price(pe_prices, t, pe_trade["strike"])
            if opt_price is not None and opt_price < pe_trade["sell_price"]:
                ep = opt_price
                pnl = round((pe_trade["sell_price"] - ep) * qty, 2)
                capital = round(capital + pnl, 2)
                trades.append({**pe_trade,
                    "type": "PE",
                    "exit_time": t, "exit_price": ep,
                    "exit_reason": f"Ceiling Hit (spot {close:.1f} >= entry {pe_trade['entry_spot']:.1f} + {settlement_ceiling} & option {opt_price:.2f} < {pe_trade['sell_price']:.2f})",
                    "pnl": pnl, "capital_after": capital,
                })
                pe_trade = None
                
        if t < start_hhmmss:
            continue

        strike = nearest_strike(close)

        # ── 4. Upper BB Trigger → Wait for RSI < 25 ──
        if close >= B["upper"] and pe_trade is None and pe_entry_count < max_entries:
            waiting_for_pe_rsi = True
            
        if waiting_for_pe_rsi and curr_rsi is not None and curr_rsi < 25:
            sp = get_price(pe_prices, t, strike)
            if sp and sp > 0.5:
                pe_trade = {"strike": strike, "sell_price": sp, "entry_time": t,
                            "sl_spot": B["lower"], "entry_spot": close}
                pe_entry_count += 1
                waiting_for_pe_rsi = False

        # ── 5. Lower BB Trigger → Wait for RSI > 75 ──
        if close <= B["lower"] and ce_trade is None and ce_entry_count < max_entries:
            waiting_for_ce_rsi = True
            
        if waiting_for_ce_rsi and curr_rsi is not None and curr_rsi > 75:
            sp = get_price(ce_prices, t, strike)
            if sp and sp > 0.5:
                ce_trade = {"strike": strike, "sell_price": sp, "entry_time": t,
                            "sl_spot": B["upper"], "entry_spot": close}
                ce_entry_count += 1
                waiting_for_ce_rsi = False

    # Force-close anything still open at very last candle
    for trade, piv, label in [
        (ce_trade, ce_prices, "CE"),
        (pe_trade, pe_prices, "PE"),
    ]:
        if trade:
            last_t   = candles[-1]["time"] if candles else "15:30"
            ep       = get_price(piv, last_t, trade["strike"]) or trade["sell_price"]
            pnl      = round((trade["sell_price"] - ep) * qty, 2)
            capital  = round(capital + pnl, 2)
            trades.append({**trade,
                "type": label,
                "exit_time": last_t, "exit_price": ep,
                "exit_reason": "EOD Close",
                "pnl": pnl, "capital_after": capital,
            })

    return trades, capital

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
    # Data folder lives one level up (backtests root); fall back to local copy if present
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
    
    print(f"Starting Capital: ₹{capital:,.2f}")
    
    for idx, (dt, path) in enumerate(files):
        if idx % 50 == 0:
            print(f"Processing day {idx+1}/{len(files)}: {dt} ...")
            
        ce, pe, spot_prices = load_csv(path)
        candles = get_actual_nifty(spot_prices)
        
        if not candles:
            print(f"Warning: No valid candles found for {dt}, skipping.")
            continue
            
        closes = [c["close"] for c in candles]
        bands = compute_bb(closes, args.period, args.std)
        rsi_vals = compute_rsi(closes, period=2)
        
        start_cap = capital
        trades, capital = run_strategy(
            candles, bands, rsi_vals, ce, pe,
            capital, args.lots, args.lot_size, args.settle, args.start_time, args.max_entries,
            settlement_ceiling=args.settlement_ceiling
        )
        
        qty = args.lots * args.lot_size
        for tr in trades:
            all_trade_records.append({
                "Date": dt.strftime('%Y-%m-%d'),
                "Leg": tr["type"],
                "Entry_Time": tr["entry_time"],
                "Strike": tr["strike"],
                "Sell_Price": tr["sell_price"],
                "Exit_Time": tr["exit_time"],
                "Exit_Price": tr["exit_price"],
                "Qty": qty,
                "Premium_Collected": round(tr["sell_price"] * qty, 2),
                "PnL": tr["pnl"],
                "Reason": tr["exit_reason"],
                "Capital_After": tr["capital_after"]
            })
        
        ce_pnl = sum(t["pnl"] for t in trades if t["type"] == "CE")
        pe_pnl = sum(t["pnl"] for t in trades if t["type"] == "PE")
        abs_pnl = capital - start_cap
        ret_pct = (abs_pnl / start_cap) * 100 if start_cap > 0 else 0
        
        daily_records.append({
            "Date": dt.strftime('%Y-%m-%d'),
            "Starting_Capital": start_cap,
            "Ending_Capital": capital,
            "CE_PnL": ce_pnl,
            "PE_PnL": pe_pnl,
            "Total_PnL": abs_pnl,
            "Return_Pct": ret_pct,
            "Trades_Count": len(trades)
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
    out_csv = script_dir / "bb_strategy_daily_returns.csv"
    if not df.empty:
        df.to_csv(out_csv, index=False)
        print(f"Saved daily returns to {out_csv}")
    
    # Save Trade-level Details CSV
    df_trades = pd.DataFrame(all_trade_records)
    out_trades_csv = script_dir / "bb_all_trades_details.csv"
    if not df_trades.empty:
        df_trades.to_csv(out_trades_csv, index=False)
        print(f"Saved detailed trade log to {out_trades_csv}")
        
if __name__ == "__main__":
    main()
