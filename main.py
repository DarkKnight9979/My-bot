import os
import threading
import logging
import time
import requests
import pandas as pd
import numpy as np
import atexit
import pytz
from datetime import datetime
from flask import Flask
from iqoptionapi.stable_api import IQ_Option

# --- إنشاء سيرفر خفيف لإرضاء Render (Port Binding) ---
app = Flask(__name__)

@app.route('/')
def home():
    return "Bot is Running Successfully!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

# --- إغلاق سجلات الـ DEBUG المزعجة ليعمل البوت بشكل صامت ---
logging.getLogger('iqoptionapi').setLevel(logging.ERROR)
logging.getLogger('urllib3').setLevel(logging.ERROR)

# --- ضبط التوقيت الزمني لتوقيت مصر (Africa/Cairo) ---
CAIRO_TZ = pytz.timezone('Africa/Cairo')

def get_cairo_time():
    """جلب الوقت الحالي بتوقيت مصر"""
    return datetime.now(CAIRO_TZ)

# --- بيانات الحساب والتليجرام ---
IQ_EMAIL = "zain1mohamed2425@gmail.com"
IQ_PASSWORD = "ZainMohamed2425@"
ACCOUNT_TYPE = "PRACTICE"  # للحساب التجريبي

TELEGRAM_TOKEN = "8794920089:AAFnRnoudkdPrlMtDaijlaQgczrTkaM0MU4"
CHAT_ID = "1462370563"

# قواميس لمتابعة حالة التنبيهات والصفقات المعلقة
alerted_pairs = {}
active_trades = []

# --- دوال حساب المؤشرات الرياضية ---
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

# --- دالة إرسال الرسائل والتنبيهات ---
def send_telegram_message(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    try:
        res = requests.post(url, json=payload, timeout=5)
        return res.ok
    except Exception as e:
        print(f"❌ خطأ في إرسال التليجرام: {e}")
        return False

# --- دالة تعمل تلقائياً عند إيقاف البوت ---
def on_shutdown():
    print("⚠️ البوت يتوقف الآن...")
    send_telegram_message("🔴 *تنبيه: تم إيقاف بوت IQ Option!*")

atexit.register(on_shutdown)

# --- الاتصال بـ IQ Option ---
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

# --- دالة تحليل المضاعفة عالية الدقة ---
def analyze_martingale_direction(pair, original_direction):
    try:
        raw_candles = API.get_candles(pair, 300, 15, time.time())
        if not raw_candles or len(raw_candles) < 10:
            return None

        df = pd.DataFrame(raw_candles)
        df.rename(columns={'open': 'Open', 'max': 'High', 'min': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
        
        df['EMA_9'] = df['Close'].ewm(span=9, adjust=False).mean()
        df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)

        last = df.iloc[-1]
        price = last['Close']
        open_price = last['Open']
        ema = last['EMA_9']
        stoch_k = last['Stoch_K']
        stoch_d = last['Stoch_D']

        body = abs(price - open_price)
        total_range = last['High'] - last['Low']

        is_strong_reversal = body > (total_range * 0.5)

        if original_direction == "CALL":
            if is_strong_reversal and price < open_price and price < ema:
                return "PUT"
            elif price > ema and stoch_k > stoch_d:
                return "CALL"
            else:
                return None
        else:
            if is_strong_reversal and price > open_price and price > ema:
                return "CALL"
            elif price < ema and stoch_k < stoch_d:
                return "PUT"
            else:
                return None

    except Exception as e:
        print(f"⚠️ خطأ أثناء تحليل المضاعفة: {e}")
        return None

# --- دالة فحص نتائج الصفقات والمضاعفة المبكرة ---
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

            if 0 < time_left <= 20 and not trade.get('warned_loss', False):
                is_losing_now = False
                if direction == "CALL" and current_price < entry_price:
                    is_losing_now = True
                elif direction == "PUT" and current_price > entry_price:
                    is_losing_now = True

                if is_losing_now:
                    martingale_dir = analyze_martingale_direction(trade['pair'], direction)
                    if martingale_dir:
                        dir_ar = "صعود (CALL)" if martingale_dir == "CALL" else "هبوط (PUT)"
                        msg = f"⏳ *تنبيه مبكر للمضاعفة (Martingale)* ⚠️\nالزوج: `{trade['pair']}` [5m]\nالصفقة تتجه للخسارة..\n💡 *توصية المؤشرات:* جهّز مضاعفة باتجاه *{dir_ar}*"
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
                if is_win:
                    msg = f"✅ *نتيجة الصفقة: رابحة (WIN)* 🎯\nالزوج: `{trade['pair']}` [5m]\nنوع الاتجاه: {direction}\nسعر الدخول: `{entry_price}`\nسعر الإغلاق: `{current_price}`\n⏰ الوقت: `{time_str}`"
                else:
                    martingale_dir = analyze_martingale_direction(trade['pair'], direction)
                    if martingale_dir:
                        dir_ar = "صعود (CALL)" if martingale_dir == "CALL" else "هبوط (PUT)"
                        msg = f"❌ *نتيجة الصفقة: خاسرة (LOSS)*\nالزوج: `{trade['pair']}` [5m]\nسعر الدخول: `{entry_price}` | سعر الإغلاق: `{current_price}`\n⏰ الوقت: `{time_str}`\n\n🔄 *توجيه المضاعفة المؤكدة:* ادخل مضاعفة الآن باتجاه *{dir_ar}*"
                    else:
                        msg = f"❌ *نتيجة الصفقة: خاسرة (LOSS)*\nالزوج: `{trade['pair']}` [5m]\nسعر الدخول: `{entry_price}` | سعر الإغلاق: `{current_price}`\n⏰ الوقت: `{time_str}`\n\n🛑 *تنبيه:* عدم دخول مضاعفة لأن حركة السوق غير واضحة!"

                send_telegram_message(msg)
                trades_to_remove.append(trade)

        except Exception as e:
            print(f"⚠️ خطأ في متابعة نتيجة {trade['pair']}: {e}")

    for trade in trades_to_remove:
        active_trades.remove(trade)

# --- دالة جلب الشموع وتحليل الزوج ---
def analyze_pair(pair, timeframe="5m"):
    tf_seconds = 300
    duration_text = "5 دقائق"
    expire_delay = 300

    try:
        raw_candles = API.get_candles(pair, tf_seconds, 50, time.time())
    except Exception as e:
        print(f"⚠️ خطأ أثناء جلب شموع {pair}: {e}")
        return None

    if not raw_candles or len(raw_candles) < 35:
        return None

    df = pd.DataFrame(raw_candles)
    df.rename(columns={'open': 'Open', 'max': 'High', 'min': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)

    df['EMA_9'] = df['Close'].ewm(span=9, adjust=False).mean()
    df['RSI'] = calculate_rsi(df['Close'], 14)
    df['BBU'], df['BBL'] = calculate_bollinger(df['Close'], 20, 2)
    df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)

    last = df.iloc[-2]

    price = last['Close']
    open_price = last['Open']
    ema = last['EMA_9']
    rsi = last['RSI']
    stoch_k = last['Stoch_K']
    stoch_d = last['Stoch_D']

    pair_key = f"{pair}_5m"
    cairo_now = get_cairo_time()
    current_time_str = cairo_now.strftime('%I:%M %p')

    candle_seconds = (cairo_now.minute % 5) * 60 + cairo_now.second
    candle_minute = cairo_now.minute % 5

    final_signal = None
    direction = None

    # 1. شروط إشارات الدخول النهائية (مرنة ومباشرة)
    if price > ema and stoch_k > stoch_d and rsi >= 35 and stoch_k <= 70:
        direction = "CALL"
        final_signal = f"🚀 *إشارة (CALL) - قوية*\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{current_time_str}`"

    elif price < ema and stoch_k < stoch_d and rsi <= 65 and stoch_k >= 30:
        direction = "PUT"
        final_signal = f"📉 *إشارة (PUT) - قوية*\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{current_time_str}`"

    # 2. نظام التجهيز المسبق (في آخر 30 ثانية من الشمعة)
    curr_candle = df.iloc[-1]
    curr_k = curr_candle['Stoch_K']
    curr_rsi = curr_candle['RSI']
    curr_price = curr_candle['Close']
    curr_ema = curr_candle['EMA_9']

    high_potential_call = (curr_price > curr_ema) and (curr_k <= 40) and (curr_rsi <= 55)
    high_potential_put = (curr_price < curr_ema) and (curr_k >= 60) and (curr_rsi >= 45)

    if candle_minute == 4 and candle_seconds >= 30:
        if high_potential_call and pair_key not in alerted_pairs:
            send_telegram_message(f"⚠️ *تجهّز! فرصة صعود (CALL) قريبة جداً*\nالزوج: `{pair}` [5m]\nيرجى فتح الشارت وتجهيز الصفقة!")
            alerted_pairs[pair_key] = "CALL"
        elif high_potential_put and pair_key not in alerted_pairs:
            send_telegram_message(f"⚠️ *تجهّز! فرصة هبوط (PUT) قريبة جداً*\nالزوج: `{pair}` [5m]\nيرجى فتح الشارت وتجهيز الصفقة!")
            alerted_pairs[pair_key] = "PUT"

    # 3. إرسال الإشارة النهائية عند فتح الشمعة (توسيع وقت التأكيد لـ 10 ثوانٍ)
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
                    'warned_loss': False
                })
                return final_signal

    else:
        if pair_key in alerted_pairs:
            prev_dir = alerted_pairs[pair_key]
            if (prev_dir == "CALL" and curr_k > 60) or (prev_dir == "PUT" and curr_k < 40):
                send_telegram_message(f"❌ *تم إلغاء التنبيه*\nالزوج: `{pair}` [5m]\nالسبب: الشروط لم تعد متوافقة.")
                del alerted_pairs[pair_key]

    return None

# --- تشغيل البوت ---
def run_bot():
    pairs = [
        "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "EURJPY",
        "EURGBP", "AUDCAD", "AUDJPY", "CADJPY", "EURAUD", "GBPJPY", "EURCAD"
    ]
    timeframe = "5m"

    print("🚀 البوت يعمل الآن ويحلل الـ 14 زوج...")
    send_telegram_message("🤖 *تم تحديث وتشغيل البوت بنجاح!*\n⏱️ *الفريم المعتمد:* 5 دقائق حصراً\n⚡ *توسيع نافذة التأكيد لـ 10 ثوانٍ*\nجاري التداول...")

    try:
        while True:
            if not API.check_connect():
                print("⚠️ انقطع الاتصال، جاري إعادة الاتصال...")
                API.connect()

            for pair in pairs:
                try:
                    signal = analyze_pair(pair, timeframe)
                    if signal:
                        print(f"✅ إشارة جديدة لـ {pair}")
                        send_telegram_message(signal)
                except Exception as e:
                    print(f"⚠️ خطأ أثناء تحليل {pair}: {e}")

            check_trade_results()
            time.sleep(1)
    except KeyboardInterrupt:
        print("🛑 تم إيقاف البوت يدوياً.")
    finally:
        on_shutdown()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    run_bot()
