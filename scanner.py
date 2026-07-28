import requests
import time
from datetime import datetime

# ── CONFIG ───────────────────────────────────────────────────────────────────
NTFY_TOPIC     = "btcwave554433"
PEAK_WINDOW    = 3    # smaller window since we have fewer candles
MOMENTUM_BARS  = 2    # 2 consecutive candles to confirm (not 3, fewer candles)

# Single set of thresholds since all candles are same timeframe
MIN_GAP  = 100   # 2nd peak must beat 1st by $100
MIN_SIZE = 150   # wave must be $150 tall
RECENT   = 15    # 2nd peak within last 15 candles
# ─────────────────────────────────────────────────────────────────────────────

def notify(title, msg, priority="default"):
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={"Title": title, "Priority": priority},
            timeout=10
        )
        print(f"  Ntfy sent: {title}")
    except Exception as e:
        print(f"  Ntfy failed: {e}")

def get_candles():
    """Fetch BTC OHLC from CoinGecko — single call, 1 day of data."""
    r = requests.get(
        "https://api.coingecko.com/api/v3/coins/bitcoin/ohlc",
        params={"vs_currency": "usd", "days": "1"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15
    )
    r.raise_for_status()
    data = r.json()
    return [{
        "open":  float(c[1]),
        "high":  float(c[2]),
        "low":   float(c[3]),
        "close": float(c[4]),
    } for c in data]

def find_peaks_lows(candles):
    peaks, lows = [], []
    w = PEAK_WINDOW
    for i in range(w, len(candles) - w):
        wh = [c["high"] for c in candles[i-w:i+w+1]]
        wl = [c["low"]  for c in candles[i-w:i+w+1]]
        if candles[i]["high"] == max(wh) and candles[i]["high"] > min(wh) + 20:
            peaks.append((i, candles[i]["high"]))
        if candles[i]["low"] == min(wl) and candles[i]["low"] < max(wl) - 20:
            lows.append((i, candles[i]["low"]))
    return peaks, lows

def momentum_down(candles, from_idx):
    end = from_idx + 1 + MOMENTUM_BARS
    if end > len(candles): return False
    return all(candles[i]["close"] < candles[i]["open"]
               for i in range(from_idx + 1, end))

def momentum_up(candles, from_idx):
    end = from_idx + 1 + MOMENTUM_BARS
    if end > len(candles): return False
    return all(candles[i]["close"] > candles[i]["open"]
               for i in range(from_idx + 1, end))

def scan(candles):
    peaks, lows = find_peaks_lows(candles)
    price    = candles[-1]["close"]
    last_idx = len(candles) - 1
    alerts   = []

    # SHORT SETUP
    # 1. 2nd peak beats 1st by MIN_GAP
    # 2. Wave height >= MIN_SIZE
    # 3. MOMENTUM_BARS consecutive bearish candles after 2nd peak
    # 4. 2nd peak within last RECENT candles
    if len(peaks) >= 2:
        for i in range(len(peaks) - 1):
            idx1, p1 = peaks[i]
            idx2, p2 = peaks[i + 1]
            if p2 - p1 < MIN_GAP: continue
            trough = min(c["low"] for c in candles[idx1:idx2+1])
            if p2 - trough < MIN_SIZE: continue
            if not momentum_down(candles, idx2): continue
            if last_idx - idx2 > RECENT: continue
            alerts.append({
                "title": "SHORT SETUP - Wave Strategy",
                "msg":   (f"1st peak ${p1:,.0f} to 2nd peak ${p2:,.0f} (+${p2-p1:,.0f})\n"
                          f"Wave size: ${p2-trough:,.0f} | TP: ~${p1:,.0f} | Now: ${price:,.0f}"),
                "priority": "urgent"
            })

    # LONG SETUP — mirror of short
    if len(lows) >= 2:
        for i in range(len(lows) - 1):
            idx1, l1 = lows[i]
            idx2, l2 = lows[i + 1]
            if l1 - l2 < MIN_GAP: continue
            peak_b = max(c["high"] for c in candles[idx1:idx2+1])
            if peak_b - l2 < MIN_SIZE: continue
            if not momentum_up(candles, idx2): continue
            if last_idx - idx2 > RECENT: continue
            alerts.append({
                "title": "LONG SETUP - Wave Strategy",
                "msg":   (f"1st low ${l1:,.0f} to 2nd low ${l2:,.0f} (-${l1-l2:,.0f})\n"
                          f"Wave size: ${peak_b-l2:,.0f} | TP: ~${l1:,.0f} | Now: ${price:,.0f}"),
                "priority": "urgent"
            })

    return alerts

def main():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now}] BTC Wave Scanner starting...")

    notify("BTC Scanner Running", f"Scanning at {now}", priority="min")

    try:
        candles = get_candles()
        price   = candles[-1]["close"]
        print(f"  Price: ${price:,.0f} | {len(candles)} candles loaded")

        alerts = scan(candles)

        if alerts:
            for a in alerts:
                notify(a["title"], a["msg"], a.get("priority", "urgent"))
                print(f"  ALERT: {a['title']}")
        else:
            print("  No setup detected.")

    except Exception as e:
        err = f"Error: {e}"
        print(f"  {err}")
        notify("Scanner Error", err, priority="high")

    print("Done.")

if __name__ == "__main__":
    main()
