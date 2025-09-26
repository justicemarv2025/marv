import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Dict, Deque
import gspread
from google.oauth2.service_account import Credentials

import aiohttp
from aiohttp import web
import websockets
import numpy as np
import matplotlib.pyplot as plt
import matplotlib.dates as mdates
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

CONFIG = {
    "symbols": SYMBOLS,
    "timeframe": "1m",
    "ema_fast": 20,
    "ema_slow": 200,
    "bootstrap_candles": 400,

    # Chart sending
    "enable_chart_sending": True,
    "chart_interval_sec": 5,

    # Alert methods
    "alerts": {
        "telegram": True,
        "fcm": True
    },

    # API keys and tokens
    "TELEGRAM_TOKEN": os.environ.get("TELEGRAM_TOKEN", ""),
    "CHAT_ID": os.environ.get("CHAT_ID", ""),

    # Google Sheets
    "enable_google_sheets_logging": False,
    "GOOGLE_SHEETS_CREDENTIALS": os.environ.get("GOOGLE_SHEETS_CREDENTIALS", "")
}

# ================== DATA ==================
symbol_data: Dict[str, Dict] = {}
for symbol in SYMBOLS:
    symbol_data[symbol] = {
        "completed_candles": deque(maxlen=1000),
        "current_candle": None
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
    except Exception as e:
        print(f"[FCM] init error: {e}")

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

# ================== EMA CALCULATION ==================
def calculate_ema(prices, period):
    if len(prices) < period:
        return None
    alpha = 2 / (period + 1)
    sma = sum(prices[:period]) / period
    ema = sma
    for price in prices[period:]:
        ema = (price * alpha) + (ema * (1 - alpha))
    return ema

def calculate_ema_series(prices, period):
    if len(prices) < period:
        return []
    alpha = 2 / (period + 1)
    sma = sum(prices[:period]) / period
    ema_values = [None] * (period - 1)
    ema_values.append(sma)
    ema = sma
    for price in prices[period:]:
        ema = (price * alpha) + (ema * (1 - alpha))
        ema_values.append(ema)
    return ema_values

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
                else:
                    symbol_data[symbol]["current_candle"] = candle
        except Exception as e:
            print(f"[websocket] error {e}, reconnecting...")
            await asyncio.sleep(5)
            continue

# ================== CHART SENDING ==================
async def send_chart(symbol: str):
    if not CONFIG["enable_chart_sending"]:
        return
    data = symbol_data[symbol]
    candles = list(data["completed_candles"])
    if not candles:
        return
    closes = [c["close"] for c in candles]
    times = [c["timestamp"] for c in candles]
    ema_fast_series = calculate_ema_series(closes, CONFIG["ema_fast"])
    ema_slow_series = calculate_ema_series(closes, CONFIG["ema_slow"])

    plt.figure(figsize=(10, 5))
    plt.plot(times, closes, label="Close", color="black", linewidth=1)
    if ema_fast_series:
        plt.plot(times, ema_fast_series, label=f"EMA{CONFIG['ema_fast']}", color="red", linewidth=1)
    if ema_slow_series:
        plt.plot(times, ema_slow_series, label=f"EMA{CONFIG['ema_slow']}", color="blue", linewidth=1)
    plt.legend()
    plt.title(f"{symbol.upper()} EMA Snapshot")
    plt.gca().xaxis.set_major_formatter(mdates.DateFormatter('%H:%M'))
    plt.xticks(rotation=45)

    buf = io.BytesIO()
    plt.savefig(buf, format="png")
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
    app.on_startup.append(on_startup)
    app.on_cleanup.append(on_cleanup)
    return app

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 10000))
    app = create_app()
    web.run_app(app, host="0.0.0.0", port=port)
