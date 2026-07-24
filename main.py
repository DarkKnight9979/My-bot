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

# --- 1. خادم Flask لإرضاء منصة Render (Port Binding) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Running Successfully!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- إغلاق سجلات الـ DEBUG المزعجة ---
logging.getLogger('iqoptionapi').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.ERROR)

# --- 2. ضبط التوقيت الزمني الرسمي (Africa/Cairo) ---
CAIRO_TZ = pytz.timezone('Africa/Cairo')

def get_cairo_time():
    """جلب الوقت الحالي بتوقيت مصر"""
    return datetime.now(CAIRO_TZ)

# --- 3. بيانات الحساب والتليجرام ---
IQ_EMAIL = "zain1mohamed2425@gmail.com"
IQ_PASSWORD = "ZainMohamed2425@"
ACCOUNT_TYPE = "PRACTICE"

TELEGRAM_TOKEN = "8794920089:AAFnRnoudkdPrlMtDaijlaQgczrTkaM0MU4"
CHAT_ID = "1462370563"

# قواميس لمتابعة حالة التنبيهات والصفقات المعلقة
alerted_pairs = {}
active_trades = []

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

# --- 5. دالة إرسال الرسائل لـ Telegram ---
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=5)
        return res.ok
    except Exception as e:
        print(f"❌ خطأ في إرسال التليجرام: {e}")
        return False

# --- 6. التنبيه التلقائي عند إيقاف البوت ---
def on_shutdown():
    print("⚠️ البوت يتوقف الآن...")
    send_telegram_message("🔴 *تنبيه: تم إيقاف بوت IQ Option!*")

atexit.register(on_shutdown)

# --- 7. الاتصال بـ IQ Option ---
print("🔌 جاري الاتصال بمنصة IQ Option...")
API = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
check, reason = API.connect()

if check:
    print("✅ تم الاتصال بنجاح بالمنصة!")
    API.change_balance(ACCOUNT_TYPE)
else:
    print(f"❌ فشل الاتصال بالمنصة: {reason}")
    send_telegram_message(f"❌ *فشل الاتصال بالمنصة:* `{reason}`")
    exit()

# --- 8. دالة تحليل المضاعفة الذكية (معدلة لقراءة الانعكاس والتحليل الذكي) ---
def analyze_martingale_direction(pair, original_direction):
    try:
        raw_candles = API.get_candles(pair, 300, 20, time.time())
        if not raw_candles or len(raw_candles) < 10:
            return None

        df = pd.DataFrame(raw_candles)
        df.rename(columns={'open': 'Open', 'max': 'High', 'min': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
        
        df['ALMA'] = calculate_alma(df['Close'], 9, 0.85, 6)
        df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)

        last = df.iloc[-1]
        price = last['Close']
        open_price = last['Open']
        high = last['High']
        low = last['Low']
        alma = last['ALMA']
        stoch_k = last['Stoch_K']
        stoch_d = last['Stoch_D']

        total_range = high - low if (high - low) > 0 else 0.0001
        lower_shadow = min(open_price, price) - low
        upper_shadow = high - max(open_price, price)

        is_reversal_up = (price > open_price) or (lower_shadow > total_range * 0.35)
        is_reversal_down = (price < open_price) or (upper_shadow > total_range * 0.35)

        # تحليل ذكي: التحول للاتجاه العكسي المضمون في حالة كسر الاتجاه الأصلي
        if original_direction == "CALL":
            if is_reversal_down and price < alma:
                return "PUT"
            elif stoch_k > stoch_d or is_reversal_up:
                return "CALL"
            else:
                return None
        else:
            if is_reversal_up and price > alma:
                return "CALL"
            elif stoch_k < stoch_d or is_reversal_down:
                return "PUT"
            else:
                return None

    except Exception as e:
        print(f"⚠️ خطأ أثناء تحليل المضاعفة: {e}")
        return None

# --- 9. دالة فحص نتائج الصفقات والمضاعفة المبكرة ---
def check_trade_results():
    current_time = time.time()
    trades_to_remove = []

    for trade in active_trades:
        time_left = trade['expire_time'] - current_time

        try:
            candles = API.get_candles(trade['pair'], 300, 1, time.time())
            if not candles:
                continue
            
            current_price = candles[-1]['close']
            entry_price = trade['entry_price']
            direction = trade['direction']
            is_martingale = trade.get('is_martingale', False)

            if 0 < time_left <= 20 and not trade.get('warned_loss', False) and not is_martingale:
                is_losing_now = False
                if direction == "CALL" and current_price < entry_price:
                    is_losing_now = True
                elif direction == "PUT" and current_price > entry_price:
                    is_losing_now = True

                if is_losing_now:
                    martingale_dir = analyze_martingale_direction(trade['pair'], direction)
                    if martingale_dir:
                        dir_ar = "صعود (CALL)" if martingale_dir == "CALL" else "هبوط (PUT)"
                        msg = f"⏳ *تنبيه مبكر للمضاعفة (Martingale)* ⚠️\nالزوج: `{trade['pair']}` [5m]\nالصفقة تتجه للخسارة..\n💡 *توصية البوت الذكي:* جهّز مضاعفة باتجاه *{dir_ar}*"
                    else:
                        msg = f"⏳ *تنبيه مبكر* ⚠️\nالزوج: `{trade['pair']}` [5m]\nالصفقة تتجه للخسارة..\n🛑 *تنبيه:* عدم دخول مضاعفة لأن حركة السوق غير واضحة!"
                    
                    send_telegram_message(msg)
                    trade['warned_loss'] = True

            if time_left <= 0:
                is_win = False
                if direction == "CALL" and current_price > entry_price:
                    is_win = True
                elif direction == "PUT" and current_price < entry_price:
                    is_win = True

                time_str = get_cairo_time().strftime('%I:%M %p')
                
                if is_martingale:
                    # تقرير نتيجة المضاعفة
                    if is_win:
                        msg = f"✅ *نتيجة المضاعفة: رابحة (WIN)* 🎯\nالزوج: `{trade['pair']}` [5m]\nسعر الدخول: `{entry_price}` | سعر الإغلاق: `{current_price}`\n⏰ الوقت: `{time_str}`"
                    else:
                        msg = f"❌ *نتيجة المضاعفة: خاسرة (LOSS)*\nالزوج: `{trade['pair']}` [5m]\nسعر الدخول: `{entry_price}` | سعر الإغلاق: `{current_price}`\n⏰ الوقت: `{time_str}`"
                    send_telegram_message(msg)
                    trades_to_remove.append(trade)
                else:
                    # تقرير نتيجة الصفقة الأساسية
                    if is_win:
                        msg = f"✅ *نتيجة الصفقة: رابحة (WIN)* 🎯\nالزوج: `{trade['pair']}` [5m]\nنوع الاتجاه: {direction}\nسعر الدخول: `{entry_price}`\nسعر الإغلاق: `{current_price}`\n⏰ الوقت: `{time_str}`"
                        send_telegram_message(msg)
                        trades_to_remove.append(trade)
                    else:
                        martingale_dir = analyze_martingale_direction(trade['pair'], direction)
                        if martingale_dir:
                            dir_ar = "صعود (CALL)" if martingale_dir == "CALL" else "هبوط (PUT)"
                            msg = f"❌ *نتيجة الصفقة الأساسية: خاسرة (LOSS)*\nالزوج: `{trade['pair']}` [5m]\nسعر الدخول: `{entry_price}` | سعر الإغلاق: `{current_price}`\n⏰ الوقت: `{time_str}`\n\n🔄 *توجيه المضاعفة المؤكدة (واحدة فقط):* ادخل مضاعفة الآن باتجاه *{dir_ar}*"
                            
                            # تسجيل صفقة المضاعفة للمتابعة وإرسال النتيجة
                            active_trades.append({
                                'pair': trade['pair'],
                                'timeframe': '5m',
                                'direction': martingale_dir,
                                'entry_price': current_price,
                                'expire_time': time.time() + 300,
                                'warned_loss': True,
                                'is_martingale': True
                            })
                        else:
                            msg = f"❌ *نتيجة الصفقة: خاسرة (LOSS)*\nالزوج: `{trade['pair']}` [5m]\nسعر الدخول: `{entry_price}` | سعر الإغلاق: `{current_price}`\n⏰ الوقت: `{time_str}`\n\n🛑 *تنبيه:* عدم دخول مضاعفة لأن حركة السوق غير واضحة!"
                        
                        send_telegram_message(msg)
                        trades_to_remove.append(trade)

        except Exception as e:
            print(f"⚠️ خطأ في متابعة نتيجة {trade['pair']}: {e}")

    for trade in trades_to_remove:
        if trade in active_trades:
            active_trades.remove(trade)

# --- 10. دالة تحليل الأزواج المرتكزة على مناطق الارتداد القوية وإشارات السوبر ماكس ---
def analyze_pair(pair, timeframe="5m"):
    tf_seconds = 300
    duration_text = "5 دقائق"
    expire_delay = 300

    try:
        raw_candles = API.get_candles(pair, tf_seconds, 60, time.time())
    except Exception as e:
        print(f"⚠️ خطأ أثناء جلب شموع {pair}: {e}")
        return None

    if not raw_candles or len(raw_candles) < 55:
        return None

    df = pd.DataFrame(raw_candles)
    df.rename(columns={'open': 'Open', 'max': 'High', 'min': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)

    # المؤشرات القديمة كما هي
    df['ALMA'] = calculate_alma(df['Close'], 9, 0.85, 6)
    # إضافة المؤشر الجديد ALMA 50 لتقاطع السوبر ماكس
    df['ALMA_50'] = calculate_alma(df['Close'], 50, 0.85, 6)
    
    df['RSI'] = calculate_rsi(df['Close'], 14)
    df['BBU'], df['BBL'] = calculate_bollinger(df['Close'], 20, 2)
    df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)
    df['Vol_MA'] = df['Volume'].rolling(window=20).mean()
    
    resistance, support = get_fractal_levels(df)

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

    # تحديد الشراء قرب الدعم/البولنجر السفلي والبيع قرب المقاومة/البولنجر العلوي
    near_support = abs(price - support) <= (price * 0.0005) or low <= (bbl * 1.001)
    near_resistance = abs(price - resistance) <= (price * 0.0005) or high >= (bbu * 0.999)

    pair_key = f"{pair}_5m"
    cairo_now = get_cairo_time()
    current_time_str = cairo_now.strftime('%I:%M %p')

    candle_seconds = (cairo_now.minute % 5) * 60 + cairo_now.second
    candle_minute = cairo_now.minute % 5

    final_signal = None
    direction = None

    # --- أولاً: فحص تقاطع السوبر ماكس الجدبد (ALMA 9 x ALMA 50) ---
    alma_9_prev = prev['ALMA']
    alma_50_prev = prev['ALMA_50']
    alma_9_curr = last['ALMA']
    alma_50_curr = last['ALMA_50']

    # شرط التقاطع الصاعد لسوبر ماكس
    super_max_call = (alma_9_prev <= alma_50_prev) and (alma_9_curr > alma_50_curr) and (stoch_k > stoch_d) and is_strong_candle and valid_volume
    # شرط التقاطع الهابط لسوبر ماكس
    super_max_put = (alma_9_prev >= alma_50_prev) and (alma_9_curr < alma_50_curr) and (stoch_k < stoch_d) and is_strong_candle and valid_volume

    if super_max_call:
        direction = "CALL"
        final_signal = f"👑 *إشارة سوبر ماكس (SUPER MAX) - تقاطع صاعد* 🔥\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{current_time_str}`"
    elif super_max_put:
        direction = "PUT"
        final_signal = f"👑 *إشارة سوبر ماكس (SUPER MAX) - تقاطع هابط* 🔥\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{current_time_str}`"

    # --- ثانياً: شروط الإشارات العادية القديمة كما هي تماماً ---
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

    # نظام التجهيز المسبق (عند الدقيقة 4 والتانية 30)
    curr_candle = df.iloc[-1]
    curr_k = curr_candle['Stoch_K']
    curr_rsi = curr_candle['RSI']
    curr_price = curr_candle['Close']
    curr_alma = curr_candle['ALMA']

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
                entry_p = df.iloc[-1]['Open']
                
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

# --- 11. تشغيل البوت ورسالة الترحيب الثابتة بتنفيذ متوازي سريع جداً ---
def run_bot():
    pairs = [
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "EURJPY",
        "EURGBP", "AUDCAD", "AUDJPY", "CADJPY", "EURAUD", "GBPJPY", "EURCAD"
    ]
    timeframe = "5m"

    print("🚀 البوت يعمل الآن ويحلل الـ 14 زوج بالسرعة القصوى...")
    send_telegram_message("🤖 *تم تشغيل بوت IQ Option بنجاح على السيرفر!*\n⏱️ *الفريم المعتمد:* 5 دقائق حصراً\n⚡ *الدخول:* في أول 5 ثوانٍ مع نقطة الافتتاح\n👑 *النظام الجديد:* يشمل إشارات السوبر ماكس (SUPER MAX)\n🇪🇬 *التوقيت:* توقيت مصر الرسمي\nجاري الفحص...")

    try:
        while True:
            try:
                if not API.check_connect():
                    print("⚠️ انقطع الاتصال، جاري إعادة الاتصال...")
                    API.connect()

                # جلب وتحليل الـ 14 زوج بالتوازي في أقل من 0.5 ثانية
                with ThreadPoolExecutor(max_workers=14) as executor:
                    results = executor.map(lambda p: (p, analyze_pair(p, timeframe)), pairs)
                    for pair, signal in results:
                        if signal:
                            print(f"✅ إشارة جديدة لـ {pair}")
                            send_telegram_message(signal)

                check_trade_results()
            except Exception as e:
                print(f"⚠️ خطأ أثناء الحلقة الرئيسية: {e}")
                print(traceback.format_exc())

            time.sleep(1)
    except KeyboardInterrupt:
        print("🛑 تم إيقاف البوت يدوياً.")
    finally:
        on_shutdown()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    run_bot()
