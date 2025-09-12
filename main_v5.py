# main_v5.py - Modified for concurrent execution with ETHUSDT
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

# ================== CONFIG (defaults) ==================
CONFIG = {
    "symbol": os.environ.get("SYMBOL_V5", "ethusdt"),  # Changed to V5 but kept ethusdt
    "interval": os.environ.get("INTERVAL", "5m"),
    "bootstrap_candles": int(os.environ.get("BOOTSTRAP_CANDLES", 50)),
    "cci_length": int(os.environ.get("CCI_LENGTH", 9)),
    "signal_length": int(os.environ.get("SIGNAL_LENGTH", 9)),
    "signal_type": os.environ.get("SIGNAL_TYPE", "sma"),  # "sma" or "ema"
    "detection_mode": os.environ.get("DETECTION_MODE", "tick"),  # "candle" or "tick"
    "use_tick_updates": os.environ.get("USE_TICK_UPDATES", "True").lower() in ("1","true","yes"),
    "zone_high": float(os.environ.get("ZONE_HIGH", 95)),
    "zone_low": float(os.environ.get("ZONE_LOW", -95)),
    "min_cross_gap_minutes": int(os.environ.get("MIN_CROSS_GAP_MINUTES", 20)),
    "print_interval_sec": int(os.environ.get("PRINT_INTERVAL_SEC", 15)),
    "close_grace_sec": int(os.environ.get("CLOSE_GRACE_SEC", 2)),

    "alerts": {
        "telegram": True,
        "playfile": False,
        "plyer": False,
        "fcm": True
    },
    "alarm_file": "alarm.wav",

    # TELEGRAM: read from environment for safety
    "TELEGRAM_TOKEN": os.environ.get("TELEGRAM_TOKEN5", ""),  # Changed to avoid conflict
    "CHAT_ID": os.environ.get("CHAT_ID", ""),  # Changed to avoid conflict

    # FCM: Firebase Cloud Messaging
    "FCM_TOKEN": os.environ.get("FCM_TOKEN", ""),  # Changed to avoid conflict
    "FCM_TOPIC": os.environ.get("FCM_TOPIC_V5", "cci_alerts_v5")  # Changed to avoid conflict
}
# ============================================

completed_candles = deque(maxlen=500)
current_candle = None
last_cross_time = None
prev_diff = None
signal_history = deque(maxlen=100)
prev_signal = None
last_print_time = 0

# ------------- UTILITIES ----------------
def now():
    return datetime.now(timezone.utc).astimezone()

async def send_telegram(msg):
    if not CONFIG["alerts"]["telegram"]:
        return
    token = CONFIG["TELEGRAM_TOKEN"]
    chat_id = CONFIG["CHAT_ID"]
    if not token or not chat_id:
        print("[telegram-V5] token or chat_id not set; skipping telegram.")
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, data={"chat_id": chat_id, "text": msg})
    except Exception as e:
        print("[telegram-V5] error", e)

async def send_fcm_notification(title, message):
    if not CONFIG["alerts"]["fcm"]:
        return
    token = CONFIG["FCM_TOKEN"]
    topic = CONFIG["FCM_TOPIC"]
    if not token:
        print("[FCM-V5] token not set; skipping FCM notification.")
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
                    print(f"[FCM-V5] Error {resp.status}: {await resp.text()}")
                else:
                    print("[FCM-V5] Notification sent successfully")
    except Exception as e:
        print("[FCM-V5] error", e)

def play_alert():
    if CONFIG["alerts"]["plyer"]:
        try:
            from plyer import notification
            notification.notify(title="CCI Alert V5", message="Signal cross", timeout=5)
        except Exception as e:
            print("[plyer-V5] error", e)
    if CONFIG["alerts"]["playfile"]:
        if os.path.exists(CONFIG["alarm_file"]):
            try:
                import playsound
                playsound.playsound(CONFIG["alarm_file"])
            except Exception as e:
                print("[playfile-V5] error", e)
        else:
            print(f"[warning-V5] alarm_file '{CONFIG['alarm_file']}' not found.")

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
        print(f"[CCI-V5] {now()} CCI={cci:.3f} Signal={signal:.3f}")  # Added V5 identifier
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
            msg = f"🚨 CCI-V5 CROSS {direction} | CCI={cci:.2f} Signal={signal:.2f} Time={current_time}"  # Added V5 identifier
            print("[alert-V5]", msg)  # Added V5 identifier
            await send_telegram(msg)
            await send_fcm_notification(f"CCI-V5 Cross {direction}", f"CCI: {cci:.2f}, Signal: {signal:.2f}")  # Added V5 identifier
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
        print(f"[bootstrap-V5] error from Binance: {data}")  # Added V5 identifier
        return
    print(f"[bootstrap-V5] fetched {len(data)} candles")  # Added V5 identifier
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
    print(f"[bootstrap-V5] completed_candles: {len(completed_candles)}, signal_history: {len(signal_history)}")  # Added V5 identifier

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
            print(f"[candle-V5] closed {current_candle['time']} o:{current_candle['open']} h:{current_candle['high']} l:{current_candle['low']} c:{current_candle['close']}")  # Added V5 identifier
            await check_cross()
        current_candle = {"time": candle_open, "open": price, "high": price, "low": price, "close": price}
        print(f"[candle-V5] opened new candle at {candle_open}")  # Added V5 identifier
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
                print(f"[tick-V5] price={price:.2f} CCI={cci:.2f} Signal={signal:.2f} Diff={cci-signal:.2f}")  # Added V5 identifier

# ------------- MAIN (background worker) ----------------
async def ws_loop():
    url = f"wss://stream.binance.com:9443/ws/{CONFIG['symbol']}@trade"
    async with websockets.connect(url) as ws:
        print(f"[ws-V5] connected to {url}")  # Added V5 identifier
        async for msg in ws:
            data = json.loads(msg)
            print(f"[ws-V5] received trade: {data['p']} at {datetime.fromtimestamp(data['T']/1000, tz=timezone.utc)}")  # Added V5 identifier
            await handle_trade(data)

async def worker_loop():
    await bootstrap()
    while True:
        try:
            await ws_loop()
        except asyncio.CancelledError:
            print("[worker-V5] cancelled")  # Added V5 identifier
            break
        except Exception as e:
            print("[ws-V5] error", e)  # Added V5 identifier
            await asyncio.sleep(5)

# ------------- SIMPLE HTTP SERVER (for health checks / keepalive) -------------
async def health(request):
    return web.Response(text="OK-V5")  # Changed response to identify V5

async def on_startup(app):
    print("[app-V5] starting background worker")  # Added V5 identifier
    app['worker_task'] = asyncio.create_task(worker_loop())

async def on_cleanup(app):
    print("[app-V5] stopping background worker")  # Added V5 identifier
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
    # Use a different port to avoid conflict with main.py
    port = int(os.environ.get("PORT_V5", 10001))  # Changed to PORT_V5 and default to 10001
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)