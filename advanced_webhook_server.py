from flask import Flask, request, jsonify
import requests
import json
from datetime import datetime
from typing import Dict, Optional

app = Flask(__name__)

# Configuration
TELEGRAM_BOT_TOKEN = "YOUR_BOT_TOKEN"
TELEGRAM_CHAT_ID = "YOUR_CHAT_ID"

# Store active trades with entry details
active_trades: Dict[str, dict] = {}

class TrailingStopCalculator:
    """Replicates PineScript trailing stop logic"""
    
    def __init__(self):
        self.tsi = 0.9  # Trailing stop activation %
        self.ts_low_profit = 0.1  # TS offset at 0.5% profit
        self.ts_high_profit = 0.2  # TS offset at 10% profit
        self.act_ts_pump = 1.0  # Pump trailing stop activation %
    
    def ts_dynamic(self, profit_percent: float) -> float:
        """
        Dynamic trailing stop calculation - linear interpolation
        Based on profit percentage
        """
        ts = max(
            (self.ts_high_profit - self.ts_low_profit) / 9.5 * (profit_percent - 0.5) + self.ts_low_profit,
            self.ts_low_profit
        )
        return ts
    
    def calculate_long_pump_exit(self, entry_price: float, high_price: float, close_price: float) -> Optional[float]:
        """
        Calculate exit price for long pump scenario
        Matches: long_ts_pump logic
        """
        # Calculate profit
        profit_percent = abs((high_price - entry_price) / entry_price * 100)
        
        # Get dynamic trailing stop
        ts_pump = self.ts_dynamic(profit_percent)
        
        # Check pump trailing stop conditions
        activation_price = entry_price * (1 + self.act_ts_pump / 100)
        trail_trigger = activation_price * (1 + ts_pump / 100)
        close_ts_level = close_price * (1 + ts_pump / 100)
        
        # high > last_open_longCondition * (1 + Act_ts_pump / 100) * (1 + ts_pump / 100)
        # high > last_open_longCondition * (1 + Act_ts_pump / 100)
        # high >= close * (1 + ts_pump / 100)
        
        if (high_price > trail_trigger and 
            high_price > activation_price and 
            high_price >= close_ts_level):
            # Calculate actual exit price (trailing stop hit)
            # Exit occurs at: entry * (1 + act_ts_pump%) * (1 + ts_pump%)
            exit_price = activation_price * (1 + ts_pump / 100)
            return round(exit_price, 8)
        
        return None
    
    def calculate_short_pump_exit(self, entry_price: float, low_price: float, close_price: float) -> Optional[float]:
        """
        Calculate exit price for short dump scenario
        Matches: short_ts_pump logic
        """
        # Calculate profit
        profit_percent = abs((low_price - entry_price) / entry_price * 100)
        
        # Get dynamic trailing stop
        ts_pump = self.ts_dynamic(profit_percent)
        
        # Check dump trailing stop conditions
        activation_price = entry_price * (1 - self.act_ts_pump / 100)
        trail_trigger = activation_price * (1 - ts_pump / 100)
        close_ts_level = close_price * (1 - ts_pump / 100)
        
        if (low_price < trail_trigger and 
            low_price < activation_price and 
            low_price <= close_ts_level):
            # Calculate actual exit price
            exit_price = activation_price * (1 - ts_pump / 100)
            return round(exit_price, 8)
        
        return None
    
    def calculate_regular_long_exit(self, entry_price: float, high_price: float, close_price: float) -> Optional[float]:
        """Regular trailing stop for longs"""
        profit_percent = abs((high_price - entry_price) / entry_price * 100)
        ts = self.ts_dynamic(profit_percent)
        
        activation_price = entry_price * (1 + self.tsi / 100)
        trail_trigger = activation_price * (1 + ts / 100)
        close_ts_level = close_price * (1 + ts / 100)
        
        if (high_price >= close_ts_level and 
            high_price >= trail_trigger):
            exit_price = activation_price * (1 + ts / 100)
            return round(exit_price, 8)
        
        return None
    
    def calculate_regular_short_exit(self, entry_price: float, low_price: float, close_price: float) -> Optional[float]:
        """Regular trailing stop for shorts"""
        profit_percent = abs((low_price - entry_price) / entry_price * 100)
        ts = self.ts_dynamic(profit_percent)
        
        activation_price = entry_price * (1 - self.tsi / 100)
        trail_trigger = activation_price * (1 - ts / 100)
        close_ts_level = close_price * (1 - ts / 100)
        
        if (low_price <= close_ts_level and 
            low_price <= trail_trigger):
            exit_price = activation_price * (1 - ts / 100)
            return round(exit_price, 8)
        
        return None

# Initialize calculator
ts_calc = TrailingStopCalculator()

def send_telegram_message(message: str) -> Optional[dict]:
    """Send message to Telegram"""
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "HTML"
    }
    try:
        response = requests.post(url, json=payload, timeout=10)
        return response.json()
    except Exception as e:
        print(f"Error sending to Telegram: {e}")
        return None

def calculate_take_profit(entry_price: float, action: str) -> float:
    """Calculate 5% take profit from entry price"""
    if action.upper() == "BUY":
        return round(entry_price * 1.05, 8)
    else:  # SELL
        return round(entry_price * 0.95, 8)

def calculate_stop_loss(entry_price: float, action: str, stop_percent: float = 3) -> float:
    """Calculate stop loss (default 3% from entry)"""
    if action.upper() == "BUY":
        return round(entry_price * (1 - stop_percent/100), 8)
    else:  # SELL
        return round(entry_price * (1 + stop_percent/100), 8)

def process_exit_price(data: dict) -> float:
    """
    Process exit price based on exit type and market data
    Handles pump/dump scenarios with proper trailing stop logic
    """
    ticker = data.get('ticker', '').upper()
    exit_type = data.get('exit_type', 'unknown')
    raw_exit_price = float(data.get('exit_price', 0))
    
    # Get stored trade info
    trade_info = active_trades.get(ticker)
    if not trade_info:
        return raw_exit_price
    
    entry_price = trade_info['entry_price']
    action = trade_info['action']
    
    # Get additional price data if available
    high_price = float(data.get('high', raw_exit_price))
    low_price = float(data.get('low', raw_exit_price))
    close_price = float(data.get('close', raw_exit_price))
    
    # Process based on exit type
    if exit_type == 'pump_trailing' and action == 'BUY':
        calculated_exit = ts_calc.calculate_long_pump_exit(entry_price, high_price, close_price)
        return calculated_exit if calculated_exit else raw_exit_price
    
    elif exit_type == 'dump_trailing' and action == 'SELL':
        calculated_exit = ts_calc.calculate_short_pump_exit(entry_price, low_price, close_price)
        return calculated_exit if calculated_exit else raw_exit_price
    
    elif exit_type == 'trailing_stop':
        if action == 'BUY':
            calculated_exit = ts_calc.calculate_regular_long_exit(entry_price, high_price, close_price)
        else:
            calculated_exit = ts_calc.calculate_regular_short_exit(entry_price, low_price, close_price)
        return calculated_exit if calculated_exit else raw_exit_price
    
    return raw_exit_price

def format_entry_signal(data: dict) -> str:
    """Format entry signal for Cornix"""
    action = data.get('action', 'BUY').upper()
    ticker = data.get('ticker', '').upper()
    entry_price = float(data.get('entry_price', 0))
    timeframe = data.get('timeframe', '15m')
    
    take_profit = calculate_take_profit(entry_price, action)
    stop_loss = calculate_stop_loss(entry_price, action)
    
    # Store trade info for exit signal
    active_trades[ticker] = {
        'action': action,
        'entry_price': entry_price,
        'timeframe': timeframe,
        'entry_time': datetime.now().isoformat()
    }
    
    message = f"""Action: {action} 💹
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
    
    return message

def format_exit_signal(data: dict) -> str:
    """Format exit signal for Cornix"""
    ticker = data.get('ticker', '').upper()
    
    # Process exit price with trailing stop logic
    exit_price = process_exit_price(data)
    
    # Simple Cornix format to update TP and close trade
    message = f"#{ticker} Tp {exit_price}"
    
    # Log exit details
    if ticker in active_trades:
        trade = active_trades[ticker]
        profit_pct = ((exit_price - trade['entry_price']) / trade['entry_price']) * 100
        if trade['action'] == 'SELL':
            profit_pct = -profit_pct
        
        print(f"Trade closed - {ticker}: Entry={trade['entry_price']}, Exit={exit_price}, Profit={profit_pct:.2f}%")
        del active_trades[ticker]
    
    return message

@app.route('/webhook', methods=['POST'])
def webhook():
    """Main webhook endpoint for TradingView alerts"""
    try:
        data = request.get_json()
        
        if not data:
            return jsonify({"status": "error", "message": "No data received"}), 400
        
        # Log received data
        print(f"Received webhook data: {json.dumps(data, indent=2)}")
        
        signal_type = data.get('type', 'entry').lower()
        
        if signal_type == 'entry':
            message = format_entry_signal(data)
        elif signal_type == 'exit':
            message = format_exit_signal(data)
        else:
            return jsonify({"status": "error", "message": "Invalid signal type"}), 400
        
        # Send to Telegram
        result = send_telegram_message(message)
        
        if result:
            return jsonify({
                "status": "success",
                "message": "Signal sent to Telegram",
                "formatted_message": message,
                "timestamp": datetime.now().isoformat()
            }), 200
        else:
            return jsonify({
                "status": "error",
                "message": "Failed to send to Telegram"
            }), 500
            
    except Exception as e:
        print(f"Error processing webhook: {str(e)}")
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500

@app.route('/health', methods=['GET'])
def health():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "active_trades": len(active_trades),
        "trades": list(active_trades.keys()),
        "timestamp": datetime.now().isoformat()
    }), 200

@app.route('/trades', methods=['GET'])
def get_trades():
    """Get all active trades"""
    return jsonify({
        "active_trades": active_trades,
        "count": len(active_trades)
    }), 200

if __name__ == '__main__':
    print("=" * 50)
    print("TradingView to Cornix Webhook Server")
    print("=" * 50)
    print("IMPORTANT: Update the following in the code:")
    print("1. TELEGRAM_BOT_TOKEN")
    print("2. TELEGRAM_CHAT_ID")
    print("=" * 50)
    print("Server starting on http://0.0.0.0:5000")
    print("Webhook endpoint: http://YOUR_IP:5000/webhook")
    print("=" * 50)
    app.run(host='0.0.0.0', port=5000, debug=False)
