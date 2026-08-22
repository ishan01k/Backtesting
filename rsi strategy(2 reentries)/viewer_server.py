import os
import json
import csv
import urllib.parse
from http.server import HTTPServer, BaseHTTPRequestHandler
from pathlib import Path
import datetime
import threading

# Lightweight Charts displays timestamps as UTC.
# Our CSV datetimes are IST (UTC+5:30), so we add this offset so the
# chart x-axis shows IST times (09:15–15:30) instead of raw UTC.
IST_OFFSET_SECONDS = 19800  # 5 hours 30 minutes

class ViewerHTTPRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        # Suppress logging to stdout to keep console output clean
        pass

    def end_headers(self):
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'X-Requested-With, Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(200)
        self.end_headers()

    def do_GET(self):
        parsed_url = urllib.parse.urlparse(self.path)
        path = parsed_url.path
        query = urllib.parse.parse_qs(parsed_url.query)

        if path == '/':
            self.serve_file('view_day.html', 'text/html')
        elif path == '/view_day.html':
            self.serve_file('view_day.html', 'text/html')
        elif path == '/lightweight-charts.js':
            self.serve_file('lightweight-charts.js', 'application/javascript')
        elif path == '/chart.js':
            self.serve_file('chart.js', 'application/javascript')
        elif path == '/api/day_summary':
            self.handle_day_summary(query)
        elif path == '/api/option_chain':
            self.handle_option_chain(query)
        else:
            self.send_response(404)
            self.end_headers()
            self.wfile.write(b"Not Found")

    def serve_file(self, filename, content_type):
        file_path = Path(__file__).parent / filename
        if not file_path.exists():
            self.send_response(404)
            self.end_headers()
            self.wfile.write(f"File {filename} not found".encode())
            return

        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.end_headers()
        with open(file_path, 'rb') as f:
            self.wfile.write(f.read())

    def find_data_file(self, date_str):
        # date_str is YYYY-MM-DD
        year = date_str.split('-')[0]
        # Data folder lives one level up (at backtests root), not inside the strategy subfolder
        data_dir = Path(__file__).parent.parent / "Data"
        if not data_dir.exists():
            data_dir = Path(__file__).parent / "Data"  # fallback to local
        path = data_dir / year / f"nifty_options_{date_str}.csv"
        if path.exists():
            return path
        # Try recursive glob search as fallback
        for p in data_dir.rglob(f"nifty_options_{date_str}.csv"):
            return p
        return None

    def handle_day_summary(self, query):
        day = query.get('day', [''])[0]
        if not day:
            self.send_json({"error": "day parameter is required"}, 400)
            return

        csv_path = self.find_data_file(day)
        if not csv_path or not csv_path.exists():
            self.send_json({"error": f"Data file for date {day} not found"}, 404)
            return

        # Load trades for this day
        trades = []
        trades_file = Path(__file__).parent / "bb_all_trades_details.csv"
        if trades_file.exists():
            try:
                with open(trades_file, newline='') as f:
                    reader = csv.DictReader(f)
                    for row in reader:
                        if row.get('Date') == day:
                            # Convert numerical values to appropriate float/ints
                            for k in ['Strike', 'Sell_Price', 'Exit_Price', 'Qty', 'Premium_Collected', 'PnL', 'Capital_After']:
                                if k in row and row[k]:
                                    try:
                                        row[k] = float(row[k])
                                    except ValueError:
                                        pass
                            trades.append(row)
            except Exception as e:
                print(f"Error reading trades file: {e}")

        # Collect the strikes and legs that were traded on this day
        traded_options = {}
        for t in trades:
            try:
                strike = int(float(t.get("Strike", 0)))
                leg = t.get("Leg", "").strip().upper()
                if strike > 0 and leg in ("CE", "PE"):
                    traded_options[f"{strike}_{leg}"] = []
            except (ValueError, TypeError):
                continue

        # Extract unique spot price candles / series and option series
        spot_series = []
        seen_times = set()
        
        try:
            with open(csv_path, newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    dt_val = row.get('datetime', '')
                    if not dt_val:
                        continue
                    
                    # Parse time HH:MM
                    parts = dt_val.split(' ')
                    time_str = ""
                    if len(parts) > 1:
                        time_str = parts[1][:5]
                    else:
                        time_str = row.get('time', '').strip()[:5]
                    
                    try:
                        spot = float(row.get('spot', 0))
                    except ValueError:
                        continue
                    
                    if spot <= 0:
                        continue
                    
                    # Try parsing to unix timestamp
                    try:
                        # Strip timezone offset e.g. +05:30 before parsing
                        dt_no_tz = dt_val.split('+')[0].strip()
                        dt_obj = datetime.datetime.strptime(dt_no_tz, "%Y-%m-%d %H:%M:%S")
                        # Add IST offset so LightweightCharts (UTC display) shows IST times
                        ts = int(dt_obj.timestamp()) + IST_OFFSET_SECONDS
                    except Exception:
                        ts = 0

                    # Collect spot series
                    if time_str not in seen_times:
                        seen_times.add(time_str)
                        spot_series.append({
                            "time": time_str,
                            "timestamp": ts,
                            "value": spot
                        })

                    # Collect option price series if this row belongs to a traded contract
                    try:
                        strike = int(float(row.get('api_strike', 0)))
                        otype = "CE" if row.get('option_type', '').strip().upper() in ("CALL", "CE") else "PE"
                        key = f"{strike}_{otype}"
                        if key in traded_options:
                            o_close = float(row.get('close', 0))
                            o_open = float(row.get('open') or o_close)
                            o_high = float(row.get('high') or o_close)
                            o_low = float(row.get('low') or o_close)
                            
                            # Deduplicate by time_str
                            if not any(item["time_str"] == time_str for item in traded_options[key]):
                                traded_options[key].append({
                                    "time": ts,
                                    "open": o_open,
                                    "high": o_high,
                                    "low": o_low,
                                    "close": o_close,
                                    "time_str": time_str
                                })
                    except ValueError:
                        continue

        except Exception as e:
            self.send_json({"error": f"Error parsing CSV data: {str(e)}"}, 500)
            return

        # Sort spot series and option series
        spot_series.sort(key=lambda x: x["time"])
        for k in traded_options:
            traded_options[k].sort(key=lambda x: x["time_str"])

        self.send_json({
            "date": day,
            "spot_series": spot_series,
            "trades": trades,
            "option_series": traded_options
        })

    def handle_option_chain(self, query):
        day = query.get('day', [''])[0]
        time_val = query.get('time', [''])[0] # e.g. "09:30"
        
        if not day or not time_val:
            self.send_json({"error": "day and time parameters are required"}, 400)
            return

        csv_path = self.find_data_file(day)
        if not csv_path or not csv_path.exists():
            self.send_json({"error": f"Data file for date {day} not found"}, 404)
            return

        strikes_data = {}
        spot_price = 0.0

        try:
            with open(csv_path, newline='') as f:
                reader = csv.DictReader(f)
                for row in reader:
                    dt_val = row.get('datetime', '')
                    if not dt_val:
                        continue
                        
                    parts = dt_val.split(' ')
                    row_time = parts[1][:5] if len(parts) > 1 else row.get('time', '').strip()[:5]
                    
                    if row_time != time_val:
                        continue

                    # If matched, extract details
                    try:
                        strike = int(float(row.get('api_strike', 0)))
                        otype = row.get('option_type', '').strip().upper()
                        price = float(row.get('close', 0))
                        spot = float(row.get('spot', 0))
                        volume = int(float(row.get('volume', 0) or 0))
                        oi = int(float(row.get('oi', 0) or 0))
                        iv = float(row.get('iv', 0) or 0)
                    except ValueError:
                        continue

                    if spot > 0:
                        spot_price = spot

                    if strike not in strikes_data:
                        strikes_data[strike] = {
                            "strike": strike,
                            "CE": {"ltp": 0.0, "oi": 0, "iv": 0.0, "volume": 0},
                            "PE": {"ltp": 0.0, "oi": 0, "iv": 0.0, "volume": 0}
                        }

                    leg_key = "CE" if otype in ("CALL", "CE") else "PE"
                    strikes_data[strike][leg_key] = {
                        "ltp": price,
                        "oi": oi,
                        "iv": iv,
                        "volume": volume
                    }
        except Exception as e:
            self.send_json({"error": f"Error parsing CSV data: {str(e)}"}, 500)
            return

        # Sort strikes
        sorted_strikes = [strikes_data[s] for s in sorted(strikes_data.keys())]

        self.send_json({
            "date": day,
            "time": time_val,
            "spot": spot_price,
            "option_chain": sorted_strikes
        })

    def send_json(self, data, status=200):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(data).encode('utf-8'))

def run_server(port=8502):
    server_address = ('', port)
    httpd = HTTPServer(server_address, ViewerHTTPRequestHandler)
    print(f"Starting options visualizer server on port {port}...")
    httpd.serve_forever()

# Global reference to server process state
_server_started = False

def start_server_process(port=8502):
    global _server_started
    if _server_started:
        return
        
    # Check if port is already open
    import socket
    import subprocess
    import sys
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind(('127.0.0.1', port))
        s.close()
        port_free = True
    except socket.error:
        port_free = False

    if port_free:
        script_path = Path(__file__).parent / "viewer_server.py"
        # Start server as an independent background process
        subprocess.Popen([sys.executable, str(script_path)], close_fds=True)
        print(f"Started options visualizer server as background process on port {port}.")
        
    _server_started = True

# Alias for backwards compatibility
def start_server_in_thread(port=8502):
    start_server_process(port)

if __name__ == '__main__':
    run_server()
