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
from flask import Flask
from iqoptionapi.stable_api import IQ_Option

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

def get_cairo_time():
    return datetime.now(CAIRO_TZ)

# --- 3. البيانات (استخدم متغيرات بيئة في الإنتاج) ---
IQ_EMAIL = os.environ.get("IQ_EMAIL", "zain1mohamed2425@gmail.com")
IQ_PASSWORD = os.environ.get("IQ_PASSWORD", "ZainMohamed2425@")
ACCOUNT_TYPE = "PRACTICE"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8794920089:AAFnRnoudkdPrlMtDaijlaQgczrTkaM0MU4")
CHAT_ID = os.environ.get("CHAT_ID", "1462370563")

alerted_pairs = {}
active_trades = []
martingale_queue = {}  # قائمة انتظار المضاعفات

# --- 4. المؤشرات ---
def calculate_alma(series, window=9, offset=0.85, sigma=6):
    m = offset * (window - 1)
    s = window / sigma
    w = np.exp(-((np.arange(window) - m) ** 2) / (2 * s * s))
    w /= w.sum()
    return series.rolling(window).apply(lambda x: np.dot(x, w), raw=True)

def calculate_rsi(series, period=14):
    delta = series.diff()
    gain = (delta.where(delta > 0, 0)).rolling(window=period).mean()
    loss = (-delta.where(delta < 0, 0)).rolling(window=period).mean()
    rs = gain / loss
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
    bbu = sma + (std * std_dev)
    bbl = sma - (std * std_dev)
    return bbu, bbl

def get_fractal_levels(df):
    highs = df['High']
    lows = df['Low']
    resistance = highs.rolling(window=5, center=True).apply(lambda x: x[2] if max(x) == x[2] else np.nan, raw=True)
    support = lows.rolling(window=5, center=True).apply(lambda x: x[2] if min(x) == x[2] else np.nan, raw=True)
    last_res = resistance.dropna().iloc[-1] if not resistance.dropna().empty else df['High'].max()
    last_sup = support.dropna().iloc[-1] if not support.dropna().empty else df['Low'].min()
    return last_res, last_sup

# --- 5. Telegram ---
def send_telegram_message(message, retries=3):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    for attempt in range(retries):
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.ok:
                return True
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            if attempt < retries - 1:
                time.sleep(2)
    return False

# --- 6. إيقاف البوت ---
def on_shutdown():
    logger.warning("البوت يتوقف...")
    send_telegram_message("🔴 *تنبيه: تم إيقاف بوت IQ Option!*")

atexit.register(on_shutdown)

# --- 7. الاتصال بـ IQ Option ---
def connect_iqoption():
    logger.info("🔌 جاري الاتصال...")
    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    max_retries = 5
    for attempt in range(max_retries):
        check, reason = api.connect()
        if check:
            logger.info("✅ تم الاتصال!")
            api.change_balance(ACCOUNT_TYPE)
            return api
        else:
            logger.error(f"❌ فشل الاتصال ({attempt+1}/{max_retries}): {reason}")
            if attempt < max_retries - 1:
                time.sleep(5)
    send_telegram_message(f"❌ *فشل الاتصال:* `{reason}`")
    raise ConnectionError("فشل الاتصال")

API = connect_iqoption()

# --- 8. تحليل المضاعفة (بانتظار إشارة قوية) ---
def analyze_martingale(pair, original_direction):
    """بنحلل السوق من تاني ونستنى إشارة سوبر ماكس أو قوية جداً"""
    try:
        raw_candles = API.get_candles(pair, 300, 60, time.time())
        if not raw_candles or len(raw_candles) < 55:
            return None

        df = pd.DataFrame(raw_candles)
        df.rename(columns={'open': 'Open', 'max': 'High', 'min': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
        
        df['ALMA'] = calculate_alma(df['Close'], 9, 0.85, 6)
        df['ALMA_50'] = calculate_alma(df['Close'], 50, 0.85, 6)
        df['RSI'] = calculate_rsi(df['Close'], 14)
        df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)
        df['BBU'], df['BBL'] = calculate_bollinger(df['Close'], 20, 2)
        df['Vol_MA'] = df['Volume'].rolling(window=20).mean()
        
        resistance, support = get_fractal_levels(df)

        # نحلل على الشمعة المغلقة (-2) زي الصفقات العادية
        last = df.iloc[-2]
        prev = df.iloc[-3]
        
        price = last['Close']
        open_price = last['Open']
        low = last['Low']
        high = last['High']
        alma = last['ALMA']
        rsi = last['RSI']
        stoch_k = last['Stoch_K']
        stoch_d = last['Stoch_D']
        bbl = last['BBL']
        bbu = last['BBU']
        volume = last['Volume']
        vol_ma = last['Vol_MA']

        is_strong_candle = abs(price - open_price) > ((high - low) * 0.25)
        valid_volume = volume > (vol_ma * 0.8)
        
        near_support = abs(price - support) <= (price * 0.0005) or low <= (bbl * 1.001)
        near_resistance = abs(price - resistance) <= (price * 0.0005) or high >= (bbu * 0.999)

        alma_9_prev = prev['ALMA']
        alma_50_prev = prev['ALMA_50']
        alma_9_curr = last['ALMA']
        alma_50_curr = last['ALMA_50']

        # شروط السوبر ماكس الأصلية بالظبط (من غير فلاتر RSI زيادة)
        super_max_call = (
            (alma_9_prev <= alma_50_prev) and 
            (alma_9_curr > alma_50_curr) and 
            (stoch_k > stoch_d) and 
            is_strong_candle and 
            valid_volume
        )
        
        super_max_put = (
            (alma_9_prev >= alma_50_prev) and 
            (alma_9_curr < alma_50_curr) and 
            (stoch_k < stoch_d) and 
            is_strong_candle and 
            valid_volume
        )

        direction = None
        if super_max_call:
            direction = "CALL"
        elif super_max_put:
            direction = "PUT"
        elif is_strong_candle and valid_volume:
            # إشارات عادية قوية
            if price > alma and stoch_k > stoch_d and rsi <= 50 and near_support and stoch_k < 40:
                direction = "CALL"
            elif price < alma and stoch_k < stoch_d and rsi >= 50 and near_resistance and stoch_k > 60:
                direction = "PUT"

        # المضاعفة لازم تكون عكس الاتجاه الأصلي
        if direction and direction != original_direction:
            return direction
        return None

    except Exception as e:
        logger.error(f"خطأ تحليل المضاعفة: {e}")
        return None

# --- 9. فحص نتائج الصفقات ---
def check_trade_results():
    current_time = time.time()
    trades_to_remove = []

    for trade in active_trades:
        time_left = trade['expire_time'] - current_time

        try:
            if 0 < time_left <= 20 and not trade.get('warned_loss', False) and not trade.get('is_martingale', False):
                candles = API.get_candles(trade['pair'], 300, 1, time.time())
                if not candles:
                    continue
                current_price = candles[-1]['close']
                entry_price = trade['entry_price']
                direction = trade['direction']

                is_losing = (direction == "CALL" and current_price < entry_price) or (direction == "PUT" and current_price > entry_price)
                
                if is_losing:
                    send_telegram_message(f"⏳ *تنبيه مبكر*\nالزوج: `{trade['pair']}` [5m]\nالصفقة تتجه للخسارة..\n🔍 *جاري تحليل فرصة المضاعفة...*")
                    martingale_queue[trade['pair']] = {
                        'original_direction': direction,
                        'entry_price': entry_price,
                        'time': time.time()
                    }
                    trade['warned_loss'] = True

            if time_left <= 0:
                candles = API.get_candles(trade['pair'], 300, 2, time.time())
                final_price = candles[-2]['close'] if len(candles) >= 2 else candles[-1]['close']
                entry_price = trade['entry_price']
                direction = trade['direction']
                
                is_win = (direction == "CALL" and final_price > entry_price) or (direction == "PUT" and final_price < entry_price)
                time_str = get_cairo_time().strftime('%I:%M %p')
                pair = trade['pair']
                is_martingale = trade.get('is_martingale', False)

                if is_martingale:
                    msg = f"✅ *نتيجة المضاعفة: رابحة*" if is_win else f"❌ *نتيجة المضاعفة: خاسرة*"
                    msg += f"\nالزوج: `{pair}` [5m]\n⏰ `{time_str}`"
                    send_telegram_message(msg)
                    trades_to_remove.append(trade)
                else:
                    if is_win:
                        msg = f"✅ *نتيجة الصفقة: رابحة* 🎯\nالزوج: `{pair}` [5m]\n⏰ `{time_str}`"
                        send_telegram_message(msg)
                        trades_to_remove.append(trade)
                    else:
                        # نحط الزوج في قائمة انتظار المضاعفة
                        martingale_queue[pair] = {
                            'original_direction': direction,
                            'entry_price': entry_price,
                            'time': time.time()
                        }
                        msg = f"❌ *الصفقة خاسرة*\nالزوج: `{pair}` [5m]\n⏰ `{time_str}`\n\n🔍 *جاري تحليل السوق لإيجاد أفضل فرصة مضاعفة...*"
                        send_telegram_message(msg)
                        trades_to_remove.append(trade)

        except Exception as e:
            logger.error(f"خطأ متابعة {trade['pair']}: {e}")

    for trade in trades_to_remove:
        if trade in active_trades:
            active_trades.remove(trade)

# --- 10. تحليل الزوج ---
def analyze_pair(pair, timeframe="5m"):
    tf_seconds = 300
    duration_text = "5 دقائق"
    expire_delay = 300

    try:
        raw_candles = API.get_candles(pair, tf_seconds, 60, time.time())  # رجعنا 60 شمعة
    except Exception as e:
        logger.warning(f"خطأ جلب شموع {pair}: {e}")
        return None

    if not raw_candles or len(raw_candles) < 55:
        return None

    df = pd.DataFrame(raw_candles)
    df.rename(columns={'open': 'Open', 'max': 'High', 'min': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)

    df['ALMA'] = calculate_alma(df['Close'], 9, 0.85, 6)
    df['ALMA_50'] = calculate_alma(df['Close'], 50, 0.85, 6)
    df['RSI'] = calculate_rsi(df['Close'], 14)
    df['BBU'], df['BBL'] = calculate_bollinger(df['Close'], 20, 2)
    df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)
    df['Vol_MA'] = df['Volume'].rolling(window=20).mean()
    
    resistance, support = get_fractal_levels(df)

    # ========== التحليل على الشمعة المغلقة (-2) ==========
    last = df.iloc[-2]
    prev = df.iloc[-3]
    curr = df.iloc[-1]

    price = last['Close']
    open_price = last['Open']
    low = last['Low']
    high = last['High']
    alma = last['ALMA']
    rsi = last['RSI']
    stoch_k = last['Stoch_K']
    stoch_d = last['Stoch_D']
    bbl = last['BBL']
    bbu = last['BBU']
    volume = last['Volume']
    vol_ma = last['Vol_MA']

    is_strong_candle = abs(price - open_price) > ((high - low) * 0.25)
    valid_volume = volume > (vol_ma * 0.8)

    near_support = abs(price - support) <= (price * 0.0005) or low <= (bbl * 1.001)
    near_resistance = abs(price - resistance) <= (price * 0.0005) or high >= (bbu * 0.999)

    pair_key = f"{pair}_5m"
    cairo_now = get_cairo_time()
    current_time_str = cairo_now.strftime('%I:%M %p')

    candle_seconds = (cairo_now.minute % 5) * 60 + cairo_now.second
    candle_minute = cairo_now.minute % 5

    final_signal = None
    direction = None

    # ========== شروط السوبر ماكس الأصلية بالظبط ==========
    alma_9_prev = prev['ALMA']
    alma_50_prev = prev['ALMA_50']
    alma_9_curr = last['ALMA']
    alma_50_curr = last['ALMA_50']

    super_max_call = (
        (alma_9_prev <= alma_50_prev) and 
        (alma_9_curr > alma_50_curr) and 
        (stoch_k > stoch_d) and 
        is_strong_candle and 
        valid_volume
    )
    
    super_max_put = (
        (alma_9_prev >= alma_50_prev) and 
        (alma_9_curr < alma_50_curr) and 
        (stoch_k < stoch_d) and 
        is_strong_candle and 
        valid_volume
    )

    if super_max_call:
        direction = "CALL"
        final_signal = f"👑 *إشارة سوبر ماكس (SUPER MAX) - تقاطع صاعد* 🔥\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{current_time_str}`"
    elif super_max_put:
        direction = "PUT"
        final_signal = f"👑 *إشارة سوبر ماكس (SUPER MAX) - تقاطع هابط* 🔥\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{current_time_str}`"

    # ========== الإشارات العادية الأصلية بالظبط ==========
    if not final_signal and is_strong_candle and valid_volume:
        if price > alma and stoch_k > stoch_d and rsi <= 50 and near_support:
            if stoch_k < 30:
                direction = "CALL"
                final_signal = f"🔥 *إشارة (CALL) - القوة: ماكس*\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{current_time_str}`"
            elif stoch_k < 40:
                direction = "CALL"
                final_signal = f"🚀 *إشارة (CALL) - القوة: قوية جداً*\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{current_time_str}`"

        elif price < alma and stoch_k < stoch_d and rsi >= 50 and near_resistance:
            if stoch_k > 70:
                direction = "PUT"
                final_signal = f"🔥 *إشارة (PUT) - القوة: ماكس*\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{current_time_str}`"
            elif stoch_k > 60:
                direction = "PUT"
                final_signal = f"📉 *إشارة (PUT) - القوة: قوية جداً*\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{current_time_str}`"

    # ========== فحص المضاعفات في قائمة الانتظار ==========
    if pair in martingale_queue:
        mg_data = martingale_queue[pair]
        # لو لقينا إشارة قوية عكس الاتجاه الأصلي
        if direction and direction != mg_data['original_direction']:
            dir_ar = "صعود (CALL)" if direction == "CALL" else "هبوط (PUT)"
            send_telegram_message(f"🎯 *فرصة المضاعفة جاهزة!*\nالزوج: `{pair}` [5m]\nالاتجاه: *{dir_ar}*\n⏰ `{current_time_str}`\n\n⚡ *جهز الدخول الآن!*")
            # ندخل الصفقة تلقائياً في المضاعفات
            active_trades.append({
                'pair': pair,
                'timeframe': '5m',
                'direction': direction,
                'entry_price': curr['Open'],
                'expire_time': time.time() + expire_delay,
                'warned_loss': True,
                'is_martingale': True
            })
            del martingale_queue[pair]
            return None  # مش هنرجع إشارة عادية، احنا بعتنا تنبيه مضاعفة
        
        # لو عدى 4 شموع ومالقناش فرصة
        elif time.time() - mg_data['time'] > 1200:  # 20 دقيقة = 4 شموع
            send_telegram_message(f"❌ *تم إلغاء فرصة المضاعفة*\nالزوج: `{pair}` [5m]\nالسبب: لم يتم العثور على إشارة قوية واضحة.")
            del martingale_queue[pair]

    # ========== التجهيز المسبق ==========
    curr_k = curr['Stoch_K']
    curr_rsi = curr['RSI']
    curr_price = curr['Close']
    curr_alma = curr['ALMA']

    high_potential_call = (curr_price > curr_alma) and (curr_k <= 40) and (curr_rsi <= 50)
    high_potential_put = (curr_price < curr_alma) and (curr_k >= 60) and (curr_rsi >= 50)

    if candle_minute == 4 and candle_seconds >= 30:
        if high_potential_call and pair_key not in alerted_pairs:
            send_telegram_message(f"⚠️ *تجهّز! فرصة صعود (CALL) قريبة جداً*\nالزوج: `{pair}` [5m]")
            alerted_pairs[pair_key] = "CALL"
        elif high_potential_put and pair_key not in alerted_pairs:
            send_telegram_message(f"⚠️ *تجهّز! فرصة هبوط (PUT) قريبة جداً*\nالزوج: `{pair}` [5m]")
            alerted_pairs[pair_key] = "PUT"

    # ========== إرسال الإشارة ==========
    if final_signal:
        if candle_seconds <= 10:
            if pair_key in alerted_pairs:
                del alerted_pairs[pair_key]
                entry_p = curr['Open']
                
                active_trades.append({
                    'pair': pair,
                    'timeframe': '5m',
                    'direction': direction,
                    'entry_price': entry_p,
                    'expire_time': time.time() + expire_delay,
                    'warned_loss': False,
                    'is_martingale': False
                })
                return final_signal

    else:
        if pair_key in alerted_pairs:
            prev_dir = alerted_pairs[pair_key]
            if (prev_dir == "CALL" and curr_k > 60) or (prev_dir == "PUT" and curr_k < 40):
                send_telegram_message(f"❌ *تم إلغاء التنبيه*\nالزوج: `{pair}` [5m]\nالسبب: الشروط لم تعد متوافقة.")
                del alerted_pairs[pair_key]

    return None

# --- 11. تشغيل البوت ---
def run_bot():
    pairs = [
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "EURJPY",
        "EURGBP", "AUDCAD", "AUDJPY", "CADJPY", "EURAUD", "GBPJPY", "EURCAD"
    ]
    timeframe = "5m"

    logger.info("🚀 البوت يعمل...")
    send_telegram_message("🤖 *تم تشغيل بوت IQ Option!*\n⏱️ *الفريم:* 5 دقائق\n⚡ *الدخول:* أول 10 ثوانٍ\n🔍 *المضاعفة:* بتحليل جديد وانتظار فرصة قوية")

    try:
        while True:
            try:
                if not API.check_connect():
                    logger.warning("انقطع الاتصال، جاري إعادة الاتصال...")
                    API.connect()

                for pair in pairs:
                    signal = analyze_pair(pair, timeframe)
                    if signal:
                        logger.info(f"إشارة جديدة: {pair}")
                        send_telegram_message(signal)
                    time.sleep(0.3)  # تأخير بسيط بين الأزواج

                check_trade_results()
                
            except Exception as e:
                logger.error(f"خطأ في الحلقة: {e}")
                logger.error(traceback.format_exc())

            time.sleep(1)  # رجعنا ثانية زي الأصل
    except KeyboardInterrupt:
        logger.info("تم الإيقاف يدوياً")
    finally:
        on_shutdown()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    run_bot()
