#!/usr/bin/env python3
"""
Nifty 50 - Bollinger Band Options Selling Strategy Backtest
============================================================
Usage:
    python3 backtest_bb.py [options]

Options:
    --capital   Starting capital (default: 500000)
    --period    BB period (default: 20)
    --std       BB std dev multiplier (default: 2.0)
    --lots      Lots per trade (default: 1)
    --lot-size  Lot size (default: 50)
    --settle    Settlement time HH:MM (default: 15:15)
    --port      Server port (default: 8765)
    --no-browser  Skip auto-opening browser

Strategy:
    - At most ONE CE sell and ONE PE sell per day (independent legs)
    - Sell CE on FIRST touch of Lower Bollinger Band  → hold until 15:15
    - Sell PE on FIRST touch of Upper Bollinger Band  → hold until 15:15
    - After first entry, that leg is locked — no further trades on same side
    - Both legs settle at --settle time regardless
    - Nifty spot reconstructed via put-call parity (21700 strike)
"""

import argparse
import csv
import json
import math
import os
import re
import sys
import threading
import time
import webbrowser
from collections import defaultdict
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

PARITY_STRIKE = 21700
STRIKE_RE     = re.compile(r'NIFTY\d+[A-Z]+24(\d+)(CE|PE)$')


# ─────────────────────────────────────────────────────────────────────────────
def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--capital",    type=float, default=500_000)
    p.add_argument("--period",     type=int,   default=20)
    p.add_argument("--std",        type=float, default=2.0)
    p.add_argument("--lots",       type=int,   default=1)
    p.add_argument("--lot-size",   type=int,   default=50, dest="lot_size")
    p.add_argument("--settle",     default="15:15")
    p.add_argument("--port",       type=int,   default=8765)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--max-entries", type=int,  default=3)
    return p.parse_args()


# ─────────────────────────────────────────────────────────────────────────────
def load_csv(path):
    print(f"[1/4] Loading: {path}")
    ce_prices, pe_prices = defaultdict(dict), defaultdict(dict)
    with open(path, newline="") as f:
        for row in csv.DictReader(f):
            m = STRIKE_RE.search(row.get("symbol", "").strip())
            if not m:
                continue
            strike = int(m.group(1))
            otype  = m.group(2)
            t      = row["time"].strip()
            try:
                price = float(row["close"])
            except (ValueError, KeyError):
                continue
            if otype == "CE":
                ce_prices[t][strike] = price
            else:
                pe_prices[t][strike] = price
    return ce_prices, pe_prices


# ─────────────────────────────────────────────────────────────────────────────
def reconstruct_nifty(ce_prices, pe_prices):
    print(f"[2/4] Reconstructing Nifty spot (put-call parity, strike={PARITY_STRIKE})")
    candles = []
    for t in sorted(ce_prices):
        ce = ce_prices[t].get(PARITY_STRIKE)
        pe = pe_prices[t].get(PARITY_STRIKE)
        if ce is None or pe is None:
            continue
        candles.append({"time": t, "close": round(PARITY_STRIKE + ce - pe, 2)})
    closes = [c["close"] for c in candles]
    print(f"    {len(candles)} candles | Nifty {min(closes):.0f}–{max(closes):.0f}")
    return candles


# ─────────────────────────────────────────────────────────────────────────────
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

# ─────────────────────────────────────────────────────────────────────────────
def nearest_strike(spot):
    return int(round(spot / 50) * 50)


def get_price(prices, t, strike):
    row = prices.get(t, {})
    for d in [0, 50, -50, 100, -100]:
        v = row.get(strike + d)
        if v and v > 0:
            return round(v, 2)
    return None


# ─────────────────────────────────────────────────────────────────────────────
def run_strategy(candles, bands, rsi_vals, ce_prices, pe_prices,
                 starting_capital, lots, lot_size, settle_hhmm, max_entries):
    """
    One CE sell + One PE sell active at a time.
    Entered on BB touch and waits for RSI(2) condition, held to settlement or Stop Loss.
    If SL hits, a new trade can be entered later in the same day.
    """
    print("[3/4] Running backtest")

    qty         = lots * lot_size
    settle_full = settle_hhmm + ":00"

    # Two independent legs
    ce_trade = None   # {'strike', 'sell_price', 'entry_time'}
    pe_trade = None
    ce_entry_count = 0
    pe_entry_count = 0
    
    waiting_for_pe_rsi = False
    waiting_for_ce_rsi = False

    capital  = starting_capital
    trades   = []
    equity   = [{"time": candles[0]["time"], "capital": capital}]

    for i in range(1, len(candles)):
        t     = candles[i]["time"]
        close = candles[i]["close"]
        B     = bands[i]
        curr_rsi = rsi_vals[i]

        if B["mid"] is None:
            equity.append({"time": t, "capital": capital})
            continue

        # ── Settlement: close both legs ──
        if t >= settle_full:
            settled_any = False
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
                    settled_any = True

            if settled_any:
                ce_trade = pe_trade = None
                waiting_for_pe_rsi = False
                waiting_for_ce_rsi = False

            equity.append({"time": t, "capital": capital})
            if t > settle_full:
                continue

        strike = nearest_strike(close)

        # ── Check Stop Loss for active CE trade (Short Call) ──
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
            print(f"    CE SL Hit @{t}: exit=₹{ep:.2f}  nifty={close:.1f}")
            ce_trade = None

        # ── Check Stop Loss for active PE trade (Short Put) ──
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
            print(f"    PE SL Hit @{t}: exit=₹{ep:.2f}  nifty={close:.1f}")
            pe_trade = None

        # ── Upper BB Trigger → Wait for RSI < 25 ──
        if close >= B["upper"] and pe_trade is None and pe_entry_count < max_entries:
            waiting_for_pe_rsi = True
            
        if waiting_for_pe_rsi and curr_rsi is not None and curr_rsi < 25:
            sp = get_price(pe_prices, t, strike)
            if sp and sp > 0.5:
                pe_trade = {"strike": strike, "sell_price": sp, "entry_time": t, "sl_spot": B["lower"]}
                pe_entry_count += 1
                waiting_for_pe_rsi = False
                print(f"    PE entry @{t}: strike={strike}  sell=₹{sp:.2f}  nifty={close:.1f}  UB={B['upper']:.1f}")

        # ── Lower BB Trigger → Wait for RSI > 75 ──
        if close <= B["lower"] and ce_trade is None and ce_entry_count < max_entries:
            waiting_for_ce_rsi = True
            
        if waiting_for_ce_rsi and curr_rsi is not None and curr_rsi > 75:
            sp = get_price(ce_prices, t, strike)
            if sp and sp > 0.5:
                ce_trade = {"strike": strike, "sell_price": sp, "entry_time": t, "sl_spot": B["upper"]}
                ce_entry_count += 1
                waiting_for_ce_rsi = False
                print(f"    CE entry @{t}: strike={strike}  sell=₹{sp:.2f}  nifty={close:.1f}  LB={B['lower']:.1f}")

        equity.append({"time": t, "capital": capital})

    # Force-close anything still open at very last candle
    for trade, piv, label in [
        (ce_trade, ce_prices, "CE"),
        (pe_trade, pe_prices, "PE"),
    ]:
        if trade:
            last_t   = candles[-1]["time"]
            ep       = get_price(piv, last_t, trade["strike"]) or trade["sell_price"]
            pnl      = round((trade["sell_price"] - ep) * qty, 2)
            capital  = round(capital + pnl, 2)
            trades.append({**trade,
                "type": label,
                "exit_time": last_t, "exit_price": ep,
                "exit_reason": "EOD Close",
                "pnl": pnl, "capital_after": capital,
            })

    # Sort trades by entry time for display
    trades.sort(key=lambda x: x["entry_time"])
    return trades, equity, capital


# ─────────────────────────────────────────────────────────────────────────────
def compute_metrics(trades, equity, starting_capital):
    final  = equity[-1]["capital"] if equity else starting_capital
    pnl    = round(final - starting_capital, 2)
    wins   = [t for t in trades if t["pnl"] > 0]
    losses = [t for t in trades if t["pnl"] <= 0]
    peak, maxdd = starting_capital, 0.0
    for e in equity:
        peak  = max(peak, e["capital"])
        dd    = (peak - e["capital"]) / peak * 100
        maxdd = max(maxdd, dd)

    ce_trades = [t for t in trades if t["type"] == "CE"]
    pe_trades = [t for t in trades if t["type"] == "PE"]
    ce_pnl    = round(sum(t["pnl"] for t in ce_trades), 2)
    pe_pnl    = round(sum(t["pnl"] for t in pe_trades), 2)

    return {
        "starting_capital": starting_capital,
        "final_capital":    final,
        "total_pnl":        pnl,
        "return_pct":       round(pnl / starting_capital * 100, 4),
        "total_trades":     len(trades),
        "wins":             len(wins),
        "losses":           len(losses),
        "win_rate":         round(len(wins) / len(trades) * 100, 2) if trades else 0,
        "avg_win":          round(sum(t["pnl"] for t in wins)   / len(wins),   2) if wins   else 0,
        "avg_loss":         round(sum(t["pnl"] for t in losses) / len(losses), 2) if losses else 0,
        "max_drawdown_pct": round(maxdd, 2),
        "ce_pnl":           ce_pnl,
        "pe_pnl":           pe_pnl,
    }


# ─────────────────────────────────────────────────────────────────────────────
def save_results(candles, bands, trades, equity, metrics, params, out_path):
    print(f"[4/4] Saving → {out_path}")
    entry_map = {tr["entry_time"]: tr["type"] for tr in trades}
    chart_data = [
        {"t": c["time"], "close": c["close"],
         "mid": b["mid"], "upper": b["upper"], "lower": b["lower"]}
        for c, b in zip(candles, bands)
    ]
    with open(out_path, "w") as f:
        json.dump({
            "params":     params,
            "metrics":    metrics,
            "chart_data": chart_data,
            "entry_map":  entry_map,
            "trades":     trades,
            "equity":     equity,
        }, f, separators=(",", ":"))
    print(f"    {os.path.getsize(out_path) / 1024:.1f} KB written")


# ─────────────────────────────────────────────────────────────────────────────
def print_summary(metrics, trades):
    m = metrics
    print()
    print("=" * 64)
    print("   NIFTY 50 · BB STRATEGY · RESULTS  (1 CE + 1 PE per day)")
    print("=" * 64)
    print(f"   Starting Capital : ₹{m['starting_capital']:>12,.2f}")
    print(f"   Final Capital    : ₹{m['final_capital']:>12,.2f}")
    sign = "+" if m["total_pnl"] >= 0 else ""
    print(f"   Net P&L          : ₹{m['total_pnl']:>12,.2f}  ({sign}{m['return_pct']:.2f}%)")
    print(f"   CE Leg P&L       : ₹{m['ce_pnl']:>12,.2f}")
    print(f"   PE Leg P&L       : ₹{m['pe_pnl']:>12,.2f}")
    print(f"   Trades           : {m['total_trades']}  (W:{m['wins']} / L:{m['losses']}  WR:{m['win_rate']:.1f}%)")
    print(f"   Avg Win / Loss   : ₹{m['avg_win']:,.2f} / ₹{m['avg_loss']:,.2f}")
    print(f"   Max Drawdown     : {m['max_drawdown_pct']:.2f}%")
    print()

    ce_t = [t for t in trades if t["type"] == "CE"]
    pe_t = [t for t in trades if t["type"] == "PE"]

    for label, group in [("CE Leg", ce_t), ("PE Leg", pe_t)]:
        print(f"   ── {label} ──")
        if not group:
            print("      No trade fired")
        for tr in group:
            s = "+" if tr["pnl"] >= 0 else ""
            print(f"      Entry {tr['entry_time']}  strike={tr['strike']}  "
                  f"sell=₹{tr['sell_price']:.2f}  "
                  f"exit={tr['exit_time']}  exit=₹{tr['exit_price']:.2f}  "
                  f"P&L={s}₹{tr['pnl']:,.2f}  [{tr['exit_reason']}]")
        print()
    print("=" * 64)


# ─────────────────────────────────────────────────────────────────────────────
class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_): pass


def main():
    args = parse_args()
    script_dir = Path(__file__).parent
    csv_path   = script_dir / "nifty_option_data_1.1.2024.csv"
    out_json   = script_dir / "backtest_results.json"
    html_file  = script_dir / "nifty_bb_dashboard.html"

    if not csv_path.exists():
        print(f"ERROR: {csv_path} not found"); sys.exit(1)
    if not html_file.exists():
        print(f"ERROR: {html_file} not found"); sys.exit(1)

    params = {
        "starting_capital": args.capital,
        "bb_period":        args.period,
        "bb_std":           args.std,
        "lots":             args.lots,
        "lot_size":         args.lot_size,
        "settle_time":      args.settle,
        "qty":              args.lots * args.lot_size,
        "strategy":         "1 CE + 1 PE per day, both held to settlement",
    }

    print()
    print(f"  Strategy: 1 CE + 1 PE per day  |  Capital ₹{args.capital:,.0f}  |  "
          f"BB({args.period}, {args.std}σ)  |  {args.lots} lot × {args.lot_size}  |  Settle {args.settle}")
    print()

    ce, pe  = load_csv(str(csv_path))
    candles = reconstruct_nifty(ce, pe)
    closes  = [c["close"] for c in candles]
    bands   = compute_bb(closes, args.period, args.std)
    rsi_vals = compute_rsi(closes, period=2)
    trades, equity, _ = run_strategy(
        candles, bands, rsi_vals, ce, pe,
        args.capital, args.lots, args.lot_size, args.settle, args.max_entries
    )
    metrics = compute_metrics(trades, equity, args.capital)
    save_results(candles, bands, trades, equity, metrics, params, str(out_json))
    print_summary(metrics, trades)

    os.chdir(str(script_dir))
    server = HTTPServer(("localhost", args.port), QuietHandler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://localhost:{args.port}/nifty_bb_dashboard.html"
    print(f"\n  Server: {url}")
    if not args.no_browser:
        time.sleep(0.3)
        webbrowser.open(url)
        print("  Browser opened ✓")
    print("  Press Ctrl+C to stop.\n")
    try:
        while True: time.sleep(1)
    except KeyboardInterrupt:
        print("  Stopped.")


if __name__ == "__main__":
    main()
