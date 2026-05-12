"""
F&O Heikin Ashi Reversal Alert System
Data    : NSE F&O list + Yahoo Finance daily OHLC
Alerts  : Telegram
Version : 1.0
Triggers: 3:30 PM IST (EOD signals) + 9:20 AM IST (morning reminder)
"""

import os
import time
import requests
import pandas as pd
from datetime import datetime
import pytz

# ── SECRETS ──────────────────────────────────────────────────────────────────
TELEGRAM_TOKEN   = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
FORCE_RUN        = os.environ.get("FORCE_RUN", "false").lower() == "true"
MODE             = os.environ.get("MODE", "eod")   # "eod" or "morning"

# ── CONFIG ───────────────────────────────────────────────────────────────────
IST              = pytz.timezone("Asia/Kolkata")
TOP_N            = 5
MIN_TREND_CANDLES= 3      # min consecutive candles before reversal
WICK_TOLERANCE   = 0.001  # 0.1% tolerance for "no wick" check


# ── TELEGRAM ─────────────────────────────────────────────────────────────────
def send_telegram(message: str):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"Telegram error: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"Telegram exception: {e}")


# ── MARKET HOURS ─────────────────────────────────────────────────────────────
def is_eod_time() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and now.hour == 15 and 28 <= now.minute <= 45

def is_morning_time() -> bool:
    now = datetime.now(IST)
    return now.weekday() < 5 and now.hour == 9 and 15 <= now.minute <= 25


# ── FETCH F&O SYMBOL LIST FROM NSE ───────────────────────────────────────────
def get_fo_symbols() -> list:
    url = "https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv"
    headers = {
        "User-Agent": "Mozilla/5.0",
        "Referer"   : "https://www.nseindia.com/"
    }
    try:
        r = requests.get(url, headers=headers, timeout=15)
        lines = r.text.strip().split("\n")
        symbols = []
        for line in lines[1:]:  # skip header
            parts = [p.strip() for p in line.split(",")]
            if len(parts) >= 2:
                sym = parts[1].strip()
                # Skip index symbols
                if sym and sym not in ["NIFTY", "BANKNIFTY", "MIDCPNIFTY",
                                       "NIFTYNXT50", "FINNIFTY", "SENSEX",
                                       "BANKEX", "Symbol"]:
                    symbols.append(sym)
        print(f"F&O symbols fetched: {len(symbols)}")
        return symbols
    except Exception as e:
        print(f"F&O list error: {e}")
        return []


# ── FETCH DAILY OHLC FROM YAHOO ───────────────────────────────────────────────
def get_daily_ohlc(nse_symbol: str) -> pd.DataFrame:
    yahoo_symbol = nse_symbol + ".NS"
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{yahoo_symbol}?interval=1d&range=3mo"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=10)
        if r.status_code != 200:
            return pd.DataFrame()
        data   = r.json()
        result = data["chart"]["result"]
        if not result:
            return pd.DataFrame()
        r0     = result[0]
        quote  = r0["indicators"]["quote"][0]
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(r0["timestamp"], unit="s", utc=True),
            "open" : quote["open"],
            "high" : quote["high"],
            "low"  : quote["low"],
            "close": quote["close"],
            "volume": quote["volume"],
        })
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
        df = df.dropna().sort_values("timestamp").reset_index(drop=True)
        return df if len(df) >= 20 else pd.DataFrame()
    except Exception:
        return pd.DataFrame()


# ── COMPUTE HEIKIN ASHI ───────────────────────────────────────────────────────
def compute_ha(df: pd.DataFrame) -> pd.DataFrame:
    ha = df.copy()
    ha["ha_close"] = (df["open"] + df["high"] + df["low"] + df["close"]) / 4
    ha["ha_open"]  = 0.0

    # First HA open = average of first real open and close
    ha.loc[ha.index[0], "ha_open"] = (df["open"].iloc[0] + df["close"].iloc[0]) / 2

    for i in range(1, len(ha)):
        ha.loc[ha.index[i], "ha_open"] = (
            ha["ha_open"].iloc[i-1] + ha["ha_close"].iloc[i-1]
        ) / 2

    ha["ha_high"] = ha[["ha_open", "ha_close", "high"]].max(axis=1)
    ha["ha_low"]  = ha[["ha_open", "ha_close", "low"]].min(axis=1)
    ha["ha_color"]= ha.apply(
        lambda r: "GREEN" if r["ha_close"] >= r["ha_open"] else "RED", axis=1
    )
    return ha


# ── CHECK NO WICK ─────────────────────────────────────────────────────────────
def no_upper_wick(row) -> bool:
    """HA Open == HA High within tolerance"""
    if row["ha_high"] == 0:
        return False
    return abs(row["ha_open"] - row["ha_high"]) / row["ha_high"] <= WICK_TOLERANCE

def no_lower_wick(row) -> bool:
    """HA Open == HA Low within tolerance"""
    if row["ha_low"] == 0:
        return False
    return abs(row["ha_open"] - row["ha_low"]) / row["ha_low"] <= WICK_TOLERANCE


# ── CHOP FILTER ───────────────────────────────────────────────────────────────
def is_trending(ha: pd.DataFrame, end_idx: int, direction: str) -> bool:
    """
    Check if there's a clear trend before the reversal candle.
    direction: "UP" = prior trend was bullish (GREEN run before RED reversal)
               "DOWN" = prior trend was bearish (RED run before GREEN reversal)
    """
    lookback = ha.iloc[max(0, end_idx-8):end_idx]
    if len(lookback) < MIN_TREND_CANDLES:
        return False

    expected_color = "GREEN" if direction == "UP" else "RED"

    # Count consecutive same-color candles from the end of lookback
    consecutive = 0
    for i in range(len(lookback)-1, -1, -1):
        if lookback.iloc[i]["ha_color"] == expected_color:
            consecutive += 1
        else:
            break

    if consecutive < MIN_TREND_CANDLES:
        return False

    # Chop filter: check body dominance in those candles
    trend_candles = lookback.iloc[-consecutive:]
    body_sizes = abs(trend_candles["ha_close"] - trend_candles["ha_open"])
    wick_sizes = (trend_candles["ha_high"] - trend_candles["ha_low"]) - body_sizes
    
    # Body should be larger than wicks on average
    if body_sizes.mean() <= wick_sizes.mean():
        return False

    return True


# ── SCORE SIGNAL ──────────────────────────────────────────────────────────────
def score_signal(ha: pd.DataFrame, df: pd.DataFrame, idx: int, direction: str) -> float:
    """Score 0-100. Higher = stronger signal."""
    score = 0.0
    row   = ha.iloc[idx]

    # 1. Wick cleanliness (0-40)
    body  = abs(row["ha_close"] - row["ha_open"])
    total = row["ha_high"] - row["ha_low"]
    if total > 0:
        score += (body / total) * 40

    # 2. Trend strength — consecutive candles (0-30)
    expected = "GREEN" if direction == "SHORT" else "RED"
    consecutive = 0
    for i in range(idx-1, max(0, idx-10), -1):
        if ha.iloc[i]["ha_color"] == expected:
            consecutive += 1
        else:
            break
    score += min(consecutive, 6) * 5  # max 30

    # 3. Volume confirmation (0-30)
    try:
        vol_today = df["volume"].iloc[idx]
        vol_avg   = df["volume"].iloc[max(0,idx-20):idx].mean()
        if vol_avg > 0 and vol_today > vol_avg:
            score += min((vol_today / vol_avg), 2) * 15  # max 30
    except Exception:
        pass

    return round(score, 1)


# ── DETECT SIGNAL FOR ONE STOCK ───────────────────────────────────────────────
def detect_signal(symbol: str) -> dict | None:
    df = get_daily_ohlc(symbol)
    if df.empty:
        return None

    ha  = compute_ha(df)
    idx = len(ha) - 1  # last closed candle

    if idx < MIN_TREND_CANDLES + 2:
        return None

    current  = ha.iloc[idx]
    previous = ha.iloc[idx - 1]

    # ── SHORT: current RED no-upper-wick, previous GREEN, prior uptrend
    if (current["ha_color"]  == "RED"   and
        previous["ha_color"] == "GREEN" and
        no_upper_wick(current)           and
        is_trending(ha, idx, "UP")):

        sl    = round(df["high"].iloc[idx], 2)
        price = round(df["close"].iloc[idx], 2)
        score = score_signal(ha, df, idx, "SHORT")
        return {
            "symbol"   : symbol,
            "signal"   : "SHORT",
            "price"    : price,
            "sl"       : sl,
            "sl_pts"   : round(sl - price, 2),
            "score"    : score,
            "trend_run": sum(1 for i in range(idx-1, max(0,idx-10), -1)
                             if ha.iloc[i]["ha_color"] == "GREEN"),
        }

    # ── LONG: current GREEN no-lower-wick, previous RED, prior downtrend
    if (current["ha_color"]  == "GREEN" and
        previous["ha_color"] == "RED"   and
        no_lower_wick(current)           and
        is_trending(ha, idx, "DOWN")):

        sl    = round(df["low"].iloc[idx], 2)
        price = round(df["close"].iloc[idx], 2)
        score = score_signal(ha, df, idx, "LONG")
        return {
            "symbol"   : symbol,
            "signal"   : "LONG",
            "price"    : price,
            "sl"       : sl,
            "sl_pts"   : round(price - sl, 2),
            "score"    : score,
            "trend_run": sum(1 for i in range(idx-1, max(0,idx-10), -1)
                             if ha.iloc[i]["ha_color"] == "RED"),
        }

    return None


# ── FORMAT TELEGRAM MESSAGE ───────────────────────────────────────────────────
def format_message(longs: list, shorts: list, mode: str) -> str:
    now = datetime.now(IST).strftime("%d %b %Y %H:%M")
    header = (
        f"🌅 <b>MORNING REMINDER — {now} IST</b>\n"
        f"Place orders for yesterday's signals\n"
        if mode == "morning" else
        f"📊 <b>EOD F&O HA REVERSAL SIGNALS — {now} IST</b>\n"
        f"Trade tomorrow. SL = real candle high/low.\n"
    )
    msg = header + "━━━━━━━━━━━━━━━━━━━━━\n\n"

    if longs:
        msg += f"🟢 <b>LONG SIGNALS (Top {len(longs)})</b>\n"
        for i, s in enumerate(longs, 1):
            msg += (
                f"{i}. <b>{s['symbol']}</b> @ ₹{s['price']}\n"
                f"   SL: ₹{s['sl']} (−{s['sl_pts']} pts) | "
                f"Score: {s['score']} | Trend: {s['trend_run']} red candles\n"
            )
        msg += "\n"
    else:
        msg += "🟢 <b>LONG SIGNALS</b>: None today\n\n"

    if shorts:
        msg += f"🔴 <b>SHORT SIGNALS (Top {len(shorts)})</b>\n"
        for i, s in enumerate(shorts, 1):
            msg += (
                f"{i}. <b>{s['symbol']}</b> @ ₹{s['price']}\n"
                f"   SL: ₹{s['sl']} (+{s['sl_pts']} pts) | "
                f"Score: {s['score']} | Trend: {s['trend_run']} green candles\n"
            )
        msg += "\n"
    else:
        msg += "🔴 <b>SHORT SIGNALS</b>: None today\n\n"

    msg += "━━━━━━━━━━━━━━━━━━━━━\n"
    msg += "⚠️ Verify on chart. Place limit orders. SL mandatory."
    return msg


# ── MAIN ─────────────────────────────────────────────────────────────────────
def main():
    now_ist = datetime.now(IST)
    print(f"Time IST  : {now_ist.strftime('%H:%M:%S')}")
    print(f"Force run : {FORCE_RUN}")
    print(f"Mode      : {MODE}")

    # Determine if we should run
    should_run = FORCE_RUN or is_eod_time() or is_morning_time()
    if not should_run:
        print("Not EOD or morning time. Skipping.")
        return

    # Morning mode: just resend stored signals
    if MODE == "morning" and not FORCE_RUN:
        # In morning mode, we re-run detection on previous day's closed candles
        # Same logic, just different header
        pass

    # Fetch F&O symbol list
    symbols = get_fo_symbols()
    if not symbols:
        send_telegram("⚠️ Could not fetch F&O symbol list from NSE.")
        return

    print(f"Scanning {len(symbols)} F&O stocks...")
    send_telegram(f"🔍 Scanning {len(symbols)} F&O stocks for HA reversals...")

    longs  = []
    shorts = []
    failed = 0

    for i, symbol in enumerate(symbols):
        try:
            result = detect_signal(symbol)
            if result:
                if result["signal"] == "LONG":
                    longs.append(result)
                else:
                    shorts.append(result)
            time.sleep(0.3)  # rate limit Yahoo
        except Exception as e:
            failed += 1
            continue

        # Progress every 50 stocks
        if (i + 1) % 50 == 0:
            print(f"Progress: {i+1}/{len(symbols)} | "
                  f"Longs: {len(longs)} | Shorts: {len(shorts)}")

    print(f"Done. Longs: {len(longs)} | Shorts: {len(shorts)} | Failed: {failed}")

    # Sort by score, take top N
    longs  = sorted(longs,  key=lambda x: x["score"], reverse=True)[:TOP_N]
    shorts = sorted(shorts, key=lambda x: x["score"], reverse=True)[:TOP_N]

    # Send alert
    msg = format_message(longs, shorts, MODE)

    # Telegram has 4096 char limit — split if needed
    if len(msg) > 4000:
        send_telegram(msg[:4000])
        send_telegram(msg[4000:])
    else:
        send_telegram(msg)

    print("Alert sent.")


if __name__ == "__main__":
    main()