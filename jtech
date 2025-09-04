import ccxt
import pandas as pd
import numpy as np
import time
import threading
from datetime import datetime, timezone, timedelta
import logging
import warnings
from decimal import Decimal
import queue
import pygame
import os
import requests
import json

# Suppress pandas warnings
warnings.filterwarnings('ignore')

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

# Configuration variables - All settings in one section
CONFIG = {
    # ================= TIMING SETTINGS =================
    'loop_intervals': {
        'data_fetching': 30,      # seconds between historical data fetches
        'indicator_check': 2,     # seconds between indicator checks
        'tick_collection': 0.5,   # seconds between tick data collection
        'audio_cooldown': 5,      # seconds between same alert sounds
        'telegram_cooldown': 10   # seconds between same telegram alerts
    },
    
    # ================= ALERT TOGGLE SETTINGS =================
    'alert_toggles': {
        'cross_above_100': True,      # Alert when CCI crosses above +100
        'cross_below_100': True,      # Alert when CCI crosses below +100
        'cross_above_neg100': True,   # Alert when CCI crosses above -100
        'cross_below_neg100': True,   # Alert when CCI crosses below -100
        'cross_above_sma': True,      # Alert when CCI crosses above its SMA
        'cross_below_sma': True,      # Alert when CCI crosses below its SMA
        'touch_near_100': True,       # Alert when CCI is near +100
        'touch_near_neg100': True,    # Alert when CCI is near -100
        'touch_near_sma': True        # Alert when CCI is near its SMA
    },
    
    # ================= ALERT THRESHOLDS =================
    'alert_thresholds': {
        'cross_100_closeness': 5.0,   # Points from ±100 to consider "near"
        'sma_cross_closeness': 0.02,  # Threshold for SMA cross detection
        'sma_nearness_levels': {
            'VERY_CLOSE': 0.01,
            'CLOSE': 0.02,
            'NEAR': 0.05,
            'APPROACHING': 0.1,
            'FAR': 0.2
        }
    },
    
    # ================= AUDIO SETTINGS =================
    'audio_settings': {
        'enable_audio_alerts': True,
        'alert_volume': 0.7,          # Volume level (0.0 to 1.0)
        'alert_files': {
            'cross_above_100': 'alarm.wav',
            'cross_below_100': 'alarm.wav',
            'cross_above_neg100': 'alarm.wav',
            'cross_below_neg100': 'alarm.wav',
            'cross_above_sma': 'alarm.wav',
            'cross_below_sma': 'alarm.wav',
            'touch_near_100': 'alarm.wav',
            'touch_near_neg100': 'alarm.wav',
            'touch_near_sma': 'alarm.wav'
        }
    },
    
    # ================= TELEGRAM SETTINGS =================
    'telegram_settings': {
        'enable_telegram_alerts': False,  # Set to True to enable Telegram alerts
        'bot_token': 'YOUR_BOT_TOKEN_HERE',  # Your Telegram bot token
        'chat_id': 'YOUR_CHAT_ID_HERE',     # Your Telegram chat ID
        'cooldown': 10                     # seconds between same telegram alerts
    },
    
    # ================= EXCHANGE SETTINGS =================
    'exchange_settings': {
        'symbol': 'ETH/USDT',
        'timeframe': '5m',
        'batch_size': 100,
        'candle_history_size': 100
    },
    
    # ================= INDICATOR SETTINGS =================
    'indicator_settings': {
        'cci_period': 20,
        'cci_constant': 0.015,
        'macd_fast': 12,
        'macd_slow': 26,
        'macd_signal': 9,
        'cci_sma_period': 14,
        'cci_upper_level': 100,
        'cci_lower_level': -100
    },
    
    # ================= FEATURE TOGGLES =================
    'feature_toggles': {
        'enable_data_fetching': True,
        'enable_indicator_monitoring': True,
        'enable_realtime_tick_data': True,
        'enable_cci_sma_analysis': True
    }
}

class TradingMonitor:
    def __init__(self, config):
        self.config = config
        self.exchange = ccxt.binance()
        self.last_fetch_time = 0
        self.last_check_time = 0
        self.last_alert_time = {}
        self.last_telegram_alert_time = {}
        
        # Initialize audio
        self.audio_initialized = False
        self.init_audio()
        
        # In-memory data storage
        self.historical_candles = pd.DataFrame()
        self.current_candle = None
        self.current_candle_start_time = None
        
        # Real-time tick data
        self.tick_data = queue.Queue()
        self.tick_thread = None
        self.stop_tick_thread = False
        
        # Alert tracking
        self.active_alerts = set()
        
    def init_audio(self):
        """Initialize audio system"""
        if self.config['audio_settings']['enable_audio_alerts']:
            try:
                pygame.mixer.init()
                self.audio_initialized = True
                logger.info("Audio system initialized")
            except Exception as e:
                logger.error(f"Failed to initialize audio: {e}")
                self.audio_initialized = False
    
    def send_telegram_alert(self, alert_type, message):
        """Send alert message to Telegram"""
        if not self.config['telegram_settings']['enable_telegram_alerts']:
            return
            
        # Check cooldown
        current_time = time.time()
        last_alert_time = self.last_telegram_alert_time.get(alert_type, 0)
        cooldown = self.config['telegram_settings']['cooldown']
        
        if current_time - last_alert_time < cooldown:
            return
            
        bot_token = self.config['telegram_settings']['bot_token']
        chat_id = self.config['telegram_settings']['chat_id']
        
        if bot_token == 'YOUR_BOT_TOKEN_HERE' or chat_id == 'YOUR_CHAT_ID_HERE':
            logger.warning("Telegram bot token or chat ID not configured")
            return
            
        try:
            url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
            payload = {
                'chat_id': chat_id,
                'text': message,
                'parse_mode': 'HTML'
            }
            
            response = requests.post(url, data=payload, timeout=10)
            if response.status_code == 200:
                logger.info(f"Telegram alert sent: {alert_type}")
                self.last_telegram_alert_time[alert_type] = current_time
            else:
                logger.error(f"Telegram API error: {response.status_code} - {response.text}")
                
        except Exception as e:
            logger.error(f"Error sending Telegram alert: {e}")
    
    def play_alert_sound(self, alert_type, alert_message):
        """Play alert sound for specific alert type"""
        # Send Telegram alert first
        self.send_telegram_alert(alert_type, alert_message)
        
        # Then play audio alert
        if not self.audio_initialized:
            return
            
        # Check cooldown
        current_time = time.time()
        last_play_time = self.last_alert_time.get(alert_type, 0)
        cooldown = self.config['loop_intervals']['audio_cooldown']
        
        if current_time - last_play_time < cooldown:
            return
            
        try:
            alert_file = self.config['audio_settings']['alert_files'].get(alert_type, 'alarm.wav')
            if os.path.exists(alert_file):
                sound = pygame.mixer.Sound(alert_file)
                sound.set_volume(self.config['audio_settings']['alert_volume'])
                sound.play()
                self.last_alert_time[alert_type] = current_time
                logger.info(f"🔊 Playing alert: {alert_type}")
            else:
                # Try to use alarm.wav as fallback
                if os.path.exists('alarm.wav'):
                    sound = pygame.mixer.Sound('alarm.wav')
                    sound.set_volume(self.config['audio_settings']['alert_volume'])
                    sound.play()
                    self.last_alert_time[alert_type] = current_time
                    logger.info(f"🔊 Playing fallback alert: {alert_type}")
                else:
                    logger.warning(f"Alert file not found: {alert_file} and alarm.wav")
        except Exception as e:
            logger.error(f"Error playing alert sound: {e}")
    
    def check_alerts(self, cci_series, cci_sma_series, previous_cci, previous_sma):
        """Check all alert conditions and trigger audio alerts"""
        if cci_series is None or len(cci_series) < 2:
            return
            
        current_cci = cci_series.iloc[-1]
        current_sma = cci_sma_series.iloc[-1] if cci_sma_series is not None else None
        
        if pd.isna(current_cci):
            return
        
        threshold_100 = self.config['alert_thresholds']['cross_100_closeness']
        
        # Alert 1: Cross above +100
        if (self.config['alert_toggles']['cross_above_100'] and
            previous_cci < 100 and current_cci >= 100):
            message = "🚨 CCI CROSS ALERT: Crossed above +100"
            self.play_alert_sound('cross_above_100', message)
        
        # Alert 2: Cross below +100  
        if (self.config['alert_toggles']['cross_below_100'] and
            previous_cci > 100 and current_cci <= 100):
            message = "🚨 CCI CROSS ALERT: Crossed below +100"
            self.play_alert_sound('cross_below_100', message)
        
        # Alert 3: Cross above -100
        if (self.config['alert_toggles']['cross_above_neg100'] and
            previous_cci < -100 and current_cci >= -100):
            message = "🚨 CCI CROSS ALERT: Crossed above -100"
            self.play_alert_sound('cross_above_neg100', message)
        
        # Alert 4: Cross below -100  
        if (self.config['alert_toggles']['cross_below_neg100'] and
            previous_cci > -100 and current_cci <= -100):
            message = "🚨 CCI CROSS ALERT: Crossed below -100"
            self.play_alert_sound('cross_below_neg100', message)
        
        # Alert 5: Near +100 (but not crossing)
        if (self.config['alert_toggles']['touch_near_100'] and
            abs(current_cci - 100) <= threshold_100 and
            not (previous_cci < 100 and current_cci >= 100) and
            not (previous_cci > 100 and current_cci <= 100)):
            message = f"⚠️ CCI PROXIMITY: Near +100 level (Current: {current_cci:.2f})"
            self.play_alert_sound('touch_near_100', message)
        
        # Alert 6: Near -100 (but not crossing)
        if (self.config['alert_toggles']['touch_near_neg100'] and
            abs(current_cci - (-100)) <= threshold_100 and
            not (previous_cci < -100 and current_cci >= -100) and
            not (previous_cci > -100 and current_cci <= -100)):
            message = f"⚠️ CCI PROXIMITY: Near -100 level (Current: {current_cci:.2f})"
            self.play_alert_sound('touch_near_neg100', message)
        
        # CCI SMA alerts (if enabled and available)
        if (self.config['feature_toggles']['enable_cci_sma_analysis'] and 
            current_sma is not None and not pd.isna(previous_sma)):
            
            sma_threshold = self.config['alert_thresholds']['sma_cross_closeness']
            
            # Alert 7: Cross above SMA
            if (self.config['alert_toggles']['cross_above_sma'] and
                previous_cci < previous_sma and current_cci >= current_sma and
                abs(current_cci - current_sma) <= sma_threshold):
                message = f"🚨 CCI SMA CROSS: Crossed above SMA (CCI: {current_cci:.2f}, SMA: {current_sma:.2f})"
                self.play_alert_sound('cross_above_sma', message)
            
            # Alert 8: Cross below SMA
            if (self.config['alert_toggles']['cross_below_sma'] and
                previous_cci > previous_sma and current_cci <= current_sma and
                abs(current_cci - current_sma) <= sma_threshold):
                message = f"🚨 CCI SMA CROSS: Crossed below SMA (CCI: {current_cci:.2f}, SMA: {current_sma:.2f})"
                self.play_alert_sound('cross_below_sma', message)
            
            # Alert 9: Near SMA (but not crossing)
            if (self.config['alert_toggles']['touch_near_sma'] and
                abs(current_cci - current_sma) <= sma_threshold and
                not (previous_cci < previous_sma and current_cci >= current_sma) and
                not (previous_cci > previous_sma and current_cci <= current_sma)):
                message = f"⚠️ CCI SMA PROXIMITY: Near SMA (CCI: {current_cci:.2f}, SMA: {current_sma:.2f})"
                self.play_alert_sound('touch_near_sma', message)
    
    def fetch_historical_candles(self):
        """Fetch historical 5-minute candles from exchange"""
        if not self.config['feature_toggles']['enable_data_fetching']:
            return False
            
        try:
            logger.info("Fetching historical 5-minute candles from exchange...")
            
            exchange_settings = self.config['exchange_settings']
            now_ms = int(time.time() * 1000)
            since_ms = now_ms - (exchange_settings['candle_history_size'] * 5 * 60 * 1000)
            
            ohlcv = self.exchange.fetch_ohlcv(
                exchange_settings['symbol'], 
                exchange_settings['timeframe'], 
                since=since_ms, 
                limit=exchange_settings['batch_size']
            )

            if not ohlcv:
                logger.info("No historical candles found.")
                return False

            candles = []
            for candle in ohlcv:
                candle_time = datetime.fromtimestamp(candle[0] / 1000, tz=timezone.utc)
                candles.append({
                    'timestamp': candle_time,
                    'open': candle[1],
                    'high': candle[2],
                    'low': candle[3],
                    'close': candle[4],
                    'volume': candle[5],
                    'open_time_ms': candle[0]
                })
            
            df = pd.DataFrame(candles)
            df.set_index('timestamp', inplace=True)
            
            self.historical_candles = df.tail(exchange_settings['candle_history_size'])
            
            logger.info(f"Fetched {len(self.historical_candles)} historical 5-minute candles")
            if not self.historical_candles.empty:
                logger.info(f"Latest historical candle: {self.historical_candles.index[-1]}")
            
            return True
            
        except Exception as e:
            logger.error(f"Error fetching historical data: {e}")
            return False
    
    def start_tick_data_collection(self):
        """Start collecting real-time tick data in a separate thread"""
        if not self.config['feature_toggles']['enable_realtime_tick_data']:
            return
            
        def tick_collector():
            logger.info("Starting real-time tick data collection...")
            tick_interval = self.config['loop_intervals']['tick_collection']
            
            while not self.stop_tick_thread:
                try:
                    ticker = self.exchange.fetch_ticker(self.config['exchange_settings']['symbol'])
                    current_price = ticker['last']
                    current_time = int(time.time() * 1000)
                    
                    self.tick_data.put((current_time, current_price))
                    self.update_current_candle(current_price, current_time)
                    
                    time.sleep(tick_interval)
                    
                except Exception as e:
                    logger.error(f"Error in tick collector: {e}")
                    time.sleep(2)
        
        self.tick_thread = threading.Thread(target=tick_collector, daemon=True)
        self.tick_thread.start()
    
    def update_current_candle(self, price, timestamp):
        """Update the current forming 5-minute candle with new tick data"""
        current_5min = (timestamp // 300000) * 300000
        current_5min_dt = datetime.fromtimestamp(current_5min / 1000, tz=timezone.utc)
        
        if self.current_candle is None or self.current_candle_start_time != current_5min:
            self.current_candle_start_time = current_5min
            self.current_candle = {
                'open': price,
                'high': price,
                'low': price,
                'close': price,
                'volume': 0,
                'count': 1,
                'start_time': current_5min_dt
            }
        else:
            self.current_candle['close'] = price
            self.current_candle['high'] = max(self.current_candle['high'], price)
            self.current_candle['low'] = min(self.current_candle['low'], price)
            self.current_candle['count'] += 1
    
    def check_and_complete_candle(self):
        """Check if current 5-minute candle has ended and move to historical"""
        if not self.config['feature_toggles']['enable_realtime_tick_data'] or self.current_candle is None:
            return
        
        current_time_ms = int(time.time() * 1000)
        current_5min = (current_time_ms // 300000) * 300000
        next_5min = current_5min + 300000
        
        if (self.current_candle_start_time < current_5min and
            current_time_ms >= next_5min):
            
            completed_candle = {
                'timestamp': datetime.fromtimestamp(self.current_candle_start_time / 1000, tz=timezone.utc),
                'open': self.current_candle['open'],
                'high': self.current_candle['high'],
                'low': self.current_candle['low'],
                'close': self.current_candle['close'],
                'volume': self.current_candle['count'],
                'open_time_ms': self.current_candle_start_time
            }
            
            new_row = pd.DataFrame([completed_candle]).set_index('timestamp')
            self.historical_candles = pd.concat([self.historical_candles, new_row])
            
            if len(self.historical_candles) > self.config['exchange_settings']['candle_history_size']:
                self.historical_candles = self.historical_candles.tail(self.config['exchange_settings']['candle_history_size'])
            
            logger.info(f"✅ Completed 5-minute candle: O:{self.current_candle['open']:.2f} H:{self.current_candle['high']:.2f} L:{self.current_candle['low']:.2f} C:{self.current_candle['close']:.2f}")
            
            self.current_candle = None
            self.current_candle_start_time = None
    
    def get_combined_data(self):
        """Combine historical 5-minute candles with real-time current candle"""
        if self.historical_candles.empty:
            logger.warning("No historical data available")
            return None
        
        combined_data = self.historical_candles.copy()
        
        if (self.config['feature_toggles']['enable_realtime_tick_data'] and 
            self.current_candle is not None):
            
            current_candle_df = pd.DataFrame([{
                'open': self.current_candle['open'],
                'high': self.current_candle['high'],
                'low': self.current_candle['low'],
                'close': self.current_candle['close'],
                'volume': self.current_candle['count'],
                'open_time_ms': self.current_candle_start_time
            }], index=[self.current_candle['start_time']])
            
            combined_data = pd.concat([combined_data, current_candle_df])
        
        return combined_data
    
    def calculate_cci(self, df, period=20, constant=0.015):
        """Calculate Commodity Channel Index"""
        try:
            high = df['high'].astype(float)
            low = df['low'].astype(float)
            close = df['close'].astype(float)
            
            tp = (high + low + close) / 3.0
            sma = tp.rolling(window=period).mean()
            
            def mean_deviation(x):
                x_values = x.values.astype(float)
                return np.mean(np.abs(x_values - np.mean(x_values)))
            
            mad = tp.rolling(window=period).apply(mean_deviation, raw=False)
            cci = (tp - sma) / (constant * mad)
            return cci
            
        except Exception as e:
            logger.error(f"Error calculating CCI: {e}")
            return None
    
    def calculate_cci_sma(self, cci_series, period=14):
        """Calculate CCI Smoothed Moving Average"""
        if cci_series is None or len(cci_series) < period:
            return None
        
        try:
            cci_sma = cci_series.rolling(window=period).mean()
            return cci_sma
        except Exception as e:
            logger.error(f"Error calculating CCI SMA: {e}")
            return None
    
    def analyze_cci_sma_cross(self, cci_series, cci_sma_series):
        """Analyze CCI and CCI SMA cross/touch conditions"""
        if (cci_series is None or cci_sma_series is None or 
            len(cci_series) < 2 or len(cci_sma_series) < 2):
            return {"status": "INSUFFICIENT_DATA"}
        
        current_cci = cci_series.iloc[-1]
        current_sma = cci_sma_series.iloc[-1]
        previous_cci = cci_series.iloc[-2]
        previous_sma = cci_sma_series.iloc[-2]
        
        if (pd.isna(current_cci) or pd.isna(current_sma) or 
            pd.isna(previous_cci) or pd.isna(previous_sma)):
            return {"status": "INSUFFICIENT_DATA"}
        
        distance = abs(current_cci - current_sma)
        percentage_diff = (distance / abs(current_sma)) * 100 if current_sma != 0 else float('inf')
        
        nearness_level = "FAR"
        for level, threshold in self.config['alert_thresholds']['sma_nearness_levels'].items():
            if distance <= threshold:
                nearness_level = level
                break
        
        state = {
            'status': 'OK',
            'current_cci': round(current_cci, 2),
            'current_sma': round(current_sma, 2),
            'distance': round(distance, 4),
            'percentage_diff': round(percentage_diff, 2),
            'nearness_level': nearness_level,
            'action': 'NONE',
            'cross_type': 'NONE'
        }
        
        threshold = self.config['alert_thresholds']['sma_cross_closeness']
        
        if previous_cci < previous_sma and current_cci >= current_sma:
            if distance <= threshold:
                state['cross_type'] = 'BULLISH_CROSS'
                state['action'] = 'CROSSED_ABOVE'
            else:
                state['action'] = 'ABOVE_SMA'
                
        elif previous_cci > previous_sma and current_cci <= current_sma:
            if distance <= threshold:
                state['cross_type'] = 'BEARISH_CROSS'
                state['action'] = 'CROSSED_BELOW'
            else:
                state['action'] = 'BELOW_SMA'
                
        elif current_cci > current_sma:
            state['action'] = 'ABOVE_SMA'
            if distance <= threshold:
                state['action'] = 'TOUCHING_ABOVE'
                
        elif current_cci < current_sma:
            state['action'] = 'BELOW_SMA'
            if distance <= threshold:
                state['action'] = 'TOUCHING_BELOW'
        
        return state
    
    def calculate_macd(self, df, fast=12, slow=26, signal=9):
        """Calculate MACD indicator"""
        try:
            close_prices = df['close'].astype(float)
            ema_fast = close_prices.ewm(span=fast).mean()
            ema_slow = close_prices.ewm(span=slow).mean()
            macd_line = ema_fast - ema_slow
            signal_line = macd_line.ewm(span=signal).mean()
            histogram = macd_line - signal_line
            return macd_line, signal_line, histogram
            
        except Exception as e:
            logger.error(f"Error calculating MACD: {e}")
            return None, None, None
    
    def analyze_cci_state(self, cci_series):
        """Analyze current CCI state"""
        if cci_series is None or len(cci_series) < 2:
            return {"status": "INSUFFICIENT_DATA"}
        
        current_cci = cci_series.iloc[-1]
        previous_cci = cci_series.iloc[-2]
        
        if pd.isna(current_cci) or pd.isna(previous_cci):
            return {"status": "INSUFFICIENT_DATA"}
        
        upper_level = self.config['indicator_settings']['cci_upper_level']
        lower_level = self.config['indicator_settings']['cci_lower_level']
        threshold = self.config['alert_thresholds']['cross_100_closeness']
        
        state = {
            'status': 'OK',
            'current_value': round(current_cci, 2),
            'trend': 'BULLISH' if current_cci > previous_cci else 'BEARISH',
            'level': 'NEUTRAL',
            'cross': 'NONE',
            'action': 'NONE'
        }
        
        if abs(current_cci - upper_level) <= threshold:
            state['level'] = 'NEAR_UPPER'
            state['action'] = 'ABOUT_TO_CROSS_DOWN' if current_cci > upper_level else 'APPROACHING_UPPER'
        elif current_cci > upper_level:
            state['level'] = 'ABOVE_UPPER'
        elif abs(current_cci - lower_level) <= threshold:
            state['level'] = 'NEAR_LOWER'
            state['action'] = 'ABOUT_TO_CROSS_UP' if current_cci < lower_level else 'APPROACHING_LOWER'
        elif current_cci < lower_level:
            state['level'] = 'BELOW_LOWER'
        
        if (previous_cci < upper_level and current_cci >= upper_level):
            state['cross'] = 'CROSSED_ABOVE_UPPER'
        elif (previous_cci > upper_level and current_cci <= upper_level):
            state['cross'] = 'CROSSED_BELOW_UPPER'
        elif (previous_cci > lower_level and current_cci <= lower_level):
            state['cross'] = 'CROSSED_BELOW_LOWER'
        elif (previous_cci < lower_level and current_cci >= lower_level):
            state['cross'] = 'CROSSED_ABOVE_LOWER'
        
        return state
    
    def analyze_macd_state(self, macd_line, signal_line, histogram):
        """Analyze current MACD state"""
        if macd_line is None or len(macd_line) < 2:
            return {"status": "INSUFFICIENT_DATA"}
        
        current_macd = macd_line.iloc[-1]
        current_signal = signal_line.iloc[-1]
        current_hist = histogram.iloc[-1]
        previous_macd = macd_line.iloc[-2]
        previous_signal = signal_line.iloc[-2]
        
        if (pd.isna(current_macd) or pd.isna(current_signal) or 
            pd.isna(current_hist) or pd.isna(previous_macd) or pd.isna(previous_signal)):
            return {"status": "INSUFFICIENT_DATA"}
        
        state = {
            'status': 'OK',
            'macd_value': round(current_macd, 6),
            'signal_value': round(current_signal, 6),
            'histogram_value': round(current_hist, 6),
            'trend': 'BULLISH' if current_macd > current_signal else 'BEARISH',
            'crossover': 'NONE',
            'momentum': 'STABLE'
        }
        
        if previous_macd < previous_signal and current_macd >= current_signal:
            state['crossover'] = 'BULLISH_CROSSOVER'
        elif previous_macd > previous_signal and current_macd <= current_signal:
            state['crossover'] = 'BEARISH_CROSSOVER'
        
        if len(histogram) >= 3:
            prev_hist = histogram.iloc[-2]
            if not pd.isna(prev_hist):
                if abs(current_hist) > abs(prev_hist):
                    state['momentum'] = 'INCREASING'
                elif abs(current_hist) < abs(prev_hist):
                    state['momentum'] = 'DECREASING'
        
        return state
    
    def generate_report(self, cci_state, macd_state, cci_sma_state, latest_data, is_realtime=False):
        """Generate monitoring report"""
        if latest_data is None or latest_data.empty:
            return {
                'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
                'status': 'NO_DATA',
                'signal_alert': False
            }
        
        report = {
            'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
            'latest_candle_time': latest_data.index[-1].strftime('%Y-%m-%d %H:%M:%S'),
            'latest_close': float(latest_data['close'].iloc[-1]),
            'timeframe_used': self.config['exchange_settings']['timeframe'],
            'total_candles': len(latest_data),
            'cci': cci_state,
            'macd': macd_state,
            'cci_sma': cci_sma_state,
            'signal_alert': False,
            'is_realtime': is_realtime
        }
        
        if (cci_state.get('status') == 'OK' and 
            (('cross' in cci_state and cci_state['cross'] != 'NONE') or 
             cci_state['level'] in ['NEAR_UPPER', 'NEAR_LOWER'])):
            report['signal_alert'] = True
        
        if (macd_state.get('status') == 'OK' and 
            macd_state['crossover'] != 'NONE'):
            report['signal_alert'] = True
        
        if (self.config['feature_toggles']['enable_cci_sma_analysis'] and 
            cci_sma_state.get('status') == 'OK' and
            cci_sma_state['cross_type'] != 'NONE'):
            report['signal_alert'] = True
            report['cci_sma_alert'] = True
        
        return report
    
    def display_report(self, report):
        """Display the monitoring report"""
        print("\n" + "="*90)
        print(f"INDICATOR MONITORING REPORT - {report['timestamp']}")
        print(f"TIMEFRAME: {report['timeframe_used']} 📊 | Candles: {report['total_candles']}")
        if report['is_realtime']:
            print("📡 REAL-TIME DATA (Current Forming Candle)")
        else:
            print("💾 HISTORICAL DATA ONLY")
        print("="*90)
        
        if report.get('status') == 'NO_DATA':
            print("❌ No data available for analysis")
            print("="*90)
            return
        
        print(f"Latest Candle: {report['latest_candle_time']}")
        print(f"Close Price: {report['latest_close']:.8f}")
        
        print("\n🔹 CCI Analysis:")
        if report['cci'].get('status') != 'OK':
            print("  ❌ Insufficient data for CCI calculation")
        else:
            print(f"  Value: {report['cci']['current_value']}")
            print(f"  Trend: {report['cci']['trend']}")
            print(f"  Level: {report['cci']['level']}")
            if report['cci']['cross'] != 'NONE':
                print(f"  Cross: {report['cci']['cross']}")
            if report['cci']['action'] != 'NONE':
                print(f"  Action: {report['cci']['action']}")
        
        if self.config['feature_toggles']['enable_cci_sma_analysis']:
            print("\n🔸 CCI SMA Analysis:")
            if report['cci_sma'].get('status') != 'OK':
                print("  ❌ Insufficient data for CCI SMA calculation")
            else:
                print(f"  CCI Value: {report['cci_sma']['current_cci']}")
                print(f"  SMA Value: {report['cci_sma']['current_sma']}")
                print(f"  Distance: {report['cci_sma']['distance']}")
                print(f"  Difference: {report['cci_sma']['percentage_diff']}%")
                print(f"  Nearness: {report['cci_sma']['nearness_level']}")
                print(f"  Action: {report['cci_sma']['action']}")
                if report['cci_sma']['cross_type'] != 'NONE':
                    print(f"  Cross Type: {report['cci_sma']['cross_type']}")
        
        print("\n🔹 MACD Analysis:")
        if report['macd'].get('status') != 'OK':
            print("  ❌ Insufficient data for MACD calculation")
        else:
            print(f"  MACD Line: {report['macd']['macd_value']}")
            print(f"  Signal Line: {report['macd']['signal_value']}")
            print(f"  Histogram: {report['macd']['histogram_value']}")
            print(f"  Trend: {report['macd']['trend']}")
            print(f"  Crossover: {report['macd']['crossover']}")
            print(f"  Momentum: {report['macd']['momentum']}")
        
        alerts = []
        if report.get('signal_alert'):
            if report.get('cci_sma_alert'):
                alerts.append("CCI SMA CROSSOVER DETECTED!")
            else:
                alerts.append("SIGNAL ALERT!")
        
        if alerts:
            print(f"\n🚨 {' | '.join(alerts)}")
        else:
            print("\n📊 No significant signals detected")
        
        print("="*90)
    
    def check_indicators(self):
        """Check indicators, display report, and trigger alerts"""
        if not self.config['feature_toggles']['enable_indicator_monitoring']:
            return
            
        combined_data = self.get_combined_data()
        
        if combined_data is None or len(combined_data) < max(
            self.config['indicator_settings']['cci_period'], 
            self.config['indicator_settings']['macd_slow']
        ):
            logger.warning("Insufficient data for calculations")
            return
        
        is_realtime = (self.config['feature_toggles']['enable_realtime_tick_data'] and 
                      self.current_candle is not None)
        
        # Calculate indicators
        cci = self.calculate_cci(
            combined_data, 
            self.config['indicator_settings']['cci_period'], 
            self.config['indicator_settings']['cci_constant']
        )
        macd_line, signal_line, histogram = self.calculate_macd(
            combined_data, 
            self.config['indicator_settings']['macd_fast'], 
            self.config['indicator_settings']['macd_slow'], 
            self.config['indicator_settings']['macd_signal']
        )
        
        # CCI SMA calculations
        cci_sma = None
        cci_sma_state = {"status": "DISABLED"}
        if self.config['feature_toggles']['enable_cci_sma_analysis'] and cci is not None:
            cci_sma = self.calculate_cci_sma(cci, self.config['indicator_settings']['cci_sma_period'])
            if cci_sma is not None:
                cci_sma_state = self.analyze_cci_sma_cross(cci, cci_sma)
        
        # Check and trigger audio alerts
        if cci is not None and len(cci) >= 2:
            previous_cci = cci.iloc[-2] if len(cci) >= 2 else None
            previous_sma = cci_sma.iloc[-2] if cci_sma is not None and len(cci_sma) >= 2 else None
            
            self.check_alerts(cci, cci_sma, previous_cci, previous_sma)
        
        # Analyze states
        cci_state = self.analyze_cci_state(cci) if cci is not None else {"status": "ERROR"}
        macd_state = self.analyze_macd_state(macd_line, signal_line, histogram)
        
        # Generate and display report
        report = self.generate_report(cci_state, macd_state, cci_sma_state, combined_data, is_realtime)
        self.display_report(report)
    
    def run_monitoring_loop(self):
        """Main monitoring loop"""
        logger.info("Starting Trading Monitor with Audio Alerts...")
        logger.info(f"Data fetching: {'ENABLED' if self.config['feature_toggles']['enable_data_fetching'] else 'DISABLED'}")
        logger.info(f"Indicator monitoring: {'ENABLED' if self.config['feature_toggles']['enable_indicator_monitoring'] else 'DISABLED'}")
        logger.info(f"Real-time tick data: {'ENABLED' if self.config['feature_toggles']['enable_realtime_tick_data'] else 'DISABLED'}")
        logger.info(f"CCI SMA analysis: {'ENABLED' if self.config['feature_toggles']['enable_cci_sma_analysis'] else 'DISABLED'}")
        logger.info(f"Audio alerts: {'ENABLED' if self.config['audio_settings']['enable_audio_alerts'] else 'DISABLED'}")
        logger.info(f"Telegram alerts: {'ENABLED' if self.config['telegram_settings']['enable_telegram_alerts'] else 'DISABLED'}")
        
        # Display alert settings
        logger.info("Alert Toggles:")
        for alert, enabled in self.config['alert_toggles'].items():
            logger.info(f"  {alert}: {'ON' if enabled else 'OFF'}")
        
        # Initial data fetch
        self.fetch_historical_candles()
        
        if self.config['feature_toggles']['enable_realtime_tick_data']:
            self.start_tick_data_collection()
        
        try:
            data_interval = self.config['loop_intervals']['data_fetching']
            indicator_interval = self.config['loop_intervals']['indicator_check']
            
            while True:
                current_time = time.time()
                
                if self.config['feature_toggles']['enable_realtime_tick_data']:
                    self.check_and_complete_candle()
                
                # Refresh historical data periodically
                if (self.config['feature_toggles']['enable_data_fetching'] and 
                    current_time - self.last_fetch_time >= data_interval):
                    self.last_fetch_time = current_time
                    self.fetch_historical_candles()
                
                # Check indicators periodically
                if (self.config['feature_toggles']['enable_indicator_monitoring'] and 
                    current_time - self.last_check_time >= indicator_interval):
                    self.last_check_time = current_time
                    self.check_indicators()
                
                time.sleep(0.1)  # Small sleep to prevent CPU overload
                
        except KeyboardInterrupt:
            logger.info("Monitoring stopped by user")
            self.stop_tick_thread = True
            if self.tick_thread:
                self.tick_thread.join(timeout=2)
        except Exception as e:
            logger.error(f"Monitoring error: {e}")
        finally:
            self.stop_tick_thread = True
            if self.audio_initialized:
                pygame.mixer.quit()

# Main execution
if __name__ == "__main__":
    monitor = TradingMonitor(CONFIG)
    monitor.run_monitoring_loop()
