import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from collections import deque

import aiohttp
from aiohttp import web
import websockets
import numpy as np
import firebase_admin
from firebase_admin import credentials, messaging


# ========== ENVIRONMENT SETUP ==========
# Check if we're running in production (common hosting platforms)
IS_PRODUCTION = os.environ.get('RENDER', False) or os.environ.get('PYTHONANYWHERE', False) or os.environ.get('HEROKU', False)

# Only load .env file in local development
if not IS_PRODUCTION:
    try:
        from dotenv import load_dotenv
        # Try loading from .gitignore/.env first, then fall back to root .env
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



# ================== CONFIG (defaults) ==================
CONFIG = {
    "symbol": "ethusdt",
    "interval": "5m",
    "bootstrap_candles": 50,
    "cci_length": 9,
    "signal_length": 9,
    "signal_type": "sma",  # "sma" or "ema"
    "detection_mode": "tick",  # "candle" or "tick"
    "use_tick_updates": True,
    "zone_high": 95,
    "zone_low": -95,
    "min_cross_gap_minutes": 20,
    "print_interval_sec": 15,
    "close_grace_sec": 2,

    "alerts": {
        "telegram": True,
        "playfile": False,
        "plyer": False,
        "fcm": True
    },
    "alarm_file": "alarm.wav",

    # TELEGRAM: read from environment for safety
    "TELEGRAM_TOKEN": os.environ.get("TELEGRAM_TOKEN", ""),
    "CHAT_ID": os.environ.get("CHAT_ID", ""),

    # FCM: Firebase Cloud Messaging - will be loaded from environment
    "DEVICE_TOKENS_FILE": "device_token_lists.json"
}
# ============================================

completed_candles = deque(maxlen=500)
current_candle = None
last_cross_time = None
prev_diff = None
signal_history = deque(maxlen=100)
prev_signal = None
last_print_time = 0

# Firebase app instance
firebase_app = None

# ------------- FCM INITIALIZATION ----------------
def init_firebase():
    global firebase_app
    if not CONFIG["alerts"]["fcm"]:
        print("[FCM] FCM alerts disabled in config")
        return
    
    try:
        # Get Firebase credentials from environment variables
        firebase_cred_json = os.environ.get("FIREBASE_CREDENTIALS")
        
        if not firebase_cred_json:
            print("[FCM] FIREBASE_CREDENTIALS environment variable not set")
            return
            
        # Parse the JSON string from environment variable
        cred_dict = json.loads(firebase_cred_json)
        
        # Fix the private key - replace escaped newlines with actual newlines
        if 'private_key' in cred_dict:
            cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
        
        cred = credentials.Certificate(cred_dict)
        firebase_app = firebase_admin.initialize_app(cred)
        print("[FCM] Firebase app initialized successfully from environment variables")
        
    except json.JSONDecodeError as e:
        print(f"[FCM] Error parsing FIREBASE_CREDENTIALS: {e}")
    except Exception as e:
        print(f"[FCM] init error: {e}")
        firebase_app = None

def load_valid_tokens():
    """Return list of valid tokens from device_token_lists.json"""
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
                print(f"[FCM] valid token: {t['device_token'][:15]}... (expires {exp})")
                valid.append({
                    "token": t["device_token"],
                    "customer": t.get("customer_name", "Unknown")
                })
            else:
                print(f"[FCM] expired token: {t['device_token'][:15]}... (expired {exp})")
        except ValueError:
            print(f"[FCM] invalid exp date for token: {t['device_token'][:15]}...")
    return valid

def send_fcm_alert(action="play", message="CCI Alert Triggered"):
    """Send FCM alert with data payload (so Android app can handle it in foreground)"""
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
                    "action": action,
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
    return datetime.now(timezone.utc).astimezone()

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

async def send_fcm_notification(title, message):
    if not CONFIG["alerts"]["fcm"]:
        return
    
    # Send both notification and data payload for better compatibility
    if firebase_app:
        send_fcm_alert("play", f"{title}: {message}")
    
    # Also try the HTTP API method for broader compatibility
    token = os.environ.get("FCM_TOKEN", "")
    topic = os.environ.get("FCM_TOPIC", "cci_alerts")
    if not token:
        print("[FCM HTTP] token not set; skipping FCM HTTP notification.")
        return
    
    url = "https://fcm.googleapis.com/fcm/send"
    headers = {"Authorization": f"key={token}", "Content-Type": "application/json"}
    payload = {
        "to": f"/topics/{topic}",
        "notification": {"title": title, "body": message, "sound": "default"},
        "data": {"title": title, "message": message, "timestamp": datetime.now().isoformat()}
    }
    try:
        async with aiohttp.ClientSession() as session:
            async with session.post(url, json=payload, headers=headers) as resp:
                if resp.status != 200:
                    print(f"[FCM HTTP] Error {resp.status}: {await resp.text()}")
                else:
                    print("[FCM HTTP] Notification sent successfully")
    except Exception as e:
        print("[FCM HTTP] error", e)

def play_alert():
    if CONFIG["alerts"]["plyer"]:
        try:
            from plyer import notification
            notification.notify(title="CCI Alert", message="Signal cross", timeout=5)
        except Exception as e:
            print("[plyer] error", e)
    if CONFIG["alerts"]["playfile"]:
        if os.path.exists(CONFIG["alarm_file"]):
            try:
                import playsound
                playsound.playsound(CONFIG["alarm_file"])
            except Exception as e:
                print("[playfile] error", e)
        else:
            print(f"[warning] alarm_file '{CONFIG['alarm_file']}' not found.")

# ------------- CCI CALC ----------------
def compute_cci(candles, length=20):
    if len(candles) < length:
        return None
    closes = np.array([c['close'] for c in candles])
    highs = np.array([c['high'] for c in candles])
    lows = np.array([c['low'] for c in candles])
    tps = (highs + lows + closes) / 3
    tp = tps[-1]
    sma = np.mean(tps[-length:])
    md = np.mean(np.abs(tps[-length:] - sma))
    if md == 0:
        return 0
    return (tp - sma) / (0.015 * md)

def update_signal(cci_values, length, signal_type="ema"):
    if len(cci_values) < length:
        return None
    if signal_type == "sma":
        return np.mean(cci_values[-length:])
    else:
        # EMA-like weighting
        cci_array = np.array(cci_values[-length:])
        weights = np.exp(np.linspace(-1, 0, length))
        weights /= weights.sum()
        return np.dot(cci_array, weights)

# ------------- ALERT CHECK ----------------
async def check_cross():
    global prev_diff, last_cross_time, prev_signal, signal_history

    cci = compute_cci(list(completed_candles), CONFIG["cci_length"])
    if cci is None:
        return
    signal_history.append(cci)
    signal = update_signal(list(signal_history), CONFIG["signal_length"], CONFIG["signal_type"])
    if signal is None:
        return
    prev_signal = signal

    global last_print_time
    if time.time() - last_print_time > CONFIG["print_interval_sec"]:
        print(f"[CCI] {now()} CCI={cci:.3f} Signal={signal:.3f}")
        last_print_time = time.time()

    diff = cci - signal

    # simplified: ignore zone for testing
    crossed_up = prev_diff is not None and prev_diff < 0 and diff > 0
    crossed_down = prev_diff is not None and prev_diff > 0 and diff < 0

    if crossed_up or crossed_down:
        current_time = now()
        time_since_last = current_time - last_cross_time if last_cross_time else timedelta(minutes=CONFIG["min_cross_gap_minutes"] + 1)
        if last_cross_time is None or time_since_last > timedelta(minutes=CONFIG["min_cross_gap_minutes"]):
            last_cross_time = current_time
            direction = "UP" if crossed_up else "DOWN"
            msg = f"🚨 CCI CROSS {direction} | CCI={cci:.2f} Signal={signal:.2f} Time={current_time}"
            print("[alert]", msg)
            await send_telegram(msg)
            await send_fcm_notification(f"CCI Cross {direction}", f"CCI: {cci:.2f}, Signal: {signal:.2f}")
            play_alert()

    prev_diff = diff

# ------------- DATA HANDLING ----------------
def build_candle(kline):
    return {
        "open_time": int(kline[0]),
        "open": float(kline[1]),
        "high": float(kline[2]),
        "low": float(kline[3]),
        "close": float(kline[4]),
        "volume": float(kline[5]),
        "close_time": int(kline[6])
    }

async def bootstrap():
    url = f"https://api.binance.com/api/v3/klines?symbol={CONFIG['symbol'].upper()}&interval={CONFIG['interval']}&limit={CONFIG['bootstrap_candles']}"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as resp:
            data = await resp.json()
    if "code" in data:
        print(f"[bootstrap] error from Binance: {data}")
        return
    print(f"[bootstrap] fetched {len(data)} candles")
    for k in data:
        candle = {
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4])
        }
        completed_candles.append(candle)
        cci_val = compute_cci(list(completed_candles), CONFIG["cci_length"])
        if cci_val is not None:
            signal_history.append(cci_val)
    print(f"[bootstrap] completed_candles: {len(completed_candles)}, signal_history: {len(signal_history)}")

async def handle_trade(trade):
    global current_candle
    price = float(trade['p'])
    ts = int(trade['T'])
    dt = datetime.fromtimestamp(ts/1000, tz=timezone.utc)
    minute = dt.minute - dt.minute % 5
    candle_open = dt.replace(second=0, microsecond=0, minute=minute)

    if current_candle is None or candle_open != current_candle["time"]:
        if current_candle:
            # close previous candle
            completed_candles.append({
                "open": current_candle["open"],
                "high": current_candle["high"],
                "low": current_candle["low"],
                "close": current_candle["close"]
            })
            print(f"[candle] closed {current_candle['time']} o:{current_candle['open']} h:{current_candle['high']} l:{current_candle['low']} c:{current_candle['close']}")
            await check_cross()
        current_candle = {"time": candle_open, "open": price, "high": price, "low": price, "close": price}
        print(f"[candle] opened new candle at {candle_open}")
    else:
        current_candle["close"] = price
        current_candle["high"] = max(current_candle["high"], price)
        current_candle["low"] = min(current_candle["low"], price)

    if CONFIG["use_tick_updates"]:
        tmp_candles = list(completed_candles) + [{
            "open": current_candle["open"],
            "high": current_candle["high"],
            "low": current_candle["low"],
            "close": current_candle["close"]
        }]
        cci = compute_cci(tmp_candles, CONFIG["cci_length"])
        if cci:
            tmp_signal_history = list(signal_history) + [cci]
            signal = update_signal(tmp_signal_history, CONFIG["signal_length"], CONFIG["signal_type"])
            if signal:
                print(f"[tick] price={price:.2f} CCI={cci:.2f} Signal={signal:.2f} Diff={cci-signal:.2f}")

# ------------- MAIN (background worker) ----------------
async def ws_loop():
    url = f"wss://stream.binance.com:9443/ws/{CONFIG['symbol']}@trade"
    async with websockets.connect(url) as ws:
        print(f"[ws] connected to {url}")
        async for msg in ws:
            data = json.loads(msg)
            print(f"[ws] received trade: {data['p']} at {datetime.fromtimestamp(data['T']/1000, tz=timezone.utc)}")
            await handle_trade(data)

async def worker_loop():
    # Send initialization alert
    print("[init alert] Sending initialization alert")
    send_fcm_alert("play", "CCI Alert System Initialization complete")
    await send_telegram("CCI Alert System Initialization complete")
    
    await bootstrap()
    while True:
        try:
            await ws_loop()
        except asyncio.CancelledError:
            print("[worker] cancelled")
            break
        except Exception as e:
            print("[ws] error", e)
            await asyncio.sleep(5)

# ------------- SIMPLE HTTP SERVER (for health checks / keepalive) -------------
async def health(request):
    return web.Response(text="OK")

async def on_startup(app):
    print("[app] starting background worker")
    # Initialize Firebase
    init_firebase()
    app['worker_task'] = asyncio.create_task(worker_loop())

async def on_cleanup(app):
    print("[app] stopping background worker")
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