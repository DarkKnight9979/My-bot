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
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor
from flask import Flask
from iqoptionapi.stable_api import IQ_Option
from collections import deque, defaultdict

# ========== إعداد الـ Logging ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- 1. خادم Flask ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Running Successfully!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

logging.getLogger('iqoptionapi').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# --- 2. التوقيت ---
CAIRO_TZ = pytz.timezone('Africa/Cairo')
UTC_TZ = pytz.utc

def get_cairo_time():
    return datetime.now(CAIRO_TZ)

# --- 3. البيانات (بدون Default حقيقي) ---
IQ_EMAIL = os.environ.get("zain1mohamed2425@gmail.com")
IQ_PASSWORD = os.environ.get("ZainMohamed2425@")
ACCOUNT_TYPE = os.environ.get("ACCOUNT_TYPE", "PRACTICE")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8794920089:AAFnRnoudkdPrlMtDaijlaQgczrTkaM0MU4")
CHAT_ID = os.environ.get("CHAT_ID", "1462370563")

if not IQ_EMAIL or not IQ_PASSWORD:
    raise ValueError("❌ يجب تعيين IQ_EMAIL و IQ_PASSWORD في متغيرات البيئة!")
if not TELEGRAM_TOKEN or not CHAT_ID:
    raise ValueError("❌ يجب تعيين TELEGRAM_TOKEN و CHAT_ID في متغيرات البيئة!")

# --- 4. قواميس المتابعة ---
alerted_pairs = {}
active_trades = []
martingale_queue = {}
recent_signals = {}       # منع الإشارات المتعاكسة
sent_signals = {}         # منع التكرار في نفس الشمعة
candles_cache = {}        # cache الشموع
ht_trend_cache = {}       # cache فريم الساعة
df_cache = {}             # cache DataFrame المحسّن
news_data = []
last_news_update = 0

# إحصائيات
cycle_count = 0
stats = defaultdict(lambda: {"win": 0, "loss": 0, "total": 0})

# Telegram Queue
telegram_queue = deque()

# --- 5. دوال المؤشرات (Wilder RSI / ATR) ---
def wilder_rsi(series, period=14):
    delta = series.diff()
    gain = delta.where(delta > 0, 0.0)
    loss = (-delta.where(delta < 0, 0.0))
    avg_gain = gain.ewm(alpha=1.0/period, min_periods=period).mean()
    avg_loss = loss.ewm(alpha=1.0/period, min_periods=period).mean()
    rs = avg_gain / avg_loss
    return 100 - (100 / (1 + rs))

def calculate_alma(series, window=9, offset=0.85, sigma=6):
    m = offset * (window - 1)
    s = window / sigma
    w = np.exp(-((np.arange(window) - m) ** 2) / (2 * s * s))
    w /= w.sum()
    return series.rolling(window).apply(lambda x: np.dot(x, w), raw=True)

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

def get_fractal_levels(df, lookback=20):
    recent = df.tail(lookback)
    highs = recent['High']
    lows = recent['Low']
    resistance = highs.rolling(window=5, center=True).apply(lambda x: x[2] if max(x) == x[2] else np.nan, raw=True)
    support = lows.rolling(window=5, center=True).apply(lambda x: x[2] if min(x) == x[2] else np.nan, raw=True)
    last_res = resistance.dropna().iloc[-1] if not resistance.dropna().empty else recent['High'].max()
    last_sup = support.dropna().iloc[-1] if not support.dropna().empty else recent['Low'].min()
    return last_res, last_sup

def bollinger_bandwidth(df, period=20):
    sma = df['Close'].rolling(window=period).mean()
    std = df['Close'].rolling(window=period).std()
    upper = sma + (std * 2)
    lower = sma - (std * 2)
    return ((upper - lower) / sma).iloc[-1]

# --- 6. Cache & Data Management ---
def get_cached_candles(pair, tf, count, max_age=180):
    key = f"{pair}_{tf}_{count}"
    now = time.time()
    if key in candles_cache:
        data, ts = candles_cache[key]
        if now - ts < max_age:
            return data
    try:
        data = API.get_candles(pair, tf, count, now)
        if data:
            candles_cache[key] = (data, now)
        return data
    except Exception as e:
        logger.error(f"خطأ جلب شموع {pair}: {e}")
        if key in candles_cache:
            return candles_cache[key][0]
        return None

def get_cached_df(pair, tf, count):
    key = f"{pair}_{tf}_{count}"
    now = time.time()
    if key in df_cache and now - df_cache[key][1] < 180:
        return df_cache[key][0]
    raw = get_cached_candles(pair, tf, count, max_age=180)
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
    df_cache[key] = (df, now)
    return df

def cleanup_memory():
    now = time.time()
    global sent_signals, recent_signals, candles_cache, df_cache, ht_trend_cache
    sent_signals = {k:v for k,v in sent_signals.items() if now - v < 600}
    recent_signals = {k:v for k,v in recent_signals.items() if now - v[0] < 1200}
    for k in list(candles_cache.keys()):
        if now - candles_cache[k][1] > 600:
            del candles_cache[k]
    for k in list(df_cache.keys()):
        if now - df_cache[k][1] > 600:
            del df_cache[k]
    for k in list(ht_trend_cache.keys()):
        if now - ht_trend_cache[k][1] > 3600:
            del ht_trend_cache[k]

# --- 7. فلتر الأخبار (ذكي حسب العملة) ---
CURRENCY_PAIRS = {
    'USD': ['EURUSD','GBPUSD','USDJPY','AUDUSD','USDCAD','USDCHF'],
    'EUR': ['EURUSD','EURJPY','EURGBP','EURAUD','EURCAD'],
    'GBP': ['GBPUSD','EURGBP','GBPJPY'],
    'JPY': ['USDJPY','EURJPY','AUDJPY','CADJPY','GBPJPY'],
    'AUD': ['AUDUSD','AUDCAD','AUDJPY','EURAUD'],
    'CAD': ['USDCAD','AUDCAD','CADJPY','EURCAD'],
    'CHF': ['USDCHF']
}

def update_news():
    global news_data, last_news_update
    if time.time() - last_news_update < 1800:
        return
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=10)
        if r.status_code == 200:
            news_data = r.json()
            last_news_update = time.time()
            logger.info(f"✅ أخبار محدثة: {len(news_data)} حدث")
    except Exception as e:
        logger.warning(f"⚠️ فشل الأخبار: {e}")

def is_news_for_pair(pair):
    update_news()
    now = datetime.now(UTC_TZ)
    for ev in news_data:
        try:
            if str(ev.get('impact','')).upper() not in ['HIGH','RED','3']:
                continue
            curr = str(ev.get('country','')).upper()
            if curr not in CURRENCY_PAIRS:
                continue
            if pair not in CURRENCY_PAIRS[curr]:
                continue
            et = datetime.fromtimestamp(ev['date'], tz=UTC_TZ)
            diff = abs((now - et).total_seconds())
            if diff <= 900:
                return True
        except:
            continue
    return False

# --- 8. فلتر افتتاح السوق ---
def is_market_open_chaos():
    now = get_cairo_time()
    hm = now.hour * 100 + now.minute
    if (1000 <= hm <= 1030) or (1530 <= hm <= 1600):
        return True
    return False

# --- 9. فلتر فريم الساعة ---
def get_higher_tf_trend(pair):
    if pair in ht_trend_cache and time.time() - ht_trend_cache[pair][1] < 1800:
        return ht_trend_cache[pair][0]
    try:
        candles = get_cached_candles(pair, 3600, 20, max_age=1800)
        if not candles or len(candles) < 10:
            return None
        df_h = pd.DataFrame(candles)
        df_h.rename(columns={'close':'Close'}, inplace=True)
        df_h['ALMA_9'] = calculate_alma(df_h['Close'], 9, 0.85, 6)
        df_h['ALMA_50'] = calculate_alma(df_h['Close'], 50, 0.85, 6)
        lh = df_h.iloc[-2]
        trend = "CALL" if lh['ALMA_9'] > lh['ALMA_50'] else "PUT" if lh['ALMA_9'] < lh['ALMA_50'] else None
        ht_trend_cache[pair] = (trend, time.time())
        return trend
    except Exception as e:
        logger.error(f"خطأ HTF {pair}: {e}")
        return None

# --- 10. فلاتر الجودة ---
def check_candle_quality(c):
    body = abs(c['Close'] - c['Open'])
    rng = c['High'] - c['Low']
    if rng == 0:
        return False
    bp = body / rng
    if bp < 0.12:
        return False
    up_sh = c['High'] - max(c['Close'], c['Open'])
    lo_sh = min(c['Close'], c['Open']) - c['Low']
    if bp > 0.94 and (up_sh < rng*0.02 or lo_sh < rng*0.02):
        return False
    return True

def can_take_signal(pair, direction):
    if pair in recent_signals:
        lt, ld = recent_signals[pair]
        if time.time() - lt < 600 and ld != direction:
            return False
    return True

def already_sent_this_candle(pair):
    key = f"{pair}_{(int(time.time()) // 300) * 300}"
    if key in sent_signals:
        return True
    sent_signals[key] = time.time()
    return False

# --- 11. Telegram Queue (Thread منفصل) ---
def telegram_worker():
    while True:
        if telegram_queue:
            msg = telegram_queue.popleft()
            _send_telegram_raw(msg)
        time.sleep(0.5)

def _send_telegram_raw(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    for attempt in range(3):
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.ok:
                return True
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            time.sleep(2)
    return False

def send_telegram_message(message):
    telegram_queue.append(message)

# --- 12. إيقاف البوت ---
def on_shutdown():
    logger.warning("البوت يتوقف...")
    _send_telegram_raw("🔴 *تنبيه: تم إيقاف بوت IQ Option!*")

atexit.register(on_shutdown)

# --- 13. الاتصال بـ IQ Option (Exponential Backoff) ---
def connect_iqoption():
    logger.info("🔌 جاري الاتصال...")
    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    delay = 5
    for attempt in range(7):
        check, reason = api.connect()
        if check:
            logger.info("✅ تم الاتصال!")
            api.change_balance(ACCOUNT_TYPE)
            return api
        logger.error(f"❌ فشل الاتصال ({attempt+1}/7): {reason}")
        if attempt < 6:
            time.sleep(delay)
            delay = min(delay * 2, 60)
    send_telegram_message(f"❌ *فشل الاتصال نهائياً:* `{reason}`")
    raise ConnectionError("فشل الاتصال")

API = connect_iqoption()

# --- 14. المضاعفة (شروط أصعب: Super Max فقط) ---
def analyze_martingale(pair, original_direction):
    try:
        df = get_cached_df(pair, 300, 60)
        if df is None or len(df) < 55:
            return None
        last, prev = df.iloc[-2], df.iloc[-3]
        price, open_price, low, high = last['Close'], last['Open'], last['Low'], last['High']
        alma9, alma50 = last['ALMA_9'], last['ALMA_50']
        stoch_k, stoch_d = last['Stoch_K'], last['Stoch_D']
        volume, vol_ma = last['Volume'], last['Vol_MA']
        atr = calculate_atr_wilder(df, 14)
        adx, _, _ = calculate_adx(df, 14)
        bbw = bollinger_bandwidth(df, 20)
        roc = last['ROC']

        is_strong = abs(price - open_price) > ((high - low) * 0.25)
        valid_vol = volume > (vol_ma * 0.9)
        valid_atr = atr >= (price * 0.0003)
        valid_adx = adx >= 20
        valid_bbw = bbw >= 0.0015
        valid_momentum = abs(roc) >= 0.05

        a9p, a50p, a9c, a50c = prev['ALMA_9'], prev['ALMA_50'], alma9, alma50
        smc = (a9p <= a50p) and (a9c > a50c) and (stoch_k > stoch_d) and is_strong and valid_vol and valid_atr and valid_adx and valid_bbw and valid_momentum
        smp = (a9p >= a50p) and (a9c < a50c) and (stoch_k < stoch_d) and is_strong and valid_vol and valid_atr and valid_adx and valid_bbw and valid_momentum

        direction = None
        if smc:
            direction = "CALL"
        elif smp:
            direction = "PUT"

        if direction and direction != original_direction:
            return direction
        return None
    except Exception as e:
        logger.error(f"خطأ تحليل المضاعفة: {e}")
        return None

# --- 15. نتائج الصفقات (مع إحصائيات) ---
def check_trade_results():
    current_time = time.time()
    trades_to_remove = []
    for trade in active_trades:
        time_left = trade['expire_time'] - current_time
        try:
            if 0 < time_left <= 20 and not trade.get('warned_loss', False) and not trade.get('is_martingale', False):
                candles = get_cached_candles(trade['pair'], 300, 1, max_age=5)
                if not candles:
                    continue
                cp, ep, d = candles[-1]['close'], trade['entry_price'], trade['direction']
                losing = (d == "CALL" and cp < ep) or (d == "PUT" and cp > ep)
                if losing:
                    send_telegram_message(f"⏳ *تنبيه مبكر*
الزوج: `{trade['pair']}` [5m]
الصفقة تتجه للخسارة..
🔍 *جاري تحليل فرصة المضاعفة...*")
                    martingale_queue[trade['pair']] = {'original_direction': d, 'entry_price': ep, 'time': time.time()}
                    trade['warned_loss'] = True

            if time_left <= 0:
                time.sleep(1.5)
                candles = get_cached_candles(trade['pair'], 300, 2, max_age=5)
                fp = candles[-2]['close'] if len(candles) >= 2 else candles[-1]['close']
                ep, d = trade['entry_price'], trade['direction']
                is_win = (d == "CALL" and fp > ep) or (d == "PUT" and fp < ep)
                ts = get_cairo_time().strftime('%I:%M %p')
                pair, is_mg = trade['pair'], trade.get('is_martingale', False)
                stats[pair]['total'] += 1
                stats[pair]['win' if is_win else 'loss'] += 1

                if is_mg:
                    msg = f"✅ *نتيجة المضاعفة: رابحة*" if is_win else f"❌ *نتيجة المضاعفة: خاسرة*"
                    msg += f"
الزوج: `{pair}` [5m]
⏰ `{ts}`"
                    send_telegram_message(msg)
                    trades_to_remove.append(trade)
                else:
                    if is_win:
                        send_telegram_message(f"✅ *نتيجة الصفقة: رابحة* 🎯
الزوج: `{pair}` [5m]
⏰ `{ts}`")
                        trades_to_remove.append(trade)
                    else:
                        martingale_queue[pair] = {'original_direction': d, 'entry_price': ep, 'time': time.time()}
                        send_telegram_message(f"❌ *الصفقة خاسرة*
الزوج: `{pair}` [5m]
⏰ `{ts}`

🔍 *جاري تحليل السوق لإيجاد أفضل فرصة مضاعفة...*")
                        trades_to_remove.append(trade)
        except Exception as e:
            logger.error(f"خطأ متابعة {trade['pair']}: {e}")
    for trade in trades_to_remove:
        if trade in active_trades:
            active_trades.remove(trade)

# --- 16. تحليل الزوج (الرئيسي) ---
def analyze_pair(pair, timeframe="5m"):
    tf_seconds, duration_text, expire_delay = 300, "5 دقائق", 300
    df = get_cached_df(pair, tf_seconds, 60)
    if df is None or len(df) < 55:
        return None

    last, prev, curr = df.iloc[-2], df.iloc[-3], df.iloc[-1]
    price, open_price, low, high = last['Close'], last['Open'], last['Low'], last['High']
    alma9, alma50 = last['ALMA_9'], last['ALMA_50']
    rsi, stoch_k, stoch_d = last['RSI'], last['Stoch_K'], last['Stoch_D']
    bbl, bbu, volume, vol_ma = last['BBL'], last['BBU'], last['Volume'], last['Vol_MA']
    roc = last['ROC']

    atr = calculate_atr_wilder(df, 14)
    adx, plus_di, minus_di = calculate_adx(df, 14)
    bbw = bollinger_bandwidth(df, 20)
    resistance, support = get_fractal_levels(df, lookback=20)

    is_strong = abs(price - open_price) > ((high - low) * 0.25)
    valid_vol = volume > (vol_ma * 0.9)
    near_sup = abs(price - support) <= (price * 0.0005) or low <= (bbl * 1.001)
    near_res = abs(price - resistance) <= (price * 0.0005) or high >= (bbu * 0.999)

    pair_key = f"{pair}_5m"
    cn = get_cairo_time()
    cts = cn.strftime('%I:%M %p')
    csec = (cn.minute % 5) * 60 + cn.second
    cmin = cn.minute % 5
    final_signal, direction = None, None

    a9p, a50p, a9c, a50c = prev['ALMA_9'], prev['ALMA_50'], alma9, alma50
    smc = (a9p <= a50p) and (a9c > a50c) and (stoch_k > stoch_d) and is_strong and valid_vol
    smp = (a9p >= a50p) and (a9c < a50c) and (stoch_k < stoch_d) and is_strong and valid_vol

    valid_trend = adx >= 18 and bbw >= 0.001 and atr >= (price * 0.00025)
    valid_momentum = abs(roc) >= 0.03

    if smc and valid_trend and valid_momentum:
        direction, final_signal = "CALL", f"👑 *إشارة سوبر ماكس (SUPER MAX) - تقاطع صاعد* 🔥
الزوج: `{pair}` (IQ Option) [5m]
⏱️ *مدة الصفقة:* {duration_text}
⏰ *وقت الإشارة:* `{cts}`"
    elif smp and valid_trend and valid_momentum:
        direction, final_signal = "PUT", f"👑 *إشارة سوبر ماكس (SUPER MAX) - تقاطع هابط* 🔥
الزوج: `{pair}` (IQ Option) [5m]
⏱️ *مدة الصفقة:* {duration_text}
⏰ *وقت الإشارة:* `{cts}`"

    if not final_signal and is_strong and valid_vol and valid_trend and valid_momentum:
        rsi_call_zone = 40 if adx >= 25 else 45
        rsi_put_zone = 60 if adx >= 25 else 55
        stoch_max_call = 25 if adx >= 25 else 30
        stoch_max_put = 75 if adx >= 25 else 70
        stoch_strong_call = 35 if adx >= 25 else 40
        stoch_strong_put = 65 if adx >= 25 else 60

        if price > alma9 and stoch_k > stoch_d and rsi <= rsi_call_zone and near_sup:
            if stoch_k < stoch_max_call:
                direction, final_signal = "CALL", f"🔥 *إشارة (CALL) - القوة: ماكس*
الزوج: `{pair}` (IQ Option) [5m]
⏱️ *مدة الصفقة:* {duration_text}
⏰ *وقت الإشارة:* `{cts}`"
            elif stoch_k < stoch_strong_call:
                direction, final_signal = "CALL", f"🚀 *إشارة (CALL) - القوة: قوية جداً*
الزوج: `{pair}` (IQ Option) [5m]
⏱️ *مدة الصفقة:* {duration_text}
⏰ *وقت الإشارة:* `{cts}`"
        elif price < alma9 and stoch_k < stoch_d and rsi >= rsi_put_zone and near_res:
            if stoch_k > stoch_max_put:
                direction, final_signal = "PUT", f"🔥 *إشارة (PUT) - القوة: ماكس*
الزوج: `{pair}` (IQ Option) [5m]
⏱️ *مدة الصفقة:* {duration_text}
⏰ *وقت الإشارة:* `{cts}`"
            elif stoch_k > stoch_strong_put:
                direction, final_signal = "PUT", f"📉 *إشارة (PUT) - القوة: قوية جداً*
الزوج: `{pair}` (IQ Option) [5m]
⏱️ *مدة الصفقة:* {duration_text}
⏰ *وقت الإشارة:* `{cts}`"

    if pair in martingale_queue:
        mg = martingale_queue[pair]
        if direction and direction != mg['original_direction']:
            da = "صعود (CALL)" if direction == "CALL" else "هبوط (PUT)"
            send_telegram_message(f"🎯 *فرصة المضاعفة جاهزة!*
الزوج: `{pair}` [5m]
الاتجاه: *{da}*
⏰ `{cts}`

⚡ *جهز الدخول الآن!*")
            active_trades.append({'pair': pair, 'timeframe': '5m', 'direction': direction, 'entry_price': curr['Open'], 'expire_time': time.time() + expire_delay, 'warned_loss': True, 'is_martingale': True})
            del martingale_queue[pair]
            return None
        elif time.time() - mg['time'] > 1200:
            send_telegram_message(f"❌ *تم إلغاء فرصة المضاعفة*
الزوج: `{pair}` [5m]
السبب: لم يتم العثور على إشارة قوية.")
            del martingale_queue[pair]

    crp, crk, crs, cra = curr['Close'], curr['Stoch_K'], curr['RSI'], curr['ALMA_9']
    crd = curr['Stoch_D']
    ca9, ca50 = curr['ALMA_9'], curr['ALMA_50']
    pa9, pa50 = last['ALMA_9'], last['ALMA_50']
    predicted_type = None
    if (pa9 <= pa50 and ca9 > ca50 and crk > crd) or (pa9 >= pa50 and ca9 < ca50 and crk < crd):
        predicted_type = "👑 سوبر ماكس"
    elif crp > cra and crk > crd and crs <= 50:
        if crk < 30: predicted_type = "🔥 ماكس"
        elif crk < 40: predicted_type = "🚀 قوية جداً"
    elif crp < cra and crk < crd and crs >= 50:
        if crk > 70: predicted_type = "🔥 ماكس"
        elif crk > 60: predicted_type = "🚀 قوية جداً"

    hpc = (crp > cra) and (crk <= 40) and (crs <= 50)
    hpp = (crp < cra) and (crk >= 60) and (crs >= 50)

    if cmin == 4 and csec >= 30:
        if hpc and pair_key not in alerted_pairs:
            pt = f" من نوع *{predicted_type}*" if predicted_type else ""
            send_telegram_message(f"⚠️ *تجهّز! فرصة صعود (CALL){pt}* قريبة جداً
الزوج: `{pair}` [5m]
يرجى فتح الشارت وتجهيز الصفقة!")
            alerted_pairs[pair_key] = "CALL"
        elif hpp and pair_key not in alerted_pairs:
            pt = f" من نوع *{predicted_type}*" if predicted_type else ""
            send_telegram_message(f"⚠️ *تجهّز! فرصة هبوط (PUT){pt}* قريبة جداً
الزوج: `{pair}` [5m]
يرجى فتح الشارت وتجهيز الصفقة!")
            alerted_pairs[pair_key] = "PUT"

    if final_signal and csec <= 15:
        if already_sent_this_candle(pair):
            return None
        if is_news_for_pair(pair):
            logger.info(f"🛑 إشارة {pair} مرفوضة (خبر قوي)")
            return None
        if is_market_open_chaos():
            logger.info(f"🛑 إشارة {pair} مرفوضة (افتتاح سوق)")
            return None
        if not check_candle_quality(last):
            logger.info(f"🛑 إشارة {pair} مرفوضة (شمعة ضعيفة)")
            return None
        if pair_key in alerted_pairs:
            del alerted_pairs[pair_key]
        ht = get_higher_tf_trend(pair)
        if ht is not None and ht != direction:
            logger.info(f"🛑 إشارة {pair} مرفوضة (HTF عكس: {ht})")
            send_telegram_message(f"⛔ *تم رفض إشارة {pair}*
السبب: فريم الساعة عكس الاتجاه ({ht})
⏰ `{cts}`")
            return None
        if not can_take_signal(pair, direction):
            logger.info(f"🛑 إشارة {pair} مرفوضة (متعاكسة قريبة)")
            send_telegram_message(f"⛔ *تم رفض إشارة {pair}*
السبب: إشارة متعاكسة على نفس الزوج قبل 10 دقائق
⏰ `{cts}`")
            return None
        recent_signals[pair] = (time.time(), direction)
        active_trades.append({'pair': pair, 'timeframe': '5m', 'direction': direction, 'entry_price': curr['Open'], 'expire_time': time.time() + expire_delay, 'warned_loss': False, 'is_martingale': False})
        return final_signal
    else:
        if pair_key in alerted_pairs:
            prev_alert_dir = alerted_pairs[pair_key]
            if (prev_alert_dir == "CALL" and crk > 60) or (prev_alert_dir == "PUT" and crk < 40):
                send_telegram_message(f"❌ *تم إلغاء التنبيه*
الزوج: `{pair}` [5m]
السبب: الشروط لم تعد متوافقة.")
                del alerted_pairs[pair_key]
    return None

# --- 17. تشغيل البوت (Parallel محدود) ---
def analyze_pair_wrapper(pair):
    try:
        return pair, analyze_pair(pair, "5m")
    except Exception as e:
        logger.error(f"خطأ في {pair}: {e}")
        return pair, None

def run_bot():
    global cycle_count
    pairs = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "EURJPY", "EURGBP", "AUDCAD", "AUDJPY", "CADJPY", "EURAUD", "GBPJPY", "EURCAD"]
    logger.info("🚀 البوت يعمل...")
    send_telegram_message("🤖 *تم تشغيل بوت IQ Option V3!*
⏱️ *الفريم:* 5 دقائق
⚡ *الدخول:* أول 15 ثانية
🔍 *المضاعفة:* Super Max فقط
🛡️ *الفلاتر:* أخبار ذكية + ADX + ATR Wilder + BBW + ROC + جودة الشمعة + HTF + منع متعاكس
💾 *Cache:* مفعل
📊 *إحصائيات:* مفعلة")

    threading.Thread(target=telegram_worker, daemon=True).start()

    try:
        while True:
            cycle_count += 1
            cycle_start = time.time()
            try:
                if not API.check_connect():
                    API.connect()

                with ThreadPoolExecutor(max_workers=7) as executor:
                    results = executor.map(analyze_pair_wrapper, pairs)
                    for pair, signal in results:
                        if signal:
                            logger.info(f"✅ إشارة: {pair}")
                            send_telegram_message(signal)

                check_trade_results()

                if cycle_count % 50 == 0:
                    cleanup_memory()
                    total_wins = sum(s['win'] for s in stats.values())
                    total_loss = sum(s['loss'] for s in stats.values())
                    wr = (total_wins / (total_wins + total_loss) * 100) if (total_wins + total_loss) > 0 else 0
                    logger.info(f"📊 دورة #{cycle_count} | Win Rate: {wr:.1f}% | صفقات: {total_wins+total_loss}")

            except Exception as e:
                logger.error(f"خطأ في الحلقة: {e}")
                logger.error(traceback.format_exc())

            elapsed = time.time() - cycle_start
            sleep_time = max(1, 5 - elapsed)
            time.sleep(sleep_time)
    except KeyboardInterrupt:
        logger.info("تم الإيقاف يدوياً")
    finally:
        on_shutdown()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    run_bot()
