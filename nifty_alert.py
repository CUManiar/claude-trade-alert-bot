"""
Nifty Futures — BB + RSI Alert System
Data    : Yahoo Finance (free)
Alerts  : Telegram
Version : 2.1
"""

import os
import time
import requests
import pandas as pd
from datetime import datetime
import pytz

TELEGRAM_TOKEN    = os.environ.get("TELEGRAM_TOKEN", "")
TELEGRAM_CHAT_ID  = os.environ.get("TELEGRAM_CHAT_ID", "")
FORCE_RUN         = os.environ.get("FORCE_RUN", "false").lower() == "true"

BB_PERIOD         = 50
BB_STD            = 2.0
RSI_PERIOD        = 14
BB_THRESHOLD_PCT  = 0.5
RSI_OVERSOLD      = 35
RSI_OVERBOUGHT    = 65
LOT_SIZE          = 65
CHARGES_PER_TRADE = 1000
IST               = pytz.timezone("Asia/Kolkata")


def send_telegram(message: str):
    url     = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": TELEGRAM_CHAT_ID, "text": message, "parse_mode": "HTML"}
    try:
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code != 200:
            print(f"Telegram error: {r.status_code} {r.text[:100]}")
    except Exception as e:
        print(f"Telegram exception: {e}")


def is_market_open() -> bool:
    now = datetime.now(IST)
    if now.weekday() >= 5:
        return False
    open_  = now.replace(hour=9,  minute=15, second=0, microsecond=0)
    close_ = now.replace(hour=15, minute=30, second=0, microsecond=0)
    return open_ <= now <= close_


def is_closing_time() -> bool:
    now = datetime.now(IST)
    return now.hour == 15 and 10 <= now.minute <= 20


def get_candles() -> pd.DataFrame:
    url     = "https://query1.finance.yahoo.com/v8/finance/chart/%5ENSEI?interval=15m&range=5d"
    headers = {"User-Agent": "Mozilla/5.0"}
    try:
        r = requests.get(url, headers=headers, timeout=15)
        print(f"Yahoo response: {r.status_code}")
        if r.status_code != 200:
            print(f"Yahoo error: {r.text[:200]}")
            return pd.DataFrame()
        data      = r.json()
        result    = data["chart"]["result"][0]
        timestamps= result["timestamp"]
        quote     = result["indicators"]["quote"][0]
        df = pd.DataFrame({
            "timestamp": pd.to_datetime(timestamps, unit="s", utc=True),
            "open" : quote["open"],
            "high" : quote["high"],
            "low"  : quote["low"],
            "close": quote["close"],
        })
        df["timestamp"] = df["timestamp"].dt.tz_convert(IST)
        df = df.dropna().sort_values("timestamp").reset_index(drop=True)
        print(f"Candles received: {len(df)}")
        print(f"Last close: {df.iloc[-1]['close']:.0f}")
        return df
    except Exception as e:
        print(f"Candle fetch error: {e}")
        return pd.DataFrame()


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df["sma"]   = df["close"].rolling(BB_PERIOD).mean()
    df["std"]   = df["close"].rolling(BB_PERIOD).std()
    df["upper"] = df["sma"] + BB_STD * df["std"]
    df["lower"] = df["sma"] - BB_STD * df["std"]
    delta       = df["close"].diff()
    gain        = delta.clip(lower=0).rolling(RSI_PERIOD).mean()
    loss        = (-delta.clip(upper=0)).rolling(RSI_PERIOD).mean()
    rs          = gain / loss
    df["rsi"]   = 100 - (100 / (1 + rs))
    return df


def build_alerts(df: pd.DataFrame) -> list:
    alerts = []
    if len(df) < BB_PERIOD + 5:
        print(f"Not enough candles: {len(df)}")
        return alerts

    c     = df.iloc[-2]
    prev  = df.iloc[-3]
    price = round(c["close"], 0)
    rsi   = round(c["rsi"], 1)
    upper = round(c["upper"], 0)
    lower = round(c["lower"], 0)
    mid   = round(c["sma"], 0)
    try:
        ts = c["timestamp"].strftime("%H:%M")
    except Exception:
        ts = "N/A"

    near_upper = price >= upper * (1 - BB_THRESHOLD_PCT / 100)
    near_lower = price <= lower * (1 + BB_THRESHOLD_PCT / 100)

    print(f"Price: {price} | RSI: {rsi} | Upper: {upper} | Lower: {lower} | Near upper: {near_upper} | Near lower: {near_lower}")

    if near_lower and rsi <= RSI_OVERSOLD:
        entry      = price - 30
        sl         = entry - 50
        target     = entry + 100
        net_profit = int((100 * LOT_SIZE) - CHARGES_PER_TRADE)
        net_loss   = int((50  * LOT_SIZE) + CHARGES_PER_TRADE)
        alerts.append(
            f"🟢 <b>LONG SIGNAL — Nifty Futures</b>\n"
            f"🕐 {ts} IST\n"
            f"💰 Spot Price : {price}\n"
            f"📉 Lower BB   : {lower}\n"
            f"📊 RSI        : {rsi} ← oversold\n"
            f"─────────────────────\n"
            f"📌 Place LIMIT BUY at : <b>{entry:.0f}</b>\n"
            f"🛑 Stop Loss          : {sl:.0f} (−₹{net_loss:,})\n"
            f"🎯 Target             : {target:.0f} (+₹{net_profit:,} net)\n"
            f"─────────────────────\n"
            f"⚠️ Place order. Close screen. No watching."
        )

    if near_upper and rsi >= RSI_OVERBOUGHT:
        entry      = price + 30
        sl         = entry + 50
        target     = entry - 100
        net_profit = int((100 * LOT_SIZE) - CHARGES_PER_TRADE)
        net_loss   = int((50  * LOT_SIZE) + CHARGES_PER_TRADE)
        alerts.append(
            f"🔴 <b>SHORT SIGNAL — Nifty Futures</b>\n"
            f"🕐 {ts} IST\n"
            f"💰 Spot Price : {price}\n"
            f"📈 Upper BB   : {upper}\n"
            f"📊 RSI        : {rsi} ← overbought\n"
            f"─────────────────────\n"
            f"📌 Place LIMIT SELL at : <b>{entry:.0f}</b>\n"
            f"🛑 Stop Loss           : {sl:.0f} (−₹{net_loss:,})\n"
            f"🎯 Target              : {target:.0f} (+₹{net_profit:,} net)\n"
            f"─────────────────────\n"
            f"⚠️ Place order. Close screen. No watching."
        )

    try:
        crossed_up   = prev["close"] < prev["sma"] and c["close"] > c["sma"]
        crossed_down = prev["close"] > prev["sma"] and c["close"] < c["sma"]
        if crossed_up:
            alerts.append(
                f"🔔 <b>MID BB CROSS UP</b>\n"
                f"🕐 {ts} | Price: {price} | Mid: {mid}\n"
                f"→ LONG: move SL to breakeven\n"
                f"→ SHORT: price reversing, check SL"
            )
        if crossed_down:
            alerts.append(
                f"🔔 <b>MID BB CROSS DOWN</b>\n"
                f"🕐 {ts} | Price: {price} | Mid: {mid}\n"
                f"→ SHORT: move SL to breakeven\n"
                f"→ LONG: price reversing, check SL"
            )
    except Exception:
        pass

    return alerts


def main():
    now_ist = datetime.now(IST)
    print(f"Current IST time : {now_ist.strftime('%H:%M:%S')}")
    print(f"Market open      : {is_market_open()}")
    print(f"Force run        : {FORCE_RUN}")

    if is_market_open() and is_closing_time():
        send_telegram(
            "⏰ <b>3:10 PM — CLOSE ALL POSITIONS</b>\n"
            "Market closes in 5 mins.\n"
            "<b>Exit Nifty Futures now. No overnight. No exceptions.</b>"
        )
        return

    if not FORCE_RUN and not is_market_open():
        print("Market closed. Skipping.")
        return

    df = get_candles()
    if df.empty or len(df) < BB_PERIOD + 5:
        msg = f"⚠️ Insufficient data ({len(df)} candles). Yahoo may be unavailable."
        print(msg)
        send_telegram(msg)
        return

    df     = compute_indicators(df)
    alerts = build_alerts(df)
    c      = df.iloc[-2]

    if alerts:
        for alert in alerts:
            send_telegram(alert)
            print("Alert sent.")
            time.sleep(1)
    else:
        try:
            ts = c["timestamp"].strftime("%H:%M")
        except Exception:
            ts = now_ist.strftime("%H:%M")
        send_telegram(
            f"🔍 <b>NO SIGNAL</b> — {ts} IST\n"
            f"Price   : {c['close']:.0f}\n"
            f"RSI     : {c['rsi']:.1f}\n"
            f"Upper BB: {c['upper']:.0f}\n"
            f"Lower BB: {c['lower']:.0f}\n"
            f"System running ✅"
        )
        print(f"No signal | Price: {c['close']:.0f} | RSI: {c['rsi']:.1f}")


if __name__ == "__main__":
    main()