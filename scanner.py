import requests
import json
import os
from datetime import datetime

# ── CONFIG ───────────────────────────────────────────────────────────────────
NTFY_TOPIC      = "btcwave554433"
PEAK_WINDOW     = 3
MOMENTUM_BARS   = 2
MIN_GAP         = 100
MIN_SIZE        = 150
RECENT          = 15
COOLDOWN_MIN    = 30
STATE_FILE      = "scanner_state.json"
POSITION_USD    = 50000   # fixed $50k position for PnL calculation
# ─────────────────────────────────────────────────────────────────────────────

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except Exception:
            pass
    return {"last_signal": {}, "open_trade": None}

def save_state(state):
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)

def in_cooldown(state, direction):
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
    r = requests.get(
        "https://api.coingecko.com/api/v3/coins/bitcoin/ohlc",
        params={"vs_currency": "usd", "days": "1"},
        headers={"User-Agent": "Mozilla/5.0"},
        timeout=15
    )
    r.raise_for_status()
    return [{
        "open":  float(c[1]),
        "high":  float(c[2]),
        "low":   float(c[3]),
        "close": float(c[4]),
    } for c in r.json()]

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
    return all(candles[i]["close"] < candles[i]["open"] for i in range(from_idx + 1, end))

def momentum_up(candles, from_idx):
    end = from_idx + 1 + MOMENTUM_BARS
    if end > len(candles): return False
    return all(candles[i]["close"] > candles[i]["open"] for i in range(from_idx + 1, end))

def check_open_trade(state, price):
    """Check if the open trade hit TP or SL. Send report if closed."""
    trade = state.get("open_trade")
    if not trade:
        return

    direction = trade["direction"]
    entry     = trade["entry"]
    tp        = trade["tp"]
    sl        = trade["sl"]
    qty       = POSITION_USD / entry

    result = None
    if direction == "short":
        if price <= tp:
            result = "won"
            exit_price = tp
        elif price >= sl:
            result = "lost"
            exit_price = sl
    else:  # long
        if price >= tp:
            result = "won"
            exit_price = tp
        elif price <= sl:
            result = "lost"
            exit_price = sl

    if not result:
        print(f"  Open {direction.upper()} trade still active | Entry: ${entry:,.0f} | TP: ${tp:,.0f} | SL: ${sl:,.0f} | Now: ${price:,.0f}")
        return

    pnl = ((entry - exit_price) if direction == "short" else (exit_price - entry)) * qty

    if result == "won":
        msg = (f"PnL: ${pnl:+.0f} | Exit: ${exit_price:,.0f}\n"
               f"{direction.upper()} | Entry: ${entry:,.0f} | TP: ${tp:,.0f}")
        notify("Trade WON", msg, priority="default")
        print(f"  Trade WON | PnL: ${pnl:+.0f}")
        # Reset cooldown on win
        state["last_signal"].pop(direction, None)

    else:
        move = abs(exit_price - entry)
        loss_report = (
            f"TRADE LOSS REPORT\n"
            f"Signal: {trade.get('signal', 'N/A')}\n"
            f"Direction: {direction.upper()}\n"
            f"Entry: ${entry:,.0f}\n"
            f"Target: ${tp:,.0f} (needed ${abs(tp-entry):,.0f} {'up' if direction=='long' else 'down'})\n"
            f"Stop: ${sl:,.0f} (risked ${abs(sl-entry):,.0f})\n"
            f"Exit: ${exit_price:,.0f} (moved ${move:,.0f} against you)\n"
            f"PnL: ${pnl:+.0f} (qty {qty:.4f} BTC)\n"
            f"Paste to Claude to diagnose."
        )
        notify("Trade LOST", loss_report, priority="high")
        print(f"  Trade LOST | PnL: ${pnl:+.0f}")
        print(loss_report)
        # Keep cooldown on loss (block revenge trade)

    state["open_trade"] = None
    save_state(state)

def scan(candles):
    peaks, lows = find_peaks_lows(candles)
    price    = candles[-1]["close"]
    last_idx = len(candles) - 1
    alerts   = []

    # SHORT
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
                "direction": "short",
                "signal": "SHORT SETUP - Wave Strategy",
                "title": "SHORT SETUP - Wave Strategy",
                "msg":  (f"Entry: ~${price:,.0f}\n"
                         f"Target: ${p1:,.0f} | Stop: ${p2:,.0f}\n"
                         f"2nd peak ${p2:,.0f} broke 1st ${p1:,.0f} (+${p2-p1:,.0f})\n"
                         f"Wave: ${p2-trough:,.0f} tall"),
                "entry": price, "tp": p1, "sl": p2,
                "priority": "urgent"
            })

    # LONG
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
                "direction": "long",
                "signal": "LONG SETUP - Wave Strategy",
                "title": "LONG SETUP - Wave Strategy",
                "msg":  (f"Entry: ~${price:,.0f}\n"
                         f"Target: ${peak_b:,.0f} | Stop: ${l2:,.0f}\n"
                         f"2nd low ${l2:,.0f} broke 1st ${l1:,.0f} (-${l1-l2:,.0f})\n"
                         f"Wave: ${peak_b-l2:,.0f} tall"),
                "entry": price, "tp": peak_b, "sl": l2,
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

        # ── 1. Check if open trade hit TP or SL ──────────────────────────────
        check_open_trade(state, price)

        # ── 2. Scan for new setups ────────────────────────────────────────────
        # Skip if there's already an open trade (one trade at a time)
        if state.get("open_trade"):
            print("  Skipping new signals — trade already open.")
            return

        alerts = scan(candles)

        if alerts:
            for a in alerts:
                direction = a["direction"]
                blocked, mins_left = in_cooldown(state, direction)
                if blocked:
                    print(f"  [COOLDOWN] {direction.upper()} blocked — {mins_left} min remaining")
                    continue

                notify(a["title"], a["msg"], a.get("priority", "urgent"))
                print(f"  ALERT: {a['title']}")

                # Save trade to state so next run can track TP/SL
                state["open_trade"] = {
                    "direction": direction,
                    "signal":    a["signal"],
                    "entry":     a["entry"],
                    "tp":        a["tp"],
                    "sl":        a["sl"],
                    "time":      now_str,
                }
                state["last_signal"][direction] = datetime.now().isoformat()
                save_state(state)
                break  # one trade at a time

        else:
            print("  No setup detected.")

    except Exception as e:
        err = f"Error: {e}"
        print(f"  {err}")
        notify("Scanner Error", err, priority="high")

    print("Done.")

if __name__ == "__main__":
    main()
