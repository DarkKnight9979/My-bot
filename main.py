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
    return "Bot is Running Successfully V7.0 with King of Signals!"

def run_web_server():
    port = int(os.environ.get("PORT", 10000))
    app.run(host="0.0.0.0", port=port)

logging.getLogger('iqoptionapi').setLevel(logging.WARNING)
logging.getLogger('urllib3').setLevel(logging.WARNING)

# --- 2. التوقيت وزامنته مع سيرفر المنصة ---
CAIRO_TZ = pytz.timezone('Africa/Cairo')
UTC_TZ = pytz.utc
server_time_offset = 0

def get_cairo_time():
    return datetime.now(CAIRO_TZ)

def sync_server_time(api_instance):
    global server_time_offset
    try:
        iq_timestamp = api_instance.get_server_timestamp()
        if iq_timestamp:
            server_time_offset = iq_timestamp - time.time()
            logger.info(f"⏱️ تم مزامنة الوقت مع سيرفر المنصة. الفارق: {server_time_offset:.2f} ثانية")
    except Exception as e:
        logger.warning(f"⚠️ فشل مزامنة الوقت مع المنصة: {e}")

def get_iq_time():
    return time.time() + server_time_offset

# --- 3. بيانات الاعتماد ---
IQ_EMAIL = os.environ.get("IQ_EMAIL", "zain1mohamed2425@gmail.com")
IQ_PASSWORD = os.environ.get("IQ_PASSWORD", "ZainMohamed2425@")
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
recent_signals = {}
sent_signals = {}
candles_cache = {}
ht_trend_cache = {}
df_cache = {}
news_data = []
last_news_update = 0
news_fetch_failed = False
hunt_mode_announced = {}
last_hunt_message_time = 0
invalid_assets = set()

cycle_count = 0
stats = defaultdict(lambda: {"win": 0, "loss": 0, "total": 0})
telegram_queue = deque()

# ========== King of Signals — متغيرات منفصلة تماماً ==========
king_alerted_pairs = {}
king_recent_signals = {}
king_sent_signals = {}
king_df_cache = {}
king_htf_cache = {}
king_stats = defaultdict(lambda: {"win": 0, "loss": 0, "total": 0})


# ========== STATISTICAL ENGINE (V7.1) ==========
# محرك إحصائي ذكي — منفصل 100% عن الاستراتيجيات

import json
import os
from pathlib import Path

# --- ملفات البيانات ---
TRADE_LOG_FILE = "trade_log.jsonl"
STATS_STATE_FILE = "stats_state.json"
OPTIMIZATION_PROPOSAL_FILE = "optimization_proposal.json"
WEIGHTS_FILE = "king_weights.json"

# --- إنشاء الملفات لو مش موجودة ---
for f in [TRADE_LOG_FILE, STATS_STATE_FILE, OPTIMIZATION_PROPOSAL_FILE, WEIGHTS_FILE]:
    if not os.path.exists(f):
        Path(f).touch()

# --- الأوزان الافتراضية للـ King Strategy ---
DEFAULT_KING_WEIGHTS = {
    "structure": 20,
    "sweep": 20,
    "trend": 10,
    "momentum": 15,
    "volatility": 10,
    "adx": 10,
    "rsi": 5,
    "stochastic": 5,
    "candle": 15
}

def load_king_weights():
    try:
        with open(WEIGHTS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
            if data and isinstance(data, dict):
                return {k: int(v) for k, v in data.items()}
    except:
        pass
    return DEFAULT_KING_WEIGHTS.copy()

def save_king_weights(weights):
    with open(WEIGHTS_FILE, 'w', encoding='utf-8') as f:
        json.dump(weights, f, indent=2)

KING_WEIGHTS = load_king_weights()

# --- تسجيل الصفقات ---
def log_trade(trade_data):
    """
    يسجل صفقة واحدة في ملف JSONL.
    trade_data: dict مع كل تفاصيل الصفقة والفلاتر.
    """
    try:
        with open(TRADE_LOG_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(trade_data, ensure_ascii=False) + "\n")
    except Exception as e:
        logger.error(f"خطأ في تسجيل الصفقة: {e}")

def read_trade_log(max_entries=50000):
    """
    يقرأ آخر N صفقة من ملف JSONL.
    يرجع: list of dicts
    """
    trades = []
    try:
        with open(TRADE_LOG_FILE, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        for line in lines[-max_entries:]:
            line = line.strip()
            if line:
                try:
                    trades.append(json.loads(line))
                except:
                    continue
    except Exception as e:
        logger.error(f"خطأ في قراءة سجل الصفقات: {e}")
    return trades

# --- تقييم الفلاتر ---
def evaluate_filters(trades):
    """
    يحسب Win Rate لكل فلتر على حدة.
    يرجع: dict {filter_name: {"win": N, "loss": N, "wr": %, "worth": "High/Med/Low"}}
    """
    if not trades:
        return {}

    filter_stats = {}

    # نجمع كل أسماء الفلاتر من أول صفقة
    sample_filters = trades[0].get("filters", {})
    for fname in sample_filters.keys():
        filter_stats[fname] = {"win": 0, "loss": 0, "total": 0}

    for trade in trades:
        outcome = trade.get("outcome", "")
        filters = trade.get("filters", {})
        for fname, fval in filters.items():
            if fname not in filter_stats:
                continue
            if fval:  # الفلتر متحقق
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
            if wr >= 80:
                worth = "High"
            elif wr >= 65:
                worth = "Med"
            else:
                worth = "Low"
            results[fname] = {
                "win": stat["win"],
                "loss": stat["loss"],
                "total": total,
                "wr": round(wr, 1),
                "worth": worth
            }

    # ترتيب حسب Win Rate
    results = dict(sorted(results.items(), key=lambda x: x[1]["wr"], reverse=True))
    return results

# --- ترتيب الأزواج ---
def rank_pairs(trades):
    """
    يرجع ترتيب الأزواج حسب الأداء.
    Score = (WR * 0.4) + (ProfitFactor * 20 * 0.3) + (Stability * 0.2) + (CountRatio * 0.1)
    """
    if not trades:
        return {}

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

        # Profit Factor
        avg_win = 1  # binary options
        avg_loss = 1
        profit_factor = avg_win / avg_loss if avg_loss > 0 else 1
        if data["loss"] > 0:
            profit_factor = data["win"] / data["loss"]

        # Stability (نسبة الاتساق — std dev من WR في شرائح)
        chunks = [data["wins"][i:i+10] for i in range(0, len(data["wins"]), 10)]
        chunk_wrs = []
        for chunk in chunks:
            if chunk:
                chunk_wrs.append(sum(chunk) / len(chunk) * 100)
        stability = 100 - np.std(chunk_wrs) if chunk_wrs and len(chunk_wrs) > 1 else 50

        count_ratio = (total / max_total) * 100

        score = (wr * 0.4) + (min(profit_factor, 5) * 20 * 0.3) + (stability * 0.2) + (count_ratio * 0.1)

        rankings.append({
            "pair": pair,
            "wr": round(wr, 1),
            "total": total,
            "profit_factor": round(profit_factor, 2),
            "stability": round(stability, 1),
            "score": round(score, 1)
        })

    rankings.sort(key=lambda x: x["score"], reverse=True)
    return rankings

# --- تحليل الأوقات ---
def analyze_hours(trades):
    """
    يحسب Win Rate لكل ساعة من اليوم.
    """
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
                "win": stat["win"],
                "loss": stat["loss"],
                "total": stat["total"],
                "wr": round((stat["win"] / stat["total"]) * 100, 1)
            }

    return dict(sorted(results.items(), key=lambda x: x[1]["wr"], reverse=True))

# --- تحليل الـ Confidence Calibration ---
def analyze_confidence_calibration(trades):
    """
    يشيك لو الـ Score بيوحي بـ WR صحيح ولا لا.
    مثلاً: Score 90–94 المفروض يكسب ~90%، لو كسب 70% → البوت بيبالغ.
    """
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
                "total": len(trades_in_bucket),
                "actual_wr": round(actual_wr, 1),
                "expected_wr": expected_wr,
                "diff": round(diff, 1),
                "status": "✅ متوازن" if abs(diff) <= 5 else ("⚠️ يبالغ" if diff < -5 else "🔥 أقوى من المتوقع")
            }

    return calibration

# --- Grid Search Optimization ---
def grid_search_optimization(trades, strategy="king"):
    """
    يجرب تركيبات مختلفة من العتبات ويطلع الأفضل.
    يرجع: dict بالتعديلات المقترحة.
    """
    if len(trades) < 200:
        return None, "غير كافي — محتاج 200+ صفقة"

    # نحلل بس على King Strategy
    king_trades = [t for t in trades if t.get("strategy") == "king"]
    if len(king_trades) < 100:
        return None, "غير كافي — محتاج 100+ صفقة King"

    proposals = []

    # 1. ADX Threshold
    best_adx = 22
    best_adx_wr = 0
    for adx_thresh in [18, 20, 22, 24, 26, 28, 30]:
        subset = [t for t in king_trades if t.get("indicators", {}).get("adx", 0) >= adx_thresh]
        if len(subset) >= 20:
            wins = sum(1 for t in subset if t.get("outcome") == "win")
            wr = (wins / len(subset)) * 100
            if wr > best_adx_wr:
                best_adx_wr = wr
                best_adx = adx_thresh

    if best_adx != 22:
        proposals.append({
            "filter": "ADX",
            "current": 22,
            "proposed": best_adx,
            "reason": f"WR يتحسن لـ {best_adx_wr:.1f}% مع ADX ≥ {best_adx}",
            "impact": "يقلل الإشارات قليلاً ويرفع الجودة"
        })

    # 2. RSI Range for CALL
    best_rsi_low, best_rsi_high = 45, 60
    best_rsi_wr = 0
    for low in range(40, 50, 2):
        for high in range(55, 65, 2):
            subset = [t for t in king_trades 
                      if t.get("direction") == "CALL" 
                      and low <= t.get("indicators", {}).get("rsi", 0) <= high]
            if len(subset) >= 15:
                wins = sum(1 for t in subset if t.get("outcome") == "win")
                wr = (wins / len(subset)) * 100
                if wr > best_rsi_wr:
                    best_rsi_wr = wr
                    best_rsi_low, best_rsi_high = low, high

    if (best_rsi_low, best_rsi_high) != (45, 60):
        proposals.append({
            "filter": "RSI CALL",
            "current": "45–60",
            "proposed": f"{best_rsi_low}–{best_rsi_high}",
            "reason": f"WR يتحسن لـ {best_rsi_wr:.1f}%",
            "impact": "تعديل دقيق لنطاق RSI"
        })

    # 3. Body % Threshold
    best_body = 0.60
    best_body_wr = 0
    for body in [0.50, 0.55, 0.60, 0.65, 0.70]:
        # نحسب body% من الصفقات المسجلة (مش متاح مباشرة، نستخدم proxy)
        subset = [t for t in king_trades if t.get("filters", {}).get("candle_ok", False)]
        if len(subset) >= 20:
            wins = sum(1 for t in subset if t.get("outcome") == "win")
            wr = (wins / len(subset)) * 100
            if wr > best_body_wr:
                best_body_wr = wr
                best_body = body

    # 4. Sweep Threshold
    best_sweep = 0.0003
    best_sweep_wr = 0
    for sweep in [0.0002, 0.0003, 0.0004, 0.0005]:
        subset = [t for t in king_trades if t.get("filters", {}).get("sweep_ok", False)]
        if len(subset) >= 20:
            wins = sum(1 for t in subset if t.get("outcome") == "win")
            wr = (wins / len(subset)) * 100
            if wr > best_sweep_wr:
                best_sweep_wr = wr
                best_sweep = sweep

    if best_sweep != 0.0003:
        proposals.append({
            "filter": "Sweep Threshold",
            "current": 0.0003,
            "proposed": best_sweep,
            "reason": f"WR يتحسن لـ {best_sweep_wr:.1f}%",
            "impact": "تعديل حساسية Liquidity Sweep"
        })

    return proposals, "تم التحليل بنجاح"

# --- Weight Engine: تعديل أوزان King Strategy ---
def optimize_weights(trades):
    """
    يحسب الأداء الحقيقي لكل فلتر ويعدّل أوزانه.
    الفلتر اللي بيكسب أكتر → وزنه يزيد.
    الفلتر اللي بيفشل → وزنه يقل.
    """
    if len(trades) < 300:
        return None, "غير كافي — محتاج 300+ صفقة"

    king_trades = [t for t in trades if t.get("strategy") == "king"]
    if len(king_trades) < 150:
        return None, "غير كافي — محتاج 150+ صفقة King"

    filter_performance = {}
    current_weights = load_king_weights()

    for fname in current_weights.keys():
        with_filter = [t for t in king_trades if t.get("filters", {}).get(fname, False)]
        without_filter = [t for t in king_trades if not t.get("filters", {}).get(fname, False)]

        if len(with_filter) >= 20 and len(without_filter) >= 20:
            wr_with = sum(1 for t in with_filter if t.get("outcome") == "win") / len(with_filter) * 100
            wr_without = sum(1 for t in without_filter if t.get("outcome") == "win") / len(without_filter) * 100

            filter_performance[fname] = {
                "wr_with": wr_with,
                "wr_without": wr_without,
                "diff": wr_with - wr_without,
                "count": len(with_filter)
            }

    if not filter_performance:
        return None, "لا توجد بيانات كافية"

    # تعديل الأوزان
    new_weights = current_weights.copy()
    total_weight = sum(current_weights.values())

    adjustments = []
    for fname, perf in filter_performance.items():
        diff = perf["diff"]
        current = current_weights[fname]

        if diff > 10:  # الفلتر ده بيفرق كتير
            new_w = min(current + 3, 25)
            adjustments.append(f"{fname}: {current} → {new_w} (+{diff:.1f}% WR)")
        elif diff < -5:  # الفلتر ده مش بيفرق
            new_w = max(current - 2, 3)
            adjustments.append(f"{fname}: {current} → {new_w} ({diff:.1f}% WR)")
        else:
            new_w = current

        new_weights[fname] = new_w

    # Normalization: نضمن المجموع = 100
    current_sum = sum(new_weights.values())
    if current_sum != 100:
        factor = 100 / current_sum
        new_weights = {k: max(1, round(v * factor)) for k, v in new_weights.items()}
        # تعديل بسيط عشان المجموع يبقى 100 بالظبط
        diff = 100 - sum(new_weights.values())
        if diff != 0:
            # نضيف الفرق على أكبر وزن
            max_key = max(new_weights, key=new_weights.get)
            new_weights[max_key] += diff

    return {
        "old_weights": current_weights,
        "new_weights": new_weights,
        "adjustments": adjustments,
        "filter_performance": filter_performance
    }, "تم تحديث الأوزان"

# --- توليد التقرير ---
def generate_report(trades, period="daily"):
    """
    ينتج تقرير شامل.
    period: daily / weekly / monthly
    """
    if not trades:
        return None

    total = len(trades)
    wins = sum(1 for t in trades if t.get("outcome") == "win")
    losses = total - wins
    wr = (wins / total * 100) if total > 0 else 0

    # Profit Factor
    pf = wins / losses if losses > 0 else float('inf')

    # Max streaks
    max_win_streak = 0
    max_loss_streak = 0
    current_win = 0
    current_loss = 0
    for t in trades:
        if t.get("outcome") == "win":
            current_win += 1
            current_loss = 0
            max_win_streak = max(max_win_streak, current_win)
        else:
            current_loss += 1
            current_win = 0
            max_loss_streak = max(max_loss_streak, current_loss)

    # Expectancy
    avg_win = 1
    avg_loss = 1
    expectancy = (avg_win * (wr/100)) - (avg_loss * (1 - wr/100))

    # Best/Worst pairs
    pair_rank = rank_pairs(trades)
    best_pair = pair_rank[0] if pair_rank else None
    worst_pair = pair_rank[-1] if pair_rank else None

    # Hour analysis
    hour_stats = analyze_hours(trades)
    best_hour = next(iter(hour_stats.items())) if hour_stats else None
    worst_hour = list(hour_stats.items())[-1] if hour_stats else None

    # Strategy breakdown
    orig_trades = [t for t in trades if t.get("strategy") == "original"]
    king_trades = [t for t in trades if t.get("strategy") == "king"]

    orig_wr = (sum(1 for t in orig_trades if t.get("outcome") == "win") / len(orig_trades) * 100) if orig_trades else 0
    king_wr = (sum(1 for t in king_trades if t.get("outcome") == "win") / len(king_trades) * 100) if king_trades else 0

    report = {
        "period": period,
        "total_trades": total,
        "wins": wins,
        "losses": losses,
        "win_rate": round(wr, 1),
        "profit_factor": round(pf, 2),
        "max_win_streak": max_win_streak,
        "max_loss_streak": max_loss_streak,
        "expectancy": round(expectancy, 3),
        "best_pair": best_pair,
        "worst_pair": worst_pair,
        "best_hour": best_hour,
        "worst_hour": worst_hour,
        "original": {"total": len(orig_trades), "wr": round(orig_wr, 1)},
        "king": {"total": len(king_trades), "wr": round(king_wr, 1)},
        "filter_eval": evaluate_filters(trades),
        "calibration": analyze_confidence_calibration(trades),
        "pair_rankings": pair_rank[:5] if len(pair_rank) >= 5 else pair_rank,
    }

    return report

def format_report_message(report):
    """
    يحول التقرير لرسالة تليجرام منسقة.
    """
    if not report:
        return "📊 *لا توجد بيانات كافية للتقرير*"

    period_name = {"daily": "اليومي", "weekly": "الأسبوعي", "monthly": "الشهري"}.get(report["period"], report["period"])

    msg = (
        f"📊 *تقرير {period_name}*\n"
        f"━━━━━━━━━━━━━━━━━━━━\n"
        f"📈 *إجمالي الصفقات:* {report['total_trades']}\n"
        f"✅ *رابحة:* {report['wins']} | ❌ *خاسرة:* {report['losses']}\n"
        f"🎯 *Win Rate:* {report['win_rate']}%\n"
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
    msg += f"  أصلية: {report['original']['total']} صفقة — WR: {report['original']['wr']}%\n"
    msg += f"  👑 King: {report['king']['total']} صفقة — WR: {report['king']['wr']}%\n"

    # Filter evaluation
    if report.get("filter_eval"):
        msg += f"\n🔬 *ترتيب الفلاتر:*\n"
        for i, (fname, fdata) in enumerate(list(report["filter_eval"].items())[:5], 1):
            emoji = "🟢" if fdata["worth"] == "High" else ("🟡" if fdata["worth"] == "Med" else "🔴")
            msg += f"  {i}. {emoji} `{fname}` — WR: {fdata['wr']}% ({fdata['worth']})\n"

    # Calibration
    if report.get("calibration"):
        msg += f"\n⚖️ *معايرة الثقة:*\n"
        for bucket, cdata in report["calibration"].items():
            msg += f"  {bucket}: {cdata['status']} (Actual: {cdata['actual_wr']}% vs Expected: {cdata['expected_wr']}%)\n"

    return msg

# --- اقتراح التحسين وإرساله ---
def generate_and_send_optimization_proposal():
    """
    يولد اقتراح تحسين ويبعته على تليجرام.
    """
    trades = read_trade_log(max_entries=5000)

    # Grid Search
    proposals, status = grid_search_optimization(trades)

    # Weight Engine
    weight_result, weight_status = optimize_weights(trades)

    if not proposals and not weight_result:
        logger.info(f"📊 Optimization: {status}")
        return

    # بناء الرسالة
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
        msg += f"\n⚖️ *تعديلات أوزان King Strategy:*\n"
        for adj in weight_result["adjustments"]:
            msg += f"   • {adj}\n"
        msg += f"\n📊 الأوزان الجديدة:\n"
        for k, v in weight_result["new_weights"].items():
            old = weight_result["old_weights"].get(k, v)
            arrow = "↗️" if v > old else ("↘️" if v < old else "➡️")
            msg += f"   {arrow} `{k}`: {old} → {v}\n"

    msg += (
        f"\n━━━━━━━━━━━━━━━━━━━━\n"
        f"✅ *للموافقة:* رد بكلمة `موافق`\n"
        f"❌ *للرفض:* رد بكلمة `رفض`\n"
        f"⏳ *متاح لـ 24 ساعة*\n\n"
        f"⚠️ *تحذير:* التعديل هيغير عتبات الاستراتيجيات."
    )

    # حفظ الاقتراح في ملف
    proposal_data = {
        "timestamp": get_iq_time(),
        "proposals": proposals or [],
        "weight_result": weight_result,
        "status": "pending",
        "message": msg
    }
    with open(OPTIMIZATION_PROPOSAL_FILE, 'w', encoding='utf-8') as f:
        json.dump(proposal_data, f, ensure_ascii=False, indent=2)

    send_telegram_message(msg)
    logger.info("🔧 تم إرسال اقتراح تحسين على تليجرام")

# --- معالجة رد المستخدم ---
def handle_optimization_reply(reply_text):
    """
    يتعامل مع رد المستخدم على اقتراح التحسين.
    """
    reply_lower = reply_text.lower().strip()

    try:
        with open(OPTIMIZATION_PROPOSAL_FILE, 'r', encoding='utf-8') as f:
            proposal = json.load(f)
    except:
        return False, "لا يوجد اقتراح نشط"

    if proposal.get("status") != "pending":
        return False, "الاقتراح تم معالجته بالفعل"

    # التحقق من وقت الصلاحية (24 ساعة)
    if get_iq_time() - proposal.get("timestamp", 0) > 86400:
        proposal["status"] = "expired"
        with open(OPTIMIZATION_PROPOSAL_FILE, 'w', encoding='utf-8') as f:
            json.dump(proposal, f, ensure_ascii=False, indent=2)
        return False, "انتهت صلاحية الاقتراح (24 ساعة)"

    if reply_lower in ["موافق", "موافقة", "نعم", "yes", "approve", "ok"]:
        # تطبيق التعديلات
        weight_result = proposal.get("weight_result")
        if weight_result and weight_result.get("new_weights"):
            global KING_WEIGHTS
            KING_WEIGHTS = weight_result["new_weights"]
            save_king_weights(KING_WEIGHTS)
            logger.info("✅ تم تطبيق أوزان King Strategy الجديدة")

        proposal["status"] = "approved"
        with open(OPTIMIZATION_PROPOSAL_FILE, 'w', encoding='utf-8') as f:
            json.dump(proposal, f, ensure_ascii=False, indent=2)

        return True, "✅ تم تطبيق التعديلات بنجاح! البوت يستخدم الآن العتبات الجديدة."

    elif reply_lower in ["رفض", "لا", "no", "reject", "cancel"]:
        proposal["status"] = "rejected"
        with open(OPTIMIZATION_PROPOSAL_FILE, 'w', encoding='utf-8') as f:
            json.dump(proposal, f, ensure_ascii=False, indent=2)

        return True, "❌ تم رفض الاقتراح. البوت يستمر بالعتبات الحالية."

    return False, None

# --- الخلفية Worker ---
def stats_engine_worker():
    """
    يشتغل في الخلفية كل ساعة.
    يولد تقارير واقتراحات تحسين.
    """
    logger.info("📊 Statistical Engine Worker started")

    last_daily_report = 0
    last_weekly_report = 0
    last_monthly_report = 0
    last_optimization = 0

    while True:
        try:
            now = get_iq_time()
            now_dt = datetime.fromtimestamp(now, tz=CAIRO_TZ)

            trades = read_trade_log(max_entries=10000)

            # تقرير يومي — الساعة 00:00 بتوقيت القاهرة
            if now_dt.hour == 0 and now - last_daily_report > 3600:
                # ناخد صفقات آخر 24 ساعة
                day_trades = [t for t in trades if now - t.get("timestamp", 0) <= 86400]
                report = generate_report(day_trades, "daily")
                msg = format_report_message(report)
                send_telegram_message(msg)
                last_daily_report = now
                logger.info("📊 تم إرسال التقرير اليومي")

            # تقرير أسبوعي — السبت الساعة 00:00
            if now_dt.weekday() == 5 and now_dt.hour == 0 and now - last_weekly_report > 3600:
                week_trades = [t for t in trades if now - t.get("timestamp", 0) <= 604800]
                report = generate_report(week_trades, "weekly")
                msg = format_report_message(report)
                send_telegram_message(msg)

                # مع الـ Weekly Report نبعت اقتراح تحسين
                if len(trades) >= 200:
                    generate_and_send_optimization_proposal()

                last_weekly_report = now
                logger.info("📊 تم إرسال التقرير الأسبوعي + اقتراح تحسين")

            # تقرير شهري — يوم 1 الساعة 00:00
            if now_dt.day == 1 and now_dt.hour == 0 and now - last_monthly_report > 3600:
                month_trades = [t for t in trades if now - t.get("timestamp", 0) <= 2592000]
                report = generate_report(month_trades, "monthly")
                msg = format_report_message(report)
                send_telegram_message(msg)
                last_monthly_report = now
                logger.info("📊 تم إرسال التقرير الشهري")

            # اقتراح تحسين كل 3 أيام (لو فيه بيانات كافية)
            if len(trades) >= 500 and now - last_optimization > 259200:
                generate_and_send_optimization_proposal()
                last_optimization = now

        except Exception as e:
            logger.error(f"خطأ في Statistical Engine: {e}")
            logger.error(traceback.format_exc())

        time.sleep(3600)  # يشيك كل ساعة
KING_SIGNAL_NAMES = {
    1: ("ملك الإشارات 🥉", "KING BRONZE"),      # 80–84
    2: ("ملك الإشارات 🥈", "KING SILVER"),      # 85–89
    3: ("ملك الإشارات 👑", "KING GOLD"),        # 90–94
    4: ("ملك الإشارات 👑🔥", "KING ELITE"),     # 95–100
}
KING_EMOJIS = {1: "🥉", 2: "🥈", 3: "👑", 4: "👑🔥"}

def get_king_level(score):
    if score >= 95:
        return 4
    elif score >= 90:
        return 3
    elif score >= 85:
        return 2
    elif score >= 80:
        return 1
    return 0

# --- 5. دوال المؤشرات ---
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

# ========== King of Signals — دوال المؤشرات الخاصة ==========

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
    return body_pct >= 0.60 and shadow_pct <= 0.30, body_pct

def calculate_king_score(structure_ok, sweep_ok, trend_ok, momentum_ok,
                         volatility_ok, adx_ok, rsi_ok, stoch_ok, candle_ok):
    """
    نظام النقاط (Confidence Score) — يستخدم أوزان ديناميكية من Weight Engine.
    """
    w = KING_WEIGHTS
    score = 0
    if structure_ok: score += w.get('structure', 20)
    if sweep_ok: score += w.get('sweep', 20)
    if trend_ok: score += w.get('trend', 10)
    if momentum_ok: score += w.get('momentum', 15)
    if volatility_ok: score += w.get('volatility', 10)
    if adx_ok: score += w.get('adx', 10)
    if rsi_ok: score += w.get('rsi', 5)
    if stoch_ok: score += w.get('stochastic', 5)
    if candle_ok: score += w.get('candle', 15)
    return score

# --- 6. Cache & Data Management ---
def get_cached_candles(pair, tf, count, max_age=30):
    key = f"{pair}_{tf}_{count}"
    now = get_iq_time()
    if key in candles_cache:
        data, ts = candles_cache[key]
        if now - ts < max_age:
            return data
    try:
        data = API.get_candles(pair, tf, count, int(now))
        if data:
            candles_cache[key] = (data, now)
        return data
    except Exception as e:
        err_str = str(e).lower()
        if "not found" in err_str or "asset" in err_str:
            invalid_assets.add(pair)
            logger.warning(f"⚠️ الزوج {pair} غير متاح في المنصة، تم إزالته من القائمة.")
        else:
            logger.error(f"خطأ جلب شموع {pair}: {e}")
        if key in candles_cache:
            return candles_cache[key][0]
        return None

def get_cached_df(pair, tf, count):
    key = f"{pair}_{tf}_{count}"
    now = get_iq_time()
    if key in df_cache and now - df_cache[key][1] < 15:
        return df_cache[key][0]
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
    df_cache[key] = (df, now)
    return df

# ========== King of Signals — Cache منفصل ==========
def get_cached_df_king(pair, tf, count):
    key = f"king_{pair}_{tf}_{count}"
    now = get_iq_time()
    if key in king_df_cache and now - king_df_cache[key][1] < 15:
        return king_df_cache[key][0]
    raw = get_cached_candles(pair, tf, count, max_age=15)
    if not raw or len(raw) < 60:
        return None
    df = pd.DataFrame(raw)
    df.rename(columns={'open':'Open','max':'High','min':'Low','close':'Close','volume':'Volume'}, inplace=True)
    king_df_cache[key] = (df, now)
    return df

def already_sent_this_candle_king(pair):
    key = f"king_{pair}_{(int(get_iq_time()) // 300) * 300}"
    if key in king_sent_signals:
        return True
    king_sent_signals[key] = get_iq_time()
    return False

def cleanup_memory():
    now = get_iq_time()
    global sent_signals, recent_signals, candles_cache, df_cache, ht_trend_cache, hunt_mode_announced, alerted_pairs
    sent_signals = {k:v for k,v in sent_signals.items() if now - v < 600}
    recent_signals = {k:v for k,v in recent_signals.items() if now - v[0] < 1200}
    for k in list(alerted_pairs.keys()):
        val = alerted_pairs[k]
        if isinstance(val, tuple) and len(val) >= 2:
            if now - val[1] > 480:
                del alerted_pairs[k]
        else:
            del alerted_pairs[k]
    for k in list(candles_cache.keys()):
        if now - candles_cache[k][1] > 300:
            del candles_cache[k]
    for k in list(df_cache.keys()):
        if now - df_cache[k][1] > 300:
            del df_cache[k]
    for k in list(ht_trend_cache.keys()):
        if now - ht_trend_cache[k][1] > 1800:
            del ht_trend_cache[k]
    for k in list(hunt_mode_announced.keys()):
        if now - hunt_mode_announced[k] > 1200:
            del hunt_mode_announced[k]
    # تنظيف King caches
    global king_sent_signals, king_recent_signals, king_df_cache, king_htf_cache, king_alerted_pairs
    king_sent_signals = {k:v for k,v in king_sent_signals.items() if now - v < 600}
    king_recent_signals = {k:v for k,v in king_recent_signals.items() if now - v[0] < 1200}
    for k in list(king_df_cache.keys()):
        if now - king_df_cache[k][1] > 300:
            del king_df_cache[k]
    for k in list(king_htf_cache.keys()):
        if now - king_htf_cache[k][1] > 1800:
            del king_htf_cache[k]
    for k in list(king_alerted_pairs.keys()):
        val = king_alerted_pairs[k]
        if isinstance(val, tuple) and len(val) >= 2:
            if now - val[1] > 480:
                del king_alerted_pairs[k]
        else:
            del king_alerted_pairs[k]

# --- 7. فلتر الأخبار ---
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
    global news_data, last_news_update, news_fetch_failed
    if get_iq_time() - last_news_update < 1800:
        return
    try:
        r = requests.get("https://nfs.faireconomy.media/ff_calendar_thisweek.json", timeout=8)
        if r.status_code == 200:
            news_data = r.json()
            last_news_update = get_iq_time()
            news_fetch_failed = False
            logger.info(f"✅ أخبار محدثة من المصدر الرئيسي: {len(news_data)} حدث")
            return
    except Exception as e:
        logger.warning(f"⚠️ فشل المصدر الرئيسي للأخبار: {e}")
    try:
        r2 = requests.get("https://forexfactory-api.herokuapp.com/get_this_week", timeout=8)
        if r2.status_code == 200:
            news_data = r2.json()
            last_news_update = get_iq_time()
            news_fetch_failed = False
            logger.info("✅ تم جلب الأخبار من المصدر الاحتياطي بنجاح")
            return
    except Exception as e:
        logger.warning(f"⚠️ فشل المصدر الاحتياطي للأخبار: {e}")
    news_fetch_failed = True
    logger.error("❌ فشل المصدران في جلب الأخبار! تفعيل وضع الحماية.")

def is_news_for_pair(pair):
    day_of_week = datetime.now(CAIRO_TZ).weekday()
    if day_of_week in [5, 6]:
        return False
    update_news()
    if news_fetch_failed:
        logger.warning("⚠️ شبكة الأخبار غير متاحة، الإشارات مستمرة مع مراقبة يدوية")
        return False
    now = datetime.now(UTC_TZ)
    for ev in news_data:
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
        except:
            continue
    return False

# --- 8. فلتر افتتاح السوق ---
def is_market_open_chaos():
    day_of_week = datetime.now(CAIRO_TZ).weekday()
    if day_of_week in [5, 6]:
        return False
    now = get_cairo_time()
    hm = now.hour * 100 + now.minute
    if (1000 <= hm <= 1030) or (1530 <= hm <= 1600):
        return True
    return False

# --- 9. فلتر فريم الساعة (الأصلي) ---
def get_higher_tf_trend(pair):
    if pair in ht_trend_cache and get_iq_time() - ht_trend_cache[pair][1] < 900:
        return ht_trend_cache[pair][0]
    try:
        candles = get_cached_candles(pair, 3600, 10, max_age=300)
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
        ht_trend_cache[pair] = (trend, get_iq_time())
        return trend
    except Exception as e:
        logger.error(f"خطأ HTF {pair}: {e}")
        return None

# ========== King HTF Filter (15m) ==========
def get_king_htf_trend(pair):
    key = f"king_htf_{pair}"
    now = get_iq_time()
    if key in king_htf_cache and now - king_htf_cache[key][1] < 900:
        return king_htf_cache[key][0]
    try:
        candles = get_cached_candles(pair, 900, 20, max_age=300)
        if not candles or len(candles) < 10:
            return None
        df_h = pd.DataFrame(candles)
        df_h.rename(columns={'close':'Close'}, inplace=True)
        df_h['ALMA_20'] = calculate_alma(df_h['Close'], 20, 0.85, 6)
        df_h['ALMA_80'] = calculate_alma(df_h['Close'], 80, 0.85, 6)
        curr_h = df_h.iloc[-1]
        prev_h = df_h.iloc[-2]
        if curr_h['ALMA_20'] > curr_h['ALMA_80'] and prev_h['ALMA_20'] > prev_h['ALMA_80']:
            trend = "CALL"
        elif curr_h['ALMA_20'] < curr_h['ALMA_80'] and prev_h['ALMA_20'] < prev_h['ALMA_80']:
            trend = "PUT"
        else:
            trend = None
        king_htf_cache[key] = (trend, now)
        return trend
    except Exception as e:
        logger.error(f"خطأ King HTF {pair}: {e}")
        return None

# --- 10. فلاتر الجودة ---
def check_candle_quality(c, min_body_pct=0.12):
    body = abs(c['Close'] - c['Open'])
    rng = c['High'] - c['Low']
    if rng == 0:
        return False
    bp = body / rng
    if bp < min_body_pct:
        return False
    up_sh = c['High'] - max(c['Close'], c['Open'])
    lo_sh = min(c['Close'], c['Open']) - c['Low']
    if bp > 0.94 and (up_sh < rng*0.02 or lo_sh < rng*0.02):
        return False
    return True

def can_take_signal(pair, direction):
    if pair in recent_signals:
        lt, ld = recent_signals[pair]
        if get_iq_time() - lt < 600 and ld != direction:
            return False
    return True

def already_sent_this_candle(pair):
    key = f"{pair}_{(int(get_iq_time()) // 300) * 300}"
    if key in sent_signals:
        return True
    sent_signals[key] = get_iq_time()
    return False

# ========== نظام تقييم قوة الإشارة (6 مستويات) ==========
SIGNAL_NAMES = {
    2: ("قوية جداً 🔵", "VERY STRONG"),
    3: ("قوية ماكس 🟣", "STRONG MAX"),
    4: ("قوية سوبر ماكس 🟠", "STRONG SUPER MAX"),
    5: ("ماكس 🔥", "MAX"),
    6: ("سوبر ماكس 👑", "SUPER MAX")
}
SIGNAL_EMOJIS = {2: "🔵", 3: "🟣", 4: "🟠", 5: "🔥", 6: "👑"}

def evaluate_signal_strength(direction, curr, prev, df, price, alma9, alma50,
                                stoch_k, stoch_d, rsi, volume, vol_ma,
                                atr, adx, bbw, roc, near_sup, near_res):
    a9p, a50p = prev['ALMA_9'], prev['ALMA_50']
    a9c, a50c = alma9, alma50
    bullish_cross = (a9p <= a50p) and (a9c > a50c)
    bearish_cross = (a9p >= a50p) and (a9c < a50c)
    has_cross = (direction == "CALL" and bullish_cross) or (direction == "PUT" and bearish_cross)
    body = abs(curr['Close'] - curr['Open'])
    rng = curr['High'] - curr['Low']
    body_pct = body / rng if rng > 0 else 0

    if has_cross:
        if direction == "CALL":
            cond_stoch = stoch_k > stoch_d
            cond_price = price > alma9
        else:
            cond_stoch = stoch_k < stoch_d
            cond_price = price < alma9
        if (cond_stoch and cond_price and
            body_pct >= 0.25 and
            volume >= vol_ma * 1.0 and
            adx >= 25 and
            bbw >= 0.002 and
            atr >= (price * 0.0004) and
            abs(roc) >= 0.05):
            if direction == "CALL" and near_sup and rsi <= 45:
                return 6
            if direction == "PUT" and near_res and rsi >= 55:
                return 6

    if has_cross:
        if direction == "CALL":
            cond_stoch = stoch_k > stoch_d
            cond_price = price > alma9
        else:
            cond_stoch = stoch_k < stoch_d
            cond_price = price < alma9
        if (cond_stoch and cond_price and
            body_pct >= 0.20 and
            volume >= vol_ma * 0.9 and
            adx >= 20 and
            bbw >= 0.0015 and
            atr >= (price * 0.0003) and
            abs(roc) >= 0.04):
            if direction == "CALL" and near_sup and rsi <= 50:
                return 5
            if direction == "PUT" and near_res and rsi >= 50:
                return 5

    if has_cross:
        if direction == "CALL":
            cond_stoch = stoch_k > stoch_d
            cond_price = price > alma9
        else:
            cond_stoch = stoch_k < stoch_d
            cond_price = price < alma9
        if (cond_stoch and cond_price and
            body_pct >= 0.18 and
            volume >= vol_ma * 0.85 and
            adx >= 18 and
            bbw >= 0.0012 and
            atr >= (price * 0.00028) and
            abs(roc) >= 0.035):
            if direction == "CALL" and near_sup and rsi <= 52:
                return 4
            if direction == "PUT" and near_res and rsi >= 48:
                return 4

    if has_cross:
        if direction == "CALL":
            cond_stoch = stoch_k > stoch_d
            cond_price = price > alma9
        else:
            cond_stoch = stoch_k < stoch_d
            cond_price = price < alma9
        if (cond_stoch and cond_price and
            body_pct >= 0.16 and
            volume >= vol_ma * 0.8 and
            adx >= 16 and
            bbw >= 0.001 and
            atr >= (price * 0.00025) and
            abs(roc) >= 0.03):
            if direction == "CALL" and near_sup and rsi <= 55:
                return 3
            if direction == "PUT" and near_res and rsi >= 45:
                return 3

    if direction == "CALL":
        cond_base = (price > alma9 * 1.0003) and (stoch_k > stoch_d)
        cond_rsi = 30 <= rsi <= 52
        cond_zone = near_sup or (price <= curr['BBL'] * 1.001)
        cond_stoch_zone = stoch_k <= 42
    else:
        cond_base = (price < alma9 * 0.9997) and (stoch_k < stoch_d)
        cond_rsi = 48 <= rsi <= 70
        cond_zone = near_res or (price >= curr['BBU'] * 0.999)
        cond_stoch_zone = stoch_k >= 58
    if (cond_base and cond_rsi and cond_zone and cond_stoch_zone and
        body_pct >= 0.16 and
        volume >= vol_ma * 0.78 and
        adx >= 15 and
        bbw >= 0.0009 and
        atr >= (price * 0.00024) and
        abs(roc) >= 0.027):
        return 2

    return 0

# --- 11. Telegram Queue ---
def telegram_worker():
    while True:
        if telegram_queue:
            msg = telegram_queue.popleft()
            _send_telegram_raw(msg)
        time.sleep(0.3)

def _send_telegram_raw(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message, "parse_mode": "Markdown"}
    for attempt in range(3):
        try:
            res = requests.post(url, json=payload, timeout=5)
            if res.ok:
                return True
        except Exception as e:
            logger.error(f"Telegram error: {e}")
            time.sleep(1)
    return False

def send_telegram_message(message):
    telegram_queue.append(message)

# --- 12. إيقاف البوت ---
def on_shutdown():
    logger.warning("البوت يتوقف...")
    _send_telegram_raw("🔴 *تنبيه: تم إيقاف بوت IQ Option V7.0!*")

atexit.register(on_shutdown)

# --- 13. الاتصال وفحص الاستجابة ---
def connect_iqoption():
    logger.info("🔌 جاري الاتصال بالمنصة ومزامنة التوقيت...")
    api = IQ_Option(IQ_EMAIL, IQ_PASSWORD)
    delay = 3
    for attempt in range(7):
        check, reason = api.connect()
        if check:
            logger.info("✅ تم الاتصال بنجاح!")
            api.change_balance(ACCOUNT_TYPE)
            sync_server_time(api)
            return api
        logger.error(f"❌ فشل الاتصال ({attempt+1}/7): {reason}")
        if attempt < 6:
            time.sleep(delay)
            delay = min(delay * 2, 30)
    send_telegram_message(f"❌ *فشل الاتصال نهائياً:* `{reason}`")
    raise ConnectionError("فشل الاتصال")

API = connect_iqoption()

def check_connection_health():
    global API
    start = time.time()
    if not API.check_connect():
        logger.warning("🔄 تم اكتشاف انقطاع، جاري إعادة الاتصال...")
        API = connect_iqoption()
    else:
        if (time.time() - start) > 0.5:
            logger.warning("⚠️ بطء في استجابة الاتصال (High Latency)")

# --- 14. متابعة الصفقات والنتائج ---
def check_trade_results():
    current_time = get_iq_time()
    trades_to_remove = []
    for trade in active_trades:
        time_left = trade['expire_time'] - current_time
        try:
            if 0 < time_left <= 20 and not trade.get('warned_loss', False) and not trade.get('is_martingale', False) and not trade.get('is_king', False):
                candles = get_cached_candles(trade['pair'], 300, 1, max_age=5)
                if not candles:
                    continue
                cp, ep, d = candles[-1]['close'], trade['entry_price'], trade['direction']
                losing = (d == "CALL" and cp < ep) or (d == "PUT" and cp > ep)
                if losing:
                    send_telegram_message(f"⏳ *تنبيه مبكر*\nالزوج: `{trade['pair']}` [5m]\nالصفقة تتجه للخسارة..")
                    trade['warned_loss'] = True

            if time_left <= 0:
                time.sleep(1)
                candles = get_cached_candles(trade['pair'], 300, 2, max_age=5)
                fp = candles[-2]['close'] if len(candles) >= 2 else candles[-1]['close']
                ep, d = trade['entry_price'], trade['direction']
                is_win = (d == "CALL" and fp > ep) or (d == "PUT" and fp < ep)
                ts = get_cairo_time().strftime('%I:%M %p')
                pair = trade['pair']
                is_mg = trade.get('is_martingale', False)
                is_king = trade.get('is_king', False)

                if is_king:
                    king_stats[pair]['total'] += 1
                    king_stats[pair]['win' if is_win else 'loss'] += 1
                else:
                    stats[pair]['total'] += 1
                    stats[pair]['win' if is_win else 'loss'] += 1

                # ========== Statistical Engine: تسجيل الصفقة ==========
                try:
                    log_trade({
                        "timestamp": get_iq_time(),
                        "pair": pair,
                        "direction": d,
                        "strategy": trade.get('strategy', 'unknown'),
                        "level": trade.get('signal_level', 0),
                        "score": trade.get('score', 0),
                        "entry_price": float(ep),
                        "exit_price": float(fp),
                        "outcome": "win" if is_win else "loss",
                        "filters": trade.get('filters', {}),
                        "indicators": trade.get('indicators', {}),
                        "hour": trade.get('hour', datetime.now(CAIRO_TZ).hour),
                        "day_of_week": datetime.now(CAIRO_TZ).weekday(),
                        "is_martingale": is_mg,
                        "is_king": is_king
                    })
                except Exception as e:
                    logger.error(f"خطأ في تسجيل صفقة للـ Statistical Engine: {e}")

                if is_mg:
                    msg = f"✅ *نتيجة المضاعفة: رابحة*" if is_win else f"❌ *نتيجة المضاعفة: خاسرة*"
                    msg += f"\nالزوج: `{pair}` [5m]\n⏰ `{ts}`"
                    send_telegram_message(msg)
                    trades_to_remove.append(trade)
                else:
                    if is_win:
                        if is_king:
                            send_telegram_message(f"👑 *{trade.get('signal_name', 'ملك الإشارات')} — رابحة*\nالزوج: `{pair}` [5m]\n⏰ `{ts}`")
                        else:
                            send_telegram_message(f"✅ *نتيجة الصفقة: رابحة* 🎯\nالزوج: `{pair}` [5m]\n⏰ `{ts}`")
                        trades_to_remove.append(trade)
                    else:
                        if is_king:
                            send_telegram_message(
                                f"❌ *{trade.get('signal_name', 'ملك الإشارات')} — خاسرة*\n"
                                f"الزوج: `{pair}` [5m]\n"
                                f"⏰ `{ts}`"
                            )
                        else:
                            if pair not in martingale_queue:
                                martingale_queue[pair] = {'original_direction': d, 'entry_price': ep, 'time': get_iq_time()}
                            send_telegram_message(
                                f"❌ *الصفقة خاسرة*\n"
                                f"الزوج: `{pair}` [5m]\n"
                                f"⏰ `{ts}`\n\n"
                                f"🔴 *دخلنا وضع المضاعفة!*\n"
                                f"🎯 البوت يبحث الآن في *كل الأزواج* عن إشارة *قوية جداً* 🔵 أو أعلى.\n"
                                f"⏳ جاري تحليل السوق..."
                            )
                        trades_to_remove.append(trade)
        except Exception as e:
            logger.error(f"خطأ متابعة {trade['pair']}: {e}")
    for trade in trades_to_remove:
        if trade in active_trades:
            active_trades.remove(trade)

# --- 15. التحليل الرئيسي (الأصلي) ---
def analyze_pair(pair, timeframe="5m"):
    tf_seconds, duration_text = 300, "5 دقائق"
    df = get_cached_df(pair, tf_seconds, 60)
    if df is None or len(df) < 55:
        return None

    curr = df.iloc[-1]
    prev = df.iloc[-2]

    price = curr['Close']
    alma9, alma50 = curr['ALMA_9'], curr['ALMA_50']
    rsi, stoch_k, stoch_d = curr['RSI'], curr['Stoch_K'], curr['Stoch_D']
    volume, vol_ma = curr['Volume'], curr['Vol_MA']
    roc = curr['ROC']

    atr = calculate_atr_wilder(df, 14)
    adx, _, _ = calculate_adx(df, 14)
    bbw = bollinger_bandwidth(df, 20)
    resistance, support = get_fractal_levels(df, lookback=20)

    low = curr['Low']
    high = curr['High']
    near_sup = abs(price - support) <= (price * 0.0005) or low <= (curr['BBL'] * 1.001)
    near_res = abs(price - resistance) <= (price * 0.0005) or high >= (curr['BBU'] * 0.999)

    pair_key = f"{pair}_5m"
    cn = get_cairo_time()
    cts = cn.strftime('%I:%M %p')
    iq_now = get_iq_time()
    csec = int(iq_now) % 300

    potential_direction = None
    a9p, a50p = prev['ALMA_9'], prev['ALMA_50']
    a9c, a50c = alma9, alma50
    bullish_cross = (a9p <= a50p) and (a9c > a50c)
    bearish_cross = (a9p >= a50p) and (a9c < a50c)

    if bullish_cross and stoch_k > stoch_d:
        potential_direction = "CALL"
    elif bearish_cross and stoch_k < stoch_d:
        potential_direction = "PUT"
    elif price > alma9 and stoch_k > stoch_d and rsi <= 65:
        potential_direction = "CALL"
    elif price < alma9 and stoch_k < stoch_d and rsi >= 35:
        potential_direction = "PUT"

    if potential_direction is None:
        return None

    strength = evaluate_signal_strength(
        potential_direction, curr, prev, df, price, alma9, alma50,
        stoch_k, stoch_d, rsi, volume, vol_ma, atr, adx, bbw, roc, near_sup, near_res
    )

    if strength == 0:
        return None

    signal_name_ar, signal_name_en = SIGNAL_NAMES[strength]
    emoji = SIGNAL_EMOJIS[strength]
    da = "صعود (CALL)" if potential_direction == "CALL" else "هبوط (PUT)"

    in_hunt_mode = len(martingale_queue) > 0

    if in_hunt_mode and strength < 2:
        return None

    if 285 <= csec <= 292 and pair_key not in alerted_pairs:
        prefix = "⚠️ *تجهّز! مضاعفة" if in_hunt_mode else "⚠️ *تجهّز! إشارة"
        send_telegram_message(
            f"{prefix} {signal_name_ar}* قريبة جداً\n"
            f"الزوج: `{pair}` [5m]\n"
            f"الاتجاه: *{da}*\n"
            f"💡 النوع: *{signal_name_en}* {emoji}\n"
            f"📊 القوة: *{strength}/6*\n"
            f"⏱️ *انتظر إشارة الدخول النهائية في آخر 5 ثواني من الشمعة!*"
        )
        alerted_pairs[pair_key] = potential_direction

    if 295 <= csec <= 299:
        if already_sent_this_candle(pair):
            return None

        if is_news_for_pair(pair):
            logger.info(f"🛑 إشارة {pair} مرفوضة (فلتر الأخبار)")
            return None
        if is_market_open_chaos():
            logger.info(f"🛑 إشارة {pair} مرفوضة (افتتاح السوق)")
            return None

        min_body = {6: 0.25, 5: 0.20, 4: 0.18, 3: 0.16, 2: 0.15, 1: 0.12}
        if not check_candle_quality(curr, min_body_pct=min_body.get(strength, 0.12)):
            logger.info(f"🛑 إشارة {pair} مرفوضة (شمعة ضعيفة)")
            return None

        if pair_key in alerted_pairs:
            del alerted_pairs[pair_key]

        ht = get_higher_tf_trend(pair)
        if ht is not None and ht != potential_direction:
            logger.info(f"🛑 إشارة {pair} مرفوضة (فريم الساعة عكس: {ht})")
            return None

        if not can_take_signal(pair, potential_direction):
            logger.info(f"🛑 إشارة {pair} مرفوضة (إشارة متعاكسة قريبة)")
            return None

        final_signal = (
            f"{emoji} *إشارة {signal_name_ar}* {emoji}\n"
            f"الزوج: `{pair}` (IQ Option) [5m]\n"
            f"الاتجاه: *{da}*\n"
            f"⏱️ *مدة الصفقة:* {duration_text}\n"
            f"📊 *مؤشرات:* ADX={adx:.1f} | BBW={bbw:.4f} | RSI={rsi:.1f}\n"
            f"⚡ *ادخل فوراً مع بداية الشمعة التالية!*"
        )

        recent_signals[pair] = (get_iq_time(), potential_direction)
        active_trades.append({
            'pair': pair,
            'timeframe': '5m',
            'direction': potential_direction,
            'entry_price': curr['Close'],
            'expire_time': get_iq_time() + 300,
            'warned_loss': False,
            'is_martingale': in_hunt_mode,
            'signal_level': strength,
            'signal_name': signal_name_ar,
            # Statistical Engine data
            'score': strength * 16,  # rough mapping to 100 scale
            'filters': {
                'alma_cross': (a9p <= a50p and a9c > a50c) if potential_direction == "CALL" else (a9p >= a50p and a9c < a50c),
                'price_above_alma': price > alma9 if potential_direction == "CALL" else price < alma9,
                'stoch_aligned': stoch_k > stoch_d if potential_direction == "CALL" else stoch_k < stoch_d,
                'rsi_zone': (30 <= rsi <= 52) if potential_direction == "CALL" else (48 <= rsi <= 70),
                'near_sr': near_sup if potential_direction == "CALL" else near_res,
                'volume_ok': volume >= vol_ma * 0.78,
                'adx_ok': adx >= 15,
                'bbw_ok': bbw >= 0.0009,
                'atr_ok': atr >= (price * 0.00024)
            },
            'indicators': {
                'adx': float(adx),
                'rsi': float(rsi),
                'bbw': float(bbw),
                'atr': float(atr),
                'roc': float(roc),
                'stoch_k': float(stoch_k),
                'stoch_d': float(stoch_d)
            },
            'hour': datetime.now(CAIRO_TZ).hour,
            'strategy': 'original'
        })
        return final_signal

    return None

# ========== King of Signals — التحليل الرئيسي ==========
def analyze_pair_king(pair, timeframe="5m"):
    tf_seconds, duration_text = 300, "5 دقائق"
    df = get_cached_df_king(pair, tf_seconds, 80)
    if df is None or len(df) < 60:
        return None

    df = detect_swings(df, window=2)
    structure, last_sh_idx, last_sl_idx = get_market_structure(df, lookback=30)
    if structure == "NEUTRAL":
        return None

    potential_direction = "CALL" if structure == "BULLISH" else "PUT"

    df['ALMA_20'] = calculate_alma(df['Close'], 20, 0.85, 6)
    df['ALMA_80'] = calculate_alma(df['Close'], 80, 0.85, 6)
    df['RSI'] = wilder_rsi(df['Close'], 14)
    df['Stoch_K'], df['Stoch_D'] = calculate_stoch(df, 14, 3)
    df['ROC'] = calculate_roc(df['Close'], 5)

    curr = df.iloc[-1]
    prev = df.iloc[-2]
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

    sweep_ok, sweep_level = detect_liquidity_sweep(df, potential_direction, sweep_threshold=0.0003)
    if not sweep_ok:
        return None

    trend_ok = (potential_direction == "CALL" and alma20 > alma80) or                (potential_direction == "PUT" and alma20 < alma80)

    momentum_ok = (potential_direction == "CALL" and roc > 0) or                   (potential_direction == "PUT" and roc < 0)

    volatility_ok = (atr_avg * 0.8 <= atr <= atr_avg * 2.0) if atr_avg > 0 else False

    adx_ok = adx >= 22

    if potential_direction == "CALL":
        rsi_ok = 45 <= rsi <= 60
    else:
        rsi_ok = 40 <= rsi <= 55

    if potential_direction == "CALL":
        stoch_ok = stoch_k > stoch_d
    else:
        stoch_ok = stoch_k < stoch_d

    candle_ok, body_pct = check_king_candle_quality(curr)

    near_sr = False
    if potential_direction == "CALL":
        for level in sup_levels:
            if abs(price - level) <= price * 0.0005:
                near_sr = True
                break
    else:
        for level in res_levels:
            if abs(price - level) <= price * 0.0005:
                near_sr = True
                break

    last3 = df.tail(3)
    avg_body_pct = 0
    if len(last3) >= 3:
        bodies = []
        for _, r in last3.iterrows():
            rng = r['High'] - r['Low']
            if rng > 0:
                bodies.append(abs(r['Close'] - r['Open']) / rng)
        avg_body_pct = np.mean(bodies) if bodies else 0

    score = calculate_king_score(
        structure_ok=(structure in ["BULLISH", "BEARISH"]),
        sweep_ok=sweep_ok,
        trend_ok=trend_ok,
        momentum_ok=momentum_ok,
        volatility_ok=volatility_ok,
        adx_ok=adx_ok,
        rsi_ok=rsi_ok,
        stoch_ok=stoch_ok,
        candle_ok=candle_ok
    )

    level = get_king_level(score)
    if level == 0:
        return None

    if body_pct < 0.30:
        return None

    htf_trend = get_king_htf_trend(pair)
    if htf_trend is not None and htf_trend != potential_direction:
        return None

    pair_key = f"{pair}_king_5m"
    cn = get_cairo_time()
    cts = cn.strftime('%I:%M %p')
    iq_now = get_iq_time()
    csec = int(iq_now) % 300

    signal_name_ar, signal_name_en = KING_SIGNAL_NAMES[level]
    emoji = KING_EMOJIS[level]
    da = "صعود (CALL)" if potential_direction == "CALL" else "هبوط (PUT)"

    if 285 <= csec <= 292 and pair_key not in king_alerted_pairs:
        send_telegram_message(
            f"⚠️ *تجهّز! {signal_name_ar}* قريبة جداً\n"
            f"الزوج: `{pair}` [5m]\n"
            f"الاتجاه: *{da}*\n"
            f"💡 النوع: *{signal_name_en}* {emoji}\n"
            f"📊 النقاط: *{score}/100*\n"
            f"⏱️ *انتظر إشارة الدخول النهائية في آخر 5 ثواني من الشمعة!*"
        )
        king_alerted_pairs[pair_key] = potential_direction

    if 295 <= csec <= 299:
        if already_sent_this_candle_king(pair):
            return None

        if is_news_for_pair(pair):
            logger.info(f"🛑 King إشارة {pair} مرفوضة (فلتر الأخبار)")
            return None
        if is_market_open_chaos():
            logger.info(f"🛑 King إشارة {pair} مرفوضة (افتتاح السوق)")
            return None

        if pair_key in king_alerted_pairs:
            del king_alerted_pairs[pair_key]

        king_recent_signals[pair] = (get_iq_time(), potential_direction)

        active_trades.append({
            'pair': pair,
            'timeframe': '5m',
            'direction': potential_direction,
            'entry_price': curr['Close'],
            'expire_time': get_iq_time() + 300,
            'warned_loss': False,
            'is_martingale': False,
            'is_king': True,
            'signal_level': level,
            'signal_name': signal_name_ar,
            'score': score,
            # Statistical Engine data
            'filters': {
                'structure_ok': structure in ["BULLISH", "BEARISH"],
                'sweep_ok': sweep_ok,
                'trend_ok': trend_ok,
                'momentum_ok': momentum_ok,
                'volatility_ok': volatility_ok,
                'adx_ok': adx_ok,
                'rsi_ok': rsi_ok,
                'stoch_ok': stoch_ok,
                'candle_ok': candle_ok
            },
            'indicators': {
                'adx': float(adx),
                'rsi': float(rsi),
                'roc': float(roc),
                'atr': float(atr),
                'bbw': float(bbw),
                'stoch_k': float(stoch_k),
                'stoch_d': float(stoch_d)
            },
            'hour': datetime.now(CAIRO_TZ).hour,
            'strategy': 'king'
        })

        final_signal = (
            f"{emoji} *{signal_name_ar}* {emoji}\n"
            f"الزوج: `{pair}` (IQ Option) [5m]\n"
            f"الاتجاه: *{da}*\n"
            f"⏱️ *مدة الصفقة:* {duration_text}\n"
            f"📊 *النقاط:* {score}/100 | *ADX:* {adx:.1f} | *RSI:* {rsi:.1f}\n"
            f"📈 *ROC:* {roc:.2f} | *ATR:* {atr:.5f} | *BBW:* {bbw:.4f}\n"
            f"⚡ *ادخل فوراً مع بداية الشمعة التالية!*"
        )
        return final_signal

    return None

def analyze_pair_wrapper(pair):
    try:
        return pair, analyze_pair(pair, "5m")
    except Exception as e:
        logger.error(f"خطأ في {pair}: {e}")
        return pair, None

def analyze_pair_wrapper_king(pair):
    try:
        return pair, analyze_pair_king(pair, "5m")
    except Exception as e:
        logger.error(f"خطأ King Strategy في {pair}: {e}")
        return pair, None

# --- 16. تشغيل البوت ---

# ========== Telegram Reply Handler (للموافقة على التحسينات) ==========
telegram_last_update_id = 0

def telegram_reply_worker():
    """
    يشيك على ردود تليجرام كل 30 ثانية.
    لو المستخدم رد "موافق" أو "رفض" على اقتراح تحسين → يتعامل معاه.
    """
    global telegram_last_update_id
    logger.info("📱 Telegram Reply Worker started")

    while True:
        try:
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            params = {
                "offset": telegram_last_update_id + 1,
                "limit": 10
            }
            res = requests.get(url, params=params, timeout=10)
            if res.ok:
                data = res.json()
                if data.get("ok") and data.get("result"):
                    for update in data["result"]:
                        telegram_last_update_id = update["update_id"]

                        # نفحص بس الرسائل النصية
                        message = update.get("message", {})
                        if not message:
                            continue

                        chat_id_msg = message.get("chat", {}).get("id")
                        if str(chat_id_msg) != str(CHAT_ID):
                            continue

                        text = message.get("text", "").strip()
                        if not text:
                            continue

                        # نفحص لو الرد على اقتراح تحسين
                        success, response_msg = handle_optimization_reply(text)
                        if success and response_msg:
                            _send_telegram_raw(response_msg)
                            logger.info(f"📱 تم معالجة رد المستخدم: {text}")
        except Exception as e:
            logger.error(f"خطأ في Telegram Reply Worker: {e}")

        time.sleep(30)
def run_bot():
    global cycle_count, last_hunt_message_time

    # ========== دالة مساعدة لتحديد الأزواج حسب اليوم ==========
    def get_pairs_for_today():
        day_of_week = datetime.now(CAIRO_TZ).weekday()
        if day_of_week in [5, 6]:  # سبت أو أحد
            return [
                "EURUSD-OTC", "GBPUSD-OTC", "USDJPY-OTC", "USDCHF-OTC",
                "EURJPY-OTC", "EURGBP-OTC", "AUDCAD-OTC", "GBPJPY-OTC"
            ], "OTC (عطلة weekend)"
        else:  # اتنين لجمعة
            return [
                "EURUSD", "GBPUSD", "USDJPY", "AUDUSD", "USDCAD", "USDCHF",
                "EURJPY", "EURGBP", "AUDCAD", "AUDJPY", "CADJPY", "EURAUD",
                "GBPJPY", "EURCAD"
            ], "عادي (سوق مفتوح)"

    pairs, mode_text = get_pairs_for_today()
    current_mode = mode_text

    logger.info(f"🚀 البوت يعمل بالنسخة V7.0 (وضع {mode_text})...")
    send_telegram_message(
        f"🤖 *تم تشغيل بوت IQ Option V7.0!*\n"
        f"📅 *اليوم:* {datetime.now(CAIRO_TZ).strftime('%A %d/%m/%Y')}\n"
        f"🌐 *وضع الأزواج:* {mode_text}\n"
        f"📋 *الأزواج المتاحة:* {len(pairs)} زوج\n"
        f"⏱️ *الفريم:* 5 دقائق\n\n"
        f"📊 *مستويات الإشارات الأصلية (5 مستويات):*\n"
        f"  🔵 قوية جداً (2)\n"
        f"  🟣 قوية ماكس (3)\n"
        f"  🟠 قوية سوبر ماكس (4)\n"
        f"  🔥 ماكس (5)\n"
        f"  👑 سوبر ماكس (6)\n\n"
        f"👑 *King of Signals Strategy (جديد — منفصل):*\n"
        f"  🥉 King Bronze (80–84)\n"
        f"  🥈 King Silver (85–89)\n"
        f"  👑 King Gold (90–94)\n"
        f"  👑🔥 King Elite (95–100)\n\n"
        f"🎯 *وضع المضاعفة:* مفعل (للاستراتيجيات الأصلية فقط)\n"
        f"🌐 *مزامنة السيرفر:* مفعلة\n"
        f"🛡️ *الحماية:* مصدران للأخبار + مراقبة الاتصال + فلتر الساعة"
    )

    threading.Thread(target=telegram_worker, daemon=True).start()
    threading.Thread(target=stats_engine_worker, daemon=True).start()
    threading.Thread(target=telegram_reply_worker, daemon=True).start()
    logger.info("📊 Statistical Engine Worker started")

    try:
        while True:
            cycle_count += 1
            cycle_start = time.time()
            try:
                # ========== تحديث الأزواج تلقائياً حسب اليوم ==========
                pairs, mode_text = get_pairs_for_today()
                if mode_text != current_mode:
                    current_mode = mode_text
                    logger.info(f"🔄 تبديل تلقائي للوضع: {mode_text}")
                    send_telegram_message(
                        f"🔄 *تبديل تلقائي للوضع!*\n"
                        f"📅 اليوم: *{datetime.now(CAIRO_TZ).strftime('%A %d/%m/%Y')}*\n"
                        f"🌐 الوضع الجديد: *{mode_text}*\n"
                        f"📋 الأزواج: *{len(pairs)}* زوج"
                    )
                    # تنظيف caches لما يتبدل الوضع
                    invalid_assets.clear()

                check_connection_health()

                valid_pairs = [p for p in pairs if p not in invalid_assets]
                if len(valid_pairs) < len(pairs):
                    logger.info(f"📋 الأزواج المتاحة: {len(valid_pairs)}/{len(pairs)}")

                # رسالة دورية أثناء وضع المضاعفة — كل نص ساعة
                if martingale_queue:
                    now_time = get_iq_time()
                    if now_time - last_hunt_message_time >= 1800:
                        last_hunt_message_time = now_time
                        send_telegram_message(
                            f"🔍 *جاري تحليل السوق لإيجاد مضاعفة...*\n"
                            f"🎯 البوت يحلل *كل الأزواج المتاحة* بكل طاقته.\n\n"
                            f"⏳ نبحث عن *قوية جداً* 🔵 أو أعلى.\n"
                            f"⏱️ البحث مفتوح لحد ما يلاقي إشارة مناسبة.\n"
                            f"✅ كل الإشارات العادية شغالة طبيعي.\n"
                            f"👑 King Strategy شغال منفصل."
                        )

                # ========== الاستراتيجيات الأصلية (6 مستويات) ==========
                with ThreadPoolExecutor(max_workers=7) as executor:
                    results = executor.map(analyze_pair_wrapper, valid_pairs)

                    if martingale_queue:
                        martingale_found = False
                        for pair, signal in results:
                            if signal and not martingale_found:
                                logger.info(f"✅ مضاعفة ممتازة: {pair}")
                                send_telegram_message(signal)
                                martingale_found = True
                                martingale_queue.clear()
                                alerted_pairs.clear()
                    else:
                        for pair, signal in results:
                            if signal:
                                logger.info(f"✅ إشارة ممتازة: {pair}")
                                send_telegram_message(signal)

                # ========== King of Signals Strategy (منفصل تماماً) ==========
                king_signals_found = []
                for pair in valid_pairs:
                    try:
                        king_signal = analyze_pair_king(pair, "5m")
                        if king_signal:
                            king_signals_found.append((pair, king_signal))
                    except Exception as e:
                        logger.error(f"خطأ King Strategy في {pair}: {e}")

                for pair, signal in king_signals_found:
                    logger.info(f"👑 King Signal: {pair}")
                    send_telegram_message(signal)

                check_trade_results()

                if cycle_count % 60 == 0:
                    cleanup_memory()
                    sync_server_time(API)
                    total_wins = sum(s['win'] for s in stats.values())
                    total_loss = sum(s['loss'] for s in stats.values())
                    wr = (total_wins / (total_wins + total_loss) * 100) if (total_wins + total_loss) > 0 else 0

                    king_total_wins = sum(s['win'] for s in king_stats.values())
                    king_total_loss = sum(s['loss'] for s in king_stats.values())
                    king_wr = (king_total_wins / (king_total_wins + king_total_loss) * 100) if (king_total_wins + king_total_loss) > 0 else 0

                    logger.info(f"📊 دورة #{cycle_count} | Original WR: {wr:.1f}% | King WR: {king_wr:.1f}% | Total: {total_wins+total_loss} | King Total: {king_total_wins+king_total_loss}")

            except Exception as e:
                logger.error(f"خطأ في الحلقة الرئيسية: {e}")
                logger.error(traceback.format_exc())

            elapsed = time.time() - cycle_start
            sleep_time = max(0.5, 1.5 - elapsed)
            time.sleep(sleep_time)
    except KeyboardInterrupt:
        logger.info("تم الإيقاف يدوياً")
    finally:
        on_shutdown()

if __name__ == "__main__":
    threading.Thread(target=run_web_server, daemon=True).start()
    run_bot()
