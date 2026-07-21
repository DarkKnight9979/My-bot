import logging
import time
import requests
import pandas as pd
import ta
import atexit
import pytz
from datetime import datetime
from iqoptionapi.stable_api import IQ_Option
from flask import Flask
from threading import Thread

# --- سيرفر بسيط لإبقاء الخدمة تعمل 24/7 على Render ---
app = Flask('')

@app.route('/')
def home():
    return "Bot is alive and running!"

def run_server():
    app.run(host='0.0.0.0', port=10000)

def keep_alive():
    t = Thread(target=run_server)
    t.start()

# تشغيل سيرفر الـ HTTP في الخلفية
keep_alive()

# --- إغلاق سجلات الـ DEBUG المزعجة ---
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

# --- دالة إرسال الرسائل والتنبيهات ---
def send_telegram_message(message):
    """إرسال رسالة نصية إلى التليجرام"""
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

# --- دالة تحليل المضاعفة عالية الدقة (High Confidence Martingale) ---
def analyze_martingale_direction(pair, original_direction):
    try:
        raw_candles = API.get_candles(pair, 300, 15, time.time())
        if not raw_candles or len(raw_candles) < 10:
            return None

        df = pd.DataFrame(raw_candles)
        df.rename(columns={'open': 'Open', 'max': 'High', 'min': 'Low', 'close': 'Close', 'volume': 'Volume'}, inplace=True)
        
        df['EMA_9'] = ta.ema(df['Close'], length=9)
        stoch = ta.stoch(df['High'], df['Low'], df['Close'], k=14, d=3)
        df = pd.concat([df, stoch], axis=1)

        last = df.iloc[-1]
        k_col = [c for c in df.columns if 'STOCHk' in c or 'STK' in c][0]
        d_col = [c for c in df.columns if 'STOCHd' in c or 'STKd' in c][0]

        price = last['Close']
        open_price = last['Open']
        ema = last['EMA_9']
        stoch_k = last[k_col]
        stoch_d = last[d_col]

        body = abs(price - open_price)
        total_range = last['High'] - last['Low']

        is_strong_reversal = body > (total_range * 0.6)

        if original_direction == "CALL":
            if is_strong_reversal and price < open_price and price < ema:
                return "PUT"
            elif price > ema and stoch_k > stoch_d and stoch_k < 40:
                return "CALL"
            else:
                return None
        else:
            if is_strong_reversal and price > open_price and price > ema:
                return "CALL"
            elif price < ema and stoch_k < stoch_d and stoch_k > 60:
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

# --- الحلقة الرئيسية لتشغيل البوت ---
print("🚀 البوت يعمل الآن ويتابع السوق...")
send_telegram_message("🟢 *تم تشغيل بوت IQ Option بنجاح على السيرفر!*")

while True:
    try:
        check_trade_results()
        time.sleep(2)
    except Exception as e:
        print(f"⚠️ خطأ في الحلقة الرئيسية: {e}")
        time.sleep(5)
