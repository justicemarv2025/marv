import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Dict, Deque, Optional, List
import gspread
from google.oauth2.service_account import Credentials

import aiohttp
from aiohttp import web
import websockets
import numpy as np
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
# =======================================

# ================== CONFIG ==================
SYMBOLS = [
    "ethusdt"
]

CONFIG = {
    "symbols": SYMBOLS,
    "timeframe": "1m",  # Variable timeframe
    "ema_fast": 20,     # Variable fast EMA
    "ema_slow": 200,    # Variable slow EMA
    "bootstrap_candles": 300,  # Enough candles for 200 EMA calculation
    
    # Alert settings
    "min_touch_gap_minutes": 30,  # Minimum time between same-direction alerts
    "print_interval_sec": 30,
    
    # Google Sheets logging
    "enable_google_sheets_logging": False,  # Toggle for Google Sheets logging
    
    # Alert methods
    "alerts": {
        "telegram": True,
        "fcm": True
    },
    
    # API keys and tokens
    "TELEGRAM_TOKEN": os.environ.get("TELEGRAM_TOKEN", ""),
    "CHAT_ID": os.environ.get("CHAT_ID", ""),
    "DEVICE_TOKENS_FILE": "device_token_lists.json",
    
    # Google Sheets credentials (for logging)
    "GOOGLE_SHEETS_CREDENTIALS": os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
}
# ============================================

# Data structures for each symbol
symbol_data: Dict[str, Dict] = {}

# Initialize data structures for each symbol
for symbol in SYMBOLS:
    symbol_data[symbol] = {
        "completed_candles": deque(maxlen=500),
        "current_candle": None,
        "last_touch_time": {"up": None, "down": None},  # Track last touch time for each direction
        "prev_fast_ema": None,
        "prev_slow_ema": None,
        "last_print_time": 0,
        "ema_history": deque(maxlen=10)  # Keep recent EMA values for touch detection
    }

# Firebase app instance
firebase_app = None
# Google Sheets client
google_sheets_client = None

# ------------- GOOGLE SHEETS INITIALIZATION ----------------
def init_google_sheets():
    global google_sheets_client
    if not CONFIG["enable_google_sheets_logging"]:
        print("[Google Sheets] Logging disabled in config")
        return
    
    try:
        google_cred_json = os.environ.get("GOOGLE_SHEETS_CREDENTIALS")
        if not google_cred_json:
            print("[Google Sheets] GOOGLE_SHEETS_CREDENTIALS environment variable not set")
            return
            
        cred_dict = json.loads(google_cred_json)
        scopes = ['https://www.googleapis.com/auth/spreadsheets']
        credentials = Credentials.from_service_account_info(cred_dict, scopes=scopes)
        google_sheets_client = gspread.authorize(credentials)
        print("[Google Sheets] Initialized successfully")
        
    except json.JSONDecodeError as e:
        print(f"[Google Sheets] Error parsing credentials: {e}")
    except Exception as e:
        print(f"[Google Sheets] init error: {e}")
        google_sheets_client = None

def get_sheet_filename(symbol: str) -> str:
    timeframe = CONFIG["timeframe"].replace("m", "min").replace("h", "hour").replace("d", "day")
    return f"{symbol}_{timeframe}_ematouch.json"

async def log_to_google_sheets(symbol: str, touch_type: str, ema_fast: float, ema_slow: float):
    if not CONFIG["enable_google_sheets_logging"] or not google_sheets_client:
        return
    
    try:
        # Create or get the spreadsheet
        filename = get_sheet_filename(symbol)
        try:
            spreadsheet = google_sheets_client.open(filename)
        except gspread.SpreadsheetNotFound:
            spreadsheet = google_sheets_client.create(filename)
            # Share with yourself (optional)
            # spreadsheet.share('your-email@gmail.com', perm_type='user', role='writer')
        
        # Get or create worksheet
        try:
            worksheet = spreadsheet.worksheet("EMA Touches")
        except gspread.WorksheetNotFound:
            worksheet = spreadsheet.add_worksheet(title="EMA Touches", rows=1000, cols=6)
            # Add headers
            worksheet.append_row(["Alert Time", "Symbol", "Timeframe", "Touch Type", "EMA Fast", "EMA Slow"])
        
        # Append the data
        alert_time = datetime.now(timezone.utc).isoformat()
        worksheet.append_row([
            alert_time,
            symbol.upper(),
            CONFIG["timeframe"],
            touch_type.upper(),
            round(ema_fast, 4),
            round(ema_slow, 4)
        ])
        
        print(f"[Google Sheets] Logged {symbol} {touch_type} touch to {filename}")
        
    except Exception as e:
        print(f"[Google Sheets] Error logging to sheet: {e}")

# ------------- FCM INITIALIZATION ----------------
def init_firebase():
    global firebase_app
    if not CONFIG["alerts"]["fcm"]:
        print("[FCM] FCM alerts disabled in config")
        return
    
    try:
        firebase_cred_json = os.environ.get("FIREBASE_CREDENTIALS")
        if not firebase_cred_json:
            print("[FCM] FIREBASE_CREDENTIALS environment variable not set")
            return
            
        cred_dict = json.loads(firebase_cred_json)
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
        
        cred = credentials.Certificate(cred_dict)
        firebase_app = firebase_admin.initialize_app(cred)
        print("[FCM] Firebase app initialized successfully")
        
    except json.JSONDecodeError as e:
        print(f"[FCM] Error parsing FIREBASE_CREDENTIALS: {e}")
    except Exception as e:
        print(f"[FCM] init error: {e}")
        firebase_app = None

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

def send_fcm_alert(sound_type="up", message="EMA Touch Alert Triggered"):
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
            print(f"✓ FCM Message sent to {t['customer']}: {t['token'][:15]}... id={resp}")
        except Exception as e:
            print(f"✗ Failed to send FCM to {t['customer']}: {e}")

# ------------- UTILITIES ----------------
def now():
    return datetime.now(timezone.utc)

async def send_telegram(msg):
    if not CONFIG["alerts"]["telegram"]:
        return
    token = CONFIG["TELEGRAM_TOKEN"]
    chat_id = CONFIG["CHAT_ID"]
    if not token or not chat_id:
        print("[telegram] token or chat_id not set; skipping telegram.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, data={"chat_id": chat_id, "text": msg})
    except Exception as e:
        print("[telegram] error", e)

async def send_fcm_notification(title, message, sound_type):
    if not CONFIG["alerts"]["fcm"]:
        return
    
    if firebase_app:
        send_fcm_alert(sound_type, f"{title}: {message}")
    
    token = os.environ.get("FCM_TOKEN", "")
    topic = os.environ.get("FCM_TOPIC", "ema_touch_alerts")
    if not token:
        print("[FCM HTTP] token not set; skipping FCM HTTP notification.")
        return
    
    url = "https://fcm.googleapis.com/fcm/send"
    headers = {"Authorization": f"key={token}", "Content-Type": "application/json"}
    payload = {
        "to": f"/topics/{topic}",
        "notification": {"title": title, "body": message, "sound": "default"},
        "data": {
            "title": title, 
            "message": message, 
            "timestamp": datetime.now().isoformat(),
            "action": "play",
            "soundType": sound_type
        }
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    print(f"[FCM HTTP] Error {resp.status}: {await resp.text()}")
    except Exception as e:
        print("[FCM HTTP] error", e)

# ------------- EMA CALCULATION ----------------
def calculate_ema(prices, period):
    """Calculate EMA for given prices and period"""
    if len(prices) < period:
        return None
    
    # Simple EMA calculation
    alpha = 2 / (period + 1)
    ema = prices[0]
    
    for price in prices[1:]:
        ema = (price * alpha) + (ema * (1 - alpha))
    
    return ema

def calculate_emas(candles, fast_period, slow_period):
    """Calculate both fast and slow EMAs"""
    if len(candles) < slow_period:
        return None, None
    
    closes = [candle['close'] for candle in candles]
    
    # Calculate fast EMA
    fast_ema = calculate_ema(closes[-fast_period:], fast_period)
    
    # Calculate slow EMA
    slow_ema = calculate_ema(closes, slow_period)
    
    return fast_ema, slow_ema

# ------------- TOUCH DETECTION ----------------
def detect_ema_touch(prev_fast, prev_slow, current_fast, current_slow):
    """
    Detect if EMAs have touched.
    Returns: "up" if fast touches slow from below, "down" if fast touches slow from above, None if no touch
    """
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

async def check_ema_touch(symbol: str):
    data = symbol_data[symbol]
    completed_candles = list(data["completed_candles"])
    
    # Include current candle if available
    if data["current_candle"]:
        all_candles = completed_candles + [data["current_candle"]]
    else:
        all_candles = completed_candles
    
    if len(all_candles) < CONFIG["ema_slow"]:
        return
    
    # Calculate current EMAs
    current_fast_ema, current_slow_ema = calculate_emas(all_candles, CONFIG["ema_fast"], CONFIG["ema_slow"])
    
    if current_fast_ema is None or current_slow_ema is None:
        return
    
    # Store current EMAs in history
    data["ema_history"].append({
        "timestamp": now(),
        "fast_ema": current_fast_ema,
        "slow_ema": current_slow_ema
    })
    
    # Print status periodically
    if time.time() - data["last_print_time"] > CONFIG["print_interval_sec"]:
        print(f"[EMA-{symbol.upper()}] {now()} Fast EMA={current_fast_ema:.4f} Slow EMA={current_slow_ema:.4f} Diff={(current_fast_ema-current_slow_ema):.4f}")
        data["last_print_time"] = time.time()
    
    # Check for touch detection
    touch_type = detect_ema_touch(
        data["prev_fast_ema"], 
        data["prev_slow_ema"], 
        current_fast_ema, 
        current_slow_ema
    )
    
    if touch_type:
        current_time = now()
        last_touch_time = data["last_touch_time"][touch_type]
        
        # Check if enough time has passed since last same-direction touch
        if last_touch_time is None or (current_time - last_touch_time) > timedelta(minutes=CONFIG["min_touch_gap_minutes"]):
            data["last_touch_time"][touch_type] = current_time
            
            sound_type = "up" if touch_type == "up" else "down"
            msg = f"🚨 {symbol.upper()} EMA TOUCH {touch_type.upper()} | Fast EMA={current_fast_ema:.4f} Slow EMA={current_slow_ema:.4f} Time={current_time}"
            print(f"[alert-{symbol.upper()}]", msg)
            
            # Send alerts
            await send_telegram(msg)
            await send_fcm_notification(f"{symbol.upper()} EMA Touch {touch_type.upper()}", 
                                      f"Fast EMA: {current_fast_ema:.4f}, Slow EMA: {current_slow_ema:.4f}", 
                                      sound_type)
            
            # Log to Google Sheets
            await log_to_google_sheets(symbol, touch_type, current_fast_ema, current_slow_ema)
    
    # Update previous values
    data["prev_fast_ema"] = current_fast_ema
    data["prev_slow_ema"] = current_slow_ema

# ------------- DATA HANDLING ----------------
async def bootstrap(symbol: str):
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval={CONFIG['timeframe']}&limit={CONFIG['bootstrap_candles']}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
    except Exception as e:
        print(f"[bootstrap-{symbol.upper()}] Failed to fetch data: {e}")
        await asyncio.sleep(5)
        # Retry once
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
    
    if "code" in data:
        print(f"[bootstrap-{symbol.upper()}] error from Binance: {data}")
        return
    
    completed_candles = symbol_data[symbol]["completed_candles"]
    
    for k in data:
        candle = {
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4]),
            "volume": float(k[5]),
            "timestamp": datetime.fromtimestamp(k[0]/1000, tz=timezone.utc)
        }
        completed_candles.append(candle)
    
    print(f"[bootstrap-{symbol.upper()}] loaded {len(completed_candles)} candles")

async def handle_trade(symbol: str, trade: dict):
    data = symbol_data[symbol]
    price = float(trade['p'])
    ts = int(trade['T'])
    dt = datetime.fromtimestamp(ts/1000, tz=timezone.utc)
    
    # Determine candle boundaries based on timeframe
    if CONFIG['timeframe'].endswith('m'):
        minutes = int(CONFIG['timeframe'][:-1])
        minute = dt.minute - (dt.minute % minutes)
        candle_open = dt.replace(second=0, microsecond=0, minute=minute)
    elif CONFIG['timeframe'].endswith('h'):
        hours = int(CONFIG['timeframe'][:-1])
        hour = dt.hour - (dt.hour % hours)
        candle_open = dt.replace(second=0, microsecond=0, minute=0, hour=hour)
    else:
        # Default to 1 minute if unknown format
        candle_open = dt.replace(second=0, microsecond=0)

    if data["current_candle"] is None or candle_open != data["current_candle"]["time"]:
        # New candle started
        if data["current_candle"]:
            data["completed_candles"].append(data["current_candle"])
            print(f"[candle-{symbol.upper()}] closed {data['current_candle']['time']}")
        
        data["current_candle"] = {
            "time": candle_open, 
            "open": price, 
            "high": price, 
            "low": price, 
            "close": price,
            "volume": 0
        }
        print(f"[candle-{symbol.upper()}] opened new candle at {candle_open}")
    else:
        # Update current candle
        data["current_candle"]["close"] = price
        data["current_candle"]["high"] = max(data["current_candle"]["high"], price)
        data["current_candle"]["low"] = min(data["current_candle"]["low"], price)
        data["current_candle"]["volume"] += float(trade['q'])

    # Check for EMA touch on every trade (real-time detection)
    await check_ema_touch(symbol)

# ------------- WEBSOCKET HANDLING ----------------
async def ws_loop(symbol: str):
    url = f"wss://stream.binance.com:9443/ws/{symbol}@trade"
    retry_count = 0
    max_retries = 10
    while retry_count < max_retries:
        try:
            async with websockets.connect(url) as ws:
                print(f"[ws-{symbol.upper()}] connected to {url}")
                retry_count = 0
                async for msg in ws:
                    data = json.loads(msg)
                    await handle_trade(symbol, data)
        except asyncio.CancelledError:
            print(f"[worker-{symbol.upper()}] cancelled")
            break
        except Exception as e:
            retry_count += 1
            wait_time = min(2 ** retry_count, 30)
            print(f"[ws-{symbol.upper()}] error (Attempt {retry_count}/{max_retries}): {e}. Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)
    print(f"[ws-{symbol.upper()}] Failed to connect after {max_retries} attempts. Stopping.")

async def symbol_worker(symbol: str):
    await bootstrap(symbol)
    await ws_loop(symbol)

async def worker_loop():
    print("[init alert] Sending initialization alert")
    init_firebase()
    init_google_sheets()
    
    send_fcm_alert("up", "EMA Touch Alert System Initialization complete")
    await send_telegram("EMA Touch Alert System Initialization complete")
    
    symbol_tasks = [asyncio.create_task(symbol_worker(symbol)) for symbol in SYMBOLS]
    
    await asyncio.gather(*symbol_tasks, return_exceptions=True)

# ------------- HTTP SERVER ----------------
async def health(request):
    return web.Response(text="EMA Touch Alert System OK")

async def on_startup(app):
    print("[app] starting background workers")
    app['worker_task'] = asyncio.create_task(worker_loop())

async def on_cleanup(app):
    print("[app] stopping background workers")
    task = app.get('worker_task')
    if task:
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass

def create_app():
    app = web.Application()
    app.add_routes([web.get("/", health), web.get("/healthz", health)])
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
