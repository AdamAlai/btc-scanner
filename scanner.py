import requests
import json
import os
from datetime import datetime

NTFY_TOPIC      = "btcwave554433"
PEAK_WINDOW     = 3
MOMENTUM_BARS   = 2
MIN_GAP         = 300
MIN_SIZE        = 600
RECENT          = 15
COOLDOWN_MIN    = 30
STATE_FILE      = "scanner_state.json"
POSITION_USD    = 50000
DEBUG           = os.environ.get("DEBUG", "0") == "1"

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

def get_kraken_candles(interval):
    url = "https://api.kraken.com/0/public/OHLC"
    params = {"pair": "XBTUSD", "interval": interval}
    r = requests.get(url, params=params, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("error"):
        raise Exception(f"Kraken error: {data['error']}")
    result = data["result"]
    pair_key = list(result.keys())[0]
    raw = result[pair_key]
    candles = []
    for c in raw:
        candles.append({
            "time":  int(c[0]),
            "open":  float(c[1]),
            "high":  float(c[2]),
            "low":   float(c[3]),
            "close": float(c[4]),
            "vwap":  float(c[5]),
            "volume": float(c[6]),
        })
    return candles

def get_coingecko_candles():
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
            result = "won"; exit_price = tp
        elif price >= sl:
            result = "lost"; exit_price = sl
    else:
        if price >= tp:
            result = "won"; exit_price = tp
        elif price <= sl:
            result = "lost"; exit_price = sl

    if not result:
        print(f"  Open {direction.upper()} active | Entry ${entry:,.0f} | TP ${tp:,.0f} | SL ${sl:,.0f} | Now ${price:,.0f}")
        return

    pnl = ((entry - exit_price) if direction == "short" else (exit_price - entry)) * qty
    if result == "won":
        msg = f"PnL: ${pnl:+.0f} | Exit: ${exit_price:,.0f}\n{direction.upper()} | Entry: ${entry:,.0f} | TP: ${tp:,.0f}"
        notify("Trade WON", msg, priority="default")
        print(f"  Trade WON | PnL: ${pnl:+.0f}")
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
    state["open_trade"] = None
    save_state(state)

def scan_timeframe(candles, label):
    peaks, lows = find_peaks_lows(candles)
    price    = candles[-1]["close"]
    last_idx = len(candles) - 1
    alerts   = []

    if DEBUG:
        print(f"\n  [{label}] DEBUG — Last 5 peaks:")
        for idx, p in peaks[-5:]:
            age = last_idx - idx
            print(f"    idx={idx} price=${p:,.0f} age={age}bars")
        print(f"  [{label}] DEBUG — Last 5 lows:")
        for idx, l in lows[-5:]:
            age = last_idx - idx
            print(f"    idx={idx} price=${l:,.0f} age={age}bars")

    if len(peaks) >= 2:
        for i in range(len(peaks) - 1):
            idx1, p1 = peaks[i]
            idx2, p2 = peaks[i + 1]
            gap   = p2 - p1
            trough = min(c["low"] for c in candles[idx1:idx2+1])
            size  = p2 - trough
            mom   = momentum_down(candles, idx2)
            age   = last_idx - idx2
            recent_ok = age <= RECENT

            if DEBUG:
                status = []
                if gap < MIN_GAP:          status.append(f"gap ${gap:.0f} < ${MIN_GAP}")
                if size < MIN_SIZE:        status.append(f"size ${size:.0f} < ${MIN_SIZE}")
                if not mom:                status.append("no momentum")
                if not recent_ok:         status.append(f"too old ({age}bars)")
                if not status:
                    print(f"  [{label}] DEBUG SHORT idx1={idx1} idx2={idx2} -> PASS | gap=${gap:.0f} size=${size:.0f} age={age}")
                else:
                    print(f"  [{label}] DEBUG SHORT idx1={idx1} idx2={idx2} -> FAIL: {', '.join(status)}")

            if gap < MIN_GAP: continue
            if size < MIN_SIZE: continue
            if not mom: continue
            if not recent_ok: continue

            alerts.append({
                "direction": "short",
                "signal": "SHORT SETUP - Wave Strategy",
                "title": f"SELL SIGNAL [{label}]",
                "msg":  (f"Entry: ~${price:,.0f}\n"
                         f"Target: ${p1:,.0f} | Stop: ${p2:,.0f}\n"
                         f"2nd peak ${p2:,.0f} broke 1st ${p1:,.0f} (+${p2-p1:,.0f})\n"
                         f"Wave: ${p2-trough:,.0f} tall"),
                "entry": price, "tp": p1, "sl": p2,
                "priority": "urgent",
                "timeframe": label,
            })

    if len(lows) >= 2:
        for i in range(len(lows) - 1):
            idx1, l1 = lows[i]
            idx2, l2 = lows[i + 1]
            gap   = l1 - l2
            peak_b = max(c["high"] for c in candles[idx1:idx2+1])
            size  = peak_b - l2
            mom   = momentum_up(candles, idx2)
            age   = last_idx - idx2
            recent_ok = age <= RECENT

            if DEBUG:
                status = []
                if gap < MIN_GAP:          status.append(f"gap ${gap:.0f} < ${MIN_GAP}")
                if size < MIN_SIZE:        status.append(f"size ${size:.0f} < ${MIN_SIZE}")
                if not mom:                status.append("no momentum")
                if not recent_ok:         status.append(f"too old ({age}bars)")
                if not status:
                    print(f"  [{label}] DEBUG LONG  idx1={idx1} idx2={idx2} -> PASS | gap=${gap:.0f} size=${size:.0f} age={age}")
                else:
                    print(f"  [{label}] DEBUG LONG  idx1={idx1} idx2={idx2} -> FAIL: {', '.join(status)}")

            if gap < MIN_GAP: continue
            if size < MIN_SIZE: continue
            if not mom: continue
            if not recent_ok: continue

            alerts.append({
                "direction": "long",
                "signal": "LONG SETUP - Wave Strategy",
                "title": f"BUY SIGNAL [{label}]",
                "msg":  (f"Entry: ~${price:,.0f}\n"
                         f"Target: ${peak_b:,.0f} | Stop: ${l2:,.0f}\n"
                         f"2nd low ${l2:,.0f} broke 1st ${l1:,.0f} (-${l1-l2:,.0f})\n"
                         f"Wave: ${peak_b-l2:,.0f} tall"),
                "entry": price, "tp": peak_b, "sl": l2,
                "priority": "urgent",
                "timeframe": label,
            })

    return alerts

def main():
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now_str}] BTC Wave Scanner starting...")
    if DEBUG:
        print("  *** DEBUG MODE ON ***")

    notify("BTC Scanner Running", f"Scanning at {now_str}", priority="min")
    state = load_state()
    master_price = None

    try:
        candles_1m  = get_kraken_candles(1)
        candles_5m  = get_kraken_candles(5)
        candles_15m = get_kraken_candles(15)
        timeframes  = [("1m", candles_1m), ("5m", candles_5m), ("15m", candles_15m)]
        master_price = candles_1m[-1]["close"]
        print(f"  Kraken OK | 1m:{len(candles_1m)} 5m:{len(candles_5m)} 15m:{len(candles_15m)} candles")
    except Exception as e:
        print(f"  Kraken failed: {e}")
        try:
            cg = get_coingecko_candles()
            timeframes = [("live", cg)]
            master_price = cg[-1]["close"]
            print(f"  CoinGecko fallback | {len(cg)} candles")
        except Exception as e2:
            err = f"All data sources failed: {e2}"
            print(f"  {err}")
            notify("Scanner Error", err, priority="high")
            return

    check_open_trade(state, master_price)

    if state.get("open_trade"):
        print("  Skipping new signals — trade already open.")
        return

    all_alerts = []
    for label, candles in timeframes:
        alerts = scan_timeframe(candles, label)
        all_alerts.extend(alerts)

    if all_alerts:
        priority_order = {"1m": 0, "5m": 1, "15m": 2, "live": 3}
        all_alerts.sort(key=lambda x: priority_order.get(x.get("timeframe", "live"), 99))

        for a in all_alerts:
            direction = a["direction"]
            blocked, mins_left = in_cooldown(state, direction)
            if blocked:
                print(f"  [COOLDOWN] {direction.upper()} blocked — {mins_left} min remaining")
                continue
            notify(a["title"], a["msg"], a.get("priority", "urgent"))
            print(f"  ALERT: {a['title']}")
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
            break
    else:
        print("  No setup detected.")

    print("Done.")

if __name__ == "__main__":
    main()
