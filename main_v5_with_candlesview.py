import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Dict, Deque, List
import gspread
from google.oauth2.service_account import Credentials

import aiohttp
from aiohttp import web
import websockets
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
from mpl_finance import candlestick_ohlc
import io
import firebase_admin
from firebase_admin import credentials, messaging

# ========== ENVIRONMENT SETUP ==========
IS_PRODUCTION = os.environ.get('RENDER', False) or os.environ.get('PYTHONANYWHERE', False) or os.environ.get('HEROKU', False)

if not IS_PRODUCTION:
    try:
        from dotenv import load_dotenv
        env_path = None
        if os.path.exists('.gitignore/.env'):
            env_path = '.gitignore/.env'
        elif os.path.exists('.env'):
            env_path = '.env'
        if env_path:
            load_dotenv(env_path)
            print(f"Loaded environment from {env_path}")
        else:
            print("No .env file found, using system environment variables")
    except ImportError:
        print("python-dotenv not installed, using system environment variables")
else:
    print("Running in production environment, using system environment variables")

# ================== CONFIG ==================
SYMBOLS = ["ethusdt"]

# EMA Color Configuration
EMA_COLORS = {
    20: "green",      # EMA20 - Red
    50: "yellow",    # EMA50 - Green  
    100: "blue",  # EMA100 - Purple
    200: "red"     # EMA200 - Blue
}

CONFIG = {
    "symbols": SYMBOLS,
    "timeframe": "1m",
    "ema_fast": 20,
    "ema_slow": 200,
    "ema_periods": [20, 50, 100, 200],  # All EMA periods for chart display
    "bootstrap_candles": 400,
    
    # Strategy
    "strategy": "ematouch",
    
    # Alert settings
    "proximity_threshold_percent": 0.05,  # 0.20% threshold for proximity alerts
    "min_proximity_gap_minutes": 30,      # Minimum time between same-direction proximity alerts
    "min_touch_gap_minutes": 30,          # Minimum time between same-direction touch alerts
    
    # Chart sending
    "enable_chart_sending": True,
    "send_chart_on_alert": True,          # Toggle to send chart image for each alert
    "chart_interval_sec": 5,

    # Alert methods
    "alerts": {
        "telegram": True,
        "fcm": True
    },

    # API keys and tokens
    "TELEGRAM_TOKEN": os.environ.get("TELEGRAM_TOKEN", ""),
    "CHAT_ID": os.environ.get("CHAT_ID", ""),
    "DEVICE_TOKENS_FILE": "device_token_lists.json",

    # Google Sheets
    "enable_google_sheets_logging": False,
    "GOOGLE_SHEETS_CREDENTIALS": os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
}

# ================== DATA ==================
symbol_data: Dict[str, Dict] = {}
for symbol in SYMBOLS:
    symbol_data[symbol] = {
        "completed_candles": deque(maxlen=1000),
        "current_candle": None,
        "last_touch_time": {"up": None, "down": None},
        "last_proximity_time": {"up": None, "down": None},
        "prev_fast_ema": None,
        "prev_slow_ema": None,
        "ema_history": deque(maxlen=10)
    }

firebase_app = None
google_sheets_client = None

# ================== GOOGLE SHEETS ==================
def init_google_sheets():
    global google_sheets_client
    if not CONFIG["enable_google_sheets_logging"]:
        return
    try:
        google_cred_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
        if not google_cred_json:
            return
        cred_dict = json.loads(google_cred_json)
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        credentials = Credentials.from_service_account_info(cred_dict, scopes=scopes)
        google_sheets_client = gspread.authorize(credentials)
        print("[Google Sheets] Initialized successfully")
    except Exception as e:
        print(f"[Google Sheets] init error: {e}")

# ================== FIREBASE ==================
def init_firebase():
    global firebase_app
    if not CONFIG["alerts"]["fcm"]:
        return
    try:
        firebase_cred_json = os.environ.get("FIREBASE_CREDENTIALS")
        if not firebase_cred_json:
            return
        cred_dict = json.loads(firebase_cred_json)
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
        cred = credentials.Certificate(cred_dict)
        firebase_app = firebase_admin.initialize_app(cred)
        print("[FCM] Firebase app initialized successfully")
        
        # Test FCM initialization triggers
        send_fcm_test_alerts()
        
    except Exception as e:
        print(f"[FCM] init error: {e}")

def load_valid_tokens():
    if not CONFIG["alerts"]["fcm"]:
        return []
    
    try:
        with open(CONFIG["DEVICE_TOKENS_FILE"], "r") as f:
            data = json.load(f)
    except FileNotFoundError:
        print(f"[FCM] {CONFIG['DEVICE_TOKENS_FILE']} not found")
        return []
    except json.JSONDecodeError:
        print(f"[FCM] Invalid JSON in {CONFIG['DEVICE_TOKENS_FILE']}")
        return []

    today = datetime.now().date()
    valid = []
    for t in data.get("tokens", []):
        exp = t.get("expiring_date")
        if not exp:
            continue
        try:
            expiry = datetime.strptime(exp, "%Y-%m-%d").date()
            if expiry >= today:
                valid.append({
                    "token": t["device_token"],
                    "customer": t.get("customer_name", "Unknown")
                })
        except ValueError:
            continue
    return valid

def send_fcm_alert(sound_type: str, message: str = "Alert Triggered"):
    """Send FCM alert with dynamic sound type"""
    if not CONFIG["alerts"]["fcm"]:
        return
    
    if not firebase_app:
        print("[FCM] not initialized, skipping")
        return

    tokens = load_valid_tokens()
    if not tokens:
        print("[FCM] no valid subscribers")
        return

    for t in tokens:
        try:
            msg = messaging.Message(
                data={
                    "action": "play",
                    "soundType": sound_type,
                    "timestamp": datetime.now().isoformat(),
                    "message": message
                },
                token=t["token"]
            )
            resp = messaging.send(msg)
            print(f"✓ FCM Message sent to {t['customer']}: {t['token'][:15]}... sound_type={sound_type} id={resp}")
        except Exception as e:
            print(f"✗ Failed to send FCM to {t['customer']}: {e}")

def send_fcm_test_alerts():
    """Send test FCM alerts for all alert types to confirm FCM is working"""
    if not CONFIG["alerts"]["fcm"] or not firebase_app:
        return
    
    symbol = SYMBOLS[0]
    timeframe = CONFIG["timeframe"]
    strategy = CONFIG["strategy"]
    
    test_alerts = [
        f"{symbol}_{timeframe}_{strategy}_up_warning",
        f"{symbol}_{timeframe}_{strategy}_down_warning", 
        f"{symbol}_{timeframe}_{strategy}_up",
        f"{symbol}_{timeframe}_{strategy}_down"
    ]
    
    print("[FCM] Sending test alerts for initialization confirmation...")
    for alert_type in test_alerts:
        send_fcm_alert(alert_type, f"FCM Test Alert: {alert_type}")
        time.sleep(1)  # Small delay between test alerts

# ================== UTILITIES ==================
def now():
    return datetime.now(timezone.utc)

async def send_telegram(msg):
    if not CONFIG["alerts"]["telegram"]:
        return
    token = CONFIG["TELEGRAM_TOKEN"]
    chat_id = CONFIG["CHAT_ID"]
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, data={"chat_id": chat_id, "text": msg})
    except Exception as e:
        print("[telegram] error", e)

# ================== TRADINGVIEW-COMPATIBLE EMA CALCULATION ==================
def calculate_ema_tradingview(prices: List[float], period: int) -> List[float]:
    """
    TradingView-compatible EMA calculation
    Returns EMA values for each position in the input array
    """
    if len(prices) < period:
        return [None] * len(prices)
    
    alpha = 2.0 / (period + 1.0)
    ema_values = []
    
    # Calculate SMA for the first EMA value
    sma = sum(prices[:period]) / period
    ema_values.extend([None] * (period - 1))  # No EMA values before the period
    ema_values.append(sma)
    
    # Calculate EMA for subsequent values
    ema = sma
    for price in prices[period:]:
        ema = (price * alpha) + (ema * (1 - alpha))
        ema_values.append(ema)
    
    return ema_values

def calculate_ema_single_tradingview(prices: List[float], period: int) -> float:
    """
    Calculate the most recent EMA value (TradingView compatible)
    Returns the latest EMA value or None if insufficient data
    """
    if len(prices) < period:
        return None
    
    ema_series = calculate_ema_tradingview(prices, period)
    return ema_series[-1] if ema_series else None

# ================== ALERT DETECTION ==================
def detect_ema_touch(prev_fast, prev_slow, current_fast, current_slow):
    """Detect if EMAs have touched"""
    if prev_fast is None or prev_slow is None or current_fast is None or current_slow is None:
        return None
    
    # Check if EMAs have touched (current values are very close)
    ema_diff = abs(current_fast - current_slow)
    touch_threshold = (current_fast + current_slow) / 2 * 0.0001  # 0.01% threshold for touch
    
    if ema_diff <= touch_threshold:
        # Determine direction based on previous positions
        if prev_fast < prev_slow:  # Fast was below slow
            return "up"
        elif prev_fast > prev_slow:  # Fast was above slow
            return "down"
    
    return None

def detect_ema_proximity(current_fast, current_slow):
    """Detect if EMAs are approaching each other within threshold"""
    if current_fast is None or current_slow is None:
        return None
    
    price_level = (current_fast + current_slow) / 2
    ema_diff = abs(current_fast - current_slow)
    proximity_threshold = price_level * (CONFIG["proximity_threshold_percent"] / 100)
    
    if ema_diff <= proximity_threshold:
        # Determine direction
        if current_fast < current_slow:  # Fast is below slow
            return "up"
        elif current_fast > current_slow:  # Fast is above slow
            return "down"
    
    return None

async def send_alert(symbol: str, alert_type: str, fast_ema: float, slow_ema: float):
    """Send alerts for both touch and proximity events"""
    current_time = now()
    direction = alert_type.split("_")[0]  # "up" or "down"
    
    # Check time gap based on alert type
    if alert_type.endswith("warning"):
        last_alert_time = symbol_data[symbol]["last_proximity_time"][direction]
        min_gap = timedelta(minutes=CONFIG["min_proximity_gap_minutes"])
        alert_key = "last_proximity_time"
    else:
        last_alert_time = symbol_data[symbol]["last_touch_time"][direction]
        min_gap = timedelta(minutes=CONFIG["min_touch_gap_minutes"])
        alert_key = "last_touch_time"
    
    # Check if enough time has passed since last same-direction alert
    if last_alert_time is not None and (current_time - last_alert_time) < min_gap:
        return False
    
    # Update last alert time
    symbol_data[symbol][alert_key][direction] = current_time
    
    # Create sound type
    sound_type = f"{symbol}_{CONFIG['timeframe']}_{CONFIG['strategy']}_{alert_type}"
    
    # Create message
    if alert_type.endswith("warning"):
        msg = f"⚠️ {symbol.upper()} EMA PROXIMITY {direction.upper()} | Fast EMA={fast_ema:.4f} Slow EMA={slow_ema:.4f} | Time={current_time}"
    else:
        msg = f"🚨 {symbol.upper()} EMA TOUCH {direction.upper()} | Fast EMA={fast_ema:.4f} Slow EMA={slow_ema:.4f} | Time={current_time}"
    
    print(f"[alert-{symbol.upper()}] {msg}")
    
    # Send Telegram alert
    await send_telegram(msg)
    
    # Send FCM alert
    send_fcm_alert(sound_type, msg)
    
    # Send chart if enabled
    if CONFIG["send_chart_on_alert"]:
        await send_chart(symbol, alert_type)
    
    return True

async def check_ema_alerts(symbol: str):
    """Check for both EMA touch and proximity alerts"""
    data = symbol_data[symbol]
    candles = list(data["completed_candles"])
    
    # Include current candle if available
    if data["current_candle"]:
        all_candles = candles + [data["current_candle"]]
    else:
        all_candles = candles
    
    if len(all_candles) < CONFIG["ema_slow"]:
        return
    
    # Calculate current EMAs using TradingView-compatible method
    closes = [c["close"] for c in all_candles]
    current_fast_ema = calculate_ema_single_tradingview(closes, CONFIG["ema_fast"])
    current_slow_ema = calculate_ema_single_tradingview(closes, CONFIG["ema_slow"])
    
    if current_fast_ema is None or current_slow_ema is None:
        return
    
    # Store current EMAs in history
    data["ema_history"].append({
        "timestamp": now(),
        "fast_ema": current_fast_ema,
        "slow_ema": current_slow_ema
    })
    
    # Check for EMA touch
    touch_type = detect_ema_touch(
        data["prev_fast_ema"], 
        data["prev_slow_ema"], 
        current_fast_ema, 
        current_slow_ema
    )
    
    if touch_type:
        await send_alert(symbol, touch_type, current_fast_ema, current_slow_ema)
    
    # Check for EMA proximity
    proximity_type = detect_ema_proximity(current_fast_ema, current_slow_ema)
    
    if proximity_type:
        await send_alert(symbol, f"{proximity_type}_warning", current_fast_ema, current_slow_ema)
    
    # Update previous values
    data["prev_fast_ema"] = current_fast_ema
    data["prev_slow_ema"] = current_slow_ema

# ================== BOOTSTRAP CANDLES ==================
async def bootstrap_candles(symbol: str):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval={CONFIG['timeframe']}&limit={CONFIG['bootstrap_candles']}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
    candles = []
    for c in data:
        candles.append({
            "timestamp": datetime.fromtimestamp(c[0]/1000, tz=timezone.utc),
            "open": float(c[1]),
            "high": float(c[2]),
            "low": float(c[3]),
            "close": float(c[4]),
            "volume": float(c[5])
        })
    symbol_data[symbol]["completed_candles"].extend(candles)
    print(f"[bootstrap] Loaded {len(candles)} candles for {symbol}")

# ================== LIVE WEBSOCKET ==================
async def kline_listener(symbol: str):
    url = f"wss://stream.binance.com:9443/ws/{symbol}@kline_{CONFIG['timeframe']}"
    async for ws in websockets.connect(url):
        try:
            async for msg in ws:
                data = json.loads(msg)
                k = data['k']
                candle = {
                    "timestamp": datetime.fromtimestamp(k['t']/1000, tz=timezone.utc),
                    "open": float(k['o']),
                    "high": float(k['h']),
                    "low": float(k['l']),
                    "close": float(k['c']),
                    "volume": float(k['v'])
                }
                if k['x']:
                    symbol_data[symbol]["completed_candles"].append(candle)
                    # Check for alerts on candle close
                    await check_ema_alerts(symbol)
                else:
                    symbol_data[symbol]["current_candle"] = candle
        except Exception as e:
            print(f"[websocket] error {e}, reconnecting...")
            await asyncio.sleep(5)
            continue

# ================== CHART SENDING ==================
async def send_chart(symbol: str, alert_type: str = "update"):
    if not CONFIG["enable_chart_sending"] and not CONFIG["send_chart_on_alert"]:
        return
    data = symbol_data[symbol]
    candles = list(data["completed_candles"])
    if not candles:
        return
    
    # Take last 50 candles for better chart readability (display only)
    display_candles = candles[-50:] if len(candles) > 50 else candles
    
    # But use ALL available candles for EMA calculation to ensure we have enough data
    all_closes = [c["close"] for c in candles]
    display_times = [c["timestamp"] for c in display_candles]
    
    # Prepare data for candlestick chart
    ohlc_data = []
    for i, candle in enumerate(display_candles):
        # Convert datetime to matplotlib date format
        date_num = mdates.date2num(candle["timestamp"])
        ohlc_data.append((
            date_num,
            candle["open"],
            candle["high"], 
            candle["low"],
            candle["close"]
        ))
    
    plt.figure(figsize=(12, 6))
    ax = plt.subplot(1, 1, 1)
    
    # Plot candlestick chart
    candlestick_ohlc(ax, ohlc_data, width=0.0004, colorup='green', colordown='red', alpha=0.8)
    
    # Calculate EMAs using ALL available data for accuracy
    # Then extract only the portion that corresponds to our display candles
    for period in CONFIG["ema_periods"]:
        # Calculate EMA for entire dataset
        full_ema_series = calculate_ema_tradingview(all_closes, period)
        
        if full_ema_series:
            # Extract only the EMA values that correspond to our display candles
            # If we're showing last 50 candles, take last 50 EMA values
            display_ema_values = full_ema_series[-len(display_candles):]
            
            # Filter out None values and prepare for plotting
            valid_data = []
            for i, (candle_time, ema_value) in enumerate(zip(display_times, display_ema_values)):
                if ema_value is not None:
                    valid_data.append((candle_time, ema_value))
            
            if valid_data:
                valid_times, valid_ema = zip(*valid_data)
                color = EMA_COLORS.get(period, "gray")
                # Convert times to matplotlib format for plotting
                valid_dates = [mdates.date2num(t) for t in valid_times]
                ax.plot(valid_dates, valid_ema, label=f"EMA{period}", color=color, linewidth=1.5)
                print(f"[chart] Plotted EMA{period} with {len(valid_data)} data points")
    
    plt.legend()
    
    if alert_type != "update":
        plt.title(f"{symbol.upper()} EMA {alert_type.upper()} Alert - {CONFIG['timeframe']}")
    else:
        plt.title(f"{symbol.upper()} Candlestick Chart - {CONFIG['timeframe']}")
        
    # Format x-axis
    ax.xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    plt.xticks(rotation=45)
    plt.grid(True, alpha=0.3)
    
    # Adjust layout
    plt.tight_layout()

    buf = io.BytesIO()
    plt.savefig(buf, format="png", dpi=100, bbox_inches='tight')
    plt.close()
    buf.seek(0)

    token = CONFIG["TELEGRAM_TOKEN"]
    chat_id = CONFIG["CHAT_ID"]
    if token and chat_id:
        url = f"https://api.telegram.org/bot{token}/sendPhoto"
        form = aiohttp.FormData()
        form.add_field("chat_id", chat_id)
        form.add_field("photo", buf, filename="chart.png", content_type="image/png")
        try:
            async with aiohttp.ClientSession() as session:
                await session.post(url, data=form)
        except Exception as e:
            print("[telegram-photo] error", e)

async def chart_sender_loop():
    while True:
        if CONFIG["enable_chart_sending"]:
            for symbol in SYMBOLS:
                await send_chart(symbol)
        await asyncio.sleep(CONFIG["chart_interval_sec"])

# ================== HTTP ENDPOINTS ==================
async def toggle_chart(request):
    enable = request.query.get("enable")
    if enable is not None:
        CONFIG["enable_chart_sending"] = enable.lower() in ["1", "true", "yes"]
        print(f"[toggle_chart] Chart sending set to {CONFIG['enable_chart_sending']}")
    return web.json_response({"enable_chart_sending": CONFIG["enable_chart_sending"]})

async def set_chart_interval(request):
    sec = request.query.get("sec")
    if sec:
        try:
            CONFIG["chart_interval_sec"] = int(sec)
            print(f"[set_chart_interval] Chart interval set to {CONFIG['chart_interval_sec']}s")
        except ValueError:
            pass
    return web.json_response({"chart_interval_sec": CONFIG["chart_interval_sec"]})

async def toggle_alert_chart(request):
    enable = request.query.get("enable")
    if enable is not None:
        CONFIG["send_chart_on_alert"] = enable.lower() in ["1", "true", "yes"]
        print(f"[toggle_alert_chart] Send chart on alert set to {CONFIG['send_chart_on_alert']}")
    return web.json_response({"send_chart_on_alert": CONFIG["send_chart_on_alert"]})

async def set_proximity_threshold(request):
    threshold = request.query.get("threshold")
    if threshold:
        try:
            CONFIG["proximity_threshold_percent"] = float(threshold)
            print(f"[set_proximity_threshold] Proximity threshold set to {CONFIG['proximity_threshold_percent']}%")
        except ValueError:
            pass
    return web.json_response({"proximity_threshold_percent": CONFIG["proximity_threshold_percent"]})

async def set_alert_gaps(request):
    proximity_gap = request.query.get("proximity_gap")
    touch_gap = request.query.get("touch_gap")
    
    if proximity_gap:
        try:
            CONFIG["min_proximity_gap_minutes"] = int(proximity_gap)
            print(f"[set_alert_gaps] Proximity gap set to {CONFIG['min_proximity_gap_minutes']} minutes")
        except ValueError:
            pass
    
    if touch_gap:
        try:
            CONFIG["min_touch_gap_minutes"] = int(touch_gap)
            print(f"[set_alert_gaps] Touch gap set to {CONFIG['min_touch_gap_minutes']} minutes")
        except ValueError:
            pass
    
    return web.json_response({
        "min_proximity_gap_minutes": CONFIG["min_proximity_gap_minutes"],
        "min_touch_gap_minutes": CONFIG["min_touch_gap_minutes"]
    })

# ================== APP ==================
async def on_startup(app):
    init_firebase()
    init_google_sheets()
    for symbol in SYMBOLS:
        await bootstrap_candles(symbol)
        app[f'worker_{symbol}'] = asyncio.create_task(kline_listener(symbol))
    app['worker_chart'] = asyncio.create_task(chart_sender_loop())
    await send_telegram("EMA Touch Alert System Initialization complete")

async def on_cleanup(app):
    for symbol in SYMBOLS:
        task = app.get(f'worker_{symbol}')
        if task:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
    task = app.get('worker_chart')
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

def create_app():
    app = web.Application()
    app.router.add_get("/toggle_chart", toggle_chart)
    app.router.add_get("/set_chart_interval", set_chart_interval)
    app.router.add_get("/toggle_alert_chart", toggle_alert_chart)
    app.router.add_get("/set_proximity_threshold", set_proximity_threshold)
    app.router.add_get("/set_alert_gaps", set_alert_gaps)
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
