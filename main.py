import os
import threading
import logging
import time
import requests
import pandas as pd
import numpy as np
import atexit
import pytz
import traceback
import json
import queue
from datetime import datetime, timedelta
from concurrent.futures import ThreadPoolExecutor
from flask import Flask
from iqoptionapi.stable_api import IQ_Option
from collections import defaultdict, deque

# ============================================================
# VERSION FINAL - 3-STAGE ALERT SYSTEM (ARABIC) + HTF ENHANCED
# ============================================================

VERSION = "7.6-FINAL-FIXED-CANDLE-HTFP"

# ========== CONSTANTS ==========
CAIRO_TZ = pytz.timezone('Africa/Cairo')
UTC_TZ = pytz.utc
TIMEFRAME_5M = 300
TIMEFRAME_15M = 900
TIMEFRAME_1H = 3600
CACHE_TTL = 300
CLEANUP_INTERVAL = 3600
MAX_WORKERS = 5
MAX_PAIRS_PER_CYCLE = 14
MARTINGALE_HUNT_INTERVAL = 1800
DISABLE_WINDOW = 50
DISABLE_THRESHOLD = 45
DISABLE_DURATION = 604800
PAYOUT_RATIO = float(os.environ.get("PAYOUT_RATIO", "0.85"))
STRATEGY_SCORE_WINDOW = 100
WALK_FORWARD_MIN_TRADES = 1000
WALK_FORWARD_TRAIN_RATIO = 0.70
WALK_FORWARD_FILE = "walk_forward_state.json"
MONTE_CARLO_MIN_TRADES = 500
MONTE_CARLO_SIMULATIONS = 1000
MONTE_CARLO_FILE = "monte_carlo_results.json"
BLOCK_SIZE = 20
REGIME_CACHE_TTL = 300
ADAPTIVE_THRESHOLD_ENABLED = True
ADAPTIVE_THRESHOLD_WINDOW = 250
ADAPTIVE_THRESHOLD_MIN = 65
ADAPTIVE_THRESHOLD_MAX = 100
SETTINGS_CACHE_TTL = 300

# ========== SCORE CONFIGURATION ==========
MIN_SCORE_ALL_STRATEGIES = 80

SCORE_LEVELS = {
    1: (80, 84),
    2: (85, 89),
    3: (90, 94),
    4: (95, 100)
}

# ========== REGIME ASSIGNMENT ==========
STRATEGY_REGIMES = {
    'original': ['trending', 'high_vol'],
    'king': ['trending', 'high_vol'],
    'quantum': ['trending', 'high_vol'],
    'smart': ['high_vol'],
    'pro': ['ranging', 'mixed']
}

# ========== HTF & PAIR-SPECIFIC CONFIGURATION ==========
TIMEFRAME_4H = 14400
HTF_REGIME_CACHE_TTL = 900

# Pair-specific volatility and ADX thresholds (based on average daily ranges)
PAIR_THRESHOLDS = {
    # Major pairs - lower volatility
    "EURUSD": {"adx_trending": 22, "adx_ranging": 16, "atr_min_pct": 0.00025, "atr_max_pct": 0.003, "volatility_ideal_low": 0.0008, "volatility_ideal_high": 0.003},
    "GBPUSD": {"adx_trending": 22, "adx_ranging": 16, "atr_min_pct": 0.00030, "atr_max_pct": 0.004, "volatility_ideal_low": 0.0010, "volatility_ideal_high": 0.004},
    "USDJPY": {"adx_trending": 22, "adx_ranging": 16, "atr_min_pct": 0.00025, "atr_max_pct": 0.003, "volatility_ideal_low": 0.0008, "volatility_ideal_high": 0.003},
    "USDCHF": {"adx_trending": 22, "adx_ranging": 16, "atr_min_pct": 0.00025, "atr_max_pct": 0.003, "volatility_ideal_low": 0.0008, "volatility_ideal_high": 0.003},
    "AUDUSD": {"adx_trending": 22, "adx_ranging": 16, "atr_min_pct": 0.00030, "atr_max_pct": 0.004, "volatility_ideal_low": 0.0010, "volatility_ideal_high": 0.004},
    "USDCAD": {"adx_trending": 22, "adx_ranging": 16, "atr_min_pct": 0.00025, "atr_max_pct": 0.003, "volatility_ideal_low": 0.0008, "volatility_ideal_high": 0.003},
    # Cross pairs - medium volatility
    "EURJPY": {"adx_trending": 24, "adx_ranging": 18, "atr_min_pct": 0.00040, "atr_max_pct": 0.005, "volatility_ideal_low": 0.0015, "volatility_ideal_high": 0.005},
    "EURGBP": {"adx_trending": 22, "adx_ranging": 16, "atr_min_pct": 0.00025, "atr_max_pct": 0.003, "volatility_ideal_low": 0.0008, "volatility_ideal_high": 0.003},
    "GBPJPY": {"adx_trending": 26, "adx_ranging": 20, "atr_min_pct": 0.00050, "atr_max_pct": 0.006, "volatility_ideal_low": 0.0020, "volatility_ideal_high": 0.006},
    "AUDJPY": {"adx_trending": 24, "adx_ranging": 18, "atr_min_pct": 0.00040, "atr_max_pct": 0.005, "volatility_ideal_low": 0.0015, "volatility_ideal_high": 0.005},
    "CADJPY": {"adx_trending": 24, "adx_ranging": 18, "atr_min_pct": 0.00040, "atr_max_pct": 0.005, "volatility_ideal_low": 0.0015, "volatility_ideal_high": 0.005},
    "EURAUD": {"adx_trending": 24, "adx_ranging": 18, "atr_min_pct": 0.00035, "atr_max_pct": 0.004, "volatility_ideal_low": 0.0012, "volatility_ideal_high": 0.004},
    "EURCAD": {"adx_trending": 24, "adx_ranging": 18, "atr_min_pct": 0.00035, "atr_max_pct": 0.004, "volatility_ideal_low": 0.0012, "volatility_ideal_high": 0.004},
    "AUDCAD": {"adx_trending": 22, "adx_ranging": 16, "atr_min_pct": 0.00030, "atr_max_pct": 0.004, "volatility_ideal_low": 0.0010, "volatility_ideal_high": 0.004},
    # OTC pairs - higher volatility
    "EURUSD-OTC": {"adx_trending": 26, "adx_ranging": 20, "atr_min_pct": 0.00050, "atr_max_pct": 0.006, "volatility_ideal_low": 0.0015, "volatility_ideal_high": 0.005},
    "GBPUSD-OTC": {"adx_trending": 26, "adx_ranging": 20, "atr_min_pct": 0.00060, "atr_max_pct": 0.007, "volatility_ideal_low": 0.0020, "volatility_ideal_high": 0.006},
    "USDJPY-OTC": {"adx_trending": 26, "adx_ranging": 20, "atr_min_pct": 0.00050, "atr_max_pct": 0.006, "volatility_ideal_low": 0.0015, "volatility_ideal_high": 0.005},
    "USDCHF-OTC": {"adx_trending": 26, "adx_ranging": 20, "atr_min_pct": 0.00050, "atr_max_pct": 0.006, "volatility_ideal_low": 0.0015, "volatility_ideal_high": 0.005},
    "EURJPY-OTC": {"adx_trending": 28, "adx_ranging": 22, "atr_min_pct": 0.00070, "atr_max_pct": 0.008, "volatility_ideal_low": 0.0025, "volatility_ideal_high": 0.007},
    "EURGBP-OTC": {"adx_trending": 26, "adx_ranging": 20, "atr_min_pct": 0.00050, "atr_max_pct": 0.006, "volatility_ideal_low": 0.0015, "volatility_ideal_high": 0.005},
    "AUDCAD-OTC": {"adx_trending": 26, "adx_ranging": 20, "atr_min_pct": 0.00060, "atr_max_pct": 0.007, "volatility_ideal_low": 0.0020, "volatility_ideal_high": 0.006},
    "GBPJPY-OTC": {"adx_trending": 30, "adx_ranging": 24, "atr_min_pct": 0.00080, "atr_max_pct": 0.010, "volatility_ideal_low": 0.0030, "volatility_ideal_high": 0.008},
}

def get_pair_thresholds(pair):
    """Get pair-specific thresholds, fallback to EURUSD defaults"""
    return PAIR_THRESHOLDS.get(pair, PAIR_THRESHOLDS.get("EURUSD"))

# ========== QUANTUM CONFIGURATION ==========
QUANTUM_CONFIG = {
    "min_score_live": 70,
    "min_score_otc": 65,
    "cooldown": 300,
    "weights": {
        "structure": 20,
        "liquidity": 20,
        "order_block": 15,
        "fvg": 15,
        "volume": 5,
        "momentum": 20,
        "rsi": 5
    },
    "learning": {
        "min_trades": 50,
        "update_interval": 86400,
        "max_weight": 30,
        "min_weight": 5
    },
    "backtest": {
        "min_history": 100,
        "test_size": 0.3
    },
    "volatility_filter": {
        "min_volatility": 0.0005,
        "max_volatility": 0.01,
        "ideal_low": 0.001,
        "ideal_high": 0.005,
        "score_bonus": 5,
        "score_penalty": 10,
        "reject_low": 0.00005,
        "reject_high": 0.012
    }
}

QUANTUM_SIGNAL_NAMES = {
    1: ("Quantum Bronze 🧠🥉", "QUANTUM BRONZE"),
    2: ("Quantum Silver 🧠🥈", "QUANTUM SILVER"),
    3: ("Quantum Gold 🧠🥇", "QUANTUM GOLD"),
    4: ("Quantum Elite 🧠👑", "QUANTUM ELITE")
}
QUANTUM_EMOJIS = {1: "🧠🥉", 2: "🧠🥈", 3: "🧠🥇", 4: "🧠👑"}

quantum_memory = {}
quantum_weights_history = []
kalman_instances = {}

# ========== CREDENTIALS ==========
IQ_EMAIL = os.environ.get("IQ_EMAIL", "zain1mohamed2425@gmail.com")
IQ_PASSWORD = os.environ.get("IQ_PASSWORD", "ZainMohamed2425@")
ACCOUNT_TYPE = os.environ.get("ACCOUNT_TYPE", "PRACTICE")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8794920089:AAFnRnoudkdPrlMtDaijlaQgczrTkaM0MU4")
CHAT_ID = os.environ.get("CHAT_ID", "1462370563")

if not IQ_EMAIL or not IQ_PASSWORD:
    raise ValueError("❌ IQ_EMAIL and IQ_PASSWORD required!")
if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("❌ TELEGRAM_TOKEN and CHAT_ID required!")

# ========== LOGGING ==========
class CairoFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        ct = datetime.fromtimestamp(record.created, pytz.utc) + timedelta(hours=3)
        if datefmt:
            return ct.strftime(datefmt)
        return ct.strftime('%Y-%m-%d %H:%M:%S')

formatter = CairoFormatter('%(asctime)s - %(levelname)s - %(message)s')
file_handler = logging.FileHandler("bot.log", encoding='utf-8')
file_handler.setFormatter(formatter)
stream_handler = logging.StreamHandler()
stream_handler.setFormatter(formatter)

logging.basicConfig(
    level=logging.INFO,
    handlers=[file_handler, stream_handler]
)
logger = logging.getLogger(__name__)
logging.getLogger('iqoptionapi').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# ========== FLASK ==========
app = Flask(__name__)

@app.route('/')
def home():
    return f"✅ Bot is Running Successfully {VERSION}"

@app.route('/health')
def health():
    return {"status": "healthy", "version": VERSION}

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# ========== LOCKS ==========
data_lock = threading.RLock()
api_lock = threading.Lock()
telegram_lock = threading.Lock()
stop_event = threading.Event()

# ========== TELEGRAM QUEUE ==========
telegram_queue = queue.Queue()

# ========== CACHE CLASS ==========
class LimitedCache:
    def __init__(self, maxsize=1000):
        self.cache = {}
        self.maxsize = maxsize
        self._lock = threading.Lock()

    def get(self, key):
        with self._lock:
            if key in self.cache:
                data, ts = self.cache[key]
                if time.time() - ts < CACHE_TTL:
                    return data
                del self.cache[key]
        return None

    def set(self, key, data):
        with self._lock:
            if len(self.cache) >= self.maxsize:
                keys = sorted(self.cache.keys(), key=lambda k: self.cache[k][1])
                for k in keys[:int(self.maxsize * 0.2)]:
                    del self.cache[k]
            self.cache[key] = (data, time.time())

    def cleanup(self):
        now = time.time()
        with self._lock:
            for key in list(self.cache.keys()):
                if now - self.cache[key][1] > CACHE_TTL:
                    del self.cache[key]

# ========== BOT STATE ==========
class BotState:
    def __init__(self):
        self.active_trades = []
        self.martingale_queue = {}
        self.recent_signals = {}
        self.sent_signals = {}
        self.ht_trend_cache = {}
        self.king_sent_signals = {}
        self.king_recent_signals = {}
        self.alerted_pairs = {}
        self.king_alerted_pairs = {}
        self.smart_alerted_pairs = {}
        self.smart_sent_signals = {}
        self.pa_sent_signals = {}
        self.pa_alerted_pairs = {}
        self.disabled_pairs = {}
        self.regime_cache = {}
        self.adaptive_thresholds = {"live": 70, "otc": 70}
        self.strategy_scores = {}
        self.stats = defaultdict(lambda: {"win": 0, "loss": 0, "total": 0})
        self.king_stats = defaultdict(lambda: {"win": 0, "loss": 0, "total": 0})
        self.smart_stats = defaultdict(lambda: {"win": 0, "loss": 0, "total": 0})
        self.pro_stats = defaultdict(lambda: {"win": 0, "loss": 0, "total": 0})
        self.quantum_stats = defaultdict(lambda: {"win": 0, "loss": 0, "total": 0})
        self.settings_cache = {}
        self.invalid_assets = set()
        self.news_data = []
        self.last_news_update = 0
        self.news_fetch_failed = False
        self.last_hunt_message_time = 0
        self.hunt_mode_announced = {}
        self.last_reconnect_attempt = 0
        self.reconnect_delay = 5
        self.server_time_offset = 0
        self.cycle_count = 0
        self.is_reconnecting = False
        self.king_htf_cache = {}
        # ===== نظام التنبيهات الجديد (3 مراحل) =====
        self.pending_alerts = {}
        # ===== منع تكرار الإشارات النهائية =====
        self.sent_final_signals = {}
        # ===== Quantum =====
        self.quantum_alerted_pairs = {}
        self.quantum_sent_signals = {}

state = BotState()

# ========== CACHES ==========
candles_cache = LimitedCache(maxsize=500)
df_cache = LimitedCache(maxsize=200)
king_df_cache = LimitedCache(maxsize=200)
smart_df_cache = LimitedCache(maxsize=200)

# ========== API ==========
API = None

# ========== TIME FUNCTIONS ==========
def get_cairo_time():
    return datetime.now(pytz.utc) + timedelta(hours=3)

def get_iq_time():
    with data_lock:
        return time.time() + state.server_time_offset

def sync_server_time(api_instance):
    try:
        iq_timestamp = api_instance.get_server_timestamp()
        if iq_timestamp:
            with data_lock:
                state.server_time_offset = iq_timestamp - time.time()
            logger.info(f"⏱️ تم المزامنة: {state.server_time_offset:.2f} ثانية")
    except Exception as e:
        logger.warning(f"⚠️ فشلت المزامنة: {e}")

# ========== INIT FILES ==========
FILES = {
    "settings_live.json": {},
    "settings_otc.json": {},
    "king_weights.json": {
        "structure": 20, "sweep": 20, "trend": 15,
        "momentum": 10, "volatility": 10, "adx": 10,
        "rsi": 5, "stochastic": 5, "candle": 5
    },
    "optimization_proposal.json": {},
    "walk_forward_state.json": {},
    "stats_state.json": {},
    "stats_state_live.json": {},
    "stats_state_otc.json": {},
    "quantum_weights_history.json": [],
}

JSONL_FILES = ["trade_log_live.jsonl", "trade_log_otc.jsonl"]

def init_log_files():
    for filename, default_data in FILES.items():
        needs_fix = False
        if not os.path.exists(filename):
            needs_fix = True
        else:
            try:
                with open(filename, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if not content:
                    needs_fix = True
            except Exception:
                needs_fix = True
        if needs_fix:
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    json.dump(default_data, f)
                logger.info(f"📁 تم إنشاء {filename}")
            except Exception as e:
                logger.warning(f"⚠️ فشل إنشاء {filename}: {e}")

    for filename in JSONL_FILES:
        if not os.path.exists(filename):
            try:
                with open(filename, 'w', encoding='utf-8') as f:
                    pass
                logger.info(f"📁 تم إنشاء {filename}")
            except Exception as e:
                logger.warning(f"⚠️ فشل إنشاء {filename}: {e}")

# ========== KING WEIGHTS ==========
DEFAULT_KING_WEIGHTS = {
    "structure": 20, "sweep": 20, "trend": 15,
    "momentum": 10, "volatility": 10, "adx": 10,
    "rsi": 5, "stochastic": 5, "candle": 5
}

WEIGHTS_FILE = "king_weights.json"

def load_king_weights():
    if not os.path.exists(WEIGHTS_FILE):
        return DEFAULT_KING_WEIGHTS.copy()
    try:
        with open(WEIGHTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if data and isinstance(data, dict):
                return {k: int(v) for k, v in data.items()}
    except Exception as e:
        logger.warning(f"⚠️ فشل تحميل الأوزان: {e}")
    return DEFAULT_KING_WEIGHTS.copy()

def save_king_weights(weights):
    with data_lock:
        with open(WEIGHTS_FILE, 'w', encoding='utf-8') as f:
            json.dump(weights, f, indent=2)

KING_WEIGHTS = load_king_weights()

# ========== SETTINGS ==========
SETTINGS_LIVE_FILE = "settings_live.json"
SETTINGS_OTC_FILE = "settings_otc.json"

def load_settings(market_type="live"):
    file_path = SETTINGS_LIVE_FILE if market_type == "live" else SETTINGS_OTC_FILE
    default = {
        "adx_threshold": 22,
        "rsi_low_call": 30,
        "rsi_high_call": 50,
        "rsi_low_put": 50,
        "rsi_high_put": 70,
        "sweep_threshold": 0.0003,
        "body_pct_min": 0.60,
        "last_updated": 0,
        "market_type": market_type,
        "approved": False,
        "walk_forward_wr": 0,
        "baseline_wr": 0
    }
    if not os.path.exists(file_path):
        return default.copy()
    try:
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read().strip()
        if not content:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(default, f)
            return default.copy()
        data = json.loads(content)
        if isinstance(data, dict):
            for key in default:
                if key not in data:
                    data[key] = default[key]
            return data
    except Exception as e:
        logger.warning(f"⚠️ فشل تحميل إعدادات {market_type}: {e}")
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(default, f)
        except Exception:
            pass
    return default.copy()

def save_settings(settings, market_type="live"):
    file_path = SETTINGS_LIVE_FILE if market_type == "live" else SETTINGS_OTC_FILE
    with data_lock:
        try:
            with open(file_path, 'w', encoding='utf-8') as f:
                json.dump(settings, f, indent=2)
            logger.info(f"💾 تم حفظ إعدادات {market_type.upper()}")
        except Exception as e:
            logger.error(f"❌ فشل حفظ {market_type}: {e}")

def get_settings_for_pair(pair):
    market_type = "otc" if "-OTC" in pair.upper() else "live"
    cache_key = f"settings_{market_type}"
    now = time.time()
    with data_lock:
        if cache_key in state.settings_cache:
            data, ts = state.settings_cache[cache_key]
            if now - ts < SETTINGS_CACHE_TTL:
                return data
    settings = load_settings(market_type)
    with data_lock:
        state.settings_cache[cache_key] = (settings, now)
    return settings

# ========== HELPERS ==========
def is_otc_pair(pair):
    return "-OTC" in pair.upper()

def get_trade_log_file(pair):
    return "trade_log_otc.jsonl" if is_otc_pair(pair) else "trade_log_live.jsonl"

def log_trade(trade_data):
    try:
        log_file = get_trade_log_file(trade_data.get("pair", ""))
        with data_lock:
            with open(log_file, 'a', encoding='utf-8') as f:
                f.write(json.dumps(trade_data, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"خطأ في تسجيل الصفقة: {e}")

def read_trade_log(max_entries=50000, market_type=None):
    trades = []
    files = []
    if market_type == 'live':
        files = ["trade_log_live.jsonl"]
    elif market_type == 'otc':
        files = ["trade_log_otc.jsonl"]
    else:
        files = ["trade_log_live.jsonl", "trade_log_otc.jsonl"]
    for log_file in files:
        if not os.path.exists(log_file):
            continue
        try:
            with open(log_file, 'r', encoding='utf-8') as f:
                lines = f.readlines()
            for line in lines[-max_entries:]:
                line = line.strip()
                if line:
                    try:
                        trades.append(json.loads(line))
                    except Exception:
                        continue
        except Exception as e:
            logger.error(f"خطأ في قراءة {log_file}: {e}")
    trades.sort(key=lambda x: x.get("timestamp", 0))
    return trades[-max_entries:] if len(trades) > max_entries else trades

# ========== TELEGRAM ==========
def _send_telegram_raw(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    for attempt in range(3):
        try:
            res = requests.post(url, json=payload, timeout=5)
            if res.ok:
                return True
        except Exception as e:
            logger.error(f"خطأ في الإرسال: {e}")
            time.sleep(1)
    return False

def send_telegram_message(message):
    telegram_queue.put(message)

def telegram_worker():
    while not stop_event.is_set():
        try:
            msg = telegram_queue.get(timeout=1)
            _send_telegram_raw(msg)
        except queue.Empty:
            continue
        except Exception as e:
            logger.error(f"خطأ في Telegram: {e}")

telegram_last_update_id = 0

def telegram_reply_worker():
    global telegram_last_update_id
    logger.info("📱 بدء تشغيل الردود...")
    while not stop_event.is_set():
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {"offset": telegram_last_update_id + 1, "limit": 10}
            res = requests.get(url, params=params, timeout=10)
            if res.ok:
                data = res.json()
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        telegram_last_update_id = update["update_id"]
                        message = update.get("message", {})
                        if not message:
                            continue
                        chat_id_msg = message.get("chat", {}).get("id")
                        if str(chat_id_msg) != str(CHAT_ID):
                            continue
                        text = message.get("text", "").strip()
                        if not text:
                            continue
                        # ===== REPORT COMMAND =====
                        if text.lower() == "/report":
                            trades = read_trade_log(max_entries=10000)
                            report, error = generate_daily_sheet(trades)
                            if error:
                                _send_telegram_raw(f"📊 *{error}*")
                            else:
                                _send_telegram_raw(format_daily_sheet(report))
                            continue

                        # ===== QUANTUM COMMANDS =====
                        if text.startswith("/quantum"):
                            response = handle_quantum_command(text)
                            if response:
                                _send_telegram_raw(response)
                                continue
                        success, response_msg = handle_optimization_reply(text)
                        if success and response_msg:
                            _send_telegram_raw(response_msg)
                            logger.info(f"📱 تم معالجة الرد: {text}")
        except Exception as e:
            logger.error(f"خطأ في الردود: {e}")
        time.sleep(30)

# ========== SIGNAL NAMES ==========
SIGNAL_NAMES = {
    2: ("قوية جداً 🔵", "VERY STRONG"),
    3: ("قوية ماكس 🟣", "STRONG MAX"),
    4: ("قوية سوبر ماكس 🟠", "STRONG SUPER MAX"),
    5: ("ماكس 🔥", "MAX"),
    6: ("سوبر ماكس 👑", "SUPER MAX")
}
SIGNAL_EMOJIS = {2: "🔵", 3: "🟣", 4: "🟠", 5: "🔥", 6: "👑"}

KING_SIGNAL_NAMES = {
    1: ("King Bronze 🥉", "KING BRONZE"),
    2: ("King Silver 🥈", "KING SILVER"),
    3: ("King Gold 👑", "KING GOLD"),
    4: ("King Elite 👑🔥", "KING ELITE")
}
KING_EMOJIS = {1: "🥉", 2: "🥈", 3: "👑", 4: "👑🔥"}

SMC_SIGNAL_NAMES = {
    1: ("SMC Bronze 🥉", "SMC BRONZE"),
    2: ("SMC Silver 🥈", "SMC SILVER"),
    3: ("SMC Gold 🥇", "SMC GOLD"),
    4: ("SMC Elite 🏆", "SMC ELITE")
}
SMC_EMOJIS = {1: "🥉", 2: "🥈", 3: "🥇", 4: "🏆"}

PRO_SIGNAL_NAMES = {
    1: ("Pro Bronze 🥉", "PRO BRONZE"),
    2: ("Pro Silver 🥈", "PRO SILVER"),
    3: ("Pro Gold 🥇", "PRO GOLD"),
    4: ("Pro Elite 🔥", "PRO ELITE")
}
PRO_EMOJIS = {1: "🥉", 2: "🥈", 3: "🥇", 4: "🔥"}

# ========== KALMAN FILTER ==========
class KalmanFilter:
    def __init__(self, q=0.001, r=0.05):
        self.x = None
        self.p = 1.0
        self.q = q
        self.r = r
        self.history = deque(maxlen=100)

    def update(self, price):
        if self.x is None:
            self.x = price
            self.p = 1.0
            self.history.append(price)
            return price
        x_pred = self.x
        p_pred = self.p + self.q
        k = p_pred / (p_pred + self.r)
        self.x = x_pred + k * (price - x_pred)
        self.p = (1 - k) * p_pred
        self.history.append(self.x)
        return self.x
    
    def get_smooth_price(self):
        return self.x if self.x is not None else 0
    
    def get_volatility(self):
        if len(self.history) < 10:
            return 0
        arr = np.array(list(self.history)[-20:])
        return np.std(arr) / np.mean(arr) if np.mean(arr) > 0 else 0

def get_kalman(pair):
    if pair not in kalman_instances:
        kalman_instances[pair] = KalmanFilter(q=0.001, r=0.05)
    return kalman_instances[pair]

def get_regime_badge(strategy_name, regime, htf_data=None, indicator_counts=None):
    """
    إرجاع شارة حالة السوق مع عرض المؤشرات المحسّنة (Supertrend + MACD + EMA + ALMA)
    """
    enhancers = ""
    if indicator_counts and isinstance(indicator_counts, dict):
        htf = indicator_counts.get("1H", {})
        ltf = indicator_counts.get("5m", {})
        enhancers = f"\n📊 [1H]: {htf.get('CALL',0)}↑ / {htf.get('PUT',0)}↓ / {htf.get('NEUTRAL',0)}! / [5m]: {ltf.get('CALL',0)}↑ / {ltf.get('PUT',0)}↓ / {ltf.get('NEUTRAL',0)}!"

    badges = {
        'original': {
            'trending':  "🌊 السوق *ترندي قوي* — الاستراتيجية الأصلية *ممتازة* هنا" + enhancers,
            'ranging':   "↔️ السوق *متراوح* — الاستراتيجية الأصلية *متوسطة* هنا" + enhancers,
            'high_vol':  "⚡ تقلب عالي — الاستراتيجية الأصلية *جيدة*" + enhancers,
            'low_vol':   "😴 تقلب منخفض — الاستراتيجية الأصلية *ضعيفة* هنا" + enhancers,
            'mixed':     "🌫️ سوق مختلط — الاستراتيجية الأصلية *عادية*" + enhancers,
            'unknown':   "❓ نوع السوق غير واضح" + enhancers
        },
        'king': {
            'trending':  "🌊 السوق *ترندي قوي* — King Strategy *ممتازة* 👑" + enhancers,
            'ranging':   "↔️ السوق *متراوح* — King Strategy *متوسطة*" + enhancers,
            'high_vol':  "⚡ تقلب عالي — King Strategy *جيدة*" + enhancers,
            'low_vol':   "😴 تقلب منخفض — King Strategy *ضعيفة*" + enhancers,
            'mixed':     "🌫️ سوق مختلط — King Strategy *عادية*" + enhancers,
            'unknown':   "❓ نوع السوق غير واضح" + enhancers
        },
        'smart': {
            'trending':  "🌊 السوق *ترندي قوي* — SMC Strategy *جيدة*" + enhancers,
            'ranging':   "↔️ السوق *متراوح* — SMC Strategy *ضعيفة* هنا" + enhancers,
            'high_vol':  "⚡ تقلب عالي — SMC Strategy *ممتازة* 🏆" + enhancers,
            'low_vol':   "😴 تقلب منخفض — SMC Strategy *ضعيفة*" + enhancers,
            'mixed':     "🌫️ سوق مختلط — SMC Strategy *عادية*" + enhancers,
            'unknown':   "❓ نوع السوق غير واضح" + enhancers
        },
        'pro': {
            'trending':  "🌊 السوق *ترندي قوي* — Pro Strategy *متوسطة*" + enhancers,
            'ranging':   "↔️ السوق *متراوح* — Pro Strategy *ممتازة* 🔥" + enhancers,
            'high_vol':  "⚡ تقلب عالي — Pro Strategy *متوسطة*" + enhancers,
            'low_vol':   "😴 تقلب منخفض — Pro Strategy *ضعيفة*" + enhancers,
            'mixed':     "🌫️ سوق مختلط — Pro Strategy *جيدة*" + enhancers,
            'unknown':   "❓ نوع السوق غير واضح" + enhancers
        },
        'quantum': {
            'trending':  "🌊 السوق *ترندي قوي* — Quantum Strategy *ممتازة* 🧠 (جميع الشروط متوافقة)" + enhancers,
            'ranging':   "↔️ السوق *متراوح* — Quantum Strategy *❌ مرفوضة* (الكود يلغي الصفقة تلقائياً)" + enhancers,
            'high_vol':  "⚡ تقلب عالي — Quantum Strategy *جيدة* (مع فلتر التقلب + حذر)" + enhancers,
            'low_vol':   "😴 تقلب منخفض — Quantum Strategy *❌ مرفوضة* (فلتر التقلب يمنع الدخول)" + enhancers,
            'mixed':     "🌫️ سوق مختلط — Quantum Strategy *جيدة* (متوسطة الثقة - تحقق إضافي مطلوب)" + enhancers,
            'unknown':   "❓ نوع السوق غير واضح — Quantum Strategy *⏸️ متوقفة* (انتظر توضيح الحالة)" + enhancers
        }
    }
    return badges.get(strategy_name, badges['original']).get(regime, "🌫️ سوق مختلط")


CURRENCY_PAIRS = {
    'USD': ['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','USDCHF'],
    'EUR': ['EURUSD','EURJPY','EURGBP','EURAUD','EURCAD'],
    'GBP': ['GBPUSD','EURGBP','GBPJPY'],
    'JPY': ['USDJPY','EURJPY','AUDJPY','CADJPY','GBPJPY'],
    'AUD': ['AUDUSD','AUDCAD','AUDJPY','EURAUD'],
    'CAD': ['USDCAD','AUDCAD','CADJPY','EURCAD'],
    'CHF': ['USDCHF']
}

# ========== FUNCIONES DE ALERTAS EN ÁRABE ==========

def get_signal_level(score):
    """تقسيم موحد للمستويات"""
    if score >= 95: return 4
    elif score >= 90: return 3
    elif score >= 85: return 2
    elif score >= 80: return 1
    return 0

def check_regime_for_strategy(strategy_name, regime):
    """كل استراتيجية تشتغل في سوقها بس"""
    allowed = STRATEGY_REGIMES.get(strategy_name, ['trending'])
    return regime in allowed

def get_time_quality(strategy_name):
    now = get_cairo_time()
    hour = now.hour
    minute = now.minute
    current_minutes = hour * 60 + minute
    
    time_ranges = {
        'original': {
            'best': [(15*60, 18*60)],
            'good': [(10*60, 14*60)]
        },
        'king': {
            'best': [(10*60, 12*60)],
            'good': [(15*60, 17*60)]
        },
        'smart': {
            'best': [(11*60, 15*60)],
            'good': [(16*60, 19*60)]
        },
        'pro': {
            'best': [(15*60, 18*60)],
            'good': [(10*60, 14*60)]
        },
        'quantum': {
            'best': [(10*60, 14*60), (15*60, 18*60)],
            'good': [(9*60, 10*60), (18*60, 20*60)]
        }
    }
    
    ranges = time_ranges.get(strategy_name, {})
    
    for start, end in ranges.get('best', []):
        if start <= current_minutes <= end:
            return "⭐ الأفضل"
    
    for start, end in ranges.get('good', []):
        if start <= current_minutes <= end:
            return "🥈 جيد جداً"
    
    return "⏳ وقت عادي"

# ====== تم التعديل هنا: إضافة htf_data=None ======
def send_early_alert(pair, direction, signal_name, score, strategy_name, regime="unknown", htf_data=None, indicator_counts=None):
    da = "صعود (CALL)" if direction == "CALL" else "هبوط (PUT)"
    time_quality = get_time_quality(strategy_name)
    regime_badge = get_regime_badge(strategy_name, regime, htf_data, indicator_counts)
    msg = (
        f"⚠️ *تنبيه مبكر — {signal_name}*\n"
        f"الزوج: `{pair}` [5 دقائق]\n"
        f"الاتجاه: *{da}*\n"
        f"📊 النقاط: *{score}/100*\n"
        f"⏱️ *صفقة قادمة خلال 20 ثانية...*\n"
        f"🔄 *جاري التحقق من الشروط النهائية...*\n"
        f"━━━━━━━━━━━━\n"
        f"🕐 *الوقت:* {time_quality}\n"
        f"📍 {regime_badge}"
    )
    send_telegram_message(msg)

def send_cancelled_alert(pair, direction, reason, strategy_name):
    da = "صعود (CALL)" if direction == "CALL" else "هبوط (PUT)"
    msg = (
        f"❌ *تم إلغاء الصفقة*\n"
        f"الزوج: `{pair}` [5 دقائق]\n"
        f"الاتجاه: *{da}*\n"
        f"🚫 *السبب:* {reason}\n"
        f"💡 *الشروط تغيرت قبل الإغلاق*"
    )
    send_telegram_message(msg)

def send_final_signal(pair, direction, signal_name, score, duration_text, indicators, strategy_name, regime="unknown", signal_level=None, htf_data=None, indicator_counts=None):
    da = "صعود (CALL)" if direction == "CALL" else "هبوط (PUT)"
    time_quality = get_time_quality(strategy_name)
    regime_badge = get_regime_badge(strategy_name, regime, htf_data, indicator_counts)
    
    msg_hash = f"{pair}_{direction}_{strategy_name}_{int(get_iq_time()) // 300}"
    with data_lock:
        if msg_hash in state.sent_final_signals:
            return None
        state.sent_final_signals[msg_hash] = time.time()
    
    if strategy_name == 'quantum':
        emoji = QUANTUM_EMOJIS.get(signal_level, "🧠")
    elif strategy_name == 'original':
        emoji = SIGNAL_EMOJIS.get(signal_level, "🔥")
    elif strategy_name == 'king':
        emoji = KING_EMOJIS.get(signal_level, "👑")
    elif strategy_name == 'smart':
        emoji = SMC_EMOJIS.get(signal_level, "🏆")
    else:
        emoji = PRO_EMOJIS.get(signal_level, "🔥")
    
    msg = (
        f"{emoji} *{signal_name}* {emoji}\n"
        f"الزوج: `{pair}` (IQ Option) [5 دقائق]\n"
        f"الاتجاه: *{da}*\n"
        f"⏱️ *المدة:* {duration_text}\n"
        f"📊 *النقاط:* *{score}/100*\n"
        f"🕐 *الوقت:* {time_quality}\n"
        f"📍 *حالة السوق:* {regime_badge}\n"
        f"⚡ *ادخل الآن في الشمعة القادمة!*"
    )
    send_telegram_message(msg)
    return msg

# ========== STATISTICAL ENGINE ==========

def evaluate_filters(trades, market_type=None):
    if market_type:
        trades = [t for t in trades if ("-OTC" in t.get("pair", "").upper()) == (market_type == "otc")]
    if not trades:
        return {}
    filter_stats = {}
    sample_filters = trades[0].get("filters", {})
    for fname in sample_filters.keys():
        filter_stats[fname] = {"win": 0, "loss": 0, "total": 0}
    for trade in trades:
        outcome = trade.get("outcome", "")
        filters = trade.get("filters", {})
        for fname, fval in filters.items():
            if fname not in filter_stats:
                continue
            if fval:
                filter_stats[fname]["total"] += 1
                if outcome == "win":
                    filter_stats[fname]["win"] += 1
                else:
                    filter_stats[fname]["loss"] += 1
    results = {}
    for fname, stat in filter_stats.items():
        total = stat["total"]
        if total >= 10:
            wr = (stat["win"] / total) * 100
            worth = "High" if wr >= 80 else ("Med" if wr >= 65 else "Low")
            results[fname] = {
                "win": stat["win"], "loss": stat["loss"], "total": total,
                "wr": round(wr, 1), "worth": worth
            }
    return dict(sorted(results.items(), key=lambda x: x[1]["wr"], reverse=True))

def rank_pairs(trades, market_type=None):
    if market_type:
        trades = [t for t in trades if ("-OTC" in t.get("pair", "").upper()) == (market_type == "otc")]
    if not trades:
        return []
    pair_data = {}
    for t in trades:
        pair = t.get("pair", "UNKNOWN")
        if pair not in pair_data:
            pair_data[pair] = {"win": 0, "loss": 0, "total": 0, "wins": [], "losses": []}
        pair_data[pair]["total"] += 1
        if t.get("outcome") == "win":
            pair_data[pair]["win"] += 1
            pair_data[pair]["wins"].append(1)
            pair_data[pair]["losses"].append(0)
        else:
            pair_data[pair]["loss"] += 1
            pair_data[pair]["wins"].append(0)
            pair_data[pair]["losses"].append(1)
    rankings = []
    max_total = max(d["total"] for d in pair_data.values()) if pair_data else 1
    for pair, data in pair_data.items():
        total = data["total"]
        if total < 5:
            continue
        wr = (data["win"] / total) * 100
        avg_win = PAYOUT_RATIO
        avg_loss = 1
        profit_factor = (data["win"] * PAYOUT_RATIO) / data["loss"] if data["loss"] > 0 else float('inf')
        chunks = [data["wins"][i:i+10] for i in range(0, len(data["wins"]), 10)]
        chunk_wrs = [sum(chunk)/len(chunk)*100 for chunk in chunks if chunk]
        stability = 100 - np.std(chunk_wrs) if chunk_wrs and len(chunk_wrs) > 1 else 50
        count_ratio = (total / max_total) * 100
        score = (wr * 0.4) + (min(profit_factor, 5) * 20 * 0.3) + (stability * 0.2) + (count_ratio * 0.1)
        rankings.append({
            "pair": pair, "wr": round(wr, 1), "total": total,
            "profit_factor": round(profit_factor, 2), "stability": round(stability, 1),
            "score": round(score, 1)
        })
    rankings.sort(key=lambda x: x["score"], reverse=True)
    return rankings

def analyze_hours(trades, market_type=None):
    if market_type:
        trades = [t for t in trades if ("-OTC" in t.get("pair", "").upper()) == (market_type == "otc")]
    if not trades:
        return {}
    hour_stats = {}
    for t in trades:
        hour = t.get("hour", 0)
        if hour not in hour_stats:
            hour_stats[hour] = {"win": 0, "loss": 0, "total": 0}
        hour_stats[hour]["total"] += 1
        if t.get("outcome") == "win":
            hour_stats[hour]["win"] += 1
        else:
            hour_stats[hour]["loss"] += 1
    results = {}
    for h, stat in hour_stats.items():
        if stat["total"] >= 5:
            results[h] = {
                "win": stat["win"], "loss": stat["loss"], "total": stat["total"],
                "wr": round((stat["win"] / stat["total"]) * 100, 1)
            }
    return dict(sorted(results.items(), key=lambda x: x[1]["wr"], reverse=True))

def analyze_confidence_calibration(trades):
    if not trades:
        return {}
    score_buckets = {
        "80-84": {"trades": [], "expected_wr": 82},
        "85-89": {"trades": [], "expected_wr": 87},
        "90-94": {"trades": [], "expected_wr": 92},
        "95-100": {"trades": [], "expected_wr": 97},
    }
    for t in trades:
        score = t.get("score", 0)
        if 80 <= score <= 84:
            score_buckets["80-84"]["trades"].append(t)
        elif 85 <= score <= 89:
            score_buckets["85-89"]["trades"].append(t)
        elif 90 <= score <= 94:
            score_buckets["90-94"]["trades"].append(t)
        elif 95 <= score <= 100:
            score_buckets["95-100"]["trades"].append(t)
    calibration = {}
    for bucket, data in score_buckets.items():
        trades_in_bucket = data["trades"]
        if len(trades_in_bucket) >= 10:
            wins = sum(1 for t in trades_in_bucket if t.get("outcome") == "win")
            actual_wr = (wins / len(trades_in_bucket)) * 100
            expected_wr = data["expected_wr"]
            diff = actual_wr - expected_wr
            calibration[bucket] = {
                "total": len(trades_in_bucket), "actual_wr": round(actual_wr, 1),
                "expected_wr": expected_wr, "diff": round(diff, 1),
                "status": "✅ متوازن" if abs(diff) <= 5 else ("⚠️ مبالغ" if diff < -5 else "🔥 أقوى من المتوقع")
            }
    return calibration

def grid_search_optimization(trades, strategy="king", market_type=None):
    if market_type:
        trades = [t for t in trades if ("-OTC" in t.get("pair", "").upper()) == (market_type == "otc")]
    if len(trades) < 200:
        return None, "غير كافي — محتاج 200+ صفقة"
    king_trades = [t for t in trades if t.get("strategy") == strategy]
    if len(king_trades) < 100:
        return None, f"غير كافي — محتاج 100+ صفقة {strategy}"
    proposals = []
    best_adx = 22
    best_adx_wr = 0
    best_adx_count = 0
    for adx_thresh in [18, 20, 22, 24, 26, 28, 30]:
        subset = [t for t in king_trades if t.get("indicators", {}).get("adx", 0) >= adx_thresh]
        if len(subset) >= 20:
            wins = sum(1 for t in subset if t.get("outcome") == "win")
            wr = (wins / len(subset)) * 100
            penalty = max(0, (30 - len(subset)) / 100)
            adjusted_wr = wr - penalty
            if adjusted_wr > best_adx_wr:
                best_adx_wr = adjusted_wr
                best_adx = adx_thresh
                best_adx_count = len(subset)
    if best_adx != 22 and best_adx_count >= 30:
        proposals.append({
            "filter": "ADX", "current": 22, "proposed": best_adx,
            "reason": f"WR يتحسن لـ {best_adx_wr:.1f}% مع ADX ≥ {best_adx} (عينة: {best_adx_count})",
            "impact": "يقلل الإشارات قليلاً ويرفع الجودة"
        })
    best_rsi_low, best_rsi_high = 45, 60
    best_rsi_wr = 0
    best_rsi_count = 0
    for low in range(40, 50, 2):
        for high in range(55, 65, 2):
            subset = [t for t in king_trades
                      if t.get("direction") == "CALL"
                      and low <= t.get("indicators", {}).get("rsi", 0) <= high]
            if len(subset) >= 15:
                wins = sum(1 for t in subset if t.get("outcome") == "win")
                wr = (wins / len(subset)) * 100
                penalty = max(0, (25 - len(subset)) / 100)
                adjusted_wr = wr - penalty
                if adjusted_wr > best_rsi_wr:
                    best_rsi_wr = adjusted_wr
                    best_rsi_low, best_rsi_high = low, high
                    best_rsi_count = len(subset)
    if (best_rsi_low, best_rsi_high) != (45, 60) and best_rsi_count >= 20:
        proposals.append({
            "filter": "RSI CALL", "current": "45–60",
            "proposed": f"{best_rsi_low}–{best_rsi_high}",
            "reason": f"WR يتحسن لـ {best_rsi_wr:.1f}% (عينة: {best_rsi_count})",
            "impact": "تعديل دقيق لنطاق RSI"
        })
    return proposals, "تم التحليل بنجاح"

def optimize_weights(trades):
    if len(trades) < 300:
        return None, "غير كافي — محتاج 300+ صفقة"
    king_trades = [t for t in trades if t.get("strategy") == "king"]
    if len(king_trades) < 150:
        return None, "غير كافي — محتاج 150+ صفقة King"
    filter_performance = {}
    with data_lock:
        current_weights = dict(KING_WEIGHTS)
    for fname in current_weights.keys():
        with_filter = [t for t in king_trades if t.get("filters", {}).get(fname, False)]
        without_filter = [t for t in king_trades if not t.get("filters", {}).get(fname, False)]
        if len(with_filter) >= 20 and len(without_filter) >= 20:
            wr_with = sum(1 for t in with_filter if t.get("outcome") == "win") / len(with_filter) * 100
            wr_without = sum(1 for t in without_filter if t.get("outcome") == "win") / len(without_filter) * 100
            filter_performance[fname] = {
                "wr_with": wr_with, "wr_without": wr_without,
                "diff": wr_with - wr_without, "count": len(with_filter)
            }
    if not filter_performance:
        return None, "لا توجد بيانات كافية"
    new_weights = current_weights.copy()
    adjustments = []
    for fname, perf in filter_performance.items():
        diff = perf["diff"]
        current = current_weights[fname]
        if diff > 10:
            new_w = min(current + 3, 25)
            adjustments.append(f"{fname}: {current} → {new_w} (+{diff:.1f}% WR)")
        elif diff < -5:
            new_w = max(current - 2, 3)
            adjustments.append(f"{fname}: {current} → {new_w} ({diff:.1f}% WR)")
        else:
            new_w = current
        new_weights[fname] = new_w
    current_sum = sum(new_weights.values())
    if current_sum != 100:
        factor = 100 / current_sum
        new_weights = {k: max(1, round(v * factor)) for k, v in new_weights.items()}
        diff = 100 - sum(new_weights.values())
        if diff != 0:
            max_key = max(new_weights, key=new_weights.get)
            new_weights[max_key] += diff
    return {
        "old_weights": current_weights, "new_weights": new_weights,
        "adjustments": adjustments, "filter_performance": filter_performance
    }, "تم تحديث الأوزان"

def calculate_feature_importance(trades, strategy="king"):
    if len(trades) < 100:
        return None, "غير كافي"
    st_trades = [t for t in trades if t.get("strategy") == strategy]
    if len(st_trades) < 50:
        return None, "غير كافي للاستراتيجية"
    baseline_wins = sum(1 for t in st_trades if t.get("outcome") == "win")
    baseline_wr = (baseline_wins / len(st_trades)) * 100
    filter_names = list(st_trades[0]["filters"].keys()) if st_trades and st_trades[0].get("filters") else []
    importance = {}
    for fname in filter_names:
        without_filter = [t for t in st_trades if not t.get("filters", {}).get(fname, False)]
        with_filter = [t for t in st_trades if t.get("filters", {}).get(fname, False)]
        if len(with_filter) >= 20 and len(without_filter) >= 20:
            wr_with = sum(1 for t in with_filter if t.get("outcome") == "win") / len(with_filter) * 100
            wr_without = sum(1 for t in without_filter if t.get("outcome") == "win") / len(without_filter) * 100
            imp = wr_with - wr_without
            importance[fname] = {
                "importance": round(imp, 2), "wr_with": round(wr_with, 1),
                "wr_without": round(wr_without, 1), "count": len(with_filter)
            }
    if importance:
        total_imp = sum(abs(v["importance"]) for v in importance.values())
        if total_imp > 0:
            weights = {}
            for fname, data in importance.items():
                weights[fname] = max(1, round((abs(data["importance"]) / total_imp) * 100))
            current_sum = sum(weights.values())
            if current_sum != 100:
                factor = 100 / current_sum
                weights = {k: max(1, round(v * factor)) for k, v in weights.items()}
                diff = 100 - sum(weights.values())
                if diff != 0:
                    max_key = max(weights, key=weights.get)
                    weights[max_key] += diff
            return {"weights": weights, "importance": importance, "baseline_wr": round(baseline_wr, 1)}, "تم"
    return None, "لا توجد بيانات كافية"

def optimize_weights_feature_importance(trades):
    result, status = calculate_feature_importance(trades, strategy="king")
    if not result:
        return None, status
    with data_lock:
        old_weights = dict(KING_WEIGHTS)
    new_weights = result["weights"]
    adjustments = []
    for fname in old_weights.keys():
        old_w = old_weights.get(fname, 0)
        new_w = new_weights.get(fname, old_w)
        if old_w != new_w:
            arrow = "↗️" if new_w > old_w else ("↘️" if new_w < old_w else "➡️")
            adjustments.append(f"{arrow} {fname}: {old_w} → {new_w}")
    return {
        "old_weights": old_weights, "new_weights": new_weights,
        "importance": result["importance"], "baseline_wr": result["baseline_wr"],
        "adjustments": adjustments
    }, "تم تحديث الأوزان بناءً على Feature Importance"

# ========== REPORTS ==========
def generate_report(trades, period="daily", market_type=None):
    if market_type:
        trades = [t for t in trades if ("-OTC" in t.get("pair", "").upper()) == (market_type == "otc")]
    if not trades:
        return None
    total = len(trades)
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = total - wins
    wr = (wins / total * 100) if total > 0 else 0
    pf = (wins * PAYOUT_RATIO) / losses if losses > 0 else float('inf')
    max_win_streak = max_loss_streak = current_win = current_loss = 0
    for t in trades:
        if t.get("outcome") == "win":
            current_win += 1
            current_loss = 0
            max_win_streak = max(max_win_streak, current_win)
        else:
            current_loss += 1
            current_win = 0
            max_loss_streak = max(max_loss_streak, current_loss)
    avg_win = PAYOUT_RATIO
    avg_loss = 1
    expectancy = (avg_win * (wr/100)) - (avg_loss * (1 - wr/100))
    pair_rank = rank_pairs(trades)
    best_pair = pair_rank[0] if pair_rank else None
    worst_pair = pair_rank[-1] if pair_rank else None
    hour_stats = analyze_hours(trades)
    best_hour = next(iter(hour_stats.items())) if hour_stats else None
    worst_hour = list(hour_stats.items())[-1] if hour_stats else None
    orig_trades = [t for t in trades if t.get("strategy") == "original"]
    king_trades = [t for t in trades if t.get("strategy") == "king"]
    smart_trades = [t for t in trades if t.get("strategy") == "smart"]
    pro_trades = [t for t in trades if t.get("strategy") == "pro"]
    quantum_trades = [t for t in trades if t.get("strategy") == "quantum"]
    orig_wr = (sum(1 for t in orig_trades if t.get("outcome") == "win") / len(orig_trades) * 100) if orig_trades else 0
    king_wr = (sum(1 for t in king_trades if t.get("outcome") == "win") / len(king_trades) * 100) if king_trades else 0
    smart_wr = (sum(1 for t in smart_trades if t.get("outcome") == "win") / len(smart_trades) * 100) if smart_trades else 0
    pro_wr = (sum(1 for t in pro_trades if t.get("outcome") == "win") / len(pro_trades) * 100) if pro_trades else 0
    quantum_wr = (sum(1 for t in quantum_trades if t.get("outcome") == "win") / len(quantum_trades) * 100) if quantum_trades else 0
    return {
        "period": period, "market_type": market_type or "all",
        "total_trades": total, "wins": wins, "losses": losses,
        "win_rate": round(wr, 1), "profit_factor": round(pf, 2),
        "max_win_streak": max_win_streak, "max_loss_streak": max_loss_streak,
        "expectancy": round(expectancy, 3), "best_pair": best_pair,
        "worst_pair": worst_pair, "best_hour": best_hour, "worst_hour": worst_hour,
        "original": {"total": len(orig_trades), "wr": round(orig_wr, 1)},
        "king": {"total": len(king_trades), "wr": round(king_wr, 1)},
        "smart": {"total": len(smart_trades), "wr": round(smart_wr, 1)},
        "pro": {"total": len(pro_trades), "wr": round(pro_wr, 1)},
        "quantum": {"total": len(quantum_trades), "wr": round(quantum_wr, 1)},
        "filter_eval": evaluate_filters(trades),
        "calibration": analyze_confidence_calibration(trades),
        "pair_rankings": pair_rank[:5] if len(pair_rank) >= 5 else pair_rank,
    }

def format_report_message(report):
    if not report:
        return "📊 *لا توجد بيانات للتقرير*"
    period_name = {"daily": "اليومي", "weekly": "الأسبوعي", "monthly": "الشهري"}.get(report["period"], report["period"])
    market_label = report.get("market_type", "")
    market_prefix = f" [{market_label.upper()}]" if market_label else ""
    msg = (
        f"📊 *تقرير {period_name}{market_prefix}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *إجمالي الصفقات:* {report['total_trades']}\n"
        f"✅ *رابحة:* {report['wins']} | ❌ *خاسرة:* {report['losses']}\n"
        f"🎯 *نسبة الربح:* {report['win_rate']}%\n"
        f"💰 *Profit Factor:* {report['profit_factor']}\n"
        f"📊 *Expectancy:* {report['expectancy']}\n"
        f"🔥 *أطول سلسلة رابحة:* {report['max_win_streak']}\n"
        f"💔 *أطول سلسلة خاسرة:* {report['max_loss_streak']}\n\n"
    )
    if report.get("best_pair"):
        bp = report["best_pair"]
        msg += f"🏆 *أفضل زوج:* `{bp['pair']}` — WR: {bp['wr']}%\n"
    if report.get("worst_pair"):
        wp = report["worst_pair"]
        msg += f"⚠️ *أسوأ زوج:* `{wp['pair']}` — WR: {wp['wr']}%\n"
    msg += f"\n📋 *الاستراتيجيات:*\n"
    msg += f"  الأصلية: {report['original']['total']} صفقة — WR: {report['original']['wr']}%\n"
    msg += f"  👑 King: {report['king']['total']} صفقة — WR: {report['king']['wr']}%\n"
    msg += f"  🏆 SMC: {report['smart']['total']} صفقة — WR: {report['smart']['wr']}%\n"
    msg += f"  🔥 Pro: {report['pro']['total']} صفقة — WR: {report['pro']['wr']}%\n"
    msg += f"  🧠 Quantum: {report['quantum']['total']} صفقة — WR: {report['quantum']['wr']}%\n"
    if report.get("filter_eval"):
        msg += f"\n🔬 *ترتيب الفلاتر:*\n"
        for i, (fname, fdata) in enumerate(list(report["filter_eval"].items())[:5], 1):
            emoji = "🟢" if fdata["worth"] == "High" else ("🟡" if fdata["worth"] == "Med" else "🔴")
            msg += f"  {i}. {emoji} `{fname}` — WR: {fdata['wr']}% ({fdata['worth']})\n"
    if report.get("calibration"):
        msg += f"\n⚖️ *معايرة الثقة:*\n"
        for bucket, cdata in report["calibration"].items():
            msg += f"  {bucket}: {cdata['status']} (فعلي: {cdata['actual_wr']}% vs متوقع: {cdata['expected_wr']}%)\n"
    return msg


# ========== DAILY SHEET REPORT ==========

def generate_daily_sheet(trades):
    """توليد شيت النتائج اليومية مجمعة حسب الاستراتيجية"""
    if not trades:
        return None, "لا توجد صفقات مسجلة"

    # تصفية صفقات اليوم فقط (آخر 24 ساعة)
    now = get_iq_time()
    today_trades = [t for t in trades if now - t.get("timestamp", 0) <= 86400]

    if not today_trades:
        return None, "لا توجد صفقات اليوم"

    strategies = ["original", "king", "smart", "pro", "quantum"]
    strategy_names = {
        "original": "الأصلية",
        "king": "👑 King",
        "smart": "🏆 SMC",
        "pro": "🔥 Pro", 
        "quantum": "🧠 Quantum"
    }

    stats = {}
    for strategy in strategies:
        st_trades = [t for t in today_trades if t.get("strategy") == strategy]

        wins = sum(1 for t in st_trades if t.get("outcome") == "win")
        losses = sum(1 for t in st_trades if t.get("outcome") == "loss")
        ties = sum(1 for t in st_trades if t.get("outcome") == "tie")
        total = len(st_trades)

        stats[strategy] = {
            "name": strategy_names.get(strategy, strategy),
            "total": total,
            "wins": wins,
            "losses": losses,
            "ties": ties,
            "wr": round((wins / total * 100), 1) if total > 0 else 0
        }

    if not stats:
        return None, "لا توجد صفقات باستراتيجيات معروفة اليوم"

    total_all = sum(s["total"] for s in stats.values())
    total_wins = sum(s["wins"] for s in stats.values())
    total_losses = sum(s["losses"] for s in stats.values())
    total_ties = sum(s["ties"] for s in stats.values())
    total_wr = round((total_wins / total_all * 100), 1) if total_all > 0 else 0

    return {
        "strategies": stats,
        "total": total_all,
        "wins": total_wins,
        "losses": total_losses,
        "ties": total_ties,
        "wr": total_wr,
        "date": datetime.now(CAIRO_TZ).strftime('%d/%m/%Y')
    }, None


def format_daily_sheet(report):
    """تنسيق شيت النتائج اليومية كرسالة تليجرام"""
    if not report:
        return "📊 *لا توجد بيانات للتقرير اليومي*"

    msg = "📋 *شيت نتائج اليوم — " + report['date'] + "*\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"

    for strategy, data in report["strategies"].items():
        msg += "*" + data['name'] + "*\n"
        msg += "  📊 إجمالي: `" + str(data['total']) + "`  "
        msg += "✅ رابحة: `" + str(data['wins']) + "`  "
        msg += "❌ خاسرة: `" + str(data['losses']) + "`  "
        msg += "➖ تعادل: `" + str(data['ties']) + "`\n"
        msg += "  🎯 نسبة الربح: `" + str(data['wr']) + "%`\n\n"

    msg += "━━━━━━━━━━━━━━━━━━━━\n"
    msg += "📈 *الإجمالي الكلي لليوم*\n\n"
    msg += "  📊 إجمالي الصفقات: `" + str(report['total']) + "`\n"
    msg += "  ✅ إجمالي رابحة: `" + str(report['wins']) + "`\n"
    msg += "  ❌ إجمالي خاسرة: `" + str(report['losses']) + "`\n"
    msg += "  ➖ إجمالي تعادل: `" + str(report['ties']) + "`\n"
    msg += "  🎯 نسبة الربح الإجمالية: `" + str(report['wr']) + "%`\n"
    msg += "━━━━━━━━━━━━━━━━━━━━"

    return msg

# ========== OPTIMIZATION PROPOSAL ==========
OPTIMIZATION_PROPOSAL_FILE = "optimization_proposal.json"

def generate_and_send_optimization_proposal():
    trades = read_trade_log(max_entries=5000)
    proposals, status = grid_search_optimization(trades)
    weight_result, weight_status = optimize_weights_feature_importance(trades)
    if not proposals and not weight_result:
        logger.info(f"📊 التحسين: {status}")
        return
    msg = (
        f"🔧 *اقتراح تحسين تلقائي*\n"
        f"📅 التاريخ: {datetime.now(CAIRO_TZ).strftime('%d/%m/%Y %I:%M %p')}\n"
        f"📊 الصفقات المحللة: {len(trades)}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
    )
    if proposals:
        msg += f"📋 *تعديلات العتبات:*\n"
        for p in proposals:
            msg += (
                f"\n🔹 *{p['filter']}*\n"
                f"   الحالي: `{p['current']}`\n"
                f"   المقترح: `{p['proposed']}`\n"
                f"   السبب: {p['reason']}\n"
                f"   التأثير: {p['impact']}\n"
            )
    if weight_result:
        msg += f"\n⚖️ *تعديلات أوزان King Strategy (Feature Importance):*\n"
        msg += f"📊 Baseline WR: {weight_result.get('baseline_wr', 'N/A')}%\n"
        for adj in weight_result["adjustments"]:
            msg += f"   • {adj}\n"
        msg += f"\n📊 الأوزان الجديدة:\n"
        for k, v in weight_result["new_weights"].items():
            old = weight_result["old_weights"].get(k, v)
            arrow = "↗️" if v > old else ("↘️" if v < old else "➡️")
            msg += f"   {arrow} `{k}`: {old} → {v}\n"
        if weight_result.get("importance"):
            msg += f"\n🔬 *Feature Importance:*\n"
            sorted_imp = sorted(weight_result["importance"].items(), key=lambda x: abs(x[1]["importance"]), reverse=True)
            for fname, imp_data in sorted_imp[:5]:
                emoji = "🟢" if imp_data["importance"] > 5 else ("🟡" if imp_data["importance"] > 0 else "🔴")
                msg += f"   {emoji} `{fname}`: +{imp_data['importance']:.1f}% (مع: {imp_data['wr_with']}% | بدون: {imp_data['wr_without']}%)\n"
    msg += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *للموافقة:* رد `موافق`\n"
        f"❌ *للرفض:* رد `رفض`\n"
        f"⏳ *متاح 24 ساعة*\n\n"
        f"⚠️ *تحذير:* التعديل هيغير عتبات الاستراتيجيات."
    )
    proposal_data = {
        "timestamp": get_iq_time(),
        "proposals": proposals or [],
        "weight_result": weight_result,
        "status": "pending",
        "message": msg
    }
    with data_lock:
        with open(OPTIMIZATION_PROPOSAL_FILE, 'w', encoding='utf-8') as f:
            json.dump(proposal_data, f, ensure_ascii=False, indent=2)
    send_telegram_message(msg)
    logger.info("🔧 تم إرسال اقتراح تحسين")

def handle_optimization_reply(reply_text):
    reply_lower = reply_text.lower().strip()
    try:
        with data_lock:
            with open(OPTIMIZATION_PROPOSAL_FILE, 'r', encoding='utf-8') as f:
                proposal = json.load(f)
    except Exception as e:
        logger.warning(f"⚠️ فشل قراءة الاقتراح: {e}")
        return False, "لا يوجد اقتراح نشط"
    if proposal.get("status") != "pending":
        return False, "الاقتراح تم معالجته بالفعل"
    if get_iq_time() - proposal.get("timestamp", 0) > 86400:
        proposal["status"] = "expired"
        with data_lock:
            with open(OPTIMIZATION_PROPOSAL_FILE, 'w', encoding='utf-8') as f:
                json.dump(proposal, f, ensure_ascii=False, indent=2)
        return False, "انتهت صلاحية الاقتراح (24 ساعة)"
    if reply_lower in ["موافق", "موافقة", "نعم", "yes", "approve", "ok"]:
        weight_result = proposal.get("weight_result")
        if weight_result and weight_result.get("new_weights"):
            global KING_WEIGHTS
            with data_lock:
                KING_WEIGHTS = weight_result["new_weights"]
                save_king_weights(KING_WEIGHTS)
            logger.info("✅ تم تطبيق أوزان King الجديدة")
        proposal["status"] = "approved"
        with data_lock:
            with open(OPTIMIZATION_PROPOSAL_FILE, 'w', encoding='utf-8') as f:
                json.dump(proposal, f, ensure_ascii=False, indent=2)
        return True, "✅ تم تطبيق التعديلات بنجاح!"
    elif reply_lower in ["رفض", "لا", "no", "reject", "cancel"]:
        proposal["status"] = "rejected"
        with data_lock:
            with open(OPTIMIZATION_PROPOSAL_FILE, 'w', encoding='utf-8') as f:
                json.dump(proposal, f, ensure_ascii=False, indent=2)
        return True, "❌ تم رفض الاقتراح. البوت يستمر بالعتبات الحالية."
    return False, None

# ========== WALK FORWARD ==========
WALK_FORWARD_FILE = "walk_forward_state.json"

def load_walk_forward_state():
    if not os.path.exists(WALK_FORWARD_FILE):
        return {}
    try:
        with data_lock:
            with open(WALK_FORWARD_FILE, 'r', encoding='utf-8') as f:
                content = f.read().strip()
            if not content:
                return {}
            return json.loads(content)
    except Exception:
        return {}

def save_walk_forward_state(state_data):
    with data_lock:
        try:
            with open(WALK_FORWARD_FILE, 'w', encoding='utf-8') as f:
                json.dump(state_data, f, indent=2)
        except Exception as e:
            logger.error(f"خطأ في حفظ Walk Forward: {e}")

def run_walk_forward_validation(trades, strategy="king", market_type="live"):
    market_trades = [t for t in trades if ("-OTC" in t.get("pair", "").upper()) == (market_type == "otc")]
    st_trades = [t for t in market_trades if t.get("strategy") == strategy]
    if len(st_trades) < WALK_FORWARD_MIN_TRADES:
        return False, None, f"غير كافي — محتاج {WALK_FORWARD_MIN_TRADES}+ صفقة {strategy}/{market_type.upper()} (حالياً: {len(st_trades)})"
    wf_state = load_walk_forward_state()
    state_key = f"{market_type}_{strategy}"
    last_validated_ts = wf_state.get(state_key, {}).get("last_test_timestamp", 0)
    split_idx = int(len(st_trades) * WALK_FORWARD_TRAIN_RATIO)
    train_set = st_trades[:split_idx]
    test_set = st_trades[split_idx:]
    fresh_test_set = [t for t in test_set if t.get("timestamp", 0) > last_validated_ts]
    if len(fresh_test_set) < 20:
        return False, None, "لا توجد بيانات اختبار جديدة كافية"
    test_set = fresh_test_set
    baseline_wins = sum(1 for t in test_set if t.get("outcome") == "win")
    baseline_wr = (baseline_wins / len(test_set)) * 100
    best_config = None
    best_train_wr = 0
    adx_options = [20, 22, 24, 26, 28]
    rsi_low_options = [40, 42, 45, 48]
    rsi_high_options = [55, 58, 60, 62]
    for adx_t in adx_options:
        for rsi_l in rsi_low_options:
            for rsi_h in rsi_high_options:
                filtered = []
                for t in train_set:
                    indicators = t.get("indicators", {})
                    direction = t.get("direction", "")
                    adx_ok = indicators.get("adx", 0) >= adx_t
                    rsi = indicators.get("rsi", 50)
                    if direction == "CALL":
                        rsi_ok = rsi_l <= rsi <= rsi_h
                    else:
                        rsi_ok = (100 - rsi_h) <= rsi <= (100 - rsi_l)
                    if adx_ok and rsi_ok:
                        filtered.append(t)
                if len(filtered) >= 20:
                    wins = sum(1 for t in filtered if t.get("outcome") == "win")
                    wr = (wins / len(filtered)) * 100
                    if wr > best_train_wr:
                        best_train_wr = wr
                        best_config = {"adx": adx_t, "rsi_low": rsi_l, "rsi_high": rsi_h}
    if not best_config:
        return False, None, "لم يتم العثور على إعدادات أفضل"
    test_filtered = []
    for t in test_set:
        indicators = t.get("indicators", {})
        direction = t.get("direction", "")
        adx_ok = indicators.get("adx", 0) >= best_config["adx"]
        rsi = indicators.get("rsi", 50)
        if direction == "CALL":
            rsi_ok = best_config["rsi_low"] <= rsi <= best_config["rsi_high"]
        else:
            rsi_ok = (100 - best_config["rsi_high"]) <= rsi <= (100 - best_config["rsi_low"])
        if adx_ok and rsi_ok:
            test_filtered.append(t)
    test_wr = (sum(1 for t in test_filtered if t.get("outcome") == "win") / len(test_filtered) * 100) if len(test_filtered) >= 10 else 0
    improvement = test_wr - baseline_wr
    result = {
        "market_type": market_type, "strategy": strategy,
        "train_size": len(train_set), "test_size": len(test_set),
        "baseline_wr": round(baseline_wr, 1), "new_wr": round(test_wr, 1),
        "improvement": round(improvement, 1), "best_config": best_config,
        "approved": improvement > 2
    }
    newest_ts = max((t.get("timestamp", 0) for t in test_set), default=last_validated_ts)
    wf_state[state_key] = {"last_test_timestamp": newest_ts}
    save_walk_forward_state(wf_state)
    if result["approved"]:
        msg = f"✅ Walk Forward [{market_type.upper()}/{strategy}]: مقبول — تحسين {improvement:.1f}% (Baseline: {baseline_wr:.1f}% → New: {test_wr:.1f}%)"
        settings = load_settings(market_type)
        settings["adx_threshold"] = best_config["adx"]
        settings["rsi_low_call"] = best_config["rsi_low"]
        settings["rsi_high_call"] = best_config["rsi_high"]
        settings["rsi_low_put"] = 100 - best_config["rsi_high"]
        settings["rsi_high_put"] = 100 - best_config["rsi_low"]
        settings["last_updated"] = get_iq_time()
        settings["walk_forward_wr"] = test_wr
        settings["baseline_wr"] = baseline_wr
        settings["approved"] = True
        save_settings(settings, market_type)
    else:
        msg = f"❌ Walk Forward [{market_type.upper()}/{strategy}]: مرفوض — تحسين {improvement:.1f}% فقط (Baseline: {baseline_wr:.1f}% → New: {test_wr:.1f}%)"
    return result["approved"], result, msg

# ========== MONTE CARLO ==========
MONTE_CARLO_FILE = "monte_carlo_results.json"

def run_monte_carlo(trades, strategy="king", market_type=None):
    st_trades = [t for t in trades if t.get("strategy") == strategy]
    if len(st_trades) < MONTE_CARLO_MIN_TRADES:
        return None, f"غير كافي — محتاج {MONTE_CARLO_MIN_TRADES}+ صفقة"
    n = len(st_trades)
    outcomes = [1 if t.get("outcome") == "win" else 0 for t in st_trades]
    baseline_wr = sum(outcomes) / n * 100
    simulations = []
    rng = np.random.default_rng()
    for _ in range(MONTE_CARLO_SIMULATIONS):
        sim_outcomes = []
        while len(sim_outcomes) < n:
            start_idx = rng.integers(0, n - BLOCK_SIZE + 1)
            block = outcomes[start_idx:start_idx + BLOCK_SIZE]
            sim_outcomes.extend(block)
        sim_outcomes = sim_outcomes[:n]
        sim_wr = sum(sim_outcomes) / n * 100
        cumulative = 0
        max_dd = peak = 0
        for o in sim_outcomes:
            cumulative += (1 if o == 1 else -1)
            if cumulative > peak:
                peak = cumulative
            dd = peak - cumulative
            if dd > max_dd:
                max_dd = dd
        simulations.append({"wr": sim_wr, "max_dd": max_dd})
    wrs = [s["wr"] for s in simulations]
    dds = [s["max_dd"] for s in simulations]
    wr_mean = np.mean(wrs)
    wr_std = np.std(wrs)
    wr_5th = np.percentile(wrs, 5)
    wr_95th = np.percentile(wrs, 95)
    dd_mean = np.mean(dds)
    dd_95th = np.percentile(dds, 95)
    ruin_count = sum(1 for s in simulations if s["max_dd"] > 50)
    risk_of_ruin = (ruin_count / MONTE_CARLO_SIMULATIONS) * 100
    stable_count = sum(1 for s in simulations if s["wr"] >= 60)
    stability = (stable_count / MONTE_CARLO_SIMULATIONS) * 100
    return {
        "strategy": strategy, "market_type": market_type, "trades": n, "simulations": MONTE_CARLO_SIMULATIONS,
        "baseline_wr": round(baseline_wr, 1), "mc_mean_wr": round(wr_mean, 1),
        "mc_wr_std": round(wr_std, 1), "mc_wr_5th": round(wr_5th, 1),
        "mc_wr_95th": round(wr_95th, 1), "mc_mean_dd": round(dd_mean, 1),
        "mc_dd_95th": round(dd_95th, 1), "risk_of_ruin": round(risk_of_ruin, 1),
        "stability": round(stability, 1),
        "status": "✅ مستقر" if stability >= 80 and risk_of_ruin < 5 else ("⚠️ متوسط" if stability >= 60 else "🔴 ضعيف")
    }, "تم"

def format_monte_carlo_message(result):
    if not result:
        return "📊 *Monte Carlo: لا توجد بيانات كافية*"
    market_label = result.get("market_type", "")
    market_prefix = f" [{market_label.upper()}]" if market_label else ""
    return (
        f"🎲 *محاكاة Monte Carlo{market_prefix}*\n"
        f"الاستراتيجية: `{result['strategy']}`\n"
        f"الصفقات: {result['trades']} | المحاكاة: {result['simulations']:,}\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *نسبة الربح:*\n"
        f"   Baseline: {result['baseline_wr']}%\n"
        f"   MC Mean: {result['mc_mean_wr']}% (±{result['mc_wr_std']}%)\n"
        f"   5th–95th: {result['mc_wr_5th']}% – {result['mc_wr_95th']}%\n\n"
        f"📉 *Max Drawdown:*\n"
        f"   Mean: {result['mc_mean_dd']} صفقات\n"
        f"   95th: {result['mc_dd_95th']} صفقات\n\n"
        f"⚠️ *Risk of Ruin:* {result['risk_of_ruin']}%\n"
        f"🛡️ *الاستقرار:* {result['stability']}%\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"{result['status']}"
    )

def save_monte_carlo_results(results_dict):
    try:
        with data_lock:
            with open(MONTE_CARLO_FILE, 'w', encoding='utf-8') as f:
                json.dump(results_dict, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"خطأ في حفظ نتائج Monte Carlo: {e}")

def format_monte_carlo_summary(results_dict):
    if not results_dict:
        return "📊 *Monte Carlo: لا توجد بيانات*"
    msg = "🎲 *Monte Carlo — ملخص شهري*\n"
    msg += f"📅 {datetime.now(CAIRO_TZ).strftime('%d/%m/%Y %I:%M %p')}\n"
    msg += "━━━━━━━━━━━━━━━━━━━━\n\n"
    for market in ["live", "otc"]:
        market_data = results_dict.get(market, {})
        if not market_data:
            continue
        market_emoji = "🟢" if market == "live" else "🔵"
        msg += f"{market_emoji} *{market.upper()}*\n"
        for strategy, result in market_data.items():
            msg += f"  📌 `{strategy}`:\n"
            msg += f"     ⚠️ Risk of Ruin: {result.get('risk_of_ruin', 'N/A')}%\n"
            msg += f"     📉 Max Drawdown: {result.get('mc_dd_95th', 'N/A')} صفقات\n"
            msg += f"     🛡️ الاستقرار: {result.get('stability', 'N/A')}%\n"
            msg += f"     📊 Baseline WR: {result.get('baseline_wr', 'N/A')}%\n"
            msg += f"     {result.get('status', '')}\n"
        msg += "\n"
    msg += "━━━━━━━━━━━━━━━━━━━━"
    return msg

# ========== HTF MARKET ANALYSIS (Higher Timeframe) - NEW ==========

def get_htf_market_regime(pair):
    """
    Analyze market regime on 1H timeframe with ENHANCED indicators.
    Uses: ALMA + Supertrend + MACD + EMA Alignment + ADX + Structure
    """
    key = f"htf_regime_{pair}"
    now = get_iq_time()
    with data_lock:
        if key in state.regime_cache and now - state.regime_cache[key][1] < HTF_REGIME_CACHE_TTL:
            return state.regime_cache[key][0]

    try:
        candles = get_cached_candles(pair, TIMEFRAME_1H, 80, max_age=300)
        if not candles or len(candles) < 50:
            return {"regime": "unknown", "trend": None, "structure": "unknown", "confidence": 0}

        df_h = pd.DataFrame(candles)
        df_h.rename(columns={'open':'Open','max':'High','min':'Low','close':'Close','volume':'Volume'}, inplace=True)

        # ===== المؤشرات الأساسية =====
        df_h['ALMA_9'] = calculate_alma(df_h['Close'], 9, 0.85, 6)
        df_h['ALMA_50'] = calculate_alma(df_h['Close'], 50, 0.85, 6)
        atr_series = calculate_atr_series(df_h, 14)
        atr = atr_series.iloc[-1]
        atr_avg = atr_series.tail(20).mean()
        adx, plus_di, minus_di = calculate_adx(df_h, 14)
        bbw = bollinger_bandwidth(df_h, 20)

        # ===== المؤشرات المحسّنة الجديدة =====
        # 1. Supertrend
        st_line, st_dir = calculate_supertrend(df_h, period=10, multiplier=3)
        supertrend_signal = "CALL" if st_dir.iloc[-1] == 1 else "PUT" if st_dir.iloc[-1] == -1 else None

        # 2. MACD
        macd_line, signal_line, histogram = calculate_macd(df_h['Close'])
        macd_bullish = macd_line.iloc[-1] > signal_line.iloc[-1] and histogram.iloc[-1] > histogram.iloc[-2] if len(histogram) > 1 else macd_line.iloc[-1] > signal_line.iloc[-1]
        macd_signal = "CALL" if macd_bullish else "PUT"

        # 3. EMA Alignment (تكديس EMAs)
        ema_trend, ema_strength = get_ema_alignment(df_h)

        # Detect HTF market structure
        df_h = detect_swings(df_h, window=2)
        structure, _, _ = get_market_structure(df_h, lookback=30)

        # Determine trend direction from ALMA
        curr_h = df_h.iloc[-1]
        prev_h = df_h.iloc[-2]
        if curr_h['ALMA_9'] > curr_h['ALMA_50'] and prev_h['ALMA_9'] > prev_h['ALMA_50']:
            alma_trend = "CALL"
        elif curr_h['ALMA_9'] < curr_h['ALMA_50'] and prev_h['ALMA_9'] < prev_h['ALMA_50']:
            alma_trend = "PUT"
        else:
            alma_trend = None

        # ===== تحديد الاتجاه بتوافق 4 مؤشرات =====
        trend_votes = []
        if alma_trend: trend_votes.append(alma_trend)
        if supertrend_signal: trend_votes.append(supertrend_signal)
        if macd_signal: trend_votes.append(macd_signal)
        if ema_trend: trend_votes.append(ema_trend)

        call_votes = trend_votes.count("CALL")
        put_votes = trend_votes.count("PUT")
        total_votes = len(trend_votes)

        if total_votes >= 3:
            if call_votes >= 3:
                trend_dir = "CALL"
                trend_strength = "strong"
            elif put_votes >= 3:
                trend_dir = "PUT"
                trend_strength = "strong"
            elif call_votes >= 2:
                trend_dir = "CALL"
                trend_strength = "moderate"
            elif put_votes >= 2:
                trend_dir = "PUT"
                trend_strength = "moderate"
            else:
                trend_dir = None
                trend_strength = "weak"
        else:
            trend_dir = alma_trend
            trend_strength = "weak"

        # Get pair-specific thresholds
        thresholds = get_pair_thresholds(pair)
        adx_trend = thresholds["adx_trending"]
        adx_range = thresholds["adx_ranging"]

        # Determine regime
        if adx >= adx_trend and atr > atr_avg * 1.2 and trend_strength == "strong":
            regime = "trending"
        elif adx < adx_range and bbw < 0.001:
            regime = "ranging"
        elif atr > atr_avg * 1.8:
            regime = "high_vol"
        elif atr < atr_avg * 0.5:
            regime = "low_vol"
        elif trend_strength == "weak":
            regime = "mixed"
        else:
            regime = "mixed"

        # Calculate confidence
        confidence = 50
        if structure in ["BULLISH", "BEARISH"]:
            confidence += 15
        if adx >= adx_trend or adx < adx_range:
            confidence += 10
        if trend_dir is not None:
            confidence += 15
        if trend_strength == "strong":
            confidence += 20
        elif trend_strength == "moderate":
            confidence += 10
        if ema_strength >= 1.0:
            confidence += 10

        result = {
            "regime": regime,
            "trend": trend_dir,
            "structure": structure,
            "confidence": min(confidence, 100),
            "adx": float(adx),
            "atr": float(atr),
            "bbw": float(bbw),
            "supertrend": supertrend_signal,
            "macd": macd_signal,
            "ema_alignment": ema_trend,
            "trend_strength": trend_strength,
            "votes": {"call": call_votes, "put": put_votes, "total": total_votes}
        }

        with data_lock:
            state.regime_cache[key] = (result, now)

        logger.info(f"📊 HTF Enhanced {pair}: ALMA={alma_trend} | ST={supertrend_signal} | MACD={macd_signal} | EMA={ema_trend} | Strength={trend_strength} | Regime={regime}")
        return result

    except Exception as e:
        logger.error(f"خطأ في تحليل HTF لـ {pair}: {e}")
        return {"regime": "unknown", "trend": None, "structure": "unknown", "confidence": 0}

def get_htf_trend_direction(pair):
    """Get confirmed trend direction from 1H timeframe with structure validation."""
    htf = get_htf_market_regime(pair)
    return htf.get("trend"), htf.get("structure"), htf.get("confidence")

def detect_htf_market_structure(pair):
    """
    Detect Higher Highs / Lower Lows structure on 1H timeframe.
    Returns: structure_type, strength_score
    """
    try:
        candles = get_cached_candles(pair, TIMEFRAME_1H, 100, max_age=300)
        if not candles or len(candles) < 50:
            return "unknown", 0

        df_h = pd.DataFrame(candles)
        df_h.rename(columns={'open':'Open','max':'High','min':'Low','close':'Close'}, inplace=True)
        df_h = detect_swings(df_h, window=3)

        recent = df_h.tail(60)
        sh_idx = recent[recent['is_swing_high']].index.tolist()
        sl_idx = recent[recent['is_swing_low']].index.tolist()

        if len(sh_idx) < 3 or len(sl_idx) < 3:
            return "neutral", 0

        # Check for Higher Highs + Higher Lows (Bullish)
        hh_count = 0
        hl_count = 0
        for i in range(1, min(4, len(sh_idx))):
            if df_h.loc[sh_idx[-i], 'High'] > df_h.loc[sh_idx[-(i+1)], 'High']:
                hh_count += 1
        for i in range(1, min(4, len(sl_idx))):
            if df_h.loc[sl_idx[-i], 'Low'] > df_h.loc[sl_idx[-(i+1)], 'Low']:
                hl_count += 1

        # Check for Lower Highs + Lower Lows (Bearish)
        lh_count = 0
        ll_count = 0
        for i in range(1, min(4, len(sh_idx))):
            if df_h.loc[sh_idx[-i], 'High'] < df_h.loc[sh_idx[-(i+1)], 'High']:
                lh_count += 1
        for i in range(1, min(4, len(sl_idx))):
            if df_h.loc[sl_idx[-i], 'Low'] < df_h.loc[sl_idx[-(i+1)], 'Low']:
                ll_count += 1

        if hh_count >= 2 and hl_count >= 2:
            strength = (hh_count + hl_count) * 25
            return "BULLISH", min(strength, 100)
        elif lh_count >= 2 and ll_count >= 2:
            strength = (lh_count + ll_count) * 25
            return "BEARISH", min(strength, 100)
        else:
            return "mixed", 30

    except Exception as e:
        logger.error(f"خطأ في تحليل HTF Structure لـ {pair}: {e}")
        return "unknown", 0

def confirm_regime_with_htf(pair, ltf_regime):
    """
    Confirm LTF regime with HTF analysis.
    Returns confirmed regime or 'uncertain' if HTF disagrees strongly.
    """
    htf = get_htf_market_regime(pair)
    htf_regime = htf.get("regime", "unknown")
    htf_confidence = htf.get("confidence", 0)

    # If HTF confidence is low, trust LTF
    if htf_confidence < 40:
        return ltf_regime, htf_confidence, "ltf_dominant"

    # If both agree, boost confidence
    if htf_regime == ltf_regime:
        return ltf_regime, min(htf_confidence + 20, 100), "confirmed"

    # If HTF says trending but LTF says ranging, trust HTF (higher timeframe is king)
    if htf_regime == "trending" and ltf_regime == "ranging":
        return "trending", htf_confidence, "htf_override"

    # If HTF says ranging but LTF says trending, be cautious
    if htf_regime == "ranging" and ltf_regime == "trending":
        return "mixed", htf_confidence - 20, "conflict"

    # Default: use HTF if confidence is high enough
    if htf_confidence >= 70:
        return htf_regime, htf_confidence, "htf_dominant"

    return ltf_regime, htf_confidence, "ltf_dominant"

# ========== MARKET REGIME - UPDATED ==========

def detect_market_regime(pair, tf=300):
    key = f"regime_{pair}"
    now = get_iq_time()
    with data_lock:
        if key in state.regime_cache and now - state.regime_cache[key][1] < REGIME_CACHE_TTL:
            cached = state.regime_cache[key][0]
            if isinstance(cached, dict):
                return cached.get("regime", "unknown")
            return cached
    try:
        df = get_cached_df_king(pair, tf, 80)
        if df is None or len(df) < 30:
            return "unknown"

        # Get pair-specific thresholds
        thresholds = get_pair_thresholds(pair)
        adx_trend = thresholds["adx_trending"]
        adx_range = thresholds["adx_ranging"]

        df['ALMA_20'] = calculate_alma(df['Close'], 20, 0.85, 6)
        df['ALMA_80'] = calculate_alma(df['Close'], 80, 0.85, 6)
        atr_series = calculate_atr_series(df, 14)
        atr = atr_series.iloc[-1]
        atr_avg = atr_series.tail(20).mean()
        adx, plus_di, minus_di = calculate_adx(df, 14)
        bbw = bollinger_bandwidth(df, 20)

        # LTF regime detection with pair-specific thresholds
        if adx >= adx_trend and atr > atr_avg * 1.2:
            ltf_regime = "trending"
        elif adx < adx_range and bbw < 0.001:
            ltf_regime = "ranging"
        elif atr > atr_avg * 1.8:
            ltf_regime = "high_vol"
        elif atr < atr_avg * 0.5:
            ltf_regime = "low_vol"
        else:
            ltf_regime = "mixed"

        # Confirm with HTF analysis for higher accuracy
        confirmed_regime, htf_confidence, confirmation_type = confirm_regime_with_htf(pair, ltf_regime)

        # Log the multi-timeframe analysis
        if htf_confidence >= 60:
            logger.info(f"📊 Regime {pair}: LTF={ltf_regime} | HTF={confirmed_regime} | Conf={htf_confidence}% | Type={confirmation_type}")

        with data_lock:
            state.regime_cache[key] = (confirmed_regime, now)
        return confirmed_regime
    except Exception as e:
        logger.error(f"خطأ في تحديد حالة السوق {pair}: {e}")
        return "unknown"

# ========== QUANTUM FUNCTIONS - UPDATED ==========

def analyze_volatility_filter(volatility):
    config = QUANTUM_CONFIG.get("volatility_filter", {})
    
    reject_low = config.get("reject_low", 0.000001)
    reject_high = config.get("reject_high", 0.02)
    ideal_low = config.get("ideal_low", 0.0008)
    ideal_high = config.get("ideal_high", 0.006)
    score_bonus = config.get("score_bonus", 5)
    score_penalty = config.get("score_penalty", 10)
    
    if volatility < reject_low:
        return {
            'status': 'REJECT',
            'reason': f'⚠️ تقلب منخفض جداً ({volatility:.4f}) - السوق راكد',
            'score_adjust': 0,
            'can_enter': False
        }
    
    if volatility > reject_high:
        return {
            'status': 'REJECT',
            'reason': f'⚠️ تقلب عالي جداً ({volatility:.4f}) - السوق عنيف',
            'score_adjust': 0,
            'can_enter': False
        }
    
    if volatility < ideal_low:
        return {
            'status': 'OK',
            'reason': f'✅ تقلب طبيعي ({volatility:.4f})',
            'score_adjust': 0,
            'can_enter': True,
            'emoji': '📊'
        }
    
    if ideal_low <= volatility <= ideal_high:
        return {
            'status': 'IDEAL',
            'reason': f'✅ تقلب مثالي ({volatility:.4f}) - مكافأة +{score_bonus} نقطة',
            'score_adjust': score_bonus,
            'can_enter': True,
            'emoji': '🎯'
        }
    
    if volatility < reject_high:
        penalty = score_penalty // 2
        return {
            'status': 'WARNING',
            'reason': f'⚠️ تقلب عالي ({volatility:.4f}) - خصم {penalty} نقطة',
            'score_adjust': -penalty,
            'can_enter': True,
            'emoji': '⚡'
        }
    
    return {
        'status': 'OK',
        'reason': f'تقلب متوسط ({volatility:.4f})',
        'score_adjust': 0,
        'can_enter': True,
        'emoji': '📊'
    }

def analyze_market_condition_quantum(df, pair=None):
    try:
        if len(df) < 30:
            return "unknown"

        # Get pair-specific thresholds
        thresholds = get_pair_thresholds(pair) if pair else get_pair_thresholds("EURUSD")
        adx_trend = thresholds["adx_trending"]
        adx_range = thresholds["adx_ranging"]

        adx, plus_di, minus_di = calculate_adx(df, 14)
        atr_series = calculate_atr_series(df, 14)
        atr = atr_series.iloc[-1]
        atr_avg = atr_series.tail(20).mean()
        bbw = bollinger_bandwidth(df, 20)

        if adx >= adx_trend and atr > atr_avg * 1.2:
            ltf_regime = "trending"
        elif adx < adx_range and bbw < 0.001:
            ltf_regime = "ranging"
        elif atr > atr_avg * 1.8:
            ltf_regime = "high_vol"
        elif atr < atr_avg * 0.5:
            ltf_regime = "low_vol"
        else:
            ltf_regime = "mixed"

        if pair:
            confirmed_regime, htf_confidence, confirmation_type = confirm_regime_with_htf(pair, ltf_regime)
            if htf_confidence >= 50:
                logger.info(f"🧠 Quantum {pair}: LTF={ltf_regime} | HTF={confirmed_regime} | Conf={htf_confidence}%")
            return confirmed_regime

        return ltf_regime
    except Exception as e:
        logger.error(f"خطأ في تحليل حالة السوق Quantum: {e}")
        return "unknown"

def detect_market_structure_quantum(df):
    try:
        df_swings = detect_swings(df, window=2)
        structure, _, _ = get_market_structure(df_swings, lookback=30)
        if structure in ["BULLISH", "BEARISH"]:
            return structure
        return None
    except Exception as e:
        logger.error(f"خطأ في اكتشاف هيكل السوق Quantum: {e}")
        return None

def detect_liquidity_sweep_quantum(df, curr):
    try:
        df_swings = detect_swings(df, window=2)
        sweep_call = detect_liquidity_sweep(df_swings, "CALL", 0.0003)
        if sweep_call[0]:
            return "BULLISH"
        sweep_put = detect_liquidity_sweep(df_swings, "PUT", 0.0003)
        if sweep_put[0]:
            return "BEARISH"
        return None
    except Exception as e:
        logger.error(f"خطأ في اكتشاف Liquidity Sweep Quantum: {e}")
        return None

def detect_order_block_quantum(df, curr):
    try:
        price = curr['Close']
        start = max(5, len(df) - 20)

        for i in range(start, len(df)):
            candle = df.iloc[i]
            prev = df.iloc[i-1]
            if (candle['Close'] > candle['Open'] and 
                candle['Close'] > prev['High'] and 
                prev['Close'] < prev['Open'] and
                (candle['Close'] - candle['Open']) > abs(prev['Close'] - prev['Open']) * 1.5):
                if prev['Low'] <= price <= prev['High'] * 1.002:
                    return "BULLISH"

        for i in range(start, len(df)):
            candle = df.iloc[i]
            prev = df.iloc[i-1]
            if (candle['Close'] < candle['Open'] and 
                candle['Close'] < prev['Low'] and 
                prev['Close'] > prev['Open'] and
                (candle['Open'] - candle['Close']) > abs(prev['Close'] - prev['Open']) * 1.5):
                if prev['Low'] * 0.998 <= price <= prev['High']:
                    return "BEARISH"
        return None
    except Exception as e:
        logger.error(f"خطأ في اكتشاف Order Block Quantum: {e}")
        return None

def detect_fvg_quantum(df, curr):
    try:
        start = max(2, len(df) - 20)

        for i in range(start, len(df) - 1):
            if df['Low'].iloc[i] > df['High'].iloc[i-2]:
                return {"type": "BULLISH", "top": df['Low'].iloc[i], "bottom": df['High'].iloc[i-2], "idx": i}

        for i in range(start, len(df) - 1):
            if df['High'].iloc[i] < df['Low'].iloc[i-2]:
                return {"type": "BEARISH", "top": df['Low'].iloc[i-2], "bottom": df['High'].iloc[i], "idx": i}
        return None
    except Exception as e:
        logger.error(f"خطأ في اكتشاف FVG Quantum: {e}")
        return None

def fvg_retest_quantum(df, fvg):
    try:
        if fvg is None:
            return False
        start_idx = max(fvg['idx'] + 1, len(df) - 15)
        for i in range(start_idx, len(df)):
            if df['Low'].iloc[i] <= fvg["top"] and df['High'].iloc[i] >= fvg["bottom"]:
                return True
        return False
    except Exception as e:
        logger.error(f"خطأ في التحقق من FVG Retest Quantum: {e}")
        return False

def volume_confirmation_quantum(df, curr):
    try:
        if "Volume" not in df.columns or curr['Volume'] <= 0:
            return False
        vol_ma = df['Volume'].tail(20).mean()
        return curr['Volume'] >= vol_ma * 0.9
    except Exception as e:
        logger.error(f"خطأ في تأكيد الحجم Quantum: {e}")
        return False

def momentum_confirmation_quantum(df, curr):
    try:
        if 'RSI' not in df.columns or 'ROC' not in df.columns:
            return None
        if 'ALMA_9' not in df.columns or 'ALMA_50' not in df.columns:
            return None

        rsi = curr['RSI']
        roc = curr['ROC']
        alma9 = curr['ALMA_9']
        alma50 = curr['ALMA_50']

        if rsi > 52 and alma9 > alma50 and roc > -0.1:
            return "BULLISH"
        if rsi < 48 and alma9 < alma50 and roc < 0.1:
            return "BEARISH"
        return None
    except Exception as e:
        logger.error(f"خطأ في تأكيد الزخم Quantum: {e}")
        return None

def calculate_confidence_score_quantum(structure, liquidity, order_block, fvg, volume, momentum, fvg_ok=True, rsi_val=50, regime="unknown", df=None, curr=None):
    call = 0
    put = 0
    reasons = []

    # === BASE TECHNICAL SCORE (up to 50 points) ===
    # This ensures signals exist even without perfect SMC conditions
    if df is not None and len(df) >= 20:
        # Calculate indicators if missing
        if 'ALMA_9' not in df.columns:
            df['ALMA_9'] = calculate_alma(df['Close'], 9, 0.85, 6)
        if 'ALMA_50' not in df.columns:
            df['ALMA_50'] = calculate_alma(df['Close'], 50, 0.85, 6)
        if 'RSI' not in df.columns:
            df['RSI'] = wilder_rsi(df['Close'], 14)
        if 'Stoch_K' not in df.columns:
            df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)
        if 'ROC' not in df.columns:
            df['ROC'] = calculate_roc(df['Close'], 5)

        last = df.iloc[-2] if len(df) > 2 else df.iloc[-1]

        # ALMA Trend: 20 pts
        if last['ALMA_9'] > last['ALMA_50']:
            call += 20; reasons.append("📈 ALMA صاعد")
        elif last['ALMA_9'] < last['ALMA_50']:
            put += 20; reasons.append("📉 ALMA هابط")

        # RSI: 12 pts
        if 'RSI' in last:
            if last['RSI'] > 55:
                call += 12; reasons.append("📈 RSI قوي")
            elif last['RSI'] < 45:
                put += 12; reasons.append("📉 RSI قوي")
            elif last['RSI'] >= 50:
                call += 6
            elif last['RSI'] < 50:
                put += 6

        # Stochastic: 12 pts
        if 'Stoch_K' in last and 'Stoch_D' in last:
            if last['Stoch_K'] > last['Stoch_D']:
                call += 12; reasons.append("📈 Stoch صاعد")
            elif last['Stoch_K'] < last['Stoch_D']:
                put += 12; reasons.append("📉 Stoch هابط")

        # ROC: 12 pts
        if 'ROC' in last:
            if last['ROC'] > 0:
                call += 12; reasons.append("📈 ROC صاعد")
            elif last['ROC'] < 0:
                put += 12; reasons.append("📉 ROC هابط")

        # ADX: 4 pts
        try:
            adx_val, _, _ = calculate_adx(df, 14)
            if adx_val >= 12:
                call += 4; put += 4; reasons.append("📊 ADX نشط")
        except:
            pass

    # === SMC BONUS (up to 50 points) ===
    # Strong signals when SMC concepts align
    smc_weights = {"structure": 12, "liquidity": 12, "order_block": 8, "fvg": 8, "volume": 5, "momentum": 5}

    if structure == "BULLISH":
        call += smc_weights["structure"]; reasons.append("📈 هيكل صاعد")
    elif structure == "BEARISH":
        put += smc_weights["structure"]; reasons.append("📉 هيكل هابط")

    if liquidity == "BULLISH":
        call += smc_weights["liquidity"]; reasons.append("💧 Liquidity Sweep صاعد")
    elif liquidity == "BEARISH":
        put += smc_weights["liquidity"]; reasons.append("💧 Liquidity Sweep هابط")

    if order_block == "BULLISH":
        call += smc_weights["order_block"]; reasons.append("📦 Order Block صاعد")
    elif order_block == "BEARISH":
        put += smc_weights["order_block"]; reasons.append("📦 Order Block هابط")

    if fvg and fvg["type"] == "BULLISH":
        if fvg_ok:
            call += smc_weights["fvg"]; reasons.append("🔲 FVG صاعد")
        else:
            call += smc_weights["fvg"] // 2; reasons.append("🔲 FVG صاعد (جزئي)")
    elif fvg and fvg["type"] == "BEARISH":
        if fvg_ok:
            put += smc_weights["fvg"]; reasons.append("🔲 FVG هابط")
        else:
            put += smc_weights["fvg"] // 2; reasons.append("🔲 FVG هابط (جزئي)")

    if volume:
        if call > put:
            call += smc_weights["volume"]; reasons.append("📊 حجم مرتفع")
        elif put > call:
            put += smc_weights["volume"]; reasons.append("📊 حجم مرتفع")
    else:
        if call > put:
            call += smc_weights["volume"] // 2; reasons.append("📊 حجم متوسط")
        elif put > call:
            put += smc_weights["volume"] // 2; reasons.append("📊 حجم متوسط")

    if momentum == "BULLISH":
        call += smc_weights["momentum"]; reasons.append("⚡ زخم صاعد")
    elif momentum == "BEARISH":
        put += smc_weights["momentum"]; reasons.append("⚡ زخم هابط")
    else:
        if rsi_val > 55:
            call += smc_weights["momentum"] // 2; reasons.append("⚡ زخم جزئي (RSI)")
        elif rsi_val < 45:
            put += smc_weights["momentum"] // 2; reasons.append("⚡ زخم جزئي (RSI)")

    # RSI bonus when aligned with direction
    if rsi_val > 55 and call > put:
        call += 5; reasons.append("📈 RSI مؤكد")
    elif rsi_val < 45 and put > call:
        put += 5; reasons.append("📉 RSI مؤكد")

    if call > put:
        return {"direction": "CALL", "score": min(call, 100), "reasons": reasons}
    elif put > call:
        return {"direction": "PUT", "score": min(put, 100), "reasons": reasons}
    else:
        return {"direction": None, "score": 0, "reasons": []}

def duplicate_signal_quantum(pair, direction):
    key = f"quantum_{pair}_{direction}_{(int(get_iq_time()) // 300) * 300}"
    with data_lock:
        if key in quantum_memory:
            return True
        quantum_memory[key] = get_iq_time()
        now = get_iq_time()
        for k in list(quantum_memory.keys()):
            if now - quantum_memory[k] > 600:
                del quantum_memory[k]
    return False

def update_quantum_weights(trade_history):
    if len(trade_history) < QUANTUM_CONFIG["learning"]["min_trades"]:
        return
    
    features = list(QUANTUM_CONFIG["weights"].keys())
    updates = []
    
    for feature in features:
        feature_trades = [t for t in trade_history if t.get('filters', {}).get(feature, False)]
        
        if len(feature_trades) >= 20:
            wins = sum(1 for t in feature_trades if t.get('outcome') == 'win')
            winrate = (wins / len(feature_trades)) * 100
            
            old_weight = QUANTUM_CONFIG["weights"][feature]
            
            if winrate > 75:
                new_weight = min(old_weight + 2, QUANTUM_CONFIG["learning"]["max_weight"])
                updates.append(f"{feature}: {old_weight} → {new_weight} (WR: {winrate:.1f}%) ✅")
            elif winrate < 50:
                new_weight = max(old_weight - 2, QUANTUM_CONFIG["learning"]["min_weight"])
                updates.append(f"{feature}: {old_weight} → {new_weight} (WR: {winrate:.1f}%) 🔄")
            else:
                new_weight = old_weight
            
            QUANTUM_CONFIG["weights"][feature] = new_weight
    
    if updates:
        quantum_weights_history.append({
            "timestamp": get_iq_time(),
            "updates": updates,
            "weights": QUANTUM_CONFIG["weights"].copy()
        })
        try:
            with open("quantum_weights_history.json", 'w', encoding='utf-8') as f:
                json.dump(quantum_weights_history, f, indent=2)
        except Exception as e:
            logger.error(f"خطأ في حفظ تاريخ أوزان Quantum: {e}")
        logger.info(f"🧠 Quantum تم تحديث الأوزان: {updates}")

def feature_importance_quantum(trade_history):
    if len(trade_history) < 50:
        return {"status": "بيانات غير كافية (تحتاج 50+ صفقة)"}
    
    importance = {}
    features = ['structure', 'liquidity', 'order_block', 'fvg', 'volume', 'momentum']
    feature_names = {
        'structure': 'Structure',
        'liquidity': 'Liquidity Sweep',
        'order_block': 'Order Block',
        'fvg': 'FVG',
        'volume': 'Volume',
        'momentum': 'Momentum'
    }
    
    total_trades = len(trade_history)
    
    for feature in features:
        feature_trades = [t for t in trade_history if t.get('filters', {}).get(feature, False)]
        
        if len(feature_trades) >= 20:
            wins = sum(1 for t in feature_trades if t.get('outcome') == 'win')
            winrate = (wins / len(feature_trades)) * 100
            coverage = len(feature_trades) / total_trades * 100
            importance_score = winrate * 0.7 + coverage * 0.3
            
            importance[feature_names[feature]] = {
                "winrate": round(winrate, 1),
                "coverage": round(coverage, 1),
                "importance": round(importance_score, 1),
                "trades": len(feature_trades)
            }
        else:
            importance[feature_names[feature]] = {
                "winrate": 0,
                "coverage": 0,
                "importance": 0,
                "trades": 0,
                "status": "بيانات غير كافية"
            }
    
    sorted_importance = dict(sorted(
        importance.items(),
        key=lambda x: x[1]['importance'] if isinstance(x[1], dict) else 0,
        reverse=True
    ))
    
    return sorted_importance

def generate_quantum_performance_report(trades):
    if not trades:
        return "📊 *لا توجد بيانات كافية لتقرير Quantum*"
    
    quantum_trades = [t for t in trades if t.get('strategy') == 'quantum']
    
    if len(quantum_trades) < 10:
        return f"📊 *بيانات Quantum غير كافية* (تحتاج 10+ صفقة، حالياً: {len(quantum_trades)})"
    
    total = len(quantum_trades)
    wins = sum(1 for t in quantum_trades if t.get('outcome') == 'win')
    losses = total - wins
    wr = (wins / total * 100) if total > 0 else 0
    
    levels = {}
    for t in quantum_trades:
        level = t.get('level', 0)
        if level not in levels:
            levels[level] = {"win": 0, "loss": 0, "total": 0}
        levels[level]["total"] += 1
        if t.get('outcome') == 'win':
            levels[level]["win"] += 1
        else:
            levels[level]["loss"] += 1
    
    importance = feature_importance_quantum(quantum_trades)
    weights = QUANTUM_CONFIG["weights"]
    
    msg = (
        f"🧠 *تقرير أداء Quantum Smart Flow*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *إجمالي الصفقات:* {total}\n"
        f"✅ *رابحة:* {wins} | ❌ *خاسرة:* {losses}\n"
        f"🎯 *نسبة الربح:* {wr:.1f}%\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📊 *توزيع المستويات:*\n"
    )
    
    for level, data in sorted(levels.items()):
        lwr = (data["win"] / data["total"] * 100) if data["total"] > 0 else 0
        name = QUANTUM_SIGNAL_NAMES.get(level, (f"Level {level}", ""))[0]
        msg += f"  {name}: {data['total']} صفقة — WR: {lwr:.1f}%\n"
    
    msg += (
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"⚖️ *الأوزان الحالية:*\n"
    )
    
    for key, value in weights.items():
        msg += f"  {key}: {value}\n"
    
    if isinstance(importance, dict) and "status" not in importance:
        msg += (
            f"━━━━━━━━━━━━━━━━━━━━\n"
            f"🔬 *أهمية العوامل:*\n"
        )
        for feature, data in list(importance.items())[:4]:
            if isinstance(data, dict) and data.get('winrate', 0) > 0:
                msg += f"  {feature}: WR {data['winrate']}% (غطاء {data['coverage']}%)\n"
    
    return msg

def handle_quantum_command(command):
    cmd = command.lower().strip()
    
    if cmd == "/quantum_weights":
        msg = "🧠 *الأوزان الحالية Quantum:*\n"
        for key, value in QUANTUM_CONFIG["weights"].items():
            msg += f"  {key}: {value}\n"
        return msg
    
    elif cmd == "/quantum_stats":
        trades = read_trade_log(max_entries=5000)
        return generate_quantum_performance_report(trades)
    
    elif cmd == "/quantum_feature":
        trades = read_trade_log(max_entries=5000)
        quantum_trades = [t for t in trades if t.get('strategy') == 'quantum']
        importance = feature_importance_quantum(quantum_trades)
        
        if isinstance(importance, dict) and "status" in importance:
            return f"🔬 *Feature Importance - Quantum:*\n{importance['status']}"
        
        msg = "🔬 *Feature Importance - Quantum:*\n"
        for feature, data in importance.items():
            if isinstance(data, dict):
                msg += f"  {feature}: WR {data.get('winrate', 0)}% (غطاء {data.get('coverage', 0)}%)\n"
        return msg
    
    elif cmd == "/quantum_weights reset":
        QUANTUM_CONFIG["weights"] = {
            "structure": 20,
            "liquidity": 20,
            "order_block": 15,
            "fvg": 15,
            "volume": 5,
            "momentum": 20,
            "rsi": 5
        }
        return "✅ *تم إعادة ضبط الأوزان إلى القيم الافتراضية*"
    
    else:
        return None

def quantum_stats_worker():
    logger.info("🧠 محرك إحصائيات Quantum بدأ")
    last_learning = 0
    last_report = 0
    
    while not stop_event.is_set():
        try:
            now = get_iq_time()
            
            if now - last_learning > QUANTUM_CONFIG["learning"]["update_interval"]:
                trades = read_trade_log(max_entries=5000)
                quantum_trades = [t for t in trades if t.get('strategy') == 'quantum']
                
                if len(quantum_trades) >= QUANTUM_CONFIG["learning"]["min_trades"]:
                    update_quantum_weights(quantum_trades)
                    logger.info(f"🧠 تم تحديث أوزان Quantum: {QUANTUM_CONFIG['weights']}")
                    
                    msg = "🧠 *تحديث أوزان Quantum*\n"
                    for key, value in QUANTUM_CONFIG["weights"].items():
                        msg += f"  {key}: {value}\n"
                    send_telegram_message(msg)
                
                last_learning = now
            
            if now - last_report > 604800:
                trades = read_trade_log(max_entries=10000)
                report = generate_quantum_performance_report(trades)
                if report:
                    send_telegram_message(report)
                last_report = now
                
        except Exception as e:
            logger.error(f"خطأ في محرك Quantum: {e}")
            logger.error(traceback.format_exc())
        
        stop_event.wait(3600)

def init_quantum_system():
    logger.info("🧠 تهيئة Quantum Smart Flow Engine...")
    
    try:
        if os.path.exists("quantum_weights_history.json"):
            with open("quantum_weights_history.json", 'r', encoding='utf-8') as f:
                history = json.load(f)
                if history:
                    last_weights = history[-1].get('weights', {})
                    if last_weights:
                        QUANTUM_CONFIG["weights"] = last_weights
                        logger.info(f"🧠 تم تحميل الأوزان السابقة: {last_weights}")
    except Exception as e:
        logger.warning(f"⚠️ فشل تحميل تاريخ الأوزان: {e}")
    
    logger.info("🧠 Quantum Smart Flow Engine جاهز!")
    logger.info(f"📊 الأوزان الحالية: {QUANTUM_CONFIG['weights']}")
    logger.info(f"🎯 الحد الأدنى Live: {QUANTUM_CONFIG['min_score_live']}")
    logger.info(f"🎯 الحد الأدنى OTC: {QUANTUM_CONFIG['min_score_otc']}")

def analyze_pair_quantum(pair, timeframe="5m"):
    tf_seconds, duration_text = 300, "5 دقائق"
    
    df = get_cached_df_smart(pair, tf_seconds, 100)
    if df is None or len(df) < 100:
        logger.info(f"🛑 Quantum {pair}: لا يوجد بيانات كافية")
        return None

    # استخدام تحليل HTF المحسن
    regime = analyze_market_condition_quantum(df, pair=pair)
    if regime == "ranging":
        logger.info(f"⚠️ Quantum {pair}: سوق عرضي (RANGE) - مستمر بتخفيض 10 نقاط")

    # استخدام آخر شمعة مقفولة
    curr = df.iloc[-2]
    price = curr['Close']

    kalman = get_kalman(pair)
    smoothed_price = kalman.update(price)
    atr_val = calculate_atr_series(df, 14).iloc[-1] / price if len(df) >= 14 else 0
    volatility = atr_val if atr_val > 0 else kalman.get_volatility()

    vol_filter = analyze_volatility_filter(volatility)
    
    if not vol_filter['can_enter']:
        logger.info(f"🛑 Quantum {pair}: {vol_filter['reason']}")
        return None
    
    logger.info(f"📊 Quantum {pair}: {vol_filter['reason']}")

    structure = detect_market_structure_quantum(df)
    liquidity = detect_liquidity_sweep_quantum(df, curr)
    order_block = detect_order_block_quantum(df, curr)
    fvg = detect_fvg_quantum(df, curr)

    fvg_ok = False
    if fvg:
        if fvg_retest_quantum(df, fvg):
            fvg_ok = True
            logger.info(f"✅ Quantum {pair}: FGV معاد اختباره")
        else:
            logger.info(f"⚠️ Quantum {pair}: FVG لم يُعاد اختباره - جزئي فقط")
    else:
        logger.info(f"⚠️ Quantum {pair}: لا يوجد FVG - جزئي فقط")

    volume = volume_confirmation_quantum(df, curr)
    momentum = momentum_confirmation_quantum(df, curr)

    # Get RSI for extra scoring
    rsi_val = curr.get('RSI', 50) if 'RSI' in curr else 50

    result = calculate_confidence_score_quantum(structure, liquidity, order_block, fvg, volume, momentum, fvg_ok, rsi_val, regime, df, curr)

    adjusted_score = result['score'] + vol_filter['score_adjust']
    if regime == "ranging":
        adjusted_score -= 5
    adjusted_score = max(0, min(100, adjusted_score))
    
    logger.info(f"📊 Quantum {pair}: النتيجة الأصلية {result['score']} → معدلة {adjusted_score} ({vol_filter['reason']})")

    min_score = QUANTUM_CONFIG["min_score_otc"] if "OTC" in pair.upper() else QUANTUM_CONFIG["min_score_live"]
    final_score = adjusted_score
    
    if result['direction'] is None or final_score < min_score:
        logger.info(f"🛑 Quantum {pair}: النتيجة {final_score} < {min_score}")
        return None

    # duplicate check moved to Phase 3 (Final Signal only)

    if final_score >= 90:
        level = 4
        signal_name_ar, signal_name_en = QUANTUM_SIGNAL_NAMES[level]
    elif final_score >= 85:
        level = 3
        signal_name_ar, signal_name_en = QUANTUM_SIGNAL_NAMES[level]
    elif final_score >= 80:
        level = 2
        signal_name_ar, signal_name_en = QUANTUM_SIGNAL_NAMES[level]
    elif final_score >= 70:
        level = 1
        signal_name_ar, signal_name_en = QUANTUM_SIGNAL_NAMES[level]
    else:
        logger.info(f"🛑 Quantum {pair}: Score={final_score} < 70")
        return None

    da = "صعود (CALL)" if result['direction'] == "CALL" else "هبوط (PUT)"

    iq_now = get_iq_time()
    csec = int(iq_now) % 300
    candle_start = (int(iq_now) // 300) * 300
    pair_key = f"quantum_{pair}_{candle_start}"
    pending_key = f"quantum_{pair}_{candle_start}"

    ok, reason = passes_common_entry_filters(pair)
    if not ok:
        logger.info(f"🛑 Quantum {pair}: إلغاء - {reason}")
        return None

    # ===== المرحلة 1: تنبيه مبكر (270-280) =====
    if 270 <= csec <= 280:
        with data_lock:
            if pair_key not in state.quantum_alerted_pairs:
                state.pending_alerts[pending_key] = {
                    'direction': result['direction'],
                    'level': level,
                    'signal_name': signal_name_ar,
                    'score': final_score,
                    'alert_time': iq_now,
                    'strategy': 'quantum'
                }

                indicator_counts = get_indicator_counts(pair, df)
                time_quality = get_time_quality('quantum')
                regime_badge = get_regime_badge('quantum', regime, None, indicator_counts)
                vol_emoji = vol_filter.get('emoji', '📊')
                vol_status = vol_filter['reason']

                msg = (
                    f"⚠️ *تنبيه مبكر — {signal_name_ar}*\n"
                    f"الزوج: `{pair}` [5 دقائق]\n"
                    f"الاتجاه: *{da}*\n"
                    f"📊 النقاط: *{final_score}/100* (معدلة)\n"
                    f"⏱️ *صفقة قادمة خلال 20 ثانية...*\n"
                    f"🔄 *جاري التحقق من الشروط النهائية...*\n"
                    f"━━━━━━━━━━━━\n"
                    f"🕐 *الوقت:* {time_quality}\n"
                    f"📍 {regime_badge}\n"
                    f"⚛️ Kalman: {smoothed_price:.5f} | {vol_emoji} {vol_status}"
                )
                send_telegram_message(msg)
                state.quantum_alerted_pairs[pair_key] = iq_now
        return None

    # ===== المرحلة 2 & 3 (280-299) =====
    if not (280 <= csec <= 299):
        return None

    with data_lock:
        pending = state.pending_alerts.get(pending_key)

    if pending and pending['direction'] != result['direction']:
        send_cancelled_alert(pair, pending['direction'], "الاتجاه تغير في Quantum", 'quantum')
        with data_lock:
            if pending_key in state.pending_alerts:
                del state.pending_alerts[pending_key]
        return None

    ok, reason = passes_common_entry_filters(pair)
    if not ok:
        if pending:
            send_cancelled_alert(pair, result['direction'], reason, 'quantum')
        with data_lock:
            if pending_key in state.pending_alerts:
                del state.pending_alerts[pending_key]
        return None

    # ===== المرحلة 3: الإشارة النهائية (293-299) =====
    if csec >= 293:
        if already_sent_this_candle_quantum(pair):
            return None

        with data_lock:
            if pair_key in state.quantum_alerted_pairs:
                del state.quantum_alerted_pairs[pair_key]
            if pending_key in state.pending_alerts:
                del state.pending_alerts[pending_key]

        indicator_counts = get_indicator_counts(pair, df)
        time_quality = get_time_quality('quantum')
        regime_badge = get_regime_badge('quantum', regime, None, indicator_counts)
        
        kalman_info = f"Kalman: {smoothed_price:.5f}"
        vol_emoji = vol_filter.get('emoji', '📊')
        vol_status = vol_filter['reason']
        volatility_info = f"{vol_emoji} {vol_status}"
        
        indicators_str = f"Score={final_score}/100 | " + " | ".join(result['reasons'][:3])
        indicators_str += f" | {kalman_info} | {volatility_info}"

        new_trade = _build_trade_dict(
            pair=pair,
            direction=result['direction'],
            entry_price=price,
            expire_offset=300,
            is_king=False,
            is_martingale=False,
            signal_level=level,
            signal_name=signal_name_ar,
            score=final_score,
            filters={
                'structure': structure is not None,
                'liquidity': liquidity is not None,
                'order_block': order_block is not None,
                'fvg': fvg is not None,
                'volume': volume,
                'momentum': momentum is not None,
                'volatility_ok': vol_filter['can_enter']
            },
            indicators={
                'score': final_score,
                'original_score': result['score'],
                'score_adjust': vol_filter['score_adjust'],
                'reasons': result['reasons'],
                'market': regime,
                'structure': str(structure) if structure else 'None',
                'liquidity': str(liquidity) if liquidity else 'None',
                'order_block': str(order_block) if order_block else 'None',
                'fvg': str(fvg) if fvg else 'None',
                'kalman_price': round(smoothed_price, 5),
                'volatility': round(volatility, 4),
                'volatility_status': vol_filter['status']
            },
            strategy='quantum'
        )

        if not add_trade_atomic(new_trade):
            return None

        htf_data = get_htf_market_regime(pair)
        final_signal = send_final_signal(
            pair, result['direction'], signal_name_ar, final_score,
            duration_text, indicators_str, 'quantum', regime=regime, signal_level=level, htf_data=htf_data
        )
        
        if final_signal is None:
            logger.info(f"⛔ Quantum {pair}: تم إرسالها مسبقاً (منع التكرار)")
            return None

        logger.info(f"🧠 Quantum {pair}: {signal_name_ar} تم الإرسال (Score={final_score} | Vol={volatility:.4f})")
        return final_signal

    return None

def analyze_pair_wrapper_quantum(pair):
    try:
        return pair, analyze_pair_quantum(pair, "5m")
    except Exception as e:
        logger.error(f"خطأ Quantum في {pair}: {e}")
        return pair, None

# ===================================================================
# ========== ORIGINAL STRATEGY ENHANCED WEIGHTS ==========
# ===================================================================
ORIGINAL_WEIGHTS = {
    "trend": 20,        # HTF Trend + ALMA Cross
    "momentum": 20,     # Stoch + ROC
    "structure": 15,    # Market Structure (HH/HL/LH/LL)
    "volume": 15,       # Volume confirmation
    "candle": 15,       # Candle quality
    "zone": 10,         # Near S/R zone
    "adx": 5            # ADX filter
}

def evaluate_signal_strength_enhanced(direction, curr, prev, df, price, alma9, alma50,
                                      stoch_k, stoch_d, rsi, volume, vol_ma,
                                      atr, adx, bbw, roc, near_sup, near_res,
                                      structure=None, htf_regime=None):
    """
    نظام تقييم محسّن للاستراتيجية الأصلية — 0 إلى 100 نقطة
    Base Technical: 0-60 | Structure/HTF Bonus: 0-40
    """
    score = 0
    reasons = []

    # ========== BASE TECHNICAL SCORE (0-60) ==========

    # 1. ALMA Trend — 15 pts
    a9p, a50p = prev['ALMA_9'], prev['ALMA_50']
    a9c, a50c = alma9, alma50
    bullish_cross = (a9p <= a50p) and (a9c > a50c)
    bearish_cross = (a9p >= a50p) and (a9c < a50c)

    has_cross = (direction == "CALL" and bullish_cross) or (direction == "PUT" and bearish_cross)
    trend_aligned = (direction == "CALL" and price > alma9) or (direction == "PUT" and price < alma9)

    if has_cross:
        score += 15
        reasons.append("ALMA Cross")
    elif trend_aligned:
        score += 10
        reasons.append("ALMA Aligned")
    else:
        score += 3
        reasons.append("ALMA Weak")

    # 2. Momentum (Stoch + ROC) — 15 pts
    if direction == "CALL":
        stoch_ok = stoch_k > stoch_d
        roc_ok = roc > 0
    else:
        stoch_ok = stoch_k < stoch_d
        roc_ok = roc < 0

    if stoch_ok:
        score += 8
        reasons.append("Stoch OK")
    else:
        score += 2
        reasons.append("Stoch Weak")

    if roc_ok:
        score += 7
        reasons.append("ROC OK")
    else:
        score += 1
        reasons.append("ROC Weak")

    # 3. Volume — 10 pts
    vol_ratio = volume / vol_ma if vol_ma > 0 else 0
    if vol_ratio >= 2.0:
        score += 10
        reasons.append("Vol Strong")
    elif vol_ratio >= 1.5:
        score += 7
        reasons.append("Vol OK")
    elif vol_ratio >= 1.2:
        score += 4
        reasons.append("Vol Weak")
    else:
        score += 1
        reasons.append("Vol Low")

    # 4. Candle Quality — 10 pts
    body = abs(curr['Close'] - curr['Open'])
    rng = curr['High'] - curr['Low']
    if rng > 0:
        body_pct = body / rng
        upper_wick = curr['High'] - max(curr['Close'], curr['Open'])
        lower_wick = min(curr['Close'], curr['Open']) - curr['Low']
        shadow_pct = (upper_wick + lower_wick) / rng

        if body_pct >= 0.65 and shadow_pct <= 0.25:
            score += 10
            reasons.append("Candle Strong")
        elif body_pct >= 0.50 and shadow_pct <= 0.35:
            score += 7
            reasons.append("Candle OK")
        elif body_pct >= 0.40:
            score += 4
            reasons.append("Candle Weak")
        else:
            score += 1
            reasons.append("Candle Poor")

    # 5. Zone (S/R) — 5 pts
    if (direction == "CALL" and near_sup) or (direction == "PUT" and near_res):
        score += 5
        reasons.append("Zone")

    # 6. ADX — 5 pts
    if adx >= 25:
        score += 5
        reasons.append("ADX Strong")
    elif adx >= 20:
        score += 3
        reasons.append("ADX OK")
    elif adx >= 15:
        score += 1
        reasons.append("ADX Weak")

    # ========== STRUCTURE / HTF BONUS (0-40) ==========

    # 7. Market Structure — 15 pts
    if structure:
        if (direction == "CALL" and structure == "BULLISH") or (direction == "PUT" and structure == "BEARISH"):
            score += 15
            reasons.append("Structure")
        else:
            score += 5
            reasons.append("Structure Weak")

    # 8. HTF Trend Confirmation — 15 pts
    if htf_regime:
        if htf_regime.get("trend") == direction:
            score += 15
            reasons.append("HTF Confirm")
        elif htf_regime.get("trend") is not None:
            score += 3
            reasons.append("HTF Mixed")

    # 9. HTF Structure — 10 pts
    if htf_regime:
        htf_struct = htf_regime.get("structure")
        if htf_struct:
            if (direction == "CALL" and htf_struct == "BULLISH") or (direction == "PUT" and htf_struct == "BEARISH"):
                score += 10
                reasons.append("HTF Structure")
            else:
                score += 2
                reasons.append("HTF Structure Weak")

    # Hard filters (reduce score but don't reject)
    if atr < price * 0.00015:
        score = max(0, score - 10)
        reasons.append("ATR Low")
    if bbw < 0.0008:
        score = max(0, score - 5)
        reasons.append("BBW Low")

    return int(min(score, 100)), reasons


def analyze_pair(pair, timeframe="5m"):
    tf_seconds, duration_text = 300, "5 دقائق"
    df = get_cached_df(pair, tf_seconds, 60)
    if df is None or len(df) < 55:
        logger.warning(f"⛔ {pair}: لا يوجد بيانات")
        return None

    # ========== تحسين 1: Regime Detection (مثل Quantum) ==========
    regime = detect_market_regime(pair)
    if regime == "ranging":
        logger.info(f"🛑 {pair}: Original — سوق عرضي (مرفوض)")
        return None
    
    # ========== تحسين 2: HTF Analysis متقدم (مش بس Trend) ==========
    htf = get_htf_market_regime(pair)  # استخدم الدالة المتقدمة اللي موجودة
    htf_trend = htf.get("trend") if htf else None
    htf_structure = htf.get("structure") if htf else None

    curr = df.iloc[-2]
    prev = df.iloc[-3]

    price = curr['Close']
    alma9, alma50 = curr['ALMA_9'], curr['ALMA_50']
    rsi, stoch_k, stoch_d = curr['RSI'], curr['Stoch_K'], curr['Stoch_D']
    volume, vol_ma = curr['Volume'], curr['Vol_MA']
    roc = curr['ROC']

    atr = calculate_atr_wilder(df, 14)
    atr_series = calculate_atr_series(df, 14)
    adx, _, _ = calculate_adx(df, 14)
    bbw = bollinger_bandwidth(df, 20)
    
        # ========== تحسين 4: Market Structure ==========
    df = detect_swings(df, window=2)
    structure, _, _ = get_market_structure(df, lookback=30)

    # ========== تحسين 3: Smart S/R Levels (بدل Fractals) ==========
    sup_levels, res_levels = get_smart_sr_levels(df, lookback=30)

    # تحديد الاتجاه
    potential_direction = None
    a9p, a50p = prev['ALMA_9'], prev['ALMA_50']
    a9c, a50c = alma9, alma50
    bullish_cross = (a9p <= a50p) and (a9c > a50c)
    bearish_cross = (a9p >= a50p) and (a9c < a50c)

    # شروط أقوى: أضفنا stoch_k > 50 للـ CALL و < 50 للـ PUT
    if bullish_cross and stoch_k > stoch_d and stoch_k > 50:
        potential_direction = "CALL"
    elif bearish_cross and stoch_k < stoch_d and stoch_k < 50:
        potential_direction = "PUT"
    elif price > alma9 and stoch_k > stoch_d and rsi <= 65 and stoch_k > 50:
        potential_direction = "CALL"
    elif price < alma9 and stoch_k < stoch_d and rsi >= 35 and stoch_k < 50:
        potential_direction = "PUT"

    if potential_direction is None:
        return None

    # ========== تحسين 5: HTF Filter أقوى ==========
    if htf_trend is not None and htf_trend != potential_direction:
        logger.info(f"🛑 {pair}: HTF Trend عكسي")
        return None
    
    # Structure عكسي = رفض
    if htf_structure:
        if (potential_direction == "CALL" and htf_structure == "BEARISH") or \
           (potential_direction == "PUT" and htf_structure == "BULLISH"):
            logger.info(f"🛑 {pair}: HTF Structure عكسي")
            return None

    # ========== تحسين 6: فلاتر صارمة ==========
    atr_avg = atr_series.tail(20).mean()
    if atr < atr_avg * 0.4:  # رفع من 0.5
        logger.info(f"🛑 {pair}: تقلب منخفض (ATR < avg*0.7)")
        return None
    
    # فلتر تقلب عالي: تحذير فقط مش رفض
    if atr > atr_avg * 3.5:
        logger.info(f"⚠️ {pair}: تقلب عالي (ATR > avg*3.5) — مستمر بحذر")

    if curr['Volume'] <= vol_ma * 1.2:  # رفع من 1.5
        logger.info(f"🛑 {pair}: حجم ضعيف (Vol < MA*1.8)")
        return None

    # ========== تحسين 7: تقييم محسّن ==========
    near_sup = any(abs(price - level) <= price * 0.0005 for level in sup_levels)
    near_res = any(abs(price - level) <= price * 0.0005 for level in res_levels)

    score, reasons = evaluate_signal_strength_enhanced(
        potential_direction, curr, prev, df, price, alma9, alma50,
        stoch_k, stoch_d, rsi, volume, vol_ma, atr, adx, bbw, roc,
        near_sup, near_res, structure=structure, htf_regime=htf
    )

    if score < 70:
        logger.info(f"🛑 {pair}: مرفوضة — Score={score} < 70")
        return None

    if score >= 95:
        strength = 6
    elif score >= 90:
        strength = 5
    elif score >= 85:
        strength = 4
    elif score >= 80:
        strength = 3
    else:
        strength = 2

    signal_name_ar, signal_name_en = SIGNAL_NAMES[strength]
    emoji = SIGNAL_EMOJIS[strength]
    da = "صعود (CALL)" if potential_direction == "CALL" else "هبوط (PUT)"

    # ===== نظام 3 مراحل (Early Alert → Confirmation → Final) =====
    iq_now = get_iq_time()
    csec = int(iq_now) % 300

    if 270 <= csec <= 280:
        with data_lock:
            if pair not in state.alerted_pairs:
                state.pending_alerts[pair] = {
                    'direction': potential_direction,
                    'strength': strength,
                    'signal_name': signal_name_ar,
                    'score': strength * 16,
                    'alert_time': iq_now,
                    'strategy': 'original'
                }
                indicator_counts = get_indicator_counts(pair, df)
                htf_data = get_htf_market_regime(pair)
                send_early_alert(pair, potential_direction, signal_name_ar, score, 'original', regime=regime, htf_data=htf_data, indicator_counts=indicator_counts)
                state.alerted_pairs[pair] = (potential_direction, iq_now)
        return None

    if 280 <= csec <= 299:

        with data_lock:
            pending = state.pending_alerts.get(pair)

        if pending and pending['direction'] != potential_direction:
            send_cancelled_alert(pair, pending['direction'], "الاتجاه تغير", 'original')
            with data_lock:
                if pair in state.pending_alerts:
                    del state.pending_alerts[pair]
            logger.info(f"🛑 {pair}: إلغاء — الاتجاه تغير")
            return None

        ok, reason = passes_common_entry_filters(pair)
        if not ok:
            if pending:
                send_cancelled_alert(pair, potential_direction, reason, 'original')
            with data_lock:
                if pair in state.pending_alerts:
                    del state.pending_alerts[pair]
            logger.info(f"🛑 {pair}: إلغاء ({reason})")
            return None

        # فحص جودة الشمعة مرة أخرى
        body = abs(curr['Close'] - curr['Open'])
        rng = curr['High'] - curr['Low']
        if rng == 0:
            return None
        body_pct = body / rng
        if body_pct < 0.45:
            if pending:
                send_cancelled_alert(pair, potential_direction, f"شمعة ضعيفة ({body_pct:.1%})", 'original')
            with data_lock:
                if pair in state.pending_alerts:
                    del state.pending_alerts[pair]
            logger.info(f"🛑 {pair}: إلغاء — شمعة ضعيفة ({body_pct:.2%})")
            return None

        if not can_take_signal(pair, potential_direction):
            if pending:
                send_cancelled_alert(pair, potential_direction, "إشارة معاكسة حديثة", 'original')
            with data_lock:
                if pair in state.pending_alerts:
                    del state.pending_alerts[pair]
            logger.info(f"🛑 {pair}: إلغاء — إشارة معاكسة")
            return None

        if csec >= 293:
            if already_sent_this_candle(pair):
                logger.info(f"⛔ {pair}: تم الإرسال مسبقاً")
                return None
            with data_lock:
                if pair in state.alerted_pairs:
                    del state.alerted_pairs[pair]
                if pair in state.pending_alerts:
                    del state.pending_alerts[pair]

            indicators_str = f"ADX={adx:.1f} | BBW={bbw:.4f} | RSI={rsi:.1f} | Reasons: {', '.join(reasons[:3])}"
            indicator_counts = get_indicator_counts(pair, df)
            htf_data = get_htf_market_regime(pair)
            final_signal = send_final_signal(
                pair, potential_direction, signal_name_ar, score,
                duration_text, indicators_str, 'original', regime=regime, signal_level=strength, htf_data=htf_data, indicator_counts=indicator_counts
            )

            if final_signal is None:
                logger.info(f"⛔ {pair}: تم إرسالها مسبقاً (منع التكرار)")
                return None

            with data_lock:
                state.recent_signals[pair] = (get_iq_time(), potential_direction)

            new_trade = _build_trade_dict(
                pair=pair, direction=potential_direction, entry_price=curr['Close'],
                expire_offset=300, is_king=False, is_martingale=False,
                signal_level=strength, signal_name=signal_name_ar, score=score,
                filters={
                    'alma_cross': (a9p <= a50p and a9c > a50c) if potential_direction == "CALL" else (a9p >= a50p and a9c < a50c),
                    'price_above_alma': price > alma9 if potential_direction == "CALL" else price < alma9,
                    'stoch_aligned': stoch_k > stoch_d if potential_direction == "CALL" else stoch_k < stoch_d,
                    'rsi_zone': (28 <= rsi <= 65) if potential_direction == "CALL" else (35 <= rsi <= 72),
                    'near_sr': near_sup if potential_direction == "CALL" else near_res,
                    'volume_ok': volume >= vol_ma * 1.5,
                    'adx_ok': adx >= 20,
                    'bbw_ok': bbw >= 0.001,
                    'atr_ok': atr >= (price * 0.00025),
                    'structure_ok': structure in ["BULLISH", "BEARISH"]
                },
                indicators={
                    'adx': float(adx), 'rsi': float(rsi), 'bbw': float(bbw),
                    'atr': float(atr), 'roc': float(roc),
                    'stoch_k': float(stoch_k), 'stoch_d': float(stoch_d),
                    'structure': str(structure) if structure else 'None',
                    'reasons': reasons
                },
                strategy='original'
            )

            if add_trade_atomic(new_trade):
                logger.info(f"✅ {pair}: {signal_name_ar} تم الإرسال (Level={strength} | Score={score})")
                return final_signal
            else:
                logger.info(f"🛑 {pair}: مرفوضة (مكررة)")
                return None
        else:
            logger.info(f"⏳ {pair}: في انتظار التأكيد ({csec}s)")
            return None

    return None

def analyze_pair_wrapper(pair):
    try:
        return pair, analyze_pair(pair, "5m")
    except Exception as e:
        logger.error(f"خطأ في {pair}: {e}")
        return pair, None

# ========== analyze_pair_king (King Strategy - 3-Stage Arabic) ==========

def analyze_pair_king(pair, timeframe="5m"):
    tf_seconds, duration_text = 300, "5 دقائق"
    settings = get_settings_for_pair(pair)
    adx_threshold = settings.get("adx_threshold", 22)
    rsi_low_call = settings.get("rsi_low_call", 30)
    rsi_high_call = settings.get("rsi_high_call", 50)
    rsi_low_put = settings.get("rsi_low_put", 50)
    rsi_high_put = settings.get("rsi_high_put", 70)
    sweep_threshold = settings.get("sweep_threshold", 0.0003)
    body_pct_min = settings.get("body_pct_min", 0.60)
    market_type = "otc" if is_otc_pair(pair) else "live"

    df = get_cached_df_king(pair, tf_seconds, 80)
    if df is None or len(df) < 60:
        logger.info(f"🛑 King {pair}: لا يوجد بيانات")
        return None

    regime = detect_market_regime(pair)

    df = detect_swings(df, window=2)
    structure, last_sh_idx, last_sl_idx = get_market_structure(df, lookback=30)
    
    if structure == "NEUTRAL":
        adx_check, _, _ = calculate_adx(df, 14)
        if adx_check < 10:
            logger.info(f"🛑 King {pair}: NEUTRAL و ADX={adx_check:.1f} < 10")
            return None
        else:
            logger.info(f"ℹ️ King {pair}: NEUTRAL لكن ADX={adx_check:.1f} >= 10")

    potential_direction = "CALL" if structure == "BULLISH" else "PUT"

    df['ALMA_20'] = calculate_alma(df['Close'], 20, 0.85, 6)
    df['ALMA_80'] = calculate_alma(df['Close'], 80, 0.85, 6)
    df['RSI'] = wilder_rsi(df['Close'], 14)
    df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)
    df['ROC'] = calculate_roc(df['Close'], 5)

    curr = df.iloc[-2]
    prev = df.iloc[-3]
    price = curr['Close']
    alma20 = curr['ALMA_20']
    alma80 = curr['ALMA_80']
    rsi = curr['RSI']
    stoch_k = curr['Stoch_K']
    stoch_d = curr['Stoch_D']
    roc = curr['ROC']

    atr_series = calculate_atr_series(df, 14)
    atr = atr_series.iloc[-1]
    atr_avg = atr_series.tail(20).mean()
    adx, plus_di, minus_di = calculate_adx(df, 14)
    bbw = bollinger_bandwidth(df, 20)
    sup_levels, res_levels = get_smart_sr_levels(df, lookback=30)

    sweep_ok, sweep_level = detect_liquidity_sweep(df, potential_direction, sweep_threshold=sweep_threshold)
    if not sweep_ok:
        if adx < 12:
            logger.info(f"🛑 King {pair}: لا يوجد Sweep و ADX={adx:.1f} < 12")
            return None
        else:
            logger.info(f"ℹ️ King {pair}: لا يوجد Sweep لكن ADX={adx:.1f} >= 12 — مستمر (+10pts)")

    trend_ok = (potential_direction == "CALL" and alma20 > alma80) or (potential_direction == "PUT" and alma20 < alma80)
    momentum_ok = (potential_direction == "CALL" and roc > -0.3) or (potential_direction == "PUT" and roc < 0.3)
    volatility_ok = (atr_avg * 0.5 <= atr <= atr_avg * 3.0) if atr_avg > 0 else True
    
    min_atr = price * 0.0001
    if atr < min_atr:
        logger.info(f"🛑 King {pair}: ATR={atr:.5f} < {min_atr:.5f}")
        return None

    adx_ok = adx >= 15

    if potential_direction == "CALL":
        rsi_ok = 25 <= rsi <= 55
    else:
        rsi_ok = 45 <= rsi <= 75

    if potential_direction == "CALL":
        stoch_ok = stoch_k >= stoch_d
    else:
        stoch_ok = stoch_k <= stoch_d

    candle_ok, body_pct = check_king_candle_quality(curr)
    if body_pct < 0.45:
        logger.info(f"🛑 King {pair}: body_pct < 0.45")
        return None

    near_sr = False
    if potential_direction == "CALL":
        for level in sup_levels:
            if abs(price - level) <= price * 0.0007:
                near_sr = True
                break
    else:
        for level in res_levels:
            if abs(price - level) <= price * 0.0007:
                near_sr = True
                break

    score = calculate_king_score(
        structure_ok=(structure in ["BULLISH", "BEARISH"]),
        sweep_ok=sweep_ok, trend_ok=trend_ok, momentum_ok=momentum_ok,
        volatility_ok=volatility_ok, adx_ok=adx_ok, rsi_ok=rsi_ok,
        stoch_ok=stoch_ok, candle_ok=candle_ok
    )

    level = get_adaptive_king_level(score, market_type=market_type)
    if level == 0:
        logger.info(f"🛑 King {pair}: Score={score} < 70")
        return None

    htf_trend = get_king_htf_trend(pair)
    if htf_trend is not None and htf_trend != potential_direction:
        logger.warning(f"⚠️ King HTF عكسي لـ {pair} لكن الإشارة مستمرة")

    is_trending = (potential_direction == "CALL" and htf_trend == "CALL") or \
                  (potential_direction == "PUT" and htf_trend == "PUT")
    trend_tag = " 🌊 سوق متجه" if is_trending else ""

    pair_key = f"{pair}_king_5m"
    iq_now = get_iq_time()
    csec = int(iq_now) % 300

    signal_name_ar, signal_name_en = KING_SIGNAL_NAMES[level]
    emoji = KING_EMOJIS[level]
    da = "صعود (CALL)" if potential_direction == "CALL" else "هبوط (PUT)"

    if 270 <= csec <= 280:
        with data_lock:
            if pair_key not in state.king_alerted_pairs:
                state.pending_alerts[f"king_{pair}"] = {
                    'direction': potential_direction,
                    'level': level,
                    'signal_name': signal_name_ar,
                    'score': score,
                    'alert_time': iq_now,
                    'strategy': 'king'
                }
                indicator_counts = get_indicator_counts(pair, df)
                htf_data = get_htf_market_regime(pair)
                send_early_alert(pair, potential_direction, signal_name_ar, score, 'king', regime=regime, htf_data=htf_data, indicator_counts=indicator_counts)
                state.king_alerted_pairs[pair_key] = (potential_direction, iq_now)
        return None

    if 280 <= csec <= 299:

        with data_lock:
            pending = state.pending_alerts.get(f"king_{pair}")

        if pending and pending['direction'] != potential_direction:
            send_cancelled_alert(pair, pending['direction'], "الاتجاه تغير", 'king')
            with data_lock:
                if f"king_{pair}" in state.pending_alerts:
                    del state.pending_alerts[f"king_{pair}"]
            logger.info(f"🛑 King {pair}: إلغاء — الاتجاه تغير")
            return None

        ok, reason = passes_common_entry_filters(pair)
        if not ok:
            if pending:
                send_cancelled_alert(pair, potential_direction, reason, 'king')
            with data_lock:
                if f"king_{pair}" in state.pending_alerts:
                    del state.pending_alerts[f"king_{pair}"]
            logger.info(f"🛑 King {pair}: إلغاء ({reason})")
            return None

        with data_lock:
            if pair_key in state.king_alerted_pairs:
                del state.king_alerted_pairs[pair_key]

        if csec >= 293:
            if already_sent_this_candle_king(pair):
                logger.info(f"🛑 King {pair}: تم الإرسال مسبقاً")
                return None
            with data_lock:
                if f"king_{pair}" in state.pending_alerts:
                    del state.pending_alerts[f"king_{pair}"]

            new_trade = _build_trade_dict(
                pair=pair, direction=potential_direction, entry_price=curr['Close'],
                expire_offset=300, is_king=True, is_martingale=False,
                signal_level=level, signal_name=signal_name_ar, score=score,
                filters={
                    'structure_ok': structure in ["BULLISH", "BEARISH"],
                    'sweep_ok': sweep_ok, 'trend_ok': trend_ok,
                    'momentum_ok': momentum_ok, 'volatility_ok': volatility_ok,
                    'adx_ok': adx_ok, 'rsi_ok': rsi_ok, 'stoch_ok': stoch_ok,
                    'candle_ok': candle_ok
                },
                indicators={
                    'adx': float(adx), 'rsi': float(rsi), 'roc': float(roc),
                    'atr': float(atr), 'bbw': float(bbw),
                    'stoch_k': float(stoch_k), 'stoch_d': float(stoch_d),
                    'sweep_threshold_used': float(sweep_threshold)
                },
                strategy='king'
            )

            if not add_trade_atomic(new_trade):
                logger.info(f"🛑 King {pair}: مكررة")
                return None

            indicator_counts = get_indicator_counts(pair, df)
            indicators_str = f"Score={score}/100 | ADX={adx:.1f} | RSI={rsi:.1f}"
            htf_data = get_htf_market_regime(pair)
            final_signal = send_final_signal(
                pair, potential_direction, signal_name_ar, score,
                duration_text, indicators_str, 'king', regime=regime, htf_data=htf_data, indicator_counts=indicator_counts
            )
            
            if final_signal is None:
                logger.info(f"⛔ King {pair}: تم إرسالها مسبقاً (منع التكرار)")
                return None
                
            logger.info(f"👑 King {pair}: {signal_name_ar} تم الإرسال")
            return final_signal
        else:
            logger.info(f"⏳ King {pair}: في انتظار التأكيد ({csec}s)")
            return None

    return None

def analyze_pair_wrapper_king(pair):
    try:
        return pair, analyze_pair_king(pair, "5m")
    except Exception as e:
        logger.error(f"خطأ King في {pair}: {e}")
        return pair, None

# ========== SMC STRATEGY - 3-STAGE ARABIC ==========

def detect_fvg(df):
    fvg_bull, fvg_bear = [], []
    for i in range(2, len(df)):
        if df['Low'].iloc[i] > df['High'].iloc[i-2]:
            fvg_bull.append({'top': df['Low'].iloc[i], 'bottom': df['High'].iloc[i-2], 'idx': i})
        elif df['High'].iloc[i] < df['Low'].iloc[i-2]:
            fvg_bear.append({'top': df['Low'].iloc[i-2], 'bottom': df['High'].iloc[i], 'idx': i})
    return fvg_bull, fvg_bear

def detect_order_blocks(df, lookback=30):
    obs = {'bull': [], 'bear': []}
    recent = df.tail(lookback)
    for i in range(3, len(recent)):
        c, p = recent.iloc[i], recent.iloc[i-1]
        if c['Close'] > c['Open'] and c['Close'] > p['High'] and p['Close'] < p['Open'] and (c['Close']-c['Open']) > abs(p['Close']-p['Open'])*1.5:
            obs['bull'].append({'high': p['High'], 'low': p['Low'], 'idx': i-1})
        elif c['Close'] < c['Open'] and c['Close'] < p['Low'] and p['Close'] > p['Open'] and (c['Open']-c['Close']) > abs(p['Close']-p['Open'])*1.5:
            obs['bear'].append({'high': p['High'], 'low': p['Low'], 'idx': i-1})
    return obs

def detect_breaker_blocks(df, lookback=40):
    df = detect_swings(df, window=2)
    swings = df[df['is_swing_high'] | df['is_swing_low']].tail(lookback)
    breakers = {'bull': [], 'bear': []}
    if len(swings) < 4: return breakers
    for i in range(3, len(swings)):
        s = [swings.iloc[j] for j in range(i-3, i+1)]
        if s[0]['is_swing_low'] and s[1]['is_swing_high'] and s[2]['is_swing_low'] and s[3]['is_swing_high']:
            if s[2]['Low'] < s[0]['Low'] and s[3]['High'] > s[1]['High']:
                breakers['bear'].append({'high': s[2]['High'], 'low': s[2]['Low'], 'idx': swings.index[i-2]})
        elif s[0]['is_swing_high'] and s[1]['is_swing_low'] and s[2]['is_swing_high'] and s[3]['is_swing_low']:
            if s[2]['High'] > s[0]['High'] and s[3]['Low'] < s[1]['Low']:
                breakers['bull'].append({'high': s[2]['High'], 'low': s[2]['Low'], 'idx': swings.index[i-2]})
    return breakers

def analyze_pair_smc(pair, timeframe="5m"):
    tf_seconds, duration_text = 300, "5 دقائق"
    df = get_cached_df_smart(pair, tf_seconds, 100)
    if df is None or len(df) < 80:
        logger.info(f"🛑 SMC {pair}: لا يوجد بيانات")
        return None

    regime = detect_market_regime(pair)

    htf = get_higher_tf_trend(pair)
    mtf = get_king_htf_trend(pair)
    
    if htf is None and mtf is None:
        last10 = df.tail(10)
        avg_close = last10['Close'].mean()
        last_close = last10['Close'].iloc[-1]
        bias = "CALL" if last_close > avg_close else "PUT"
        logger.info(f"ℹ️ SMC {pair}: HTF/MTF None, باستخدام 10 شموع: {bias}")
    elif htf == mtf and htf is not None:
        bias = htf
    else:
        if htf is not None:
            bias = htf
        elif mtf is not None:
            bias = mtf
        else:
            logger.info(f"🛑 SMC {pair}: لا يوجد Bias")
            return None

    df = detect_swings(df, window=2)
    df['ALMA_20'] = calculate_alma(df['Close'], 20, 0.85, 6)
    df['ALMA_50'] = calculate_alma(df['Close'], 50, 0.85, 6)
    df['RSI'] = wilder_rsi(df['Close'], 14)

    curr = df.iloc[-2]
    price = curr['Close']
    rsi = curr['RSI']

    fvg_bull, fvg_bear = detect_fvg(df)
    obs = detect_order_blocks(df)
    breakers = detect_breaker_blocks(df)
    structure, _, _ = get_market_structure(df, lookback=40)

    score = 0
    conf = []

    if (bias == "CALL" and structure == "BULLISH") or (bias == "PUT" and structure == "BEARISH"):
        score += 25; conf.append("Structure")

    sweep_ok, _ = detect_liquidity_sweep(df, bias, 0.0003)
    if sweep_ok: score += 25; conf.append("Sweep")

    ob_hit = False
    target_obs = obs['bull'][-3:] if bias == "CALL" else obs['bear'][-3:]
    for ob in reversed(target_obs):
        if (bias == "CALL" and ob['low'] <= price <= ob['high']*1.001) or (bias == "PUT" and ob['high']*0.999 <= price <= ob['low']):
            ob_hit = True; score += 20; conf.append("OB"); break

    bb_hit = False
    target_bb = breakers['bull'][-2:] if bias == "CALL" else breakers['bear'][-2:]
    for bb in reversed(target_bb):
        if bb['low'] <= price <= bb['high']:
            bb_hit = True; score += 15; conf.append("Breaker"); break

    fvg_hit = False
    target_fvg = fvg_bull[-3:] if bias == "CALL" else fvg_bear[-3:]
    for fvg in reversed(target_fvg):
        if fvg['bottom'] <= price <= fvg['top']:
            fvg_hit = True; score += 15; conf.append("FVG"); break

    if (bias == "CALL" and curr['ALMA_20'] > curr['ALMA_50']) or (bias == "PUT" and curr['ALMA_20'] < curr['ALMA_50']):
        score += 10; conf.append("Trend")

    if (bias == "CALL" and 30 <= rsi <= 50) or (bias == "PUT" and 50 <= rsi <= 70):
        score += 10; conf.append("RSI")

    if score < 70:
        logger.info(f"🛑 SMC {pair}: Score={score} < 80")
        return None

    if not (sweep_ok or ob_hit or bb_hit or fvg_hit):
        logger.info(f"🛑 SMC {pair}: لا يوجد Sweep/OB/Breaker/FVG")
        return None

    if score >= 95:
        level, name = 4, "SMC Elite 🏆"
    elif score >= 85:
        level, name = 3, "SMC Gold 🥇"
    elif score >= 80:
        level, name = 2, "SMC Silver 🥈"
    else:
        level, name = 1, "SMC Bronze 🥉"
    
    emoji = SMC_EMOJIS[level]

    iq_now = get_iq_time()
    csec = int(iq_now) % 300

    if 270 <= csec <= 280:
        with data_lock:
            pair_key = f"smart_{pair}"
            if pair_key not in state.smart_alerted_pairs:
                state.pending_alerts[f"smart_{pair}"] = {
                    'direction': bias,
                    'level': level,
                    'signal_name': name,
                    'score': score,
                    'alert_time': iq_now,
                    'strategy': 'smart'
                }
                indicator_counts = get_indicator_counts(pair, df)
                htf_data = get_htf_market_regime(pair)
                send_early_alert(pair, bias, name, score, 'smart', regime=regime, htf_data=htf_data, indicator_counts=indicator_counts)
                state.smart_alerted_pairs[pair_key] = iq_now
        return None

    if not (280 <= csec <= 299):
        logger.info(f"🛑 SMC {pair}: الوقت غير مناسب ({csec})")
        return None


    with data_lock:
        pending = state.pending_alerts.get(f"smart_{pair}")

    if pending and pending['direction'] != bias:
        send_cancelled_alert(pair, pending['direction'], "الاتجاه تغير", 'smart')
        with data_lock:
            if f"smart_{pair}" in state.pending_alerts:
                del state.pending_alerts[f"smart_{pair}"]
        logger.info(f"🛑 SMC {pair}: إلغاء — الاتجاه تغير")
        return None

    ok, reason = passes_common_entry_filters(pair)
    if not ok:
        if pending:
            send_cancelled_alert(pair, bias, reason, 'smart')
        with data_lock:
            if f"smart_{pair}" in state.pending_alerts:
                del state.pending_alerts[f"smart_{pair}"]
        logger.info(f"🛑 SMC {pair}: إلغاء ({reason})")
        return None

    if csec >= 293:
        if already_sent_this_candle_smart(pair):
            logger.info(f"🛑 SMC {pair}: تم الإرسال مسبقاً")
            return None
        with data_lock:
            pair_key = f"smart_{pair}"
            if pair_key in state.smart_alerted_pairs:
                del state.smart_alerted_pairs[pair_key]
            if f"smart_{pair}" in state.pending_alerts:
                del state.pending_alerts[f"smart_{pair}"]

        da = "صعود (CALL)" if bias == "CALL" else "هبوط (PUT)"
        
        new_trade = _build_trade_dict(
            pair=pair,
            direction=bias,
            entry_price=price,
            expire_offset=300,
            is_king=False,
            is_martingale=False,
            signal_level=level,
            signal_name=name,
            score=score,
            filters={
                'structure': structure in ["BULLISH", "BEARISH"],
                'sweep': sweep_ok,
                'ob': ob_hit,
                'breaker': bb_hit,
                'fvg': fvg_hit
            },
            indicators={
                'rsi': float(rsi),
                'score': score,
                'conf': conf,
                'htf': htf,
                'mtf': mtf
            },
            strategy='smart'
        )

        if not add_trade_atomic(new_trade):
            logger.info(f"🛑 SMC {pair}: مكررة")
            return None

        conf_str = ', '.join(conf)
        indicator_counts = get_indicator_counts(pair, df)
        indicators_str = f"Score={score}/100 | Factors: {conf_str}"
        htf_data = get_htf_market_regime(pair)
        final_signal = send_final_signal(
            pair, bias, name, score,
            duration_text, indicators_str, 'smart', regime=regime, htf_data=htf_data, indicator_counts=indicator_counts
        )
        
        if final_signal is None:
            logger.info(f"⛔ SMC {pair}: تم إرسالها مسبقاً (منع التكرار)")
            return None
            
        logger.info(f"🏆 SMC {pair}: {name} تم الإرسال")
        return final_signal
    else:
        logger.info(f"⏳ SMC {pair}: في انتظار التأكيد ({csec}s)")
        return None

def analyze_pair_wrapper_smc(pair):
    try:
        return pair, analyze_pair_smc(pair, "5m")
    except Exception as e:
        logger.error(f"خطأ SMC في {pair}: {e}")
        return pair, None

# ========== PRO STRATEGY - 3-STAGE ARABIC ==========

def analyze_pair_pro(pair, timeframe="5m"):
    tf_seconds, duration_text = 300, "5 دقائق"
    
    df = get_cached_df_king(pair, tf_seconds, 60)
    if df is None or len(df) < 40:
        logger.info(f"🛑 Pro {pair}: لا يوجد بيانات")
        return None

    regime = detect_market_regime(pair)
    
    df = detect_swings(df, window=2)
    structure, _, _ = get_market_structure(df, lookback=30)
    if structure == "NEUTRAL":
        # Allow NEUTRAL if we have at least some swing points
        recent_swings = df.tail(30)
        sh_count = len(recent_swings[recent_swings['is_swing_high']])
        sl_count = len(recent_swings[recent_swings['is_swing_low']])
        if sh_count < 1 or sl_count < 1:
            logger.info(f"🛑 Pro {pair}: Structure NEUTRAL ولا يوجد قمم/قيعان")
            return None
        logger.info(f"ℹ️ Pro {pair}: Structure NEUTRAL لكن يوجد {sh_count} قمة و {sl_count} قاع — مستمر")
    
    recent = df.tail(30)
    highs = recent[recent['is_swing_high']]['High'].values
    lows = recent[recent['is_swing_low']]['Low'].values
    if len(highs) < 2 or len(lows) < 2:
        logger.info(f"🛑 Pro {pair}: لا توجد قمم/قيعان كافية")
        return None
    
    last_res = highs[-1]
    last_sup = lows[-1]
    
    curr = df.iloc[-2]
    prev = df.iloc[-3]
    price = curr['Close']
    
    vol_ma = df['Volume'].tail(20).mean()
    vol_ok = curr['Volume'] >= vol_ma * 0.65
    
    body = abs(curr['Close'] - curr['Open'])
    rng = curr['High'] - curr['Low']
    if rng == 0:
        return None
    
    upper_wick = curr['High'] - max(curr['Close'], curr['Open'])
    lower_wick = min(curr['Close'], curr['Open']) - curr['Low']
    
    score = 0
    direction = None
    factors = []
    
    if structure == "BULLISH":
        at_sup = (abs(price - last_sup) <= price * 0.0005) or (curr['Low'] <= last_sup * 1.0003)
        
        if at_sup and lower_wick > body and curr['Close'] > curr['Open']:
            score = 60
            direction = "CALL"
            factors.append("رفض من الدعم")
            
            if lower_wick > upper_wick * 1.5:
                score += 15
                factors.append("ظل قوي")
            if curr['Close'] > prev['High']:
                score += 10
                factors.append("زخم")
            if vol_ok:
                score += 10
                factors.append("حجم")
            if curr['Low'] < last_sup and curr['Close'] > last_sup:
                score += 5
                factors.append("Sweep")
    
    elif structure == "BEARISH":
        at_res = (abs(price - last_res) <= price * 0.0005) or (curr['High'] >= last_res * 0.9997)
        
        if at_res and upper_wick > body and curr['Close'] < curr['Open']:
            score = 60
            direction = "PUT"
            factors.append("رفض من المقاومة")
            
            if upper_wick > lower_wick * 1.5:
                score += 15
                factors.append("ظل قوي")
            if curr['Close'] < prev['Low']:
                score += 10
                factors.append("زخم")
            if vol_ok:
                score += 10
                factors.append("حجم")
            if curr['High'] > last_res and curr['Close'] < last_res:
                score += 5
                factors.append("Sweep")
    
    if direction is None or score < 65:
        logger.info(f"🛑 Pro {pair}: Score={score} < 75 أو لا يوجد اتجاه")
        return None
    
    if score >= 95: level = 4
    elif score >= 85: level = 3
    elif score >= 80: level = 2
    else: level = 1
    
    iq_now = get_iq_time()
    csec = int(iq_now) % 300
    
    name_ar, name_en = PRO_SIGNAL_NAMES[level]
    emoji = PRO_EMOJIS[level]
    da = "صعود (CALL)" if direction == "CALL" else "هبوط (PUT)"
    
    if 270 <= csec <= 280:
        with data_lock:
            if pair not in state.pa_alerted_pairs:
                state.pending_alerts[f"pro_{pair}"] = {
                    'direction': direction,
                    'level': level,
                    'signal_name': name_ar,
                    'score': score,
                    'alert_time': iq_now,
                    'strategy': 'pro'
                }
                indicator_counts = get_indicator_counts(pair, df)
                htf_data = get_htf_market_regime(pair)
                send_early_alert(pair, direction, name_ar, score, 'pro', regime=regime, htf_data=htf_data, indicator_counts=indicator_counts)
                state.pa_alerted_pairs[pair] = iq_now
        return None
    
    if not (280 <= csec <= 299):
        logger.info(f"🛑 Pro {pair}: الوقت غير مناسب ({csec})")
        return None
    
    
    with data_lock:
        pending = state.pending_alerts.get(f"pro_{pair}")
    
    if pending and pending['direction'] != direction:
        send_cancelled_alert(pair, pending['direction'], "الاتجاه تغير", 'pro')
        with data_lock:
            if f"pro_{pair}" in state.pending_alerts:
                del state.pending_alerts[f"pro_{pair}"]
        logger.info(f"🛑 Pro {pair}: إلغاء — الاتجاه تغير")
        return None
    
    ok, reason = passes_common_entry_filters(pair)
    if not ok:
        if pending:
            send_cancelled_alert(pair, direction, reason, 'pro')
        with data_lock:
            if f"pro_{pair}" in state.pending_alerts:
                del state.pending_alerts[f"pro_{pair}"]
        logger.info(f"🛑 Pro {pair}: إلغاء ({reason})")
        return None
    
    if csec >= 293:
        if already_sent_this_candle_pro(pair):
            logger.info(f"🛑 Pro {pair}: تم الإرسال مسبقاً")
            return None
        with data_lock:
            if pair in state.pa_alerted_pairs:
                del state.pa_alerted_pairs[pair]
            if f"pro_{pair}" in state.pending_alerts:
                del state.pending_alerts[f"pro_{pair}"]
    
        with data_lock:
            state.recent_signals[pair] = (get_iq_time(), direction)
    
        new_trade = _build_trade_dict(
            pair=pair, direction=direction, entry_price=curr['Close'],
            expire_offset=300, is_king=False, is_martingale=False,
            signal_level=level, signal_name=name_ar, score=score,
            filters={
                'structure': structure,
                'rejection': True,
                'volume_ok': vol_ok,
                'sweep': (curr['Low'] < last_sup and curr['Close'] > last_sup) if direction == "CALL" else (curr['High'] > last_res and curr['Close'] < last_res)
            },
            indicators={
                'score': score,
                'upper_wick': float(upper_wick),
                'lower_wick': float(lower_wick),
                'body': float(body),
                'volume': float(curr['Volume']),
                'vol_ma': float(vol_ma)
            },
            strategy='pro'
        )
    
        if not add_trade_atomic(new_trade):
            logger.info(f"🛑 Pro {pair}: مكررة")
            return None
    
        indicator_counts = get_indicator_counts(pair, df)
        factors_str = ' | '.join(factors)
        indicators_str = f"Score={score}/100 | {factors_str}"
        htf_data = get_htf_market_regime(pair)
        final_signal = send_final_signal(
            pair, direction, name_ar, score,
            duration_text, indicators_str, 'pro', regime=regime, htf_data=htf_data, indicator_counts=indicator_counts
        )
        
        if final_signal is None:
            logger.info(f"⛔ Pro {pair}: تم إرسالها مسبقاً (منع التكرار)")
            return None
        
        logger.info(f"🔥 Pro {pair}: {name_ar} تم الإرسال")
        return final_signal
    else:
        logger.info(f"⏳ Pro {pair}: في انتظار التأكيد ({csec}s)")
        return None

def analyze_pair_wrapper_pro(pair):
    try:
        return pair, analyze_pair_pro(pair, "5m")
    except Exception as e:
        logger.error(f"خطأ Pro في {pair}: {e}")
        return pair, None

# ========== TRADE HELPERS ==========

def _build_trade_dict(pair, direction, entry_price, expire_offset, is_king, is_martingale,
                      signal_level, signal_name, score, filters, indicators, strategy):
    def convert_bool_to_int(obj):
        if isinstance(obj, dict):
            return {k: convert_bool_to_int(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            return [convert_bool_to_int(item) for item in obj]
        elif isinstance(obj, bool):
            return int(obj)
        else:
            return obj

    filters_clean = convert_bool_to_int(filters)
    indicators_clean = convert_bool_to_int(indicators)

    return {
        'pair': pair,
        'timeframe': '5m',
        'direction': direction,
        'entry_price': entry_price,
        'expire_time': get_iq_time() + expire_offset,
        'warned_loss': False,
        'is_martingale': is_martingale,
        'is_king': is_king,
        'signal_level': signal_level,
        'signal_name': signal_name,
        'score': score,
        'filters': filters_clean,
        'indicators': indicators_clean,
        'hour': datetime.now(CAIRO_TZ).hour,
        'strategy': strategy
    }

def add_trade_atomic(trade_dict):
    pair = trade_dict.get('pair')
    with data_lock:
        for t in state.active_trades:
            if t.get('pair') == pair:
                return False
        state.active_trades.append(trade_dict)
        return True

def check_candle_quality(c, min_body_pct=0.08):
    body = abs(c['Close'] - c['Open'])
    rng = c['High'] - c['Low']
    if rng == 0:
        return False
    bp = body / rng
    if bp < min_body_pct:
        return False
    return True

def can_take_signal(pair, direction):
    with data_lock:
        if pair in state.recent_signals:
            lt, ld = state.recent_signals[pair]
            if get_iq_time() - lt < 600 and ld != direction:
                return False
    return True

def already_sent_this_candle(pair):
    key = f"{pair}_{(int(get_iq_time()) // 300) * 300}"
    with data_lock:
        if key in state.sent_signals:
            return True
        state.sent_signals[key] = get_iq_time()
    return False

def already_sent_this_candle_king(pair):
    key = f"king_{pair}_{(int(get_iq_time()) // 300) * 300}"
    with data_lock:
        if key in state.king_sent_signals:
            return True
        state.king_sent_signals[key] = get_iq_time()
    return False

def already_sent_this_candle_smart(pair):
    key = f"smart_{pair}_{(int(get_iq_time()) // 300) * 300}"
    with data_lock:
        if key in state.smart_sent_signals:
            return True
        state.smart_sent_signals[key] = get_iq_time()
    return False

def already_sent_this_candle_pro(pair):
    key = f"pro_{pair}_{(int(get_iq_time()) // 300) * 300}"
    with data_lock:
        if key in state.pa_sent_signals:
            return True
        state.pa_sent_signals[key] = get_iq_time()
    return False

def already_sent_this_candle_quantum(pair):
    key = f"quantum_{pair}_{(int(get_iq_time()) // 300) * 300}"
    with data_lock:
        if key in state.quantum_sent_signals:
            return True
        state.quantum_sent_signals[key] = get_iq_time()
    return False

# ========== CHECK PAIR DISABLED ==========

def check_pair_disabled(pair):
    now = get_iq_time()
    with data_lock:
        if pair in state.disabled_pairs:
            if now < state.disabled_pairs[pair]:
                return True, f"متوقف حتى {datetime.fromtimestamp(state.disabled_pairs[pair]).strftime('%d/%m %H:%M')}"
            else:
                del state.disabled_pairs[pair]
                logger.info(f"✅ {pair} عاد للعمل")
                return False, None
    return False, None

def update_disabled_pairs():
    try:
        all_trades = read_trade_log(max_entries=10000)
        pair_stats = {}
        for t in all_trades:
            p = t.get("pair", "")
            if p not in pair_stats:
                pair_stats[p] = {"win": 0, "loss": 0, "total": 0}
            if pair_stats[p]["total"] < DISABLE_WINDOW:
                pair_stats[p]["total"] += 1
                if t.get("outcome") == "win":
                    pair_stats[p]["win"] += 1
                else:
                    pair_stats[p]["loss"] += 1
        newly_disabled = []
        with data_lock:
            for pair, stat in pair_stats.items():
                if stat["total"] >= 30:
                    wr = (stat["win"] / stat["total"]) * 100
                    if wr < DISABLE_THRESHOLD and pair not in state.disabled_pairs:
                        disabled_until = get_iq_time() + DISABLE_DURATION
                        state.disabled_pairs[pair] = disabled_until
                        newly_disabled.append((pair, wr))
                        logger.warning(f"🚫 {pair} متوقف — WR: {wr:.1f}% (آخر {stat['total']} صفقة)")
        if newly_disabled:
            msg = "🚫 *توقيف أزواج تلقائي*\n\n"
            for p, wr in newly_disabled:
                msg += f"• `{p}` — WR: {wr:.1f}% (7 أيام)\n"
            send_telegram_message(msg)
    except Exception as e:
        logger.error(f"خطأ في تحديث الأزواج المتوقفة: {e}")

def update_strategy_scores():
    try:
        all_trades = read_trade_log(max_entries=STRATEGY_SCORE_WINDOW * 2)
        for strategy in ["original", "king", "smart", "pro", "quantum"]:
            trades = [t for t in all_trades if t.get("strategy") == strategy]
            if len(trades) >= 20:
                wins = sum(1 for t in trades if t.get("outcome") == "win")
                wr = (wins / len(trades)) * 100
                chunks = [trades[i:i+10] for i in range(0, len(trades), 10)]
                chunk_wrs = []
                for chunk in chunks:
                    if chunk:
                        cw = sum(1 for t in chunk if t.get("outcome") == "win") / len(chunk) * 100
                        chunk_wrs.append(cw)
                stability = 100 - np.std(chunk_wrs) if len(chunk_wrs) > 1 else 50
                score = (wr * 0.6) + (stability * 0.4)
                with data_lock:
                    state.strategy_scores[strategy] = {
                        "win": wins, "loss": len(trades) - wins, "total": len(trades),
                        "wr": round(wr, 1), "stability": round(stability, 1), "score": round(score, 1)
                    }
                logger.info(f"📊 Strategy Score — {strategy}: WR={wr:.1f}%, Score={score:.1f}")
    except Exception as e:
        logger.error(f"خطأ في تحديث Strategy Scores: {e}")

def select_strategy_for_regime(regime):
    return ["original", "king", "smart", "pro", "quantum"]

def calculate_adaptive_threshold(trades, market_type="live"):
    if not ADAPTIVE_THRESHOLD_ENABLED:
        return ADAPTIVE_THRESHOLD_MIN
    market_trades = [t for t in trades if ("-OTC" in t.get("pair", "").upper()) == (market_type == "otc")]
    recent = market_trades[-ADAPTIVE_THRESHOLD_WINDOW:]
    if len(recent) < 50:
        return state.adaptive_thresholds.get(market_type, ADAPTIVE_THRESHOLD_MIN)
    wins = sum(1 for t in recent if t.get("outcome") == "win")
    wr = (wins / len(recent)) * 100
    if wr >= 80:
        threshold = 80
    elif wr >= 70:
        threshold = 85
    elif wr >= 60:
        threshold = 90
    elif wr >= 50:
        threshold = 95
    else:
        threshold = 100
    threshold = max(ADAPTIVE_THRESHOLD_MIN, min(ADAPTIVE_THRESHOLD_MAX, threshold))
    with data_lock:
        state.adaptive_thresholds[market_type] = threshold
    if len(recent) >= 100:
        logger.info(f"📊 Adaptive Threshold [{market_type.upper()}]: WR={wr:.1f}% → Threshold={threshold}")
    return threshold

def get_adaptive_king_level(score, market_type="live"):
    if score >= 90:
        return 4
    elif score >= 85:
        return 3
    elif score >= 80:
        return 2
    elif score >= 70:
        return 1
    return 0

# ========== NEWS FUNCTIONS ==========

def update_news():
    if get_iq_time() - state.last_news_update < 1800:
        return
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=8)
        if r.status_code == 200:
            with data_lock:
                state.news_data = r.json()
                state.last_news_update = get_iq_time()
                state.news_fetch_failed = False
            logger.info(f"✅ تم تحديث الأخبار: {len(state.news_data)} حدث")
            return
    except Exception as e:
        logger.warning(f"⚠️ فشل المصدر الرئيسي للأخبار: {e}")
    try:
        r2 = requests.get("https://forexfactory-api.herokuapp.com/get_this_week", timeout=8)
        if r2.status_code == 200:
            with data_lock:
                state.news_data = r2.json()
                state.last_news_update = get_iq_time()
                state.news_fetch_failed = False
            logger.info("✅ تم جلب الأخبار من المصدر الاحتياطي")
            return
    except Exception as e:
        logger.warning(f"⚠️ فشل المصدر الاحتياطي: {e}")
    with data_lock:
        state.news_fetch_failed = True
    logger.error("❌ فشل المصدران في جلب الأخبار")

def is_news_for_pair(pair):
    day_of_week = datetime.now(CAIRO_TZ).weekday()
    if day_of_week in [5, 6]:
        return False
    update_news()
    with data_lock:
        if state.news_fetch_failed:
            logger.warning("⚠️ الأخبار غير متاحة، الإشارات مستمرة")
            return False
        news_snapshot = state.news_data.copy()
    now = datetime.now(UTC_TZ)
    for ev in news_snapshot:
        try:
            impact = str(ev.get('impact','')).upper()
            if impact not in ['HIGH','RED','3']:
                continue
            curr = str(ev.get('country', ev.get('currency', ''))).upper()
            if curr not in CURRENCY_PAIRS or pair not in CURRENCY_PAIRS[curr]:
                continue
            ev_date = ev.get('date')
            et = datetime.fromtimestamp(ev_date, tz=UTC_TZ) if isinstance(ev_date, (int, float)) else pd.to_datetime(ev_date).tz_localize(UTC_TZ)
            diff = abs((now - et).total_seconds())
            if diff <= 900:
                return True
        except Exception:
            continue
    return False

def is_market_open_chaos():
    day_of_week = datetime.now(CAIRO_TZ).weekday()
    if day_of_week in [5, 6]:
        return False
    now = get_cairo_time()
    hm = now.hour * 100 + now.minute
    return (1000 <= hm <= 1030) or (1530 <= hm <= 1600)

def passes_common_entry_filters(pair):
    if is_news_for_pair(pair):
        return False, "فلتر الأخبار"
    if is_market_open_chaos():
        return False, "افتتاح السوق"
    return True, None

# ========== TECHNICAL INDICATORS ==========

def calculate_alma(series, window=9, offset=0.85, sigma=6):
    m = offset * (window - 1)
    s = window / sigma
    w = np.exp(-((np.arange(window) - m) ** 2) / (2 * s * s))
    w /= w.sum()
    return series.rolling(window).apply(lambda x: np.dot(x, w), raw=True)

def wilder_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))
    avg_gain = gain.ewm(alpha=1.0/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_stoch(df, k_period=14, d_period=3):
    low_min = df['Low'].rolling(window=k_period).min()
    high_max = df['High'].rolling(window=k_period).max()
    stoch_k = 100 * ((df['Close'] - low_min) / (high_max - low_min))
    stoch_d = stoch_k.rolling(window=d_period).mean()
    return stoch_k, stoch_d

def calculate_bollinger(series, period=20, std_dev=2):
    sma = series.rolling(window=period).mean()
    std = series.rolling(window=period).std()
    return sma + (std * std_dev), sma - (std * std_dev), sma

def calculate_atr_wilder(df, period=14):
    hl = df['High'] - df['Low']
    hc = (df['High'] - df['Close'].shift()).abs()
    lc = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, min_periods=period).mean().iloc[-1]

def calculate_atr_series(df, period=14):
    hl = df['High'] - df['Low']
    hc = (df['High'] - df['Close'].shift()).abs()
    lc = (df['Low'] - df['Close'].shift()).abs()
    tr = pd.concat([hl, hc, lc], axis=1).max(axis=1)
    return tr.ewm(alpha=1.0/period, min_periods=period).mean()

def calculate_adx(df, period=14):
    plus_dm = df['High'].diff()
    minus_dm = -df['Low'].diff()
    plus_dm = plus_dm.where((plus_dm > minus_dm) & (plus_dm > 0), 0.0)
    minus_dm = minus_dm.where((minus_dm > plus_dm) & (minus_dm > 0), 0.0)
    tr = pd.concat([df['High']-df['Low'], (df['High']-df['Close'].shift()).abs(), (df['Low']-df['Close'].shift()).abs()], axis=1).max(axis=1)
    atr = tr.ewm(alpha=1.0/period, min_periods=period).mean()
    plus_di = 100 * plus_dm.ewm(alpha=1.0/period, min_periods=period).mean() / atr
    minus_di = 100 * minus_dm.ewm(alpha=1.0/period, min_periods=period).mean() / atr
    dx = (abs(plus_di - minus_di) / (plus_di + minus_di)) * 100
    adx = dx.ewm(alpha=1.0/period, min_periods=period).mean()
    return adx.iloc[-1], plus_di.iloc[-1], minus_di.iloc[-1]

def calculate_roc(series, period=5):
    return ((series - series.shift(period)) / series.shift(period)) * 100

def bollinger_bandwidth(df, period=20):
    sma = df['Close'].rolling(window=period).mean()
    std = df['Close'].rolling(window=period).std()
    upper = sma + (std * 2)
    lower = sma - (std * 2)
    return ((upper - lower) / sma).iloc[-1]

def get_fractal_levels(df, lookback=20):
    recent = df.tail(lookback)
    highs = recent['High']
    lows = recent['Low']
    resistance = highs.rolling(window=5, center=True).apply(lambda x: x[2] if max(x) == x[2] else np.nan, raw=True)
    support = lows.rolling(window=5, center=True).apply(lambda x: x[2] if min(x) == x[2] else np.nan, raw=True)
    last_res = resistance.dropna().iloc[-1] if not resistance.dropna().empty else recent['High'].max()
    last_sup = support.dropna().iloc[-1] if not support.dropna().empty else recent['Low'].min()
    return last_res, last_sup



def calculate_supertrend(df, period=10, multiplier=3):
    """حساب Supertrend — أقوى مؤشر لتحديد الاتجاه"""
    hl2 = (df['High'] + df['Low']) / 2
    atr = calculate_atr_series(df, period)
    upper_band = hl2 + (multiplier * atr)
    lower_band = hl2 - (multiplier * atr)

    st = pd.Series(0.0, index=df.index)
    st_dir = pd.Series(1, index=df.index)  # 1 = صاعد, -1 = هابط

    for i in range(1, len(df)):
        if df['Close'].iloc[i] > st.iloc[i-1]:
            st.iloc[i] = max(lower_band.iloc[i], st.iloc[i-1] if st.iloc[i-1] != 0 else lower_band.iloc[i])
            st_dir.iloc[i] = 1
        else:
            st.iloc[i] = min(upper_band.iloc[i], st.iloc[i-1] if st.iloc[i-1] != 0 else upper_band.iloc[i])
            st_dir.iloc[i] = -1

    # إعادة حساب أكثر دقة
    st = pd.Series(0.0, index=df.index)
    st_dir = pd.Series(1, index=df.index)

    for i in range(period, len(df)):
        if df['Close'].iloc[i] > upper_band.iloc[i-1]:
            st_dir.iloc[i] = 1
        elif df['Close'].iloc[i] < lower_band.iloc[i-1]:
            st_dir.iloc[i] = -1
        else:
            st_dir.iloc[i] = st_dir.iloc[i-1]

        if st_dir.iloc[i] == 1:
            st.iloc[i] = max(lower_band.iloc[i], st.iloc[i-1] if i > 0 else lower_band.iloc[i])
        else:
            st.iloc[i] = min(upper_band.iloc[i], st.iloc[i-1] if i > 0 else upper_band.iloc[i])

    return st, st_dir


def calculate_macd(series, fast=12, slow=26, signal=9):
    """حساب MACD"""
    ema_fast = series.ewm(span=fast).mean()
    ema_slow = series.ewm(span=slow).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal).mean()
    histogram = macd_line - signal_line
    return macd_line, signal_line, histogram


def get_ema_alignment(df):
    """تكديس EMAs — أقوى تأكيد للترند"""
    ema9 = df['Close'].ewm(span=9).mean()
    ema21 = df['Close'].ewm(span=21).mean()
    ema50 = df['Close'].ewm(span=50).mean()

    curr = df.iloc[-1]
    e9 = ema9.iloc[-1]
    e21 = ema21.iloc[-1]
    e50 = ema50.iloc[-1]

    # صاعد قوي: السعر > EMA9 > EMA21 > EMA50
    bullish_stack = curr['Close'] > e9 > e21 > e50
    # هابط قوي: السعر < EMA9 < EMA21 < EMA50
    bearish_stack = curr['Close'] < e9 < e21 < e50

    if bullish_stack:
        return "CALL", 1.0
    elif bearish_stack:
        return "PUT", 1.0
    # صاعد ضعيف: EMA9 > EMA21 بس
    elif e9 > e21:
        return "CALL", 0.6
    # هابط ضعيف
    elif e9 < e21:
        return "PUT", 0.6
    else:
        return None, 0.0

def calculate_indicator_votes_for_df(df):
    """حساب أصوات المؤشرات الأربعة: Supertrend, MACD, EMA, ALMA"""
    votes = {"CALL": 0, "PUT": 0, "NEUTRAL": 0}
    if df is None or len(df) < 10:
        return votes

    curr = df.iloc[-1]
    prev = df.iloc[-2] if len(df) > 1 else curr

    # 1. Supertrend
    try:
        st_line, st_dir = calculate_supertrend(df, period=10, multiplier=3)
        if st_dir.iloc[-1] == 1:
            votes["CALL"] += 1
        elif st_dir.iloc[-1] == -1:
            votes["PUT"] += 1
        else:
            votes["NEUTRAL"] += 1
    except Exception:
        votes["NEUTRAL"] += 1

    # 2. MACD
    try:
        macd_line, signal_line, histogram = calculate_macd(df['Close'])
        if macd_line.iloc[-1] > signal_line.iloc[-1]:
            votes["CALL"] += 1
        else:
            votes["PUT"] += 1
    except Exception:
        votes["NEUTRAL"] += 1

    # 3. EMA Alignment
    try:
        ema_trend, _ = get_ema_alignment(df)
        if ema_trend == "CALL":
            votes["CALL"] += 1
        elif ema_trend == "PUT":
            votes["PUT"] += 1
        else:
            votes["NEUTRAL"] += 1
    except Exception:
        votes["NEUTRAL"] += 1

    # 4. ALMA Trend
    try:
        if 'ALMA_9' not in df.columns:
            df['ALMA_9'] = calculate_alma(df['Close'], 9, 0.85, 6)
        if 'ALMA_50' not in df.columns:
            df['ALMA_50'] = calculate_alma(df['Close'], 50, 0.85, 6)
        a9c = df['ALMA_9'].iloc[-1]
        a50c = df['ALMA_50'].iloc[-1]
        a9p = df['ALMA_9'].iloc[-2] if len(df) > 1 else a9c
        a50p = df['ALMA_50'].iloc[-2] if len(df) > 1 else a50c

        if a9c > a50c and a9p > a50p:
            votes["CALL"] += 1
        elif a9c < a50c and a9p < a50p:
            votes["PUT"] += 1
        else:
            votes["NEUTRAL"] += 1
    except Exception:
        votes["NEUTRAL"] += 1

    return votes


def get_indicator_counts(pair, df_5m=None):
    """جمع أصوات المؤشرات من 1H و 5m"""
    counts = {"1H": {"CALL": 0, "PUT": 0, "NEUTRAL": 0}, "5m": {"CALL": 0, "PUT": 0, "NEUTRAL": 0}}

    # 1H
    try:
        candles_1h = get_cached_candles(pair, TIMEFRAME_1H, 80, max_age=300)
        if candles_1h and len(candles_1h) >= 50:
            df_h = pd.DataFrame(candles_1h)
            df_h.rename(columns={'open':'Open','max':'High','min':'Low','close':'Close','volume':'Volume'}, inplace=True)
            counts["1H"] = calculate_indicator_votes_for_df(df_h)
    except Exception:
        pass

    # 5m
    try:
        if df_5m is not None and len(df_5m) >= 10:
            counts["5m"] = calculate_indicator_votes_for_df(df_5m.copy())
        else:
            candles_5m = get_cached_candles(pair, TIMEFRAME_5M, 80, max_age=300)
            if candles_5m and len(candles_5m) >= 50:
                df_5m_new = pd.DataFrame(candles_5m)
                df_5m_new.rename(columns={'open':'Open','max':'High','min':'Low','close':'Close','volume':'Volume'}, inplace=True)
                counts["5m"] = calculate_indicator_votes_for_df(df_5m_new)
    except Exception:
        pass

    return counts



def detect_swings(df, window=2):
    df = df.copy()
    n = len(df)
    swing_high = [False] * n
    swing_low = [False] * n
    for i in range(window, n - window):
        is_high = True
        for j in range(1, window + 1):
            if df['High'].iloc[i] < df['High'].iloc[i - j] or df['High'].iloc[i] < df['High'].iloc[i + j]:
                is_high = False
                break
        if is_high:
            swing_high[i] = True
        is_low = True
        for j in range(1, window + 1):
            if df['Low'].iloc[i] > df['Low'].iloc[i - j] or df['Low'].iloc[i] > df['Low'].iloc[i + j]:
                is_low = False
                break
        if is_low:
            swing_low[i] = True
    df['is_swing_high'] = swing_high
    df['is_swing_low'] = swing_low
    return df

def get_market_structure(df, lookback=30):
    recent = df.tail(lookback).copy()
    sh_idx = recent[recent['is_swing_high']].index.tolist()
    sl_idx = recent[recent['is_swing_low']].index.tolist()
    if len(sh_idx) < 2 or len(sl_idx) < 2:
        return "NEUTRAL", None, None
    sh_vals = [df.loc[i, 'High'] for i in sh_idx[-2:]]
    sl_vals = [df.loc[i, 'Low'] for i in sl_idx[-2:]]
    is_hh = sh_vals[-1] > sh_vals[-2]
    is_hl = sl_vals[-1] > sl_vals[-2]
    is_lh = sh_vals[-1] < sh_vals[-2]
    is_ll = sl_vals[-1] < sl_vals[-2]
    if is_hh and is_hl:
        return "BULLISH", sh_idx[-1], sl_idx[-1]
    elif is_lh and is_ll:
        return "BEARISH", sh_idx[-1], sl_idx[-1]
    return "NEUTRAL", sh_idx[-1] if sh_idx else None, sl_idx[-1] if sl_idx else None

def detect_liquidity_sweep(df, direction, sweep_threshold=0.0003):
    if len(df) < 10:
        return False, None
    if direction == "CALL":
        swing_lows = df[df['is_swing_low']].tail(3)
        if swing_lows.empty:
            return False, None
        for idx, row in swing_lows.iterrows():
            sl_price = row['Low']
            for i in range(max(-3, -len(df)), 0):
                candle = df.iloc[i]
                if candle['Low'] < sl_price * (1 - sweep_threshold):
                    if candle['Close'] > sl_price:
                        return True, sl_price
        return False, None
    else:
        swing_highs = df[df['is_swing_high']].tail(3)
        if swing_highs.empty:
            return False, None
        for idx, row in swing_highs.iterrows():
            sh_price = row['High']
            for i in range(max(-3, -len(df)), 0):
                candle = df.iloc[i]
                if candle['High'] > sh_price * (1 + sweep_threshold):
                    if candle['Close'] < sh_price:
                        return True, sh_price
        return False, None

def get_smart_sr_levels(df, lookback=30, tolerance=0.0002):
    recent = df.tail(lookback)
    highs = recent[recent['is_swing_high']]['High'].values
    lows = recent[recent['is_swing_low']]['Low'].values
    def cluster(values):
        if len(values) == 0:
            return []
        s = sorted(values)
        clusters = [[s[0]]]
        for v in s[1:]:
            if abs(v - clusters[-1][0]) / clusters[-1][0] <= tolerance:
                clusters[-1].append(v)
            else:
                clusters.append([v])
        return [sum(c) / len(c) for c in clusters]
    return cluster(lows), cluster(highs)

def check_king_candle_quality(candle):
    body = abs(candle['Close'] - candle['Open'])
    rng = candle['High'] - candle['Low']
    if rng == 0:
        return False, 0
    body_pct = body / rng
    upper_shadow = candle['High'] - max(candle['Close'], candle['Open'])
    lower_shadow = min(candle['Close'], candle['Open']) - candle['Low']
    shadow_pct = (upper_shadow + lower_shadow) / rng
    return body_pct >= 0.50 and shadow_pct <= 0.40, body_pct

def calculate_king_score(structure_ok, sweep_ok, trend_ok, momentum_ok,
                         volatility_ok, adx_ok, rsi_ok, stoch_ok, candle_ok):
    with data_lock:
        w = dict(KING_WEIGHTS)
    score = 0
    if structure_ok: score += w.get('structure', 20)
    if sweep_ok: score += w.get('sweep', 20)
    elif adx_ok: score += 10  # partial credit if no sweep but ADX ok
    if trend_ok: score += w.get('trend', 15)
    if momentum_ok: score += w.get('momentum', 10)
    if volatility_ok: score += w.get('volatility', 10)
    if adx_ok: score += w.get('adx', 10)
    if rsi_ok: score += w.get('rsi', 5)
    if stoch_ok: score += w.get('stochastic', 5)
    if candle_ok: score += w.get('candle', 5)
    return score

# ========== CACHE FUNCTIONS ==========

def get_cached_candles(pair, tf, count, max_age=30, force_refresh=False):
    key = f"{pair}_{tf}_{count}"

    if not force_refresh:
        data = candles_cache.get(key)
        if data is not None:
            return data

    try:
        with api_lock:
            if API is None:
                return None
            data = API.get_candles(pair, tf, count, int(get_iq_time()))
        if data:
            candles_cache.set(key, data)
        return data
    except Exception as e:
        err_str = str(e).lower()
        if "not found" in err_str or "asset" in err_str:
            with data_lock:
                state.invalid_assets.add(pair)
            logger.warning(f"⚠️ {pair} غير متاح")
        else:
            logger.error(f"خطأ جلب شموع {pair}: {e}")
        return None

def get_cached_df(pair, tf, count):
    key = f"{pair}_{tf}_{count}"
    data = df_cache.get(key)
    if data is not None:
        return data
    raw = get_cached_candles(pair, tf, count, max_age=15)
    if not raw or len(raw) < 55:
        return None
    df = pd.DataFrame(raw)
    df.rename(columns={'open':'Open','max':'High','min':'Low','close':'Close','volume':'Volume'}, inplace=True)
    df['ALMA_9'] = calculate_alma(df['Close'], 9, 0.85, 6)
    df['ALMA_50'] = calculate_alma(df['Close'], 50, 0.85, 6)
    df['RSI'] = wilder_rsi(df['Close'], 14)
    df['BBU'], df['BBL'], df['BB_MID'] = calculate_bollinger(df['Close'], 20, 2)
    df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)
    df['Vol_MA'] = df['Volume'].rolling(window=20).mean()
    df['ROC'] = calculate_roc(df['Close'], 5)
    df_cache.set(key, df)
    return df

def get_cached_df_king(pair, tf, count):
    key = f"king_{pair}_{tf}_{count}"
    data = king_df_cache.get(key)
    if data is not None:
        return data
    raw = get_cached_candles(pair, tf, count, max_age=15)
    if not raw or len(raw) < 60:
        return None
    df = pd.DataFrame(raw)
    df.rename(columns={'open':'Open','max':'High','min':'Low','close':'Close','volume':'Volume'}, inplace=True)
    king_df_cache.set(key, df)
    return df

def get_cached_df_smart(pair, tf, count):
    key = f"smart_{pair}_{tf}_{count}"
    data = smart_df_cache.get(key)
    if data is not None:
        return data
    raw = get_cached_candles(pair, tf, count, max_age=15)
    if not raw or len(raw) < 80:
        return None
    df = pd.DataFrame(raw)
    df.rename(columns={'open':'Open','max':'High','min':'Low','close':'Close','volume':'Volume'}, inplace=True)
    smart_df_cache.set(key, df)
    return df

# ========== HIGHER TIMEFRAME TRENDS ==========

def get_higher_tf_trend(pair):
    with data_lock:
        if pair in state.ht_trend_cache and get_iq_time() - state.ht_trend_cache[pair][1] < 900:
            return state.ht_trend_cache[pair][0]
    try:
        candles = get_cached_candles(pair, TIMEFRAME_1H, 10, max_age=300)
        if not candles or len(candles) < 5:
            return None
        df_h = pd.DataFrame(candles)
        df_h.rename(columns={'close':'Close'}, inplace=True)
        df_h['ALMA_9'] = calculate_alma(df_h['Close'], 9, 0.85, 6)
        df_h['ALMA_50'] = calculate_alma(df_h['Close'], 50, 0.85, 6)
        curr_h = df_h.iloc[-1]
        prev_h = df_h.iloc[-2]
        if curr_h['ALMA_9'] > curr_h['ALMA_50'] and prev_h['ALMA_9'] > prev_h['ALMA_50']:
            trend = "CALL"
        elif curr_h['ALMA_9'] < curr_h['ALMA_50'] and prev_h['ALMA_9'] < prev_h['ALMA_50']:
            trend = "PUT"
        else:
            trend = None
        with data_lock:
            state.ht_trend_cache[pair] = (trend, get_iq_time())
        return trend
    except Exception as e:
        logger.error(f"خطأ HTF {pair}: {e}")
        return None

def get_king_htf_trend(pair):
    key = f"king_htf_{pair}"
    now = time.time()
    with data_lock:
        if key in state.king_htf_cache and now - state.king_htf_cache[key][1] < 900:
            return state.king_htf_cache[key][0]
    try:
        candles = get_cached_candles(pair, TIMEFRAME_1H, 10, max_age=300)
        if not candles or len(candles) < 5:
            return None
        df_h = pd.DataFrame(candles)
        df_h.rename(columns={'close':'Close'}, inplace=True)
        df_h['ALMA_9'] = calculate_alma(df_h['Close'], 9, 0.85, 6)
        df_h['ALMA_50'] = calculate_alma(df_h['Close'], 50, 0.85, 6)
        curr_h = df_h.iloc[-1]
        prev_h = df_h.iloc[-2]
        if curr_h['ALMA_9'] > curr_h['ALMA_50'] and prev_h['ALMA_9'] > prev_h['ALMA_50']:
            trend = "CALL"
        elif curr_h['ALMA_9'] < curr_h['ALMA_50'] and prev_h['ALMA_9'] < prev_h['ALMA_50']:
            trend = "PUT"
        else:
            trend = None
        with data_lock:
            state.king_htf_cache[key] = (trend, now)
        return trend
    except Exception as e:
        logger.error(f"خطأ King HTF {pair}: {e}")
        return None

# ========== TRADE RESULTS CHECK ==========

def check_tie(ep, fp):
    """التعادل: سعر الدخول يساوي سعر الخروج بالضبط"""
    return float(ep) == float(fp)

def check_trade_results():
    current_time = get_iq_time()
    trades_to_remove = []

    with data_lock:
        trades_snapshot = list(state.active_trades)

    for trade in trades_snapshot:
        time_left = trade['expire_time'] - current_time
        pair = trade['pair']
        ep = trade['entry_price']
        direction = trade['direction']
        strategy = trade.get('strategy', 'unknown')
        is_mg = trade.get('is_martingale', False)
        is_king = trade.get('is_king', False)

        try:
            if time_left <= 0:
                candles = get_cached_candles(pair, 300, 5, max_age=0, force_refresh=True)

                if not candles or len(candles) < 2:
                    logger.warning("⏳ " + pair + ": شموع غير كافية للتقييم، هيتم المحاولة في الدورة الجاية")
                    continue

                target_candle = None
                for c in reversed(candles):
                    candle_to = c.get('to', 0)
                    if candle_to <= trade['expire_time'] + 5:
                        target_candle = c
                        break

                if target_candle is None:
                    target_candle = candles[-2] if len(candles) >= 2 else candles[-1]
                    logger.warning("⚠️ " + pair + ": استخدام fallback للشمعة (مش متطابقة بالـ timestamp)")

                fp = target_candle['close']
                candle_to = target_candle.get('to', 0)
                candle_from = target_candle.get('from', 0)

                logger.info(
                    "📊 RESULT DEBUG | " + str(pair) + " | Dir:" + str(direction) + " | "
                    "EP:" + "{:.5f}".format(ep) + " | FP:" + "{:.5f}".format(fp) + " | "
                    "Expire:" + str(trade['expire_time']) + " | "
                    "CandleFrom:" + str(candle_from) + " | CandleTo:" + str(candle_to) + " | "
                    "CurrentTime:" + str(current_time)
                )

                is_tie = check_tie(ep, fp)
                if direction == "CALL":
                    is_win = fp > ep and not is_tie
                else:
                    is_win = fp < ep and not is_tie

                diff_pct = abs(fp - ep) / ep * 100 if ep != 0 else 0
                logger.info(
                    "📊 RESULT | " + str(pair) + " | Win:" + str(is_win) + " | Tie:" + str(is_tie) + " | "
                    "Diff:" + "{:.4f}".format(diff_pct) + "% | Strategy:" + str(strategy)
                )

                ts = get_cairo_time().strftime('%I:%M %p')

                if strategy == 'quantum':
                    with data_lock:
                        state.quantum_stats[pair]['total'] += 1
                        if is_tie:
                            state.quantum_stats[pair]['win'] += 0.5
                        else:
                            state.quantum_stats[pair]['win' if is_win else 'loss'] += 1
                elif strategy == 'smart':
                    with data_lock:
                        state.smart_stats[pair]['total'] += 1
                        if is_tie:
                            state.smart_stats[pair]['win'] += 0.5
                        else:
                            state.smart_stats[pair]['win' if is_win else 'loss'] += 1
                elif strategy == 'pro':
                    with data_lock:
                        state.pro_stats[pair]['total'] += 1
                        if is_tie:
                            state.pro_stats[pair]['win'] += 0.5
                        else:
                            state.pro_stats[pair]['win' if is_win else 'loss'] += 1
                elif is_king:
                    with data_lock:
                        state.king_stats[pair]['total'] += 1
                        if is_tie:
                            state.king_stats[pair]['win'] += 0.5
                        else:
                            state.king_stats[pair]['win' if is_win else 'loss'] += 1
                else:
                    with data_lock:
                        state.stats[pair]['total'] += 1
                        if is_tie:
                            state.stats[pair]['win'] += 0.5
                        else:
                            state.stats[pair]['win' if is_win else 'loss'] += 1

                try:
                    log_trade({
                        "timestamp": get_iq_time(),
                        "pair": pair,
                        "direction": direction,
                        "strategy": strategy,
                        "level": trade.get('signal_level', 0),
                        "score": trade.get('score', 0),
                        "entry_price": float(ep),
                        "exit_price": float(fp),
                        "outcome": "win" if is_win else ("tie" if is_tie else "loss"),
                        "filters": trade.get('filters', {}),
                        "indicators": trade.get('indicators', {}),
                        "hour": trade.get('hour', datetime.now(CAIRO_TZ).hour),
                        "day_of_week": datetime.now(CAIRO_TZ).weekday(),
                        "is_martingale": False,
                        "is_king": is_king,
                        "candle_to": candle_to,
                        "candle_from": candle_from,
                        "expire_time": trade['expire_time']
                    })
                except Exception as e:
                    logger.error("خطأ في تسجيل الصفقة: " + str(e))

                else:
                    pass  # Result messages removed per user request

                trades_to_remove.append(trade)

        except Exception as e:
            logger.error("خطأ في متابعة " + str(pair) + ": " + str(e))
            logger.error(traceback.format_exc())

    if trades_to_remove:
        with data_lock:
            for trade in trades_to_remove:
                if trade in state.active_trades:
                    state.active_trades.remove(trade)

# ========== CONNECTION ==========

def connect_iqoption():
    logger.info("🔌 جاري الاتصال بـ IQ Option...")
    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    delay = 3
    reason = None
    for attempt in range(7):
        check, reason = api.connect()
        if check:
            logger.info("✅ تم الاتصال!")
            api.change_balance(ACCOUNT_TYPE)
            sync_server_time(api)
            return api
        logger.error(f"❌ فشل الاتصال ({attempt+1}/7): {reason}")
        if attempt < 6:
            time.sleep(delay)
            delay = min(delay * 2, 30)
    send_telegram_message(f"❌ *فشل الاتصال نهائياً:* `{reason}`")
    raise ConnectionError("فشل الاتصال")

def _reconnect_worker():
    global API
    try:
        API = connect_iqoption()
    except Exception as e:
        logger.error(f"❌ فشلت إعادة الاتصال: {e}")
    finally:
        with data_lock:
            state.is_reconnecting = False

def check_connection_health():
    global API
    if API is None:
        return
    start = time.time()
    try:
        connected = API.check_connect()
    except Exception:
        connected = False
    if not connected:
        with data_lock:
            already_reconnecting = state.is_reconnecting
            if not already_reconnecting:
                state.is_reconnecting = True
        if not already_reconnecting:
            logger.warning("🔄 تم اكتشاف انقطاع، جاري إعادة الاتصال...")
            threading.Thread(target=_reconnect_worker, daemon=True).start()
    else:
        if (time.time() - start) > 0.5:
            logger.warning("⚠️ زمن استجابة عالي")

# ========== STATS ENGINE WORKER ==========

def stats_engine_worker():
    logger.info("📊 محرك الإحصائيات بدأ")
    last_daily_report = 0
    last_weekly_report = 0
    last_monthly_report = 0
    last_optimization = 0
    last_disabled_check = 0
    last_walk_forward = 0
    last_monte_carlo = 0
    last_adaptive_update = 0

    while not stop_event.is_set():
        try:
            now = get_iq_time()
            now_dt = datetime.fromtimestamp(now, tz=CAIRO_TZ)

            if now - last_adaptive_update > 3600:
                all_trades = read_trade_log(max_entries=10000)
                for market in ["live", "otc"]:
                    old_thresh = state.adaptive_thresholds.get(market, 80)
                    new_thresh = calculate_adaptive_threshold(all_trades, market_type=market)
                    if old_thresh != new_thresh:
                        logger.info(f"📊 Adaptive Threshold [{market.upper()}]: {old_thresh} → {new_thresh}")
                last_adaptive_update = now

            if now_dt.hour == 0 and now - last_daily_report > 3600:
                for market in ["live", "otc"]:
                    day_trades = read_trade_log(max_entries=10000, market_type=market)
                    day_trades = [t for t in day_trades if now - t.get("timestamp", 0) <= 86400]
                    report = generate_report(day_trades, "daily", market_type=market)
                    msg = format_report_message(report)
                    if msg and "لا توجد بيانات" not in msg:
                        send_telegram_message(msg)
                last_daily_report = now
                logger.info("📊 تم إرسال التقرير اليومي")

            if now_dt.weekday() == 5 and now_dt.hour == 0 and now - last_weekly_report > 3600:
                for market in ["live", "otc"]:
                    week_trades = read_trade_log(max_entries=10000, market_type=market)
                    week_trades = [t for t in week_trades if now - t.get("timestamp", 0) <= 604800]
                    report = generate_report(week_trades, "weekly", market_type=market)
                    msg = format_report_message(report)
                    if msg and "لا توجد بيانات" not in msg:
                        send_telegram_message(msg)
                all_trades = read_trade_log(max_entries=10000)
                if len(all_trades) >= 200:
                    generate_and_send_optimization_proposal()
                last_weekly_report = now
                logger.info("📊 تم إرسال التقرير الأسبوعي")

            if now_dt.hour == 1 and now - last_disabled_check > 3600:
                update_disabled_pairs()
                last_disabled_check = now

            if now_dt.day == 1 and now_dt.hour == 0 and now - last_monthly_report > 3600:
                for market in ["live", "otc"]:
                    month_trades = read_trade_log(max_entries=10000, market_type=market)
                    month_trades = [t for t in month_trades if now - t.get("timestamp", 0) <= 2592000]
                    report = generate_report(month_trades, "monthly", market_type=market)
                    msg = format_report_message(report)
                    if msg and "لا توجد بيانات" not in msg:
                        send_telegram_message(msg)
                mc_results = {}
                for market in ["live", "otc"]:
                    market_trades = read_trade_log(max_entries=10000, market_type=market)
                    mc_results[market] = {}
                    for strategy in ["original", "king", "smart", "pro", "quantum"]:
                        mc_result, mc_status = run_monte_carlo(market_trades, strategy=strategy, market_type=market)
                        if mc_result:
                            mc_results[market][strategy] = mc_result
                            mc_msg = format_monte_carlo_message(mc_result)
                            send_telegram_message(mc_msg)
                        else:
                            logger.info(f"📊 Monte Carlo [{market.upper()}/{strategy}]: {mc_status}")
                if mc_results:
                    save_monte_carlo_results(mc_results)
                    summary_msg = format_monte_carlo_summary(mc_results)
                    send_telegram_message(summary_msg)
                last_monthly_report = now
                logger.info("📊 تم إرسال التقرير الشهري + Monte Carlo")

            for market in ["live", "otc"]:
                market_trades = read_trade_log(max_entries=10000, market_type=market)
                if len(market_trades) >= WALK_FORWARD_MIN_TRADES and now - last_walk_forward > 1209600:
                    for strategy in ["original", "king", "smart", "pro", "quantum"]:
                        approved, wf_result, wf_msg = run_walk_forward_validation(
                            market_trades, strategy=strategy, market_type=market
                        )
                        send_telegram_message(
                            f"🔬 *Walk Forward [{market.upper()}/{strategy.upper()}]*\n{wf_msg}"
                        )
                        if approved and wf_result:
                            config = wf_result.get("best_config", {})
                            send_telegram_message(
                                f"📋 *إعدادات مقترحة (مطبقة تلقائياً):*\n"
                                f"   السوق: *{market.upper()}*\n"
                                f"   الاستراتيجية: *{strategy.upper()}*\n"
                                f"   ADX ≥ {config.get('adx', 'N/A')}\n"
                                f"   RSI CALL: {config.get('rsi_low', 'N/A')}–{config.get('rsi_high', 'N/A')}\n"
                                f"   RSI PUT: {100 - config.get('rsi_high', 'N/A')}–{100 - config.get('rsi_low', 'N/A')}\n"
                                f"\n✅ تم حفظها في `settings_{market}.json`"
                            )
                    last_walk_forward = now
                elif len(market_trades) < WALK_FORWARD_MIN_TRADES and now - last_walk_forward > 604800:
                    remaining = WALK_FORWARD_MIN_TRADES - len(market_trades)
                    logger.info(f"⏳ Walk Forward [{market.upper()}]: محتاج {remaining} صفقة أخرى ({len(market_trades)}/{WALK_FORWARD_MIN_TRADES})")
                    last_walk_forward = now

            all_trades = read_trade_log(max_entries=10000)
            if len(all_trades) >= 500 and now - last_optimization > 259200:
                generate_and_send_optimization_proposal()
                last_optimization = now

        except Exception as e:
            logger.error(f"خطأ في محرك الإحصائيات: {e}")
            logger.error(traceback.format_exc())

        stop_event.wait(3600)

# ========== CLEANUP MEMORY ==========

def cleanup_memory():
    now = time.time()
    candles_cache.cleanup()
    df_cache.cleanup()
    king_df_cache.cleanup()
    smart_df_cache.cleanup()
    with data_lock:
        state.sent_signals = {k:v for k,v in state.sent_signals.items() if now - v < 600}
        state.recent_signals = {k:v for k,v in state.recent_signals.items() if now - v[0] < 1200}
        state.king_sent_signals = {k:v for k,v in state.king_sent_signals.items() if now - v < 600}
        state.king_recent_signals = {k:v for k,v in state.king_recent_signals.items() if now - v[0] < 1200}
        state.smart_sent_signals = {k:v for k,v in state.smart_sent_signals.items() if now - v < 600}
        state.pa_sent_signals = {k:v for k,v in state.pa_sent_signals.items() if now - v < 600}
        state.quantum_sent_signals = {k:v for k,v in state.quantum_sent_signals.items() if now - v < 600}
        state.quantum_alerted_pairs = {k:v for k,v in state.quantum_alerted_pairs.items() if isinstance(v, (int, float)) and now - v < 480}
        state.sent_final_signals = {k:v for k,v in state.sent_final_signals.items() if now - v < 600}
        state.pa_alerted_pairs = {k:v for k,v in state.pa_alerted_pairs.items() if isinstance(v, (int, float)) and now - v < 600}
        state.pending_alerts = {k:v for k,v in state.pending_alerts.items() if isinstance(v, dict) and now - v.get('alert_time', 0) < 600}
        state.settings_cache = {k:v for k,v in state.settings_cache.items() if now - v[1] < SETTINGS_CACHE_TTL}
        for k in list(state.alerted_pairs.keys()):
            val = state.alerted_pairs[k]
            if isinstance(val, tuple) and len(val) >= 2:
                if now - val[1] > 480:
                    del state.alerted_pairs[k]
            else:
                del state.alerted_pairs[k]
        for k in list(state.king_alerted_pairs.keys()):
            val = state.king_alerted_pairs[k]
            if isinstance(val, tuple) and len(val) >= 2:
                if now - val[1] > 480:
                    del state.king_alerted_pairs[k]
            else:
                del state.king_alerted_pairs[k]
        for k in list(state.smart_alerted_pairs.keys()):
            val = state.smart_alerted_pairs[k]
            if isinstance(val, (int, float)):
                if now - val > 480:
                    del state.smart_alerted_pairs[k]
            else:
                del state.smart_alerted_pairs[k]
        for k in list(state.hunt_mode_announced.keys()):
            if now - state.hunt_mode_announced[k] > 1200:
                del state.hunt_mode_announced[k]

# ========== PAIRS ==========

def get_pairs_for_today():
    day_of_week = datetime.now(CAIRO_TZ).weekday()
    if day_of_week in [5, 6]:
        return [
            "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "USDCHF-OTC",
            "EURJPY-OTC", "EURGBP-OTC", "AUDCAD-OTC", "GBPJPY-OTC"
        ], "OTC (عطلة)"
    else:
        return [
            "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
            "EURJPY", "EURGBP", "AUDCAD", "AUDJPY", "CADJPY", "EURAUD",
            "GBPJPY", "EURCAD"
        ], "عادي (سوق مفتوح)"

# ========== SHUTDOWN ==========

def on_shutdown():
    logger.warning("🛑 البوت يتوقف...")
    stop_event.set()
    _send_telegram_raw(f"🔴 *تم إيقاف البوت {VERSION}!*")

atexit.register(on_shutdown)

# ========== MAIN RUN ==========

def run_bot():
    global API

    init_log_files()
    API = connect_iqoption()
    
    # ===== تهيئة Quantum System =====
    init_quantum_system()

    pairs, mode_text = get_pairs_for_today()
    current_mode = mode_text

    logger.info(f"🚀 البوت يعمل {VERSION} ({mode_text})...")
    
    send_telegram_message(
        f"🤖 *تم تشغيل البوت {VERSION}!*\n"
        f"📅 {datetime.now(CAIRO_TZ).strftime('%A %d/%m/%Y')}\n"
        f"🌐 الوضع: {mode_text}\n"
        f"📋 الأزواج: {len(pairs)}\n"
        f"📊 *الاستراتيجيات:* الأصلية | King | SMC | Pro | 🧠 Quantum\n"
        f"⏱️ *نظام التنبيهات:* 3 مراحل (تنبيه → تأكيد → إشارة)\n"
        f"🧠 *Quantum Features:* Kalman Filter + Volatility Filter + Self-Learning"
    )

    threading.Thread(target=telegram_worker, daemon=True).start()
    threading.Thread(target=stats_engine_worker, daemon=True).start()
    threading.Thread(target=telegram_reply_worker, daemon=True).start()
    threading.Thread(target=quantum_stats_worker, daemon=True).start()
    logger.info("🧠 تم تشغيل محرك Quantum")
    logger.info("📊 محرك الإحصائيات بدأ")

    executor = ThreadPoolExecutor(max_workers=MAX_WORKERS)

    try:
        while not stop_event.is_set():
            with data_lock:
                state.cycle_count += 1
                current_cycle = state.cycle_count
            cycle_start = time.time()
            try:
                pairs, mode_text = get_pairs_for_today()
                if mode_text != current_mode:
                    current_mode = mode_text
                    logger.info(f"🔄 تبديل الوضع: {mode_text}")
                    send_telegram_message(
                        f"🔄 *تبديل الوضع!*\n"
                        f"📅 {datetime.now(CAIRO_TZ).strftime('%A %d/%m/%Y')}\n"
                        f"🌐 الوضع الجديد: {mode_text}\n"
                        f"📋 الأزواج: {len(pairs)}"
                    )
                    with data_lock:
                        state.invalid_assets.clear()

                check_connection_health()

                with data_lock:
                    reconnecting_now = state.is_reconnecting
                if reconnecting_now or API is None:
                    time.sleep(1)
                    continue

                with data_lock:
                    valid_pairs = [p for p in pairs if p not in state.invalid_assets]
                if len(valid_pairs) < len(pairs):
                    logger.info(f"📋 الأزواج المتاحة: {len(valid_pairs)}/{len(pairs)}")



                active_pairs = []
                disabled_count = 0
                for pair in valid_pairs:
                    is_disabled, reason = check_pair_disabled(pair)
                    if is_disabled:
                        disabled_count += 1
                        if current_cycle % 300 == 0:
                            logger.info(f"🚫 {pair}: {reason}")
                    else:
                        active_pairs.append(pair)

                if disabled_count > 0 and current_cycle % 300 == 0:
                    logger.info(f"📋 متاحة: {len(active_pairs)} | متوقفة: {disabled_count}")

                strategies_to_run = ['original', 'king', 'smart', 'pro', 'quantum']

                if current_cycle % 10 == 0:
                    logger.info(f"🎯 الاستراتيجيات النشطة: {strategies_to_run}")
                    with data_lock:
                        scores_copy = dict(state.strategy_scores)
                    for st, data in scores_copy.items():
                        if data["total"] > 0:
                            logger.info(f"   {st}: Score={data['score']}, WR={data.get('wr', 0)}%")

                if current_cycle % 10 == 0:
                    with data_lock:
                        thresh_copy = dict(state.adaptive_thresholds)
                    for market in ["live", "otc"]:
                        thresh = thresh_copy.get(market, 80)
                        logger.info(f"📊 Adaptive Threshold [{market.upper()}]: {thresh}")

                # ========== ORIGINAL ==========
                if "original" in strategies_to_run:
                    results = list(executor.map(analyze_pair_wrapper, active_pairs))

                    for pair, signal in results:
                        if signal:
                            logger.info(f"✅ إشارة: {pair}")

                # ========== KING ==========
                if "king" in strategies_to_run:
                    king_results = list(executor.map(analyze_pair_wrapper_king, active_pairs))
                    for pair, signal in king_results:
                        if signal:
                            logger.info(f"👑 King Signal: {pair}")

                # ========== SMC ==========
                if "smart" in strategies_to_run:
                    smc_results = list(executor.map(analyze_pair_wrapper_smc, active_pairs))
                    for pair, signal in smc_results:
                        if signal:
                            logger.info(f"🏆 SMC Signal: {pair}")

                # ========== PRO ==========
                if "pro" in strategies_to_run:
                    pro_results = list(executor.map(analyze_pair_wrapper_pro, active_pairs))
                    for pair, signal in pro_results:
                        if signal:
                            logger.info(f"🔥 Pro Signal: {pair}")

                # ========== QUANTUM ==========
                if "quantum" in strategies_to_run:
                    quantum_results = list(executor.map(analyze_pair_wrapper_quantum, active_pairs))
                    for pair, signal in quantum_results:
                        if signal:
                            logger.info(f"🧠 Quantum Signal: {pair}")

                check_trade_results()

                if current_cycle % 10 == 0:
                    cleanup_memory()
                    sync_server_time(API)
                    with data_lock:
                        total_wins = sum(s['win'] for s in state.stats.values())
                        total_loss = sum(s['loss'] for s in state.stats.values())
                        wr = (total_wins / (total_wins + total_loss) * 100) if (total_wins + total_loss) > 0 else 0
                        king_total_wins = sum(s['win'] for s in state.king_stats.values())
                        king_total_loss = sum(s['loss'] for s in state.king_stats.values())
                        king_wr = (king_total_wins / (king_total_wins + king_total_loss) * 100) if (king_total_wins + king_total_loss) > 0 else 0
                        smart_total_wins = sum(s['win'] for s in state.smart_stats.values())
                        smart_total_loss = sum(s['loss'] for s in state.smart_stats.values())
                        smart_wr = (smart_total_wins / (smart_total_wins + smart_total_loss) * 100) if (smart_total_wins + smart_total_loss) > 0 else 0
                        pro_total_wins = sum(s['win'] for s in state.pro_stats.values())
                        pro_total_loss = sum(s['loss'] for s in state.pro_stats.values())
                        pro_wr = (pro_total_wins / (pro_total_wins + pro_total_loss) * 100) if (pro_total_wins + pro_total_loss) > 0 else 0
                        quantum_total_wins = sum(s['win'] for s in state.quantum_stats.values())
                        quantum_total_loss = sum(s['loss'] for s in state.quantum_stats.values())
                        quantum_wr = (quantum_total_wins / (quantum_total_wins + quantum_total_loss) * 100) if (quantum_total_wins + quantum_total_loss) > 0 else 0
                    logger.info(f"📊 دورة #{current_cycle} | الأصلية WR: {wr:.1f}% | King WR: {king_wr:.1f}% | SMC WR: {smart_wr:.1f}% | Pro WR: {pro_wr:.1f}% | Quantum WR: {quantum_wr:.1f}% | الإجمالي: {total_wins+total_loss} | King: {king_total_wins+king_total_loss} | SMC: {smart_total_wins+smart_total_loss} | Pro: {pro_total_wins+pro_total_loss} | Quantum: {quantum_total_wins+quantum_total_loss}")

            except Exception as e:
                logger.error(f"خطأ في الحلقة الرئيسية: {e}")
                logger.error(traceback.format_exc())

            elapsed = time.time() - cycle_start
            sleep_time = max(0.5, 1.5 - elapsed)
            stop_event.wait(sleep_time)
    except KeyboardInterrupt:
        logger.info("تم الإيقاف يدوياً")
    finally:
        executor.shutdown(wait=False)
        on_shutdown()

if __name__ == "__main__":
    init_log_files()
    threading.Thread(target=run_web_server, daemon=True).start()
    run_bot()
