import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from collections import deque, defaultdict
from typing import Dict, Deque, Optional, List, Tuple
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
SYMBOLS = ["ethusdt"]

SUPPORT_RESISTANCE_CONFIG = {
    "symbols": SYMBOLS,
    "timeframes": ["5m", "15m", "30m"],  # Configurable timeframes
    "pivot_lookback_periods": 5,  # Number of candles to look back for pivot points
    "strength_threshold": 2,  # Minimum touches to consider a level strong
    "zone_width_percent": 0.002,  # 0.2% price zone around levels
    "min_distance_percent": 0.005,  # 0.5% minimum distance between levels
    "cooldown_minutes": {  # Configurable cooldown for each timeframe
        "5m": 25,
        "15m": 45,
        "30m": 90
    },
    "levels_to_track": 3,  # Number of strongest support/resistance levels to track
    "volume_weight": 0.3,  # Weight for volume in level strength calculation
    "recent_weight": 0.7,  # Weight for recent touches
    
    "bootstrap_candles": 100,  # More candles for better level detection
    
    "alerts": {
        "telegram": True,
        "fcm": True
    },
    
    "TELEGRAM_TOKEN": os.environ.get("TELEGRAM_TOKEN", ""),
    "CHAT_ID": os.environ.get("CHAT_ID", ""),
    "DEVICE_TOKENS_FILE": "device_token_lists.json"
}

# Merge with existing CCI config if needed
CONFIG = {**SUPPORT_RESISTANCE_CONFIG}
# ============================================

# Data structures for support/resistance detection
sr_data: Dict[str, Dict] = {}
level_cooldowns: Dict[str, Dict] = {}

# Initialize data structures
for symbol in SYMBOLS:
    sr_data[symbol] = {}
    level_cooldowns[symbol] = {}
    
    for timeframe in CONFIG["timeframes"]:
        sr_data[symbol][timeframe] = {
            "completed_candles": deque(maxlen=200),
            "current_candle": None,
            "support_levels": [],
            "resistance_levels": [],
            "level_strength": {},  # level_value -> strength_score
            "last_alert_time": {}  # level_value -> last_alert_timestamp
        }
        level_cooldowns[symbol][timeframe] = {}

# Firebase app instance (reuse from existing)
firebase_app = None

# =============== SUPPORT/RESISTANCE DETECTION LOGIC ===============
def is_pivot_high(candles: List[Dict], index: int, left_bars: int, right_bars: int) -> bool:
    """Check if candle at index is a pivot high"""
    if index < left_bars or index >= len(candles) - right_bars:
        return False
    
    pivot_high = candles[index]['high']
    
    # Check left side
    for i in range(1, left_bars + 1):
        if candles[index - i]['high'] >= pivot_high:
            return False
    
    # Check right side
    for i in range(1, right_bars + 1):
        if candles[index + i]['high'] >= pivot_high:
            return False
    
    return True

def is_pivot_low(candles: List[Dict], index: int, left_bars: int, right_bars: int) -> bool:
    """Check if candle at index is a pivot low"""
    if index < left_bars or index >= len(candles) - right_bars:
        return False
    
    pivot_low = candles[index]['low']
    
    # Check left side
    for i in range(1, left_bars + 1):
        if candles[index - i]['low'] <= pivot_low:
            return False
    
    # Check right side
    for i in range(1, right_bars + 1):
        if candles[index + i]['low'] <= pivot_low:
            return False
    
    return True

def find_support_resistance_levels(candles: List[Dict], lookback_periods: int = 5) -> Tuple[List[float], List[float]]:
    """Find support and resistance levels from candle data"""
    if len(candles) < lookback_periods * 2 + 1:
        return [], []
    
    support_levels = []
    resistance_levels = []
    
    # Find pivot points
    for i in range(lookback_periods, len(candles) - lookback_periods):
        if is_pivot_high(candles, i, lookback_periods, lookback_periods):
            resistance_levels.append(candles[i]['high'])
        elif is_pivot_low(candles, i, lookback_periods, lookback_periods):
            support_levels.append(candles[i]['low'])
    
    return support_levels, resistance_levels

def cluster_levels(levels: List[float], zone_width_percent: float) -> List[float]:
    """Cluster nearby levels together"""
    if not levels:
        return []
    
    levels.sort()
    clusters = []
    current_cluster = [levels[0]]
    
    for level in levels[1:]:
        if abs(level - current_cluster[-1]) / current_cluster[-1] <= zone_width_percent:
            current_cluster.append(level)
        else:
            # Use weighted average for cluster center
            clusters.append(sum(current_cluster) / len(current_cluster))
            current_cluster = [level]
    
    if current_cluster:
        clusters.append(sum(current_cluster) / len(current_cluster))
    
    return clusters

def calculate_level_strength(level: float, candles: List[Dict], zone_width_percent: float) -> float:
    """Calculate strength score for a level based on touches and volume"""
    strength = 0.0
    zone_low = level * (1 - zone_width_percent)
    zone_high = level * (1 + zone_width_percent)
    
    for i, candle in enumerate(candles):
        # Check if price touched the level zone
        if (candle['low'] <= zone_high and candle['high'] >= zone_low):
            # Recent touches get higher weight
            recency_weight = (len(candles) - i) / len(candles) * CONFIG["recent_weight"]
            volume_weight = (candle.get('volume', 1) / max(1, np.mean([c.get('volume', 1) for c in candles]))) * CONFIG["volume_weight"]
            strength += recency_weight + volume_weight
    
    return strength

def update_support_resistance_levels(symbol: str, timeframe: str):
    """Update support and resistance levels for given symbol and timeframe"""
    data = sr_data[symbol][timeframe]
    candles = list(data["completed_candles"])
    
    if len(candles) < CONFIG["pivot_lookback_periods"] * 2:
        return
    
    # Find raw levels
    support_levels, resistance_levels = find_support_resistance_levels(
        candles, CONFIG["pivot_lookback_periods"]
    )
    
    # Cluster nearby levels
    clustered_support = cluster_levels(support_levels, CONFIG["zone_width_percent"])
    clustered_resistance = cluster_levels(resistance_levels, CONFIG["zone_width_percent"])
    
    # Calculate strength for each level
    support_with_strength = []
    resistance_with_strength = []
    
    for level in clustered_support:
        strength = calculate_level_strength(level, candles, CONFIG["zone_width_percent"])
        support_with_strength.append((level, strength))
    
    for level in level in clustered_resistance:
        strength = calculate_level_strength(level, candles, CONFIG["zone_width_percent"])
        resistance_with_strength.append((level, strength))
    
    # Sort by strength and take strongest levels
    support_with_strength.sort(key=lambda x: x[1], reverse=True)
    resistance_with_strength.sort(key=lambda x: x[1], reverse=True)
    
    # Filter levels that are too close to each other
    def filter_close_levels(levels, min_distance_percent):
        filtered = []
        for level, strength in levels:
            if not filtered or all(abs(level - existing) / existing > min_distance_percent 
                                 for existing, _ in filtered):
                filtered.append((level, strength))
            if len(filtered) >= CONFIG["levels_to_track"]:
                break
        return filtered
    
    data["support_levels"] = filter_close_levels(
        support_with_strength, CONFIG["min_distance_percent"]
    )
    data["resistance_levels"] = filter_close_levels(
        resistance_with_strength, CONFIG["min_distance_percent"]
    )
    
    # Update level strength dictionary
    data["level_strength"] = {}
    for level, strength in data["support_levels"] + data["resistance_levels"]:
        data["level_strength"][level] = strength

def is_price_near_level(price: float, level: float, zone_width_percent: float) -> bool:
    """Check if current price is near a support/resistance level"""
    zone_low = level * (1 - zone_width_percent)
    zone_high = level * (1 + zone_width_percent)
    return zone_low <= price <= zone_high

def is_cooldown_active(symbol: str, timeframe: str, level: float, current_time: datetime) -> bool:
    """Check if cooldown period is active for a level"""
    cooldown_key = f"{level:.6f}"
    last_alert_time = sr_data[symbol][timeframe]["last_alert_time"].get(cooldown_key)
    
    if not last_alert_time:
        return False
    
    cooldown_minutes = CONFIG["cooldown_minutes"][timeframe]
    time_since_last_alert = current_time - last_alert_time
    
    return time_since_last_alert < timedelta(minutes=cooldown_minutes)

async def check_support_resistance_alerts(symbol: str, timeframe: str, current_price: float):
    """Check for support/resistance alerts"""
    data = sr_data[symbol][timeframe]
    current_time = now()
    
    # Check support levels
    for level, strength in data["support_levels"]:
        if (is_price_near_level(current_price, level, CONFIG["zone_width_percent"]) and
            strength >= CONFIG["strength_threshold"] and
            not is_cooldown_active(symbol, timeframe, level, current_time)):
            
            await trigger_support_alert(symbol, timeframe, level, strength, current_price)
            # Update last alert time
            sr_data[symbol][timeframe]["last_alert_time"][f"{level:.6f}"] = current_time
    
    # Check resistance levels
    for level, strength in data["resistance_levels"]:
        if (is_price_near_level(current_price, level, CONFIG["zone_width_percent"]) and
            strength >= CONFIG["strength_threshold"] and
            not is_cooldown_active(symbol, timeframe, level, current_time)):
            
            await trigger_resistance_alert(symbol, timeframe, level, strength, current_price)
            # Update last alert time
            sr_data[symbol][timeframe]["last_alert_time"][f"{level:.6f}"] = current_time

async def trigger_support_alert(symbol: str, timeframe: str, level: float, strength: float, current_price: float):
    """Trigger support level alert"""
    sound_type = f"{symbol}_{timeframe}_support"
    message = f"🛡️ {symbol.upper()} {timeframe} SUPPORT | Level: {level:.4f} | Current: {current_price:.4f} | Strength: {strength:.1f}"
    
    print(f"[SR-ALERT-{symbol.upper()}-{timeframe}] {message}")
    await send_telegram(message)
    await send_fcm_notification(
        f"{symbol.upper()} {timeframe} Support", 
        f"Price: {current_price:.4f} | Level: {level:.4f}", 
        sound_type
    )

async def trigger_resistance_alert(symbol: str, timeframe: str, level: float, strength: float, current_price: float):
    """Trigger resistance level alert"""
    sound_type = f"{symbol}_{timeframe}_resistance"
    message = f"🚧 {symbol.upper()} {timeframe} RESISTANCE | Level: {level:.4f} | Current: {current_price:.4f} | Strength: {strength:.1f}"
    
    print(f"[SR-ALERT-{symbol.upper()}-{timeframe}] {message}")
    await send_telegram(message)
    await send_fcm_notification(
        f"{symbol.upper()} {timeframe} Resistance", 
        f"Price: {current_price:.4f} | Level: {level:.4f}", 
        sound_type
    )

# =============== REUSE EXISTING INFRASTRUCTURE ===============
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
        
    except Exception as e:
        print(f"[FCM] init error: {e}")
        firebase_app = None

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
    
    # Use existing FCM sending logic from your script
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
                    "message": f"{title}: {message}"
                },
                token=t["token"]
            )
            resp = messaging.send(msg)
            print(f"✓ FCM Message sent to {t['customer']}: {t['token'][:15]}... id={resp}")
        except Exception as e:
            print(f"✗ Failed to send FCM to {t['customer']}: {e}")

def load_valid_tokens():
    # Your existing token loading logic
    try:
        with open(CONFIG["DEVICE_TOKENS_FILE"], "r") as f:
            data = json.load(f)
    except FileNotFoundError:
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

def now():
    return datetime.now(timezone.utc)

# =============== DATA FETCHING AND PROCESSING ===============
async def fetch_historical_candles(symbol: str, interval: str, limit: int = 100):
    """Fetch historical candles for bootstrap"""
    url = f"https://api.binance.com/api/v3/klines?symbol={symbol.upper()}&interval={interval}&limit={limit}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url) as resp:
                data = await resp.json()
        
        candles = []
        for k in data:
            candles.append({
                "open": float(k[1]),
                "high": float(k[2]),
                "low": float(k[3]),
                "close": float(k[4]),
                "volume": float(k[5]),
                "time": datetime.fromtimestamp(k[0] / 1000, tz=timezone.utc)
            })
        return candles
    except Exception as e:
        print(f"[bootstrap-{symbol.upper()}-{interval}] Failed to fetch data: {e}")
        return []

async def bootstrap_timeframe(symbol: str, timeframe: str):
    """Bootstrap historical data for a specific timeframe"""
    print(f"[bootstrap-{symbol.upper()}-{timeframe}] Loading historical data...")
    
    candles = await fetch_historical_candles(symbol, timeframe, CONFIG["bootstrap_candles"])
    if not candles:
        print(f"[bootstrap-{symbol.upper()}-{timeframe}] No data received")
        return
    
    data = sr_data[symbol][timeframe]
    data["completed_candles"].extend(candles)
    
    # Initial level detection
    update_support_resistance_levels(symbol, timeframe)
    
    print(f"[bootstrap-{symbol.upper()}-{timeframe}] Loaded {len(candles)} candles, "
          f"found {len(data['support_levels'])} support, {len(data['resistance_levels'])} resistance levels")

def get_timeframe_seconds(timeframe: str) -> int:
    """Convert timeframe string to seconds"""
    unit = timeframe[-1]
    value = int(timeframe[:-1])
    if unit == 'm':
        return value * 60
    elif unit == 'h':
        return value * 3600
    elif unit == 'd':
        return value * 86400
    return 300  # default 5 minutes

async def process_candle_data(symbol: str, timeframe: str, candle_data: Dict):
    """Process new candle data for support/resistance detection"""
    data = sr_data[symbol][timeframe]
    
    if data["current_candle"] is None:
        data["current_candle"] = candle_data
        return
    
    # Check if we have a new candle
    current_candle_time = data["current_candle"]["time"]
    new_candle_time = candle_data["time"]
    
    if new_candle_time > current_candle_time:
        # Close current candle and start new one
        data["completed_candles"].append(data["current_candle"])
        data["current_candle"] = candle_data
        
        # Update support/resistance levels
        update_support_resistance_levels(symbol, timeframe)
        
        print(f"[SR-{symbol.upper()}-{timeframe}] Updated levels - "
              f"Support: {[f'{l:.4f}' for l, s in data['support_levels']]}, "
              f"Resistance: {[f'{l:.4f}' for l, s in data['resistance_levels']]}")
    else:
        # Update current candle
        data["current_candle"]["high"] = max(data["current_candle"]["high"], candle_data["high"])
        data["current_candle"]["low"] = min(data["current_candle"]["low"], candle_data["low"])
        data["current_candle"]["close"] = candle_data["close"]
        data["current_candle"]["volume"] += candle_data.get("volume", 0)

async def monitor_price_levels(symbol: str, current_price: float, timestamp: datetime):
    """Monitor current price against all timeframe levels"""
    for timeframe in CONFIG["timeframes"]:
        await check_support_resistance_alerts(symbol, timeframe, current_price)

# =============== MAIN WORKFLOW ===============
async def sr_worker_loop():
    """Main support/resistance worker loop"""
    print("[SR-System] Initializing Support/Resistance Detection System")
    init_firebase()
    
    # Bootstrap all timeframes
    for symbol in CONFIG["symbols"]:
        for timeframe in CONFIG["timeframes"]:
            await bootstrap_timeframe(symbol, timeframe)
    
    # You can integrate this with your existing WebSocket connection
    # For now, this provides the complete detection logic
    print("[SR-System] Support/Resistance system ready")

# =============== INTEGRATION WITH EXISTING SYSTEM ===============
# Add this to your existing handle_trade function or create a separate WebSocket handler
async def handle_price_update(symbol: str, price: float, volume: float, timestamp: datetime):
    """Handle new price updates for support/resistance detection"""
    # Process for each timeframe
    for timeframe in CONFIG["timeframes"]:
        tf_seconds = get_timeframe_seconds(timeframe)
        candle_start = timestamp.replace(second=0, microsecond=0)
        candle_start = candle_start.replace(minute=candle_start.minute - (candle_start.minute % (tf_seconds // 60)))
        
        candle_data = {
            "time": candle_start,
            "open": price,  # This will be updated properly in real implementation
            "high": price,
            "low": price,
            "close": price,
            "volume": volume
        }
        
        await process_candle_data(symbol, timeframe, candle_data)
    
    # Check for alerts
    await monitor_price_levels(symbol, price, timestamp)

if __name__ == "__main__":
    # Initialize and run the system
    asyncio.run(sr_worker_loop())