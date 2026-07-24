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

# ========== إعداد الـ Logging الاحترافي ==========
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("bot.log", encoding='utf-8'),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

# --- 1. خادم Flask لإرضاء منصة Render (Port Binding) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Running Successfully!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- إغلاق سجلات الـ DEBUG المزعجة ---
logging.getLogger('iqoptionapi').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# --- 2. ضبط التوقيت الزمني الرسمي (Africa/Cairo) ---
CAIRO_TZ = pytz.timezone('Africa/Cairo')

def get_cairo_time():
    """جلب الوقت الحالي بتوقيت مصر"""
    return datetime.now(CAIRO_TZ)

# --- 3. بيانات الحساب والتليجرام (استخدم متغيرات البيئة في الإنتاج) ---
IQ_EMAIL = os.environ.get("IQ_EMAIL", "zain1mohamed2425@gmail.com")
IQ_PASSWORD = os.environ.get("IQ_PASSWORD", "ZainMohamed2425@")
ACCOUNT_TYPE = "PRACTICE"

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8794920089:AAFnRnoudkdPrlMtDaijlaQgczrTkaM0MU4")
CHAT_ID = os.environ.get("CHAT_ID", "1462370563")

# قواميس لمتابعة حالة التنبيهات والصفقات المعلقة
alerted_pairs = {}
active_trades = []
martingale_count = {}  # عداد المضاعفات لكل زوج (منع المضاعفة المتتالية)

# --- 4. دوال حساب المؤشرات الرياضية بنقاء 100% ---
def calculate_alma(series, window=9, offset=0.85, sigma=6):
    """حساب مؤشر ALMA لإلغاء التأخير تماماً وتنقية الشارت"""
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
    """حساب مستويات الدعوم والمقاومات الحقيقية الناتجة عن الارتدادات الفعالية"""
    highs = df['High']
    lows = df['Low']
    resistance = highs.rolling(window=5, center=True).apply(lambda x: x[2] if max(x) == x[2] else np.nan, raw=True)
    support = lows.rolling(window=5, center=True).apply(lambda x: x[2] if min(x) == x[2] else np.nan, raw=True)
    
    last_res = resistance.dropna().iloc[-1] if not resistance.dropna().empty else df['High'].max()
    last_sup = support.dropna().iloc[-1] if not support.dropna().empty else df['Low'].min()
    return last_res, last_sup

# --- 5. دالة إرسال الرسائل لـ Telegram (مع Retry) ---
def send_telegram_message(message, retries=3):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    
    for attempt in range(retries):
        try:
            res = requests.post(url, json=payload, timeout=10)
            if res.ok:
                return True
            else:
                logger.warning(f"Telegram API returned status {res.status_code}: {res.text}")
        except Exception as e:
            logger.error(f"Telegram error (attempt {attempt+1}/{retries}): {e}")
            if attempt < retries - 1:
                time.sleep(2)
    return False

# --- 6. التنبيه التلقائي عند إيقاف البوت ---
def on_shutdown():
    logger.warning("البوت يتوقف الآن...")
    send_telegram_message("🔴 *تنبيه: تم إيقاف بوت IQ Option!*")

atexit.register(on_shutdown)

# --- 7. الاتصال بـ IQ Option (مع إعادة محاولة ذكية) ---
def connect_iqoption():
    """اتصال بـ IQ Option مع نظام إعادة المحاولة"""
    logger.info("🔌 جاري الاتصال بمنصة IQ Option...")
    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    
    max_retries = 5
    for attempt in range(max_retries):
        check, reason = api.connect()
        if check:
            logger.info("✅ تم الاتصال بنجاح بالمنصة!")
            api.change_balance(ACCOUNT_TYPE)
            return api
        else:
            logger.error(f"❌ فشل الاتصال (محاولة {attempt+1}/{max_retries}): {reason}")
            if attempt < max_retries - 1:
                time.sleep(5)
    
    send_telegram_message(f"❌ *فشل الاتصال بالمنصة نهائياً:* `{reason}`")
    raise ConnectionError(f"فشل الاتصال بعد {max_retries} محاولات")

API = connect_iqoption()

# --- 8. دالة تحليل المضاعفة الذكية (معدلة بشكل كبير) ---
def analyze_martingale_direction(pair, original_direction):
    """
    تحليل اتجاه المضاعفة باستخدام 3 شموع للتأكد من الانعكاس
    مانعملش مضاعفة لو الزوج خسر مضاعفة قبل كده في نفس الجلسة
    """
    try:
        # منع المضاعفة المتتالية (أقصى مضاعفة واحدة لكل زوج)
        pair_key = f"{pair}_martingale"
        if martingale_count.get(pair_key, 0) >= 1:
            logger.info(f"تم تجاهل مضاعفة {pair} (تمت مضاعفة من قبل)")
            return None
        
        raw_candles = API.get_candles(pair, 300, 5, time.time())  # 5 شموع بدل 20
        if not raw_candles or len(raw_candles) < 5:
            return None

        df = pd.DataFrame(raw_candles)
        df.rename(columns={'open': 'Open', 'max': 'High', 'min': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
        
        df['ALMA'] = calculate_alma(df['Close'], 9, 0.85, 6)
        df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)

        # نحلل آخر 3 شموع مش شمعة واحدة
        last_three = df.tail(3)
        last = df.iloc[-1]
        price = last['Close']
        alma = last['ALMA']
        stoch_k = last['Stoch_K']
        stoch_d = last['Stoch_D']

        # شروط الانعكاس القوي (3 شموع تأكيد)
        bullish_reversal = (
            last_three['Close'].iloc[-1] > last_three['Close'].iloc[0] and  # السعر صاعد
            price > alma and
            stoch_k > stoch_d and
            stoch_k < 50  # لسه في منطقة الشراء
        )
        
        bearish_reversal = (
            last_three['Close'].iloc[-1] < last_three['Close'].iloc[0] and  # السعر هابط
            price < alma and
            stoch_k < stoch_d and
            stoch_k > 50  # لسه في منطقة البيع
        )

        if original_direction == "CALL":
            if bearish_reversal:
                martingale_count[pair_key] = martingale_count.get(pair_key, 0) + 1
                return "PUT"
            else:
                return None
        else:
            if bullish_reversal:
                martingale_count[pair_key] = martingale_count.get(pair_key, 0) + 1
                return "CALL"
            else:
                return None

    except Exception as e:
        logger.error(f"⚠️ خطأ أثناء تحليل المضاعفة: {e}")
        return None

# --- 9. دالة فحص نتائج الصفقات (معدلة بالكامل) ---
def check_trade_results():
    """فحص نتائج الصفقات باستخدام API الرسمي للنتائج"""
    current_time = time.time()
    trades_to_remove = []

    for trade in active_trades:
        time_left = trade['expire_time'] - current_time

        try:
            # التنبيه المبكر (آخر 20 ثانية)
            if 0 < time_left <= 20 and not trade.get('warned_loss', False) and not trade.get('is_martingale', False):
                candles = API.get_candles(trade['pair'], 300, 1, time.time())
                if not candles:
                    continue
                
                current_price = candles[-1]['close']
                entry_price = trade['entry_price']
                direction = trade['direction']

                is_losing_now = False
                if direction == "CALL" and current_price < entry_price:
                    is_losing_now = True
                elif direction == "PUT" and current_price > entry_price:
                    is_losing_now = True

                if is_losing_now:
                    martingale_dir = analyze_martingale_direction(trade['pair'], direction)
                    if martingale_dir:
                        dir_ar = "صعود (CALL)" if martingale_dir == "CALL" else "هبوط (PUT)"
                        msg = (f"⏳ *تنبيه مبكر للمضاعفة* ⚠️\n"
                               f"الزوج: `{trade['pair']}` [5m]\n"
                               f"الصفقة تتجه للخسارة..\n"
                               f"💡 *توصية البوت:* جهّز مضاعفة باتجاه *{dir_ar}*")
                    else:
                        msg = (f"⏳ *تنبيه مبكر* ⚠️\n"
                               f"الزوج: `{trade['pair']}` [5m]\n"
                               f"الصفقة تتجه للخسارة..\n"
                               f"🛑 *تنبيه:* عدم دخول مضاعفة - حركة السوق غير واضحة!")
                    
                    send_telegram_message(msg)
                    trade['warned_loss'] = True

            # فحص النتيجة النهائية
            if time_left <= 0:
                # محاولة الحصول على النتيجة الحقيقية من API
                is_win = None
                try:
                    # ننتظر ثانية عشان النتيجة تتحدث في المنصة
                    time.sleep(1.5)
                    candles = API.get_candles(trade['pair'], 300, 2, time.time())
                    if candles and len(candles) >= 2:
                        # نستخدم الشمعة المغلقة اللي بعد انتهاء الصفقة
                        final_price = candles[-2]['close'] if len(candles) >= 2 else candles[-1]['close']
                        entry_price = trade['entry_price']
                        direction = trade['direction']
                        
                        if direction == "CALL":
                            is_win = final_price > entry_price
                        else:
                            is_win = final_price < entry_price
                except Exception as e:
                    logger.error(f"خطأ في جلب النتيجة النهائية: {e}")
                    is_win = False  # افتراض خسارة لو مقدرناش نجيب النتيجة

                time_str = get_cairo_time().strftime('%I:%M %p')
                pair = trade['pair']
                is_martingale = trade.get('is_martingale', False)

                if is_martingale:
                    if is_win:
                        msg = (f"✅ *نتيجة المضاعفة: رابحة (WIN)* 🎯\n"
                               f"الزوج: `{pair}` [5m]\n"
                               f"⏰ الوقت: `{time_str}`")
                    else:
                        msg = (f"❌ *نتيجة المضاعفة: خاسرة (LOSS)*\n"
                               f"الزوج: `{pair}` [5m]\n"
                               f"⏰ الوقت: `{time_str}`")
                    send_telegram_message(msg)
                    trades_to_remove.append(trade)
                else:
                    if is_win:
                        msg = (f"✅ *نتيجة الصفقة: رابحة (WIN)* 🎯\n"
                               f"الزوج: `{pair}` [5m]\n"
                               f"الاتجاه: {direction}\n"
                               f"⏰ الوقت: `{time_str}`")
                        send_telegram_message(msg)
                        trades_to_remove.append(trade)
                    else:
                        martingale_dir = analyze_martingale_direction(pair, direction)
                        if martingale_dir:
                            dir_ar = "صعود (CALL)" if martingale_dir == "CALL" else "هبوط (PUT)"
                            msg = (f"❌ *نتيجة الصفقة الأساسية: خاسرة (LOSS)*\n"
                                   f"الزوج: `{pair}` [5m]\n"
                                   f"⏰ الوقت: `{time_str}`\n\n"
                                   f"🔄 *توجيه المضاعفة:* ادخل مضاعفة باتجاه *{dir_ar}*")
                            
                            # تسجيل صفقة المضاعفة
                            active_trades.append({
                                'pair': pair,
                                'timeframe': '5m',
                                'direction': martingale_dir,
                                'entry_price': trade.get('current_price', trade['entry_price']),
                                'expire_time': time.time() + 300,
                                'warned_loss': True,
                                'is_martingale': True
                            })
                        else:
                            msg = (f"❌ *نتيجة الصفقة: خاسرة (LOSS)*\n"
                                   f"الزوج: `{pair}` [5m]\n"
                                   f"⏰ الوقت: `{time_str}`\n\n"
                                   f"🛑 *تنبيه:* عدم دخول مضاعفة - حركة السوق غير واضحة!")
                        
                        send_telegram_message(msg)
                        trades_to_remove.append(trade)

        except Exception as e:
            logger.error(f"⚠️ خطأ في متابعة نتيجة {trade['pair']}: {e}")

    # تنظيف الصفقات المنتهية
    for trade in trades_to_remove:
        if trade in active_trades:
            active_trades.remove(trade)
            logger.info(f"تمت إزالة صفقة {trade['pair']} من القائمة")

# --- 10. دالة تحليل الأزواج (معدلة بالكامل) ---
def analyze_pair(pair, timeframe="5m"):
    tf_seconds = 300
    duration_text = "5 دقائق"
    expire_delay = 300

    try:
        # 40 شمعة بدل 60 (توفير في الأداء)
        raw_candles = API.get_candles(pair, tf_seconds, 40, time.time())
    except Exception as e:
        logger.warning(f"⚠️ خطأ أثناء جلب شموع {pair}: {e}")
        return None

    if not raw_candles or len(raw_candles) < 35:
        return None

    df = pd.DataFrame(raw_candles)
    df.rename(columns={'open': 'Open', 'max': 'High', 'min': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)

    # المؤشرات
    df['ALMA'] = calculate_alma(df['Close'], 9, 0.85, 6)
    df['ALMA_50'] = calculate_alma(df['Close'], 50, 0.85, 6)
    df['RSI'] = calculate_rsi(df['Close'], 14)
    df['BBU'], df['BBL'] = calculate_bollinger(df['Close'], 20, 2)
    df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)
    df['Vol_MA'] = df['Volume'].rolling(window=20).mean()
    
    resistance, support = get_fractal_levels(df)

    # ========== التحليل على الشمعة المغلقة (-2) مش الحالية (-1) ==========
    last = df.iloc[-2]      # الشمعة المغلقة الأخيرة
    prev = df.iloc[-3]      # اللي قبلها
    curr = df.iloc[-1]      # الشمعة الحالية (للتجهيز المسبق فقط)

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

    # --- أولاً: فحص تقاطع السوبر ماكس (على الشمعة المغلقة) ---
    alma_9_prev = prev['ALMA']
    alma_50_prev = prev['ALMA_50']
    alma_9_curr = last['ALMA']
    alma_50_curr = last['ALMA_50']

    super_max_call = (
        (alma_9_prev <= alma_50_prev) and 
        (alma_9_curr > alma_50_curr) and 
        (stoch_k > stoch_d) and 
        is_strong_candle and 
        valid_volume and
        rsi < 60  # تأكيد إضافي: RSI مش في منطقة تشبع شرائي
    )
    
    super_max_put = (
        (alma_9_prev >= alma_50_prev) and 
        (alma_9_curr < alma_50_curr) and 
        (stoch_k < stoch_d) and 
        is_strong_candle and 
        valid_volume and
        rsi > 40  # تأكيد إضافي: RSI مش في منطقة تشبع بيعي
    )

    if super_max_call:
        direction = "CALL"
        final_signal = (f"👑 *إشارة سوبر ماكس (SUPER MAX) - تقاطع صاعد* 🔥\n"
                       f"الزوج: `{pair}` (IQ Option) [5m]\n"
                       f"⏱️ *مدة الصفقة:* {duration_text}\n"
                       f"⏰ *وقت الإشارة:* `{current_time_str}`")
    elif super_max_put:
        direction = "PUT"
        final_signal = (f"👑 *إشارة سوبر ماكس (SUPER MAX) - تقاطع هابط* 🔥\n"
                       f"الزوج: `{pair}` (IQ Option) [5m]\n"
                       f"⏱️ *مدة الصفقة:* {duration_text}\n"
                       f"⏰ *وقت الإشارة:* `{current_time_str}`")

    # --- ثانياً: شروط الإشارات العادية (على الشمعة المغلقة) ---
    if not final_signal and is_strong_candle and valid_volume:
        if price > alma and stoch_k > stoch_d and rsi <= 50 and near_support:
            if stoch_k < 30:
                direction = "CALL"
                final_signal = (f"🔥 *إشارة (CALL) - القوة: ماكس*\n"
                               f"الزوج: `{pair}` (IQ Option) [5m]\n"
                               f"⏱️ *مدة الصفقة:* {duration_text}\n"
                               f"⏰ *وقت الإشارة:* `{current_time_str}`")
            elif stoch_k < 40:
                direction = "CALL"
                final_signal = (f"🚀 *إشارة (CALL) - القوة: قوية جداً*\n"
                               f"الزوج: `{pair}` (IQ Option) [5m]\n"
                               f"⏱️ *مدة الصفقة:* {duration_text}\n"
                               f"⏰ *وقت الإشارة:* `{current_time_str}`")

        elif price < alma and stoch_k < stoch_d and rsi >= 50 and near_resistance:
            if stoch_k > 70:
                direction = "PUT"
                final_signal = (f"🔥 *إشارة (PUT) - القوة: ماكس*\n"
                               f"الزوج: `{pair}` (IQ Option) [5m]\n"
                               f"⏱️ *مدة الصفقة:* {duration_text}\n"
                               f"⏰ *وقت الإشارة:* `{current_time_str}`")
            elif stoch_k > 60:
                direction = "PUT"
                final_signal = (f"📉 *إشارة (PUT) - القوة: قوية جداً*\n"
                               f"الزوج: `{pair}` (IQ Option) [5m]\n"
                               f"⏱️ *مدة الصفقة:* {duration_text}\n"
                               f"⏰ *وقت الإشارة:* `{current_time_str}`")

    # نظام التجهيز المسبق (على الشمعة الحية - الدقيقة 4:30)
    curr_k = curr['Stoch_K']
    curr_rsi = curr['RSI']
    curr_price = curr['Close']
    curr_alma = curr['ALMA']

    high_potential_call = (curr_price > curr_alma) and (curr_k <= 40) and (curr_rsi <= 50)
    high_potential_put = (curr_price < curr_alma) and (curr_k >= 60) and (curr_rsi >= 50)

    if candle_minute == 4 and candle_seconds >= 30:
        if high_potential_call and pair_key not in alerted_pairs:
            send_telegram_message(f"⚠️ *تجهّز! فرصة صعود (CALL) قريبة جداً*\nالزوج: `{pair}` [5m]\nيرجى فتح الشارت وتجهيز الصفقة!")
            alerted_pairs[pair_key] = "CALL"
        elif high_potential_put and pair_key not in alerted_pairs:
            send_telegram_message(f"⚠️ *تجهّز! فرصة هبوط (PUT) قريبة جداً*\nالزوج: `{pair}` [5m]\nيرجى فتح الشارت وتجهيز الصفقة!")
            alerted_pairs[pair_key] = "PUT"

    # إرسال الإشارة عند بداية الشمعة (نافذة 10 ثوانٍ)
    if final_signal:
        if candle_seconds <= 10:
            if pair_key in alerted_pairs:
                del alerted_pairs[pair_key]
                # نستخدم سعر افتتاح الشمعة الحية للدخول
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

# --- 11. تشغيل البوت (مع Rate Limiting وتحسينات) ---
def run_bot():
    pairs = [
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "EURJPY",
        "EURGBP", "AUDCAD", "AUDJPY", "CADJPY", "EURAUD", "GBPJPY", "EURCAD"
    ]
    timeframe = "5m"

    logger.info("🚀 البوت يعمل الآن ويحلل الـ 14 زوج...")
    send_telegram_message("🤖 *تم تشغيل بوت IQ Option بنجاح!*\n"
                         "⏱️ *الفريم المعتمد:* 5 دقائق\n"
                         "⚡ *الدخول:* في أول 10 ثوانٍ مع نقطة الافتتاح\n"
                         "👑 *النظام:* يشمل إشارات السوبر ماكس\n"
                         "🇪🇬 *التوقيت:* توقيت مصر الرسمي\n"
                         "✅ *الإصدار:* 2.0 (معدل)")

    cycle_count = 0
    
    try:
        while True:
            cycle_count += 1
            cycle_start = time.time()
            
            try:
                # فحص الاتصال
                if not API.check_connect():
                    logger.warning("⚠️ انقطع الاتصال، جاري إعادة الاتصال...")
                    API.connect()
                    time.sleep(2)

                # تحليل الأزواج (بدون ThreadPool عشان نتحكم في الـ Rate Limit)
                for pair in pairs:
                    try:
                        signal = analyze_pair(pair, timeframe)
                        if signal:
                            logger.info(f"✅ إشارة جديدة لـ {pair}")
                            send_telegram_message(signal)
                        
                        # Rate Limiting: تأخير 0.3 ثانية بين كل زوج
                        time.sleep(0.3)
                        
                    except Exception as e:
                        logger.error(f"خطأ في تحليل {pair}: {e}")

                # فحص نتائج الصفقات
                check_trade_results()
                
                # تنظيف الذاكرة كل 50 دورة
                if cycle_count % 50 == 0:
                    logger.info(f"دورة #{cycle_count} - عدد الصفقات النشطة: {len(active_trades)}")

            except Exception as e:
                logger.error(f"⚠️ خطأ أثناء الحلقة الرئيسية: {e}")
                logger.error(traceback.format_exc())

            # حساب الوقت المتبقي عشان الدورة تكون كل 5 ثواني بالظبط
            elapsed = time.time() - cycle_start
            sleep_time = max(1, 5 - elapsed)  # 5 ثواني بين كل دورة
            time.sleep(sleep_time)
            
    except KeyboardInterrupt:
        logger.info("🛑 تم إيقاف البوت يدوياً.")
    finally:
        on_shutdown()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    run_bot()
