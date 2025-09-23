import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from collections import deque
from typing import Dict, List, Tuple
import aiohttp
import numpy as np
import firebase_admin
from firebase_admin import credentials, messaging

# ========== ENVIRONMENT SETUP ==========
IS_PRODUCTION = os.environ.get('RENDER', False)

if not IS_PRODUCTION:
    try:
        from dotenv import load_dotenv
        load_dotenv()
        print("Loaded environment from .env")
    except ImportError:
        print("python-dotenv not installed, using system environment variables")
else:
    print("Running in production environment")

# ================== CONFIG ==================
CONFIG = {
    "symbols": ["ethusdt"],
    "timeframes": ["5m", "15m", "30m"],
    "pivot_lookback_periods": 5,
    "strength_threshold": 2,
    "zone_width_percent": 0.002,
    "min_distance_percent": 0.005,
    "cooldown_minutes": {"5m": 25, "15m": 45, "30m": 90},
    "levels_to_track": 3,
    "volume_weight": 0.3,
    "recent_weight": 0.7,
    "bootstrap_candles": 100,
    "websocket_url": "wss://stream.binance.com:9443/ws/{}@trade",
    "rest_url": "https://api.binance.com/api/v3/klines",
    
    "alerts": {
        "telegram": True,
        "fcm": True
    },
    
    "TELEGRAM_TOKEN": os.environ.get("TELEGRAM_TOKEN", ""),
    "CHAT_ID": os.environ.get("CHAT_ID", ""),
    "DEVICE_TOKENS_FILE": "device_token_lists.json"
}

# Global data structures
sr_data = {}
level_cooldowns = {}

for symbol in CONFIG["symbols"]:
    sr_data[symbol] = {}
    for timeframe in CONFIG["timeframes"]:
        sr_data[symbol][timeframe] = {
            "completed_candles": deque(maxlen=200),
            "current_candle": None,
            "support_levels": [],
            "resistance_levels": [],
            "level_strength": {},
            "last_alert_time": {}
        }

# Firebase app instance
firebase_app = None

# =============== CORE SR LOGIC ===============
def is_pivot_high(candles: List[Dict], index: int, left_bars: int, right_bars: int) -> bool:
    if index < left_bars or index >= len(candles) - right_bars:
        return False
    pivot_high = candles[index]['high']
    for i in range(1, left_bars + 1):
        if candles[index - i]['high'] >= pivot_high:
            return False
    for i in range(1, right_bars + 1):
        if candles[index + i]['high'] >= pivot_high:
            return False
    return True

def is_pivot_low(candles: List[Dict], index: int, left_bars: int, right_bars: int) -> bool:
    if index < left_bars or index >= len(candles) - right_bars:
        return False
    pivot_low = candles[index]['low']
    for i in range(1, left_bars + 1):
        if candles[index - i]['low'] <= pivot_low:
            return False
    for i in range(1, right_bars + 1):
        if candles[index + i]['low'] <= pivot_low:
            return False
    return True

def find_support_resistance_levels(candles: List[Dict], lookback_periods: int) -> Tuple[List[float], List[float]]:
    if len(candles) < lookback_periods * 2 + 1:
        return [], []
    
    support_levels, resistance_levels = [], []
    
    for i in range(lookback_periods, len(candles) - lookback_periods):
        if is_pivot_high(candles, i, lookback_periods, lookback_periods):
            resistance_levels.append(candles[i]['high'])
        elif is_pivot_low(candles, i, lookback_periods, lookback_periods):
            support_levels.append(candles[i]['low'])
    
    return support_levels, resistance_levels

def cluster_levels(levels: List[float], zone_width_percent: float) -> List[float]:
    if not levels:
        return []
    
    levels.sort()
    clusters, current_cluster = [], [levels[0]]
    
    for level in levels[1:]:
        if abs(level - current_cluster[-1]) / current_cluster[-1] <= zone_width_percent:
            current_cluster.append(level)
        else:
            clusters.append(sum(current_cluster) / len(current_cluster))
            current_cluster = [level]
    
    if current_cluster:
        clusters.append(sum(current_cluster) / len(current_cluster))
    
    return clusters

def calculate_level_strength(level: float, candles: List[Dict], zone_width_percent: float) -> float:
    strength = 0.0
    zone_low = level * (1 - zone_width_percent)
    zone_high = level * (1 + zone_width_percent)
    
    for i, candle in enumerate(candles):
        if (candle['low'] <= zone_high and candle['high'] >= zone_low):
            recency_weight = (len(candles) - i) / len(candles) * CONFIG["recent_weight"]
            volume_weight = (candle.get('volume', 1) / max(1, np.mean([c.get('volume', 1) for c in candles]))) * CONFIG["volume_weight"]
            strength += recency_weight + volume_weight
    
    return strength

def update_support_resistance_levels(symbol: str, timeframe: str):
    data = sr_data[symbol][timeframe]
    candles = list(data["completed_candles"])
    
    if len(candles) < CONFIG["pivot_lookback_periods"] * 2:
        return
    
    support_levels, resistance_levels = find_support_resistance_levels(
        candles, CONFIG["pivot_lookback_periods"]
    )
    
    clustered_support = cluster_levels(support_levels, CONFIG["zone_width_percent"])
    clustered_resistance = cluster_levels(resistance_levels, CONFIG["zone_width_percent"])
    
    support_with_strength = [(level, calculate_level_strength(level, candles, CONFIG["zone_width_percent"])) 
                            for level in clustered_support]
    resistance_with_strength = [(level, calculate_level_strength(level, candles, CONFIG["zone_width_percent"])) 
                              for level in clustered_resistance]
    
    support_with_strength.sort(key=lambda x: x[1], reverse=True)
    resistance_with_strength.sort(key=lambda x: x[1], reverse=True)
    
    def filter_close_levels(levels, min_distance_percent):
        filtered = []
        for level, strength in levels:
            if not filtered or all(abs(level - existing) / existing > min_distance_percent 
                                 for existing, _ in filtered):
                filtered.append((level, strength))
            if len(filtered) >= CONFIG["levels_to_track"]:
                break
        return filtered
    
    data["support_levels"] = filter_close_levels(support_with_strength, CONFIG["min_distance_percent"])
    data["resistance_levels"] = filter_close_levels(resistance_with_strength, CONFIG["min_distance_percent"])
    
    data["level_strength"] = {}
    for level, strength in data["support_levels"] + data["resistance_levels"]:
        data["level_strength"][level] = strength

def is_price_near_level(price: float, level: float, zone_width_percent: float) -> bool:
    zone_low = level * (1 - zone_width_percent)
    zone_high = level * (1 + zone_width_percent)
    return zone_low <= price <= zone_high

def is_cooldown_active(symbol: str, timeframe: str, level: float, current_time: datetime) -> bool:
    cooldown_key = f"{level:.6f}"
    last_alert_time = sr_data[symbol][timeframe]["last_alert_time"].get(cooldown_key)
    
    if not last_alert_time:
        return False
    
    cooldown_minutes = CONFIG["cooldown_minutes"][timeframe]
    return (current_time - last_alert_time) < timedelta(minutes=cooldown_minutes)

# =============== ALERT FUNCTIONS ===============
async def send_telegram(msg: str):
    if not CONFIG["alerts"]["telegram"]:
        return
    token, chat_id = CONFIG["TELEGRAM_TOKEN"], CONFIG["CHAT_ID"]
    if not token or not chat_id:
        return
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    try:
        async with aiohttp.ClientSession() as session:
            await session.post(url, data={"chat_id": chat_id, "text": msg})
    except Exception as e:
        print(f"[Telegram Error] {e}")

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

def send_fcm_alert(sound_type: str, message: str):
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

async def trigger_support_alert(symbol: str, timeframe: str, level: float, strength: float, current_price: float):
    sound_type = f"{symbol}_{timeframe}_support"
    message = f"🛡️ {symbol.upper()} {timeframe} SUPPORT | Level: {level:.4f} | Current: {current_price:.4f} | Strength: {strength:.1f}"
    print(f"[SR-ALERT] {message}")
    await send_telegram(message)
    send_fcm_alert(sound_type, message)

async def trigger_resistance_alert(symbol: str, timeframe: str, level: float, strength: float, current_price: float):
    sound_type = f"{symbol}_{timeframe}_resistance"
    message = f"🚧 {symbol.upper()} {timeframe} RESISTANCE | Level: {level:.4f} | Current: {current_price:.4f} | Strength: {strength:.1f}"
    print(f"[SR-ALERT] {message}")
    await send_telegram(message)
    send_fcm_alert(sound_type, message)

async def check_support_resistance_alerts(symbol: str, timeframe: str, current_price: float):
    data = sr_data[symbol][timeframe]
    current_time = datetime.now(timezone.utc)
    
    for level, strength in data["support_levels"]:
        if (is_price_near_level(current_price, level, CONFIG["zone_width_percent"]) and
            strength >= CONFIG["strength_threshold"] and
            not is_cooldown_active(symbol, timeframe, level, current_time)):
            
            await trigger_support_alert(symbol, timeframe, level, strength, current_price)
            sr_data[symbol][timeframe]["last_alert_time"][f"{level:.6f}"] = current_time
    
    for level, strength in data["resistance_levels"]:
        if (is_price_near_level(current_price, level, CONFIG["zone_width_percent"]) and
            strength >= CONFIG["strength_threshold"] and
            not is_cooldown_active(symbol, timeframe, level, current_time)):
            
            await trigger_resistance_alert(symbol, timeframe, level, strength, current_price)
            sr_data[symbol][timeframe]["last_alert_time"][f"{level:.6f}"] = current_time

# =============== BOOTSTRAP FUNCTION ===============
async def bootstrap_candles(symbol: str, timeframe: str):
    url = f"{CONFIG['rest_url']}?symbol={symbol.upper()}&interval={timeframe}&limit={CONFIG['bootstrap_candles']}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
    except Exception as e:
        print(f"[Bootstrap-{symbol.upper()}-{timeframe}] Failed: {e}")
        return

    if "code" in data:
        print(f"[Bootstrap-{symbol.upper()}-{timeframe}] Error from Binance: {data}")
        return

    candles = [{
        "time": datetime.fromtimestamp(int(k[0]) / 1000, tz=timezone.utc),
        "open": float(k[1]),
        "high": float(k[2]),
        "low": float(k[3]),
        "close": float(k[4]),
        "volume": float(k[5])
    } for k in data]

    sr_data[symbol][timeframe]["completed_candles"].extend(candles)
    update_support_resistance_levels(symbol, timeframe)
    print(f"[Bootstrap-{symbol.upper()}-{timeframe}] Loaded {len(candles)} candles")

    # Immediately check for alerts at last close
    if candles:
        last_close = candles[-1]["close"]
        await check_support_resistance_alerts(symbol, timeframe, last_close)

# =============== WEB SOCKET HANDLER ===============
async def handle_websocket(symbol: str):
    url = CONFIG["websocket_url"].format(symbol)
    retry_count = 0
    max_retries = 10
    
    while retry_count < max_retries:
        try:
            async with aiohttp.ClientSession() as session:
                async with session.ws_connect(url) as ws:
                    print(f"[WS-{symbol.upper()}] Connected")
                    retry_count = 0
                    
                    async for msg in ws:
                        if msg.type == aiohttp.WSMsgType.TEXT:
                            data = json.loads(msg.data)
                            await process_trade_data(symbol, data)
                        elif msg.type == aiohttp.WSMsgType.ERROR:
                            break
                            
        except Exception as e:
            retry_count += 1
            wait_time = min(2 ** retry_count, 30)
            print(f"[WS-{symbol.upper()}] Error: {e}. Retrying in {wait_time}s...")
            await asyncio.sleep(wait_time)
    
    print(f"[WS-{symbol.upper()}] Max retries exceeded")

async def process_trade_data(symbol: str, trade_data: dict):
    price = float(trade_data['p'])
    volume = float(trade_data.get('q', 0))
    timestamp = datetime.fromtimestamp(trade_data['T'] / 1000, tz=timezone.utc)
    
    for timeframe in CONFIG["timeframes"]:
        await update_timeframe_candle(symbol, timeframe, price, volume, timestamp)
    
    for timeframe in CONFIG["timeframes"]:
        await check_support_resistance_alerts(symbol, timeframe, price)

async def update_timeframe_candle(symbol: str, timeframe: str, price: float, volume: float, timestamp: datetime):
    data = sr_data[symbol][timeframe]
    
    tf_minutes = int(timeframe[:-1])
    candle_minute = timestamp.minute - (timestamp.minute % tf_minutes)
    candle_start = timestamp.replace(minute=candle_minute, second=0, microsecond=0)
    
    if data["current_candle"] is None or data["current_candle"]["time"] != candle_start:
        if data["current_candle"]:
            data["completed_candles"].append(data["current_candle"])
            update_support_resistance_levels(symbol, timeframe)
        
        data["current_candle"] = {
            "time": candle_start,
            "open": price,
            "high": price,
            "low": price,
            "close": price,
            "volume": volume
        }
    else:
        data["current_candle"]["high"] = max(data["current_candle"]["high"], price)
        data["current_candle"]["low"] = min(data["current_candle"]["low"], price)
        data["current_candle"]["close"] = price
        data["current_candle"]["volume"] += volume

# =============== MAIN FUNCTION ===============
async def main():
    global firebase_app
    print("🚀 Starting Support/Resistance Detection System")
    
    # Initialize Firebase
    try:
        firebase_cred_json = os.environ.get("FIREBASE_CREDENTIALS")
        if firebase_cred_json and CONFIG["alerts"]["fcm"]:
            cred_dict = json.loads(firebase_cred_json)
            if 'private_key' in cred_dict:
                cred_dict['private_key'] = cred_dict['private_key'].replace('\\n', '\n')
            cred = credentials.Certificate(cred_dict)
            firebase_app = firebase_admin.initialize_app(cred)
            print("[FCM] Firebase initialized")
    except Exception as e:
        print(f"[FCM] Initialization error: {e}")
    
    # Bootstrap candles + initial alerts
    for symbol in CONFIG["symbols"]:
        for timeframe in CONFIG["timeframes"]:
            await bootstrap_candles(symbol, timeframe)

    # Start WebSocket connections
    tasks = [handle_websocket(symbol) for symbol in CONFIG["symbols"]]
    await asyncio.gather(*tasks)

if __name__ == "__main__":
    asyncio.run(main())
