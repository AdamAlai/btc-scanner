import requests
import time
import json
import os
from datetime import datetime, timezone

# ── CONFIG ───────────────────────────────────────────────────────────────────
NTFY_TOPIC      = "btcwave554433"
PEAK_WINDOW     = 3
MOMENTUM_BARS   = 2
MIN_GAP         = 100    # 2nd peak must beat 1st by $100
MIN_SIZE        = 150    # wave must be $150 tall
RECENT          = 15     # 2nd peak/low within last 15 candles
COOLDOWN_MIN    = 30     # block same direction within 30 minutes
STATE_FILE      = "scanner_state.json"   # persists cooldown across runs
# ─────────────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_signal": {}}   # {"long": "2026-07-30T12:00:00", "short": ...}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)

def in_cooldown(state, direction):
    """Return (True, minutes_remaining) if still in cooldown, else (False, 0)."""
    last_str = state["last_signal"].get(direction)
    if not last_str:
        return False, 0
    last = datetime.fromisoformat(last_str)
    elapsed = (datetime.now() - last).total_seconds() / 60
    if elapsed < COOLDOWN_MIN:
        return True, int(COOLDOWN_MIN - elapsed)
    return False, 0

def notify(title, msg, priority="default"):
    safe_title = title.encode("ascii", "ignore").decode("ascii")
    safe_msg   = msg.encode("ascii", "ignore").decode("ascii")
    try:
        r = requests.post(
            f"https://ntfy.sh/{NTFY_TOPIC}",
            data=safe_msg.encode("utf-8"),
            headers={"Title": safe_title, "Priority": priority},
            timeout=10
        )
        print(f"  Ntfy sent ({r.status_code}): {safe_title}")
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

    # ── SHORT SETUP ──────────────────────────────────────────────────────────
    if len(peaks) >= 2:
        for i in range(len(peaks) - 1):
            idx1, p1 = peaks[i]
            idx2, p2 = peaks[i + 1]
            if p2 - p1 < MIN_GAP: continue
            trough = min(c["low"] for c in candles[idx1:idx2+1])
            if p2 - trough < MIN_SIZE: continue
            if not momentum_down(candles, idx2): continue
            if last_idx - idx2 > RECENT: continue
            # TP = 1st peak (below entry — price should fall back to it)
            # SL = 2nd peak (if broken upward, wave invalid)
            alerts.append({
                "direction": "short",
                "title": "SHORT SETUP - Wave Strategy",
                "msg":  (f"Entry: ~${price:,.0f}\n"
                         f"Target: ${p1:,.0f} | Stop: ${p2:,.0f}\n"
                         f"2nd peak ${p2:,.0f} broke 1st ${p1:,.0f} (+${p2-p1:,.0f})\n"
                         f"Wave: ${p2-trough:,.0f} tall"),
                "priority": "urgent"
            })

    # ── LONG SETUP ───────────────────────────────────────────────────────────
    if len(lows) >= 2:
        for i in range(len(lows) - 1):
            idx1, l1 = lows[i]
            idx2, l2 = lows[i + 1]
            if l1 - l2 < MIN_GAP: continue
            peak_b = max(c["high"] for c in candles[idx1:idx2+1])
            if peak_b - l2 < MIN_SIZE: continue
            if not momentum_up(candles, idx2): continue
            if last_idx - idx2 > RECENT: continue
            # TP = peak_b (the high between the lows — ABOVE entry)
            # SL = l2 (2nd low — if broken, wave invalid)
            alerts.append({
                "direction": "long",
                "title": "LONG SETUP - Wave Strategy",
                "msg":  (f"Entry: ~${price:,.0f}\n"
                         f"Target: ${peak_b:,.0f} | Stop: ${l2:,.0f}\n"
                         f"2nd low ${l2:,.0f} broke 1st ${l1:,.0f} (-${l1-l2:,.0f})\n"
                         f"Wave: ${peak_b-l2:,.0f} tall"),
                "priority": "urgent"
            })

    return alerts

def main():
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now_str}] BTC Wave Scanner starting...")

    notify("BTC Scanner Running", f"Scanning at {now_str}", priority="min")

    state = load_state()

    try:
        candles = get_candles()
        price   = candles[-1]["close"]
        print(f"  Price: ${price:,.0f} | {len(candles)} candles loaded")

        alerts = scan(candles)

        if alerts:
            for a in alerts:
                direction = a["direction"]
                blocked, mins_left = in_cooldown(state, direction)
                if blocked:
                    print(f"  [COOLDOWN] {direction.upper()} signal blocked — {mins_left} min remaining")
                    continue

                notify(a["title"], a["msg"], a.get("priority", "urgent"))
                print(f"  ALERT: {a['title']}")
                # Record the signal time to enforce cooldown next run
                state["last_signal"][direction] = datetime.now().isoformat()
                save_state(state)
        else:
            print("  No setup detected.")

    except Exception as e:
        err = f"Error: {e}"
        print(f"  {err}")
        notify("Scanner Error", err, priority="high")

    print("Done.")

if __name__ == "__main__":
    main()
