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

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s', handlers=[logging.FileHandler("bot.log", encoding='utf-8'), logging.StreamHandler()])
logger = logging.getLogger(__name__)

app = Flask(__name__)
@app.route('/')
def home():
    return "Bot is Running Successfully!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

logging.getLogger('iqoptionapi').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

CAIRO_TZ = pytz.timezone('Africa/Cairo')
def get_cairo_time():
    return datetime.now(CAIRO_TZ)

IQ_EMAIL = os.environ.get("IQ_EMAIL", "zain1mohamed2425@gmail.com")
IQ_PASSWORD = os.environ.get("IQ_PASSWORD", "ZainMohamed2425@")
ACCOUNT_TYPE = "PRACTICE"
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8794920089:AAFnRnoudkdPrlMtDaijlaQgczrTkaM0MU4")
CHAT_ID = os.environ.get("CHAT_ID", "1462370563")

alerted_pairs = {}
active_trades = []
martingale_queue = {}

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
    return sma + (std * std_dev), sma - (std * std_dev)

def get_fractal_levels(df):
    highs, lows = df['High'], df['Low']
    resistance = highs.rolling(window=5, center=True).apply(lambda x: x[2] if max(x) == x[2] else np.nan, raw=True)
    support = lows.rolling(window=5, center=True).apply(lambda x: x[2] if min(x) == x[2] else np.nan, raw=True)
    last_res = resistance.dropna().iloc[-1] if not resistance.dropna().empty else df['High'].max()
    last_sup = support.dropna().iloc[-1] if not support.dropna().empty else df['Low'].min()
    return last_res, last_sup

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

def on_shutdown():
    logger.warning("البوت يتوقف...")
    send_telegram_message("🔴 *تنبيه: تم إيقاف بوت IQ Option!*")

atexit.register(on_shutdown)

def connect_iqoption():
    logger.info("🔌 جاري الاتصال...")
    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    for attempt in range(5):
        check, reason = api.connect()
        if check:
            logger.info("✅ تم الاتصال!")
            api.change_balance(ACCOUNT_TYPE)
            return api
        logger.error(f"❌ فشل الاتصال ({attempt+1}/5): {reason}")
        if attempt < 4:
            time.sleep(5)
    send_telegram_message(f"❌ *فشل الاتصال:* `{reason}`")
    raise ConnectionError("فشل الاتصال")

API = connect_iqoption()

def analyze_martingale(pair, original_direction):
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
        last, prev = df.iloc[-2], df.iloc[-3]
        price, open_price, low, high = last['Close'], last['Open'], last['Low'], last['High']
        alma, rsi, stoch_k, stoch_d = last['ALMA'], last['RSI'], last['Stoch_K'], last['Stoch_D']
        bbl, bbu, volume, vol_ma = last['BBL'], last['BBU'], last['Volume'], last['Vol_MA']
        is_strong = abs(price - open_price) > ((high - low) * 0.25)
        valid_vol = volume > (vol_ma * 0.8)
        near_sup = abs(price - support) <= (price * 0.0005) or low <= (bbl * 1.001)
        near_res = abs(price - resistance) <= (price * 0.0005) or high >= (bbu * 0.999)
        a9p, a50p, a9c, a50c = prev['ALMA'], prev['ALMA_50'], last['ALMA'], last['ALMA_50']
        smc = (a9p <= a50p) and (a9c > a50c) and (stoch_k > stoch_d) and is_strong and valid_vol
        smp = (a9p >= a50p) and (a9c < a50c) and (stoch_k < stoch_d) and is_strong and valid_vol
        direction = None
        if smc:
            direction = "CALL"
        elif smp:
            direction = "PUT"
        elif is_strong and valid_vol:
            if price > alma and stoch_k > stoch_d and rsi <= 50 and near_sup and stoch_k < 40:
                direction = "CALL"
            elif price < alma and stoch_k < stoch_d and rsi >= 50 and near_res and stoch_k > 60:
                direction = "PUT"
        if direction and direction != original_direction:
            return direction
        return None
    except Exception as e:
        logger.error(f"خطأ تحليل المضاعفة: {e}")
        return None

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
                cp, ep, d = candles[-1]['close'], trade['entry_price'], trade['direction']
                losing = (d == "CALL" and cp < ep) or (d == "PUT" and cp > ep)
                if losing:
                    send_telegram_message(f"⏳ *تنبيه مبكر*\nالزوج: `{trade['pair']}` [5m]\nالصفقة تتجه للخسارة..\n🔍 *جاري تحليل فرصة المضاعفة...*")
                    martingale_queue[trade['pair']] = {'original_direction': d, 'entry_price': ep, 'time': time.time()}
                    trade['warned_loss'] = True
            if time_left <= 0:
                time.sleep(1.5)
                candles = API.get_candles(trade['pair'], 300, 2, time.time())
                fp = candles[-2]['close'] if len(candles) >= 2 else candles[-1]['close']
                ep, d = trade['entry_price'], trade['direction']
                is_win = (d == "CALL" and fp > ep) or (d == "PUT" and fp < ep)
                ts = get_cairo_time().strftime('%I:%M %p')
                pair, is_mg = trade['pair'], trade.get('is_martingale', False)
                if is_mg:
                    msg = f"✅ *نتيجة المضاعفة: رابحة*" if is_win else f"❌ *نتيجة المضاعفة: خاسرة*"
                    msg += f"\nالزوج: `{pair}` [5m]\n⏰ `{ts}`"
                    send_telegram_message(msg)
                    trades_to_remove.append(trade)
                else:
                    if is_win:
                        send_telegram_message(f"✅ *نتيجة الصفقة: رابحة* 🎯\nالزوج: `{pair}` [5m]\n⏰ `{ts}`")
                        trades_to_remove.append(trade)
                    else:
                        martingale_queue[pair] = {'original_direction': d, 'entry_price': ep, 'time': time.time()}
                        send_telegram_message(f"❌ *الصفقة خاسرة*\nالزوج: `{pair}` [5m]\n⏰ `{ts}`\n\n🔍 *جاري تحليل السوق لإيجاد أفضل فرصة مضاعفة...*")
                        trades_to_remove.append(trade)
        except Exception as e:
            logger.error(f"خطأ متابعة {trade['pair']}: {e}")
    for trade in trades_to_remove:
        if trade in active_trades:
            active_trades.remove(trade)

def analyze_pair(pair, timeframe="5m"):
    tf_seconds, duration_text, expire_delay = 300, "5 دقائق", 300
    try:
        raw_candles = API.get_candles(pair, tf_seconds, 60, time.time())
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
    last, prev, curr = df.iloc[-2], df.iloc[-3], df.iloc[-1]
    price, open_price, low, high = last['Close'], last['Open'], last['Low'], last['High']
    alma, rsi, stoch_k, stoch_d = last['ALMA'], last['RSI'], last['Stoch_K'], last['Stoch_D']
    bbl, bbu, volume, vol_ma = last['BBL'], last['BBU'], last['Volume'], last['Vol_MA']
    
    # ========== فلاتر جديدة أقوى ==========
    is_strong = abs(price - open_price) > ((high - low) * 0.30)  # 30% بدل 25%
    
    # فلتر الاتجاه العام: ALMA 9 فوق/تحت 50
    trend_up = last['ALMA'] > last['ALMA_50']
    trend_down = last['ALMA'] < last['ALMA_50']
    
    # فلاتر الحجم المختلفة لكل نوع
    vol_super = volume > (vol_ma * 1.2)   # سوبر ماكس: حجم أعلى 20%
    vol_max = volume > (vol_ma * 1.1)     # ماكس: حجم أعلى 10%
    vol_strong = volume > (vol_ma * 1.0)  # قوية: حجم أعلى من المتوسط
    
    near_sup = abs(price - support) <= (price * 0.0005) or low <= (bbl * 1.001)
    near_res = abs(price - resistance) <= (price * 0.0005) or high >= (bbu * 0.999)
    
    pair_key = f"{pair}_5m"
    cn = get_cairo_time()
    cts = cn.strftime('%I:%M %p')
    csec = (cn.minute % 5) * 60 + cn.second
    cmin = cn.minute % 5
    final_signal, direction = None, None

    # ========== سوبر ماكس (زي ما هو + حجم أقوى) ==========
    a9p, a50p, a9c, a50c = prev['ALMA'], prev['ALMA_50'], last['ALMA'], last['ALMA_50']
    smc = (a9p <= a50p) and (a9c > a50c) and (stoch_k > stoch_d) and is_strong and vol_super
    smp = (a9p >= a50p) and (a9c < a50c) and (stoch_k < stoch_d) and is_strong and vol_super
    if smc:
        direction, final_signal = "CALL", f"👑 *إشارة سوبر ماكس (SUPER MAX) - تقاطع صاعد* 🔥\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{cts}`"
    elif smp:
        direction, final_signal = "PUT", f"👑 *إشارة سوبر ماكس (SUPER MAX) - تقاطع هابط* 🔥\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{cts}`"

    # ========== ماكس (أقوى بكثير) ==========
    if not final_signal and is_strong and vol_max:
        # CALL ماكس: مع الاتجاه الصاعد + RSI < 45 + Stoch < 25
        if price > alma and stoch_k > stoch_d and rsi < 45 and near_support and stoch_k < 25 and trend_up:
            direction, final_signal = "CALL", f"🔥 *إشارة (CALL) - القوة: ماكس*\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{cts}`"
        # PUT ماكس: مع الاتجاه الهابط + RSI > 55 + Stoch > 75
        elif price < alma and stoch_k < stoch_d and rsi > 55 and near_resistance and stoch_k > 75 and trend_down:
            direction, final_signal = "PUT", f"🔥 *إشارة (PUT) - القوة: ماكس*\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{cts}`"

    # ========== قوية جداً (مع الاتجاه + RSI أصعب) ==========
    if not final_signal and is_strong and vol_strong:
        if price > alma and stoch_k > stoch_d and rsi < 45 and near_support and stoch_k < 35 and trend_up:
            direction, final_signal = "CALL", f"🚀 *إشارة (CALL) - القوة: قوية جداً*\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{cts}`"
        elif price < alma and stoch_k < stoch_d and rsi > 55 and near_resistance and stoch_k > 65 and trend_down:
            direction, final_signal = "PUT", f"📉 *إشارة (PUT) - القوة: قوية جداً*\nالزوج: `{pair}` (IQ Option) [5m]\n⏱️ *مدة الصفقة:* {duration_text}\n⏰ *وقت الإشارة:* `{cts}`"

    # فحص المضاعفات
    if pair in martingale_queue:
        mg = martingale_queue[pair]
        if direction and direction != mg['original_direction']:
            da = "صعود (CALL)" if direction == "CALL" else "هبوط (PUT)"
            send_telegram_message(f"🎯 *فرصة المضاعفة جاهزة!*\nالزوج: `{pair}` [5m]\nالاتجاه: *{da}*\n⏰ `{cts}`\n\n⚡ *جهز الدخول الآن!*")
            active_trades.append({'pair': pair, 'timeframe': '5m', 'direction': direction, 'entry_price': curr['Open'], 'expire_time': time.time() + expire_delay, 'warned_loss': True, 'is_martingale': True})
            del martingale_queue[pair]
            return None
        elif time.time() - mg['time'] > 1200:
            send_telegram_message(f"❌ *تم إلغاء فرصة المضاعفة*\nالزوج: `{pair}` [5m]\nالسبب: لم يتم العثور على إشارة قوية.")
            del martingale_queue[pair]

    # التجهيز المسبق مع توقع نوع الإشارة
    crp, crk, crs, cra = curr['Close'], curr['Stoch_K'], curr['RSI'], curr['ALMA']
    crd = curr['Stoch_D']
    ca9, ca50 = curr['ALMA'], curr['ALMA_50']
    pa9, pa50 = last['ALMA'], last['ALMA_50']
    ctrend_up = ca9 > ca50
    ctrend_down = ca9 < ca50
    
    predicted_type = None
    if (pa9 <= pa50 and ca9 > ca50 and crk > crd) or (pa9 >= pa50 and ca9 < ca50 and crk < crd):
        predicted_type = "👑 سوبر ماكس"
    elif crp > cra and crk > crd and crs < 45 and ctrend_up:
        if crk < 25:
            predicted_type = "🔥 ماكس"
        elif crk < 35:
            predicted_type = "🚀 قوية جداً"
    elif crp < cra and crk < crd and crs > 55 and ctrend_down:
        if crk > 75:
            predicted_type = "🔥 ماكس"
        elif crk > 65:
            predicted_type = "🚀 قوية جداً"

    hpc = (crp > cra) and (crk <= 40) and (crs <= 50)
    hpp = (crp < cra) and (crk >= 60) and (crs >= 50)

    if cmin == 4 and csec >= 30:
        if hpc and pair_key not in alerted_pairs:
            pt = f" من نوع *{predicted_type}*" if predicted_type else ""
            send_telegram_message(f"⚠️ *تجهّز! فرصة صعود (CALL){pt}* قريبة جداً\nالزوج: `{pair}` [5m]\nيرجى فتح الشارت وتجهيز الصفقة!")
            alerted_pairs[pair_key] = "CALL"
        elif hpp and pair_key not in alerted_pairs:
            pt = f" من نوع *{predicted_type}*" if predicted_type else ""
            send_telegram_message(f"⚠️ *تجهّز! فرصة هبوط (PUT){pt}* قريبة جداً\nالزوج: `{pair}` [5m]\nيرجى فتح الشارت وتجهيز الصفقة!")
            alerted_pairs[pair_key] = "PUT"

    if final_signal:
        if csec <= 10:
            if pair_key in alerted_pairs:
                del alerted_pairs[pair_key]
                active_trades.append({'pair': pair, 'timeframe': '5m', 'direction': direction, 'entry_price': curr['Open'], 'expire_time': time.time() + expire_delay, 'warned_loss': False, 'is_martingale': False})
                return final_signal
    else:
        if pair_key in alerted_pairs:
            prev_alert_dir = alerted_pairs[pair_key]
            if (prev_alert_dir == "CALL" and crk > 60) or (prev_alert_dir == "PUT" and crk < 40):
                send_telegram_message(f"❌ *تم إلغاء التنبيه*\nالزوج: `{pair}` [5m]\nالسبب: الشروط لم تعد متوافقة.")
                del alerted_pairs[pair_key]
    return None

def run_bot():
    pairs = ["EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF", "EURJPY", "EURGBP", "AUDCAD", "AUDJPY", "CADJPY", "EURAUD", "GBPJPY", "EURCAD"]
    logger.info("🚀 البوت يعمل...")
    send_telegram_message("🤖 *تم تشغيل بوت IQ Option!*\n⏱️ *الفريم:* 5 دقائق\n⚡ *الدخول:* أول 10 ثوانٍ\n🔍 *المضاعفة:* بتحليل جديد\n📊 *الفلاتر الجديدة:* اتجاه عام + RSI أصعب + حجم أقوى")
    try:
        while True:
            try:
                if not API.check_connect():
                    API.connect()
                for pair in pairs:
                    signal = analyze_pair(pair, "5m")
                    if signal:
                        logger.info(f"إشارة: {pair}")
                        send_telegram_message(signal)
                    time.sleep(0.3)
                check_trade_results()
            except Exception as e:
                logger.error(f"خطأ: {e}")
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("تم الإيقاف")
    finally:
        on_shutdown()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    run_bot()
