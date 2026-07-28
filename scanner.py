import requests
import json
from datetime import datetime

# ── CONFIG ───────────────────────────────────────────────────────────────────
SYMBOL     = "BTCUSDT"
NTFY_TOPIC = "btcwave554433"

TIMEFRAMES = [
    {"interval": "1",  "candles": 200, "min_gap": 150, "min_size": 200, "recent": 30,  "label": "1m"},
    {"interval": "5",  "candles": 100, "min_gap": 200, "min_size": 300, "recent": 20,  "label": "5m"},
    {"interval": "15", "candles": 200, "min_gap": 400, "min_size": 600, "recent": 15,  "label": "15m"},
]

PEAK_WINDOW    = 5
MOMENTUM_BARS  = 3
VOLUME_CONFIRM = True
# ─────────────────────────────────────────────────────────────────────────────

def notify(title, msg, priority="default"):
    """Send a notification to Ntfy."""
    try:
        requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=msg.encode("utf-8"),
            headers={
                "Title":    title,
                "Priority": priority,
                "Tags":     "chart_increasing"
            },
            timeout=10
        )
        print(f"  📱 Ntfy sent: {title}")
    except Exception as e:
        print(f"  Ntfy failed: {e}")

def get_candles(interval, limit):
    """Fetch candles from Bybit — no geo restrictions."""
    r = requests.get(
        "https://api.bybit.com/v5/market/kline",
        params={
            "category": "linear",
            "symbol":   SYMBOL,
            "interval": interval,
            "limit":    limit
        },
        timeout=10
    )
    r.raise_for_status()
    data    = r.json()
    candles = list(reversed(data["result"]["list"]))  # oldest first
    return [{
        "open":   float(c[1]),
        "high":   float(c[2]),
        "low":    float(c[3]),
        "close":  float(c[4]),
        "volume": float(c[5])
    } for c in candles]

def find_peaks_lows(candles):
    peaks, lows = [], []
    w = PEAK_WINDOW
    for i in range(w, len(candles) - w):
        wh = [c["high"] for c in candles[i-w:i+w+1]]
        wl = [c["low"]  for c in candles[i-w:i+w+1]]
        if candles[i]["high"] == max(wh) and candles[i]["high"] > min(wh) + 30:
            peaks.append((i, candles[i]["high"], candles[i]["volume"]))
        if candles[i]["low"] == min(wl) and candles[i]["low"] < max(wl) - 30:
            lows.append((i, candles[i]["low"], candles[i]["volume"]))
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

def scan(candles, label, min_gap, min_size, recent):
    peaks, lows = find_peaks_lows(candles)
    price    = candles[-1]["close"]
    last_idx = len(candles) - 1
    alerts   = []

    # ── SHORT SETUP ──────────────────────────────────────────────────────────
    # 1. 2nd peak beats 1st by at least min_gap
    # 2. Wave height (peak to trough) >= min_size
    # 3. 2nd peak volume >= 1st peak volume (real move not fake)
    # 4. 3 consecutive bearish candles after 2nd peak (momentum confirmed)
    # 5. 2nd peak is within last 'recent' candles (not stale)
    if len(peaks) >= 2:
        for i in range(len(peaks) - 1):
            idx1, p1, v1 = peaks[i]
            idx2, p2, v2 = peaks[i + 1]
            if p2 - p1 < min_gap: continue
            trough = min(c["low"] for c in candles[idx1:idx2+1])
            if p2 - trough < min_size: continue
            if VOLUME_CONFIRM and v2 < v1: continue
            if not momentum_down(candles, idx2): continue
            if last_idx - idx2 > recent: continue
            alerts.append({
                "title": f"⬇️ SHORT SETUP [{label}]",
                "msg":   (f"1st peak ${p1:,.0f} → 2nd peak ${p2:,.0f} (+${p2-p1:,.0f})\n"
                          f"Wave: ${p2-trough:,.0f} tall | TP: ~${p1:,.0f} | Now: ${price:,.0f}"),
                "priority": "urgent"
            })

    # ── LONG SETUP ───────────────────────────────────────────────────────────
    # Mirror of short — 2nd low goes deeper than 1st
    if len(lows) >= 2:
        for i in range(len(lows) - 1):
            idx1, l1, v1 = lows[i]
            idx2, l2, v2 = lows[i + 1]
            if l1 - l2 < min_gap: continue
            peak_b = max(c["high"] for c in candles[idx1:idx2+1])
            if peak_b - l2 < min_size: continue
            if VOLUME_CONFIRM and v2 < v1: continue
            if not momentum_up(candles, idx2): continue
            if last_idx - idx2 > recent: continue
            alerts.append({
                "title": f"⬆️ LONG SETUP [{label}]",
                "msg":   (f"1st low ${l1:,.0f} → 2nd low ${l2:,.0f} (-${l1-l2:,.0f})\n"
                          f"Wave: ${peak_b-l2:,.0f} tall | TP: ~${l1:,.0f} | Now: ${price:,.0f}"),
                "priority": "urgent"
            })

    # ── HIGH VOLUME SPIKE ─────────────────────────────────────────────────────
    # Volume 3x average = unexpected big move — half profit only
    if len(candles) >= 25:
        avg_vol = sum(c["volume"] for c in candles[-25:-5]) / 20
        for i in range(max(0, last_idx - 10), last_idx - 2):
            c = candles[i]
            if c["volume"] < avg_vol * 3: continue
            move = abs(c["close"] - c["open"])
            if move < min_size * 0.5: continue
            direction = "DOWN" if c["close"] < c["open"] else "UP"
            icon      = "⬇️" if direction == "DOWN" else "⬆️"
            half_tp   = (c["close"] - move * 0.5) if direction == "DOWN" else (c["close"] + move * 0.5)
            alerts.append({
                "title": f"{icon} HIGH VOL SPIKE [{label}] — HALF PROFIT",
                "msg":   (f"Vol {c['volume']:,.0f} = {c['volume']/avg_vol:.1f}x normal\n"
                          f"Move ${move:,.0f} {direction} | Half TP: ~${half_tp:,.0f} | Now: ${price:,.0f}"),
                "priority": "high"
            })

    return alerts

def main():
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now}] BTC Wave Scanner starting...")

    # Notify phone that scan is starting
    notify("🔍 BTC Scanner Running", f"Scanning 1m, 5m, 15m at {now}", priority="low")

    all_alerts = []
    errors     = []

    for tf in TIMEFRAMES:
        try:
            candles = get_candles(tf["interval"], tf["candles"])
            price   = candles[-1]["close"]
            print(f"  [{tf['label']}] Price: ${price:,.0f} — scanning {len(candles)} candles")
            found = scan(candles, tf["label"], tf["min_gap"], tf["min_size"], tf["recent"])
            all_alerts.extend(found)
            if found:
                print(f"  [{tf['label']}] {len(found)} setup(s) found!")
            else:
                print(f"  [{tf['label']}] No setup.")
        except Exception as e:
            err = f"[{tf['label']}] Error: {e}"
            print(f"  {err}")
            errors.append(err)

    # Send alerts
    if all_alerts:
        for a in all_alerts:
            notify(a["title"], a["msg"], a.get("priority", "urgent"))
    else:
        print("  No setups detected on any timeframe.")

    # Notify if errors occurred
    if errors:
        notify(
            "⚠️ Scanner Error",
            "Errors occurred:\n" + "\n".join(errors),
            priority="high"
        )

    print("Done.")

if __name__ == "__main__":
    main()
