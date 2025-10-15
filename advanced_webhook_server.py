from flask import Flask, request, jsonify
import requests
import json
import os
from datetime import datetime
from typing import Dict, Optional

app = Flask(__name__)

# Configuration from environment variables (for Render deployment)
TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '')
TELEGRAM_CHAT_ID = os.environ.get('TELEGRAM_CHAT_ID', '')

# Store active trades with entry details
active_trades: Dict[str, dict] = {}

class TrailingStopCalculator:
    """Replicates PineScript trailing stop logic using high, low, close"""
    
    def __init__(self):
        self.tsi = 0.3            # Trailing stop activation %
        self.ts_low_profit = 0.2   # TS offset at 0.5% profit
        self.ts_high_profit = 0.3  # TS offset at 10% profit
        self.act_ts_pump = 1.0     # Pump trailing stop activation %
    
    def ts_dynamic(self, profit_percent: float) -> float:
        """Dynamic trailing stop calculation - linear interpolation"""
        ts = max((self.ts_high_profit - self.ts_low_profit) / 9.5 * (profit_percent - 0.5) + self.ts_low_profit,
                 self.ts_low_profit)
        return ts

    # ---- Long / Short trailing exit calculations ----
    def calculate_regular_long_exit(self, entry_price: float, high_price: float, close_price: float) -> Optional[float]:
        profit_percent = abs((high_price - entry_price) / entry_price * 100)
        ts = self.ts_dynamic(profit_percent)
        activation_price = entry_price * (1 + self.tsi / 100)
        trail_trigger = activation_price * (1 + ts / 100)
        close_ts_level = close_price * (1 + ts / 100)
        if high_price >= close_ts_level and high_price >= trail_trigger:
            return round(activation_price * (1 + ts / 100), 8)
        return None

    def calculate_regular_short_exit(self, entry_price: float, low_price: float, close_price: float) -> Optional[float]:
        profit_percent = abs((low_price - entry_price) / entry_price * 100)
        ts = self.ts_dynamic(profit_percent)
        activation_price = entry_price * (1 - self.tsi / 100)
        trail_trigger = activation_price * (1 - ts / 100)
        close_ts_level = close_price * (1 - ts / 100)
        if low_price <= close_ts_level and low_price <= trail_trigger:
            return round(activation_price * (1 - ts / 100), 8)
        return None

    # ---- Pump trailing exits ----
    def calculate_long_pump_exit(self, entry_price: float, high_price: float, close_price: float) -> Optional[float]:
        profit_percent = abs((high_price - entry_price) / entry_price * 100)
        ts_pump = self.ts_dynamic(profit_percent)
        activation_price = entry_price * (1 + self.act_ts_pump / 100)
        trail_trigger = activation_price * (1 + ts_pump / 100)
        close_ts_level = close_price * (1 + ts_pump / 100)
        if high_price > trail_trigger and high_price > activation_price and high_price >= close_ts_level:
            return round(activation_price * (1 + ts_pump / 100), 8)
        return None

    def calculate_short_pump_exit(self, entry_price: float, low_price: float, close_price: float) -> Optional[float]:
        profit_percent = abs((low_price - entry_price) / entry_price * 100)
        ts_pump = self.ts_dynamic(profit_percent)
        activation_price = entry_price * (1 - self.act_ts_pump / 100)
        trail_trigger = activation_price * (1 - ts_pump / 100)
        close_ts_level = close_price * (1 - ts_pump / 100)
        if low_price < trail_trigger and low_price < activation_price and low_price <= close_ts_level:
            return round(activation_price * (1 - ts_pump / 100), 8)
        return None


ts_calc = TrailingStopCalculator()

# ---- Telegram integration ----
def send_telegram_message(message: str) -> Optional[dict]:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("ERROR: Telegram credentials not configured")
        return None
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Error sending to Telegram: {e}")
        return None

# ---- Entry / Exit calculation helpers ----
def calculate_take_profit(entry_price: float, action: str) -> float:
    return round(entry_price * 1.05, 8) if action.upper() == "BUY" else round(entry_price * 0.95, 8)

def calculate_stop_loss(entry_price: float, action: str, stop_percent: float = 3) -> float:
    return round(entry_price * (1 - stop_percent/100), 8) if action.upper() == "BUY" else round(entry_price * (1 + stop_percent/100), 8)

def process_exit_price(data: dict) -> float:
    ticker = data.get('ticker', '').upper()
    exit_type = data.get('exit_type', 'unknown')
    raw_exit_price = float(data.get('exit_price', 0))

    trade_info = active_trades.get(ticker)
    if not trade_info:
        return raw_exit_price

    entry_price = trade_info['entry_price']
    action = trade_info['action']

    # Use high/low/close from webhook (from strategy)
    high_price = float(data.get('high', raw_exit_price))
    low_price = float(data.get('low', raw_exit_price))
    close_price = float(data.get('close', raw_exit_price))

    if exit_type == 'pump_trailing' and action == 'BUY':
        return ts_calc.calculate_long_pump_exit(entry_price, high_price, close_price) or raw_exit_price
    elif exit_type == 'dump_trailing' and action == 'SELL':
        return ts_calc.calculate_short_pump_exit(entry_price, low_price, close_price) or raw_exit_price
    elif exit_type == 'trailing_stop':
        if action == 'BUY':
            return ts_calc.calculate_regular_long_exit(entry_price, high_price, close_price) or raw_exit_price
        else:
            return ts_calc.calculate_regular_short_exit(entry_price, low_price, close_price) or raw_exit_price
    return raw_exit_price

def format_entry_signal(data: dict) -> str:
    action = data.get('action', 'BUY').upper()
    ticker = data.get('ticker', '').upper()
    entry_price = float(data.get('entry_price', 0))
    timeframe = data.get('timeframe', '15m')
    take_profit = calculate_take_profit(entry_price, action)
    stop_loss = calculate_stop_loss(entry_price, action)

    active_trades[ticker] = {
        'action': action,
        'entry_price': entry_price,
        'timeframe': timeframe,
        'entry_time': datetime.now().isoformat()
    }

    return f"""Action: {action} 💹
Symbol: #{ticker}
--- ⌁ ---
Exchange: Binance Futures
Timeframe: {timeframe}
Leverage: Isolated (20X)
--- ⌁ ---
☑️ Entry Price: {entry_price}
☑️ Take Profit: {take_profit}
☑️ Stop Loss: {stop_loss}
--- ⌁ ---
⚠️ Wait for Close Signal!"""

def format_exit_signal(data: dict) -> str:
    ticker = data.get('ticker', '').upper()
    exit_price = process_exit_price(data)
    message = f"#{ticker} Tp {exit_price}"

    if ticker in active_trades:
        trade = active_trades[ticker]
        profit_pct = ((exit_price - trade['entry_price']) / trade['entry_price']) * 100
        if trade['action'] == 'SELL':
            profit_pct = -profit_pct
        print(f"Trade closed - {ticker}: Entry={trade['entry_price']}, Exit={exit_price}, Profit={profit_pct:.2f}%")
        del active_trades[ticker]

    return message

# ---- Flask endpoints ----
@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json()
        if not data:
            return jsonify({"status": "error", "message": "No data received"}), 400

        print(f"Received: {json.dumps(data, indent=2)}")
        signal_type = data.get('type', 'entry').lower()

        if signal_type == 'entry':
            message = format_entry_signal(data)
        elif signal_type == 'exit':
            message = format_exit_signal(data)
        else:
            return jsonify({"status": "error", "message": "Invalid signal type"}), 400

        result = send_telegram_message(message)
        if result:
            return jsonify({"status": "success", "message": "Signal sent to Telegram",
                            "formatted_message": message,
                            "timestamp": datetime.now().isoformat()}), 200
        else:
            return jsonify({"status": "error", "message": "Failed to send to Telegram"}), 500

    except Exception as e:
        print(f"Error: {str(e)}")
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({"status": "healthy",
                    "active_trades": len(active_trades),
                    "trades": list(active_trades.keys()),
                    "timestamp": datetime.now().isoformat()}), 200

@app.route('/trades', methods=['GET'])
def get_trades():
    return jsonify({"active_trades": active_trades, "count": len(active_trades)}), 200

@app.route('/', methods=['GET'])
def index():
    return jsonify({"service": "TradingView to Cornix Webhook",
                    "status": "running",
                    "endpoints": { "webhook": "/webhook (POST)",
                                   "health": "/health (GET)",
                                   "trades": "/trades (GET)"}}), 200

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("="*50)
    print("TradingView to Cornix Webhook Server")
    print("="*50)
    print(f"Server starting on port {port}")
    print("="*50)
    app.run(host='0.0.0.0', port=port)
