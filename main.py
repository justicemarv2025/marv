import asyncio
import json
import os
import time
import math
from datetime import datetime, timezone, timedelta
from collections import deque

import aiohttp
import websockets
import numpy as np

# ================== CONFIG ==================
CONFIG = {
    "symbol": "ethusdt",
    "interval": "5m",
    "bootstrap_candles": 50,
    "cci_length": 9,
    "signal_length": 9,
    "signal_type": "ema",  # "sma" or "ema"
    "detection_mode": "candle",  # "candle" or "tick"
    "use_tick_updates": True,  # compute interim CCI within open candle
    "zone_high": 0,   # overbought threshold
    "zone_low": 0,   # oversold threshold
    "min_cross_gap_minutes": 20,
    "print_interval_sec": 15,
    "close_grace_sec": 2,

    # Alerts
    "alerts": {
        "telegram": True,
        "playfile": False,
        "plyer": False
    },
    "alarm_file": "alarm.wav",

    # Telegram
    "TELEGRAM_TOKEN": "8396496933:AAHXlxYSkU3VBnfcJvWsm5fj8mtZxKqsXjA",
    "CHAT_ID": "-4848836916"
}
# ============================================

completed_candles = deque(maxlen=500)
current_candle = None
last_cross_time = None
prev_diff = None
last_print_time = 0
prev_ema = None

# ------------- UTILITIES ----------------
def now():
    return datetime.now(timezone.utc).astimezone()

async def send_telegram(msg):
    if not CONFIG["alerts"]["telegram"]:
        return
    token = CONFIG["TELEGRAM_TOKEN"]
    chat_id = CONFIG["CHAT_ID"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, data={"chat_id": chat_id, "text": msg})
    except Exception as e:
        print("[telegram] error", e)

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

def update_signal(prev_signal, cci_values, length, signal_type="ema"):
    if len(cci_values) < length:
        return None
    if signal_type == "sma":
        return np.mean(cci_values[-length:])
    else:  # EMA
        alpha = 2 / (length + 1)
        if prev_signal is None:
            return np.mean(cci_values[-length:])
        return prev_signal + alpha * (cci_values[-1] - prev_signal)

# ------------- ALERT CHECK ----------------
async def check_cross():
    global prev_diff, last_cross_time, prev_ema
    cci = compute_cci(list(completed_candles), CONFIG["cci_length"])
    if cci is None:
        return
    signal = update_signal(prev_ema, [compute_cci(list(completed_candles)[:i], CONFIG["cci_length"])
                                      for i in range(CONFIG["cci_length"], len(completed_candles)+1)],
                           CONFIG["signal_length"], CONFIG["signal_type"])
    prev_ema = signal

    # Only print periodically
    global last_print_time
    if time.time() - last_print_time > CONFIG["print_interval_sec"]:
        print(f"[CCI] {now()} CCI={cci:.3f} Signal={signal}")
        last_print_time = time.time()

    if signal is None:
        return
    diff = cci - signal

    # Zone conditions
    in_zone_up = cci <= CONFIG["zone_low"]  # oversold recovery
    in_zone_down = cci >= CONFIG["zone_high"]  # overbought reversal

    if prev_diff is not None:
        crossed_up = prev_diff < 0 and diff > 0 and in_zone_up
        crossed_down = prev_diff > 0 and diff < 0 and in_zone_down

        if crossed_up or crossed_down:
            if last_cross_time is None or (now() - last_cross_time) > timedelta(minutes=CONFIG["min_cross_gap_minutes"]):
                last_cross_time = now()
                direction = "UP" if crossed_up else "DOWN"
                msg = f"🚨 CCI CROSS {direction} | CCI={cci:.2f} Signal={signal:.2f} Time={now()}"
                print("[alert]", msg)
                await send_telegram(msg)
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
    for k in data:
        completed_candles.append({
            "open": float(k[1]),
            "high": float(k[2]),
            "low": float(k[3]),
            "close": float(k[4])
        })
    print(f"[bootstrap] loaded {len(completed_candles)} candles")

async def handle_trade(trade):
    global current_candle
    price = float(trade['p'])
    ts = int(trade['T'])
    dt = datetime.fromtimestamp(ts/1000, tz=timezone.utc)
    minute = dt.minute - dt.minute % 5
    candle_open = dt.replace(second=0, microsecond=0, minute=minute)

    if current_candle is None or candle_open != current_candle["time"]:
        if current_candle:
            delay = CONFIG["close_grace_sec"]
            if (now() - current_candle["time"]) > timedelta(minutes=5, seconds=delay):
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
        signal = update_signal(prev_ema, [compute_cci(tmp_candles[:i], CONFIG["cci_length"])
                                          for i in range(CONFIG["cci_length"], len(tmp_candles)+1)],
                               CONFIG["signal_length"], CONFIG["signal_type"])
        if cci and signal and abs(cci - signal) < 5:
            print(f"[warn] approaching cross | CCI={cci:.2f} Signal={signal:.2f}")

# ------------- MAIN ----------------
async def ws_loop():
    url = f"wss://stream.binance.com:9443/ws/{CONFIG['symbol']}@trade"
    async with websockets.connect(url) as ws:
        print(f"[ws] connected to {url}")
        async for msg in ws:
            data = json.loads(msg)
            await handle_trade(data)

async def main():
    await bootstrap()
    while True:
        try:
            await ws_loop()
        except Exception as e:
            print("[ws] error", e)
            await asyncio.sleep(5)

if __name__ == "__main__":
    asyncio.run(main())
