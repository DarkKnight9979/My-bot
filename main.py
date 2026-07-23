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

# --- 8. دالة تحليل المضاعفة الذكية (معدلة لقراءة الأذيال والانعكاس) ---
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
        high = last['High']
        low = last['Low']
        ema = last['EMA_9']
        stoch_k = last['Stoch_K']
        stoch_d = last['Stoch_D']

        body = abs(price - open_price)
        total_range = high - low if (high - low) > 0 else 0.0001
        
        lower_shadow = min(open_price, price) - low
        upper_shadow = high - max(open_price, price)

        # قراءة الانعكاس سواء بجسم الشمعة أو بديل طويل مرتد (Pinbar)
        is_reversal_up = (price > open_price) or (lower_shadow > total_range * 0.35)
        is_reversal_down = (price < open_price) or (upper_shadow > total_range * 0.35)

        if original_direction == "CALL":
            if is_reversal_down and price < ema:
                return "PUT"
            elif stoch_k > stoch_d or is_reversal_up:
                return "CALL"
            else:
                return None
        else:
            if is_reversal_up and price > ema:
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

    for trade in trades_t
