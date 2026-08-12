import requests
import json
import os
import sys
from datetime import datetime

# ── CONFIG ───────────────────────────────────────────────────────────────────
NTFY_TOPIC    = "btcwave554433"
STATE_FILE    = "scanner_state.json"
POSITION_USD  = 50000
PEAK_WINDOW   = 3
MOMENTUM_BARS = 2
COOLDOWN_MIN  = 30

# Per-timeframe thresholds — lowered to catch setups in tight/ranging markets
# GAP    = 2nd peak/low must beat 1st by this much
# SIZE   = total wave height must be at least this
# RECENT = 2nd peak/low must be within this many bars of the latest candle
TIMEFRAMES = [
    {"label": "1m",  "interval": 1,  "gap": 100, "size": 200, "recent": 15, "vol_confirm": True},
    {"label": "5m",  "interval": 5,  "gap": 150, "size": 300, "recent": 15, "vol_confirm": True},
    {"label": "15m", "interval": 15, "gap": 200, "size": 400, "recent": 15, "vol_confirm": True},
    {"label": "60m", "interval": 60, "gap": 400, "size": 800, "recent": 10, "vol_confirm": True},
]
# ─────────────────────────────────────────────────────────────────────────────

DEBUG = "--debug" in sys.argv


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


def in_cooldown(state, direction, label):
    key = f"{direction}_{label}"
    last_str = state["last_signal"].get(key)
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
    if data.get("error") and data["error"]:
        raise Exception(f"Kraken error: {data['error']}")
    result = data["result"]
    pair_key = next(k for k in result.keys() if k != "last")
    raw = result[pair_key]
    if not raw:
        raise Exception("Kraken returned empty candle data")
    return [{
        "time":   int(c[0]),
        "open":   float(c[1]),
        "high":   float(c[2]),
        "low":    float(c[3]),
        "close":  float(c[4]),
        "volume": float(c[6]),
    } for c in raw]


def get_coinbase_candles(interval_minutes):
    gran_map = {1: 60, 5: 300, 15: 900, 60: 3600}
    gran = gran_map.get(interval_minutes, 60)
    r = requests.get(
        "https://api.exchange.coinbase.com/products/BTC-USD/candles",
        params={"granularity": gran},
        timeout=15
    )
    r.raise_for_status()
    raw = list(reversed(r.json()))
    return [{
        "time":   int(c[0]),
        "open":   float(c[3]),
        "high":   float(c[2]),
        "low":    float(c[1]),
        "close":  float(c[4]),
        "volume": float(c[5]),
    } for c in raw]


def find_peaks_lows(candles):
    peaks, lows = [], []
    w = PEAK_WINDOW
    for i in range(w, len(candles) - w):
        wh = [c["high"] for c in candles[i-w:i+w+1]]
        wl = [c["low"]  for c in candles[i-w:i+w+1]]
        if candles[i]["high"] == max(wh) and candles[i]["high"] > min(wh) + 20:
            peaks.append((i, candles[i]["high"], candles[i]["volume"]))
        if candles[i]["low"] == min(wl) and candles[i]["low"] < max(wl) - 20:
            lows.append((i, candles[i]["low"], candles[i]["volume"]))
    return peaks, lows


def momentum_down(candles, from_idx):
    end = from_idx + 1 + MOMENTUM_BARS
    if end > len(candles): return False
    return all(candles[i]["close"] < candles[i]["open"] for i in range(from_idx + 1, end))


def momentum_up(candles, from_idx):
    end = from_idx + 1 + MOMENTUM_BARS
    if end > len(candles): return False
    return all(candles[i]["close"] > candles[i]["open"] for i in range(from_idx + 1, end))


def ts(candle):
    if "time" in candle:
        return datetime.utcfromtimestamp(candle["time"]).strftime("%H:%M")
    return "??"


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
    exit_price = None
    if direction == "short":
        if price <= tp:
            result, exit_price = "won", tp
        elif price >= sl:
            result, exit_price = "lost", sl
    else:
        if price >= tp:
            result, exit_price = "won", tp
        elif price <= sl:
            result, exit_price = "lost", sl

    if not result:
        print(f"  Open {direction.upper()} | Entry: ${entry:,.0f} | TP: ${tp:,.0f} | SL: ${sl:,.0f} | Now: ${price:,.0f}")
        return

    pnl = ((entry - exit_price) if direction == "short" else (exit_price - entry)) * qty

    if result == "won":
        notify("Trade WON",
               f"PnL: ${pnl:+.0f} | Exit: ${exit_price:,.0f}\n{direction.upper()} | Entry: ${entry:,.0f} | TP: ${tp:,.0f}",
               priority="default")
        print(f"  Trade WON | PnL: ${pnl:+.0f}")
        state["last_signal"].pop(f"{direction}_{trade.get('timeframe','')}", None)
    else:
        move = abs(exit_price - entry)
        loss_report = (
            f"TRADE LOSS REPORT\n"
            f"Signal: {trade.get('signal', 'N/A')}\n"
            f"Timeframe: {trade.get('timeframe', 'N/A')}\n"
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


def scan_timeframe(candles, tf):
    label       = tf["label"]
    min_gap     = tf["gap"]
    min_size    = tf["size"]
    recent      = tf["recent"]
    vol_confirm = tf["vol_confirm"]

    peaks, lows = find_peaks_lows(candles)
    price    = candles[-1]["close"]
    last_idx = len(candles) - 1
    alerts   = []

    if len(peaks) >= 2:
        for i in range(len(peaks) - 1):
            idx1, p1, v1 = peaks[i]
            idx2, p2, v2 = peaks[i + 1]
            if p2 - p1 < min_gap: continue
            trough = min(c["low"] for c in candles[idx1:idx2+1])
            if p2 - trough < min_size: continue
            if vol_confirm and v2 < v1: continue
            if not momentum_down(candles, idx2): continue
            if last_idx - idx2 > recent: continue
            alerts.append({
                "direction": "short",
                "signal":    "SHORT SETUP - Wave Strategy",
                "title":     f"SELL SIGNAL [{label}]",
                "msg":       (f"Entry: ~${price:,.0f}\n"
                              f"Target: ${p1:,.0f} | Stop: ${p2:,.0f}\n"
                              f"2nd peak ${p2:,.0f} broke 1st ${p1:,.0f} (+${p2-p1:,.0f})\n"
                              f"Wave: ${p2-trough:,.0f} tall"),
                "entry":     price, "tp": p1, "sl": p2,
                "priority":  "urgent", "timeframe": label,
            })

    if len(lows) >= 2:
        for i in range(len(lows) - 1):
            idx1, l1, v1 = lows[i]
            idx2, l2, v2 = lows[i + 1]
            if l1 - l2 < min_gap: continue
            peak_b = max(c["high"] for c in candles[idx1:idx2+1])
            if peak_b - l2 < min_size: continue
            if vol_confirm and v2 < v1: continue
            if not momentum_up(candles, idx2): continue
            if last_idx - idx2 > recent: continue
            alerts.append({
                "direction": "long",
                "signal":    "LONG SETUP - Wave Strategy",
                "title":     f"BUY SIGNAL [{label}]",
                "msg":       (f"Entry: ~${price:,.0f}\n"
                              f"Target: ${peak_b:,.0f} | Stop: ${l2:,.0f}\n"
                              f"2nd low ${l2:,.0f} broke 1st ${l1:,.0f} (-${l1-l2:,.0f})\n"
                              f"Wave: ${peak_b-l2:,.0f} tall"),
                "entry":     price, "tp": peak_b, "sl": l2,
                "priority":  "urgent", "timeframe": label,
            })

    return alerts


def debug_timeframe(candles, tf):
    label       = tf["label"]
    min_gap     = tf["gap"]
    min_size    = tf["size"]
    recent      = tf["recent"]
    vol_confirm = tf["vol_confirm"]

    print(f"\n{'='*60}")
    print(f"DEBUG [{label}] — {len(candles)} candles | gap>=${min_gap} size>=${min_size} recent<={recent} vol={vol_confirm}")
    print(f"  Range: {ts(candles[0])} -> {ts(candles[-1])} UTC")
    print(f"  Price range: ${min(c['low'] for c in candles):,.0f} - ${max(c['high'] for c in candles):,.0f}")

    peaks, lows = find_peaks_lows(candles)
    last_idx = len(candles) - 1
    price = candles[-1]["close"]

    print(f"\n  PEAKS ({len(peaks)}):")
    for idx, p, v in peaks:
        print(f"    [{ts(candles[idx])}] ${p:,.0f}  vol={v:.2f}  age={last_idx-idx}")

    print(f"\n  LOWS ({len(lows)}):")
    for idx, l, v in lows:
        print(f"    [{ts(candles[idx])}] ${l:,.0f}  vol={v:.2f}  age={last_idx-idx}")

    print(f"\n  SHORT checks:")
    for i in range(len(peaks) - 1):
        idx1, p1, v1 = peaks[i]
        idx2, p2, v2 = peaks[i + 1]
        trough = min(c["low"] for c in candles[idx1:idx2+1])
        gap, size, age = p2-p1, p2-trough, last_idx-idx2
        mom = momentum_down(candles, idx2)
        vol_ok = v2 >= v1 if vol_confirm else True
        print(f"\n    {ts(candles[idx1])} ${p1:,.0f} -> {ts(candles[idx2])} ${p2:,.0f}")
        print(f"      GAP ${gap:,.0f} (>=${min_gap}) {'OK' if gap>=min_gap else 'FAIL'}")
        print(f"      SIZE ${size:,.0f} (>=${min_size}) {'OK' if size>=min_size else 'FAIL'}")
        print(f"      VOL v2={v2:.2f} v1={v1:.2f} {'OK' if vol_ok else 'FAIL'}")
        print(f"      MOM {'OK' if mom else 'FAIL'}  AGE {age} (<={recent}) {'OK' if age<=recent else 'FAIL'}")
        if gap>=min_gap and size>=min_size and vol_ok and mom and age<=recent:
            print(f"      >>> WOULD FIRE SELL at ${price:,.0f}")
        else:
            print(f"      >>> NO SIGNAL")

    print(f"\n  LONG checks:")
    for i in range(len(lows) - 1):
        idx1, l1, v1 = lows[i]
        idx2, l2, v2 = lows[i + 1]
        peak_b = max(c["high"] for c in candles[idx1:idx2+1])
        gap, size, age = l1-l2, peak_b-l2, last_idx-idx2
        mom = momentum_up(candles, idx2)
        vol_ok = v2 >= v1 if vol_confirm else True
        print(f"\n    {ts(candles[idx1])} ${l1:,.0f} -> {ts(candles[idx2])} ${l2:,.0f}")
        print(f"      GAP ${gap:,.0f} (>=${min_gap}) {'OK' if gap>=min_gap else 'FAIL'}")
        print(f"      SIZE ${size:,.0f} (>=${min_size}) {'OK' if size>=min_size else 'FAIL'}")
        print(f"      VOL v2={v2:.2f} v1={v1:.2f} {'OK' if vol_ok else 'FAIL'}")
        print(f"      MOM {'OK' if mom else 'FAIL'}  AGE {age} (<={recent}) {'OK' if age<=recent else 'FAIL'}")
        if gap>=min_gap and size>=min_size and vol_ok and mom and age<=recent:
            print(f"      >>> WOULD FIRE BUY at ${price:,.0f}")
        else:
            print(f"      >>> NO SIGNAL")


def main():
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now_str}] BTC Wave Scanner starting...")

    if not DEBUG:
        notify("BTC Scanner Running", f"Scanning at {now_str}", priority="min")

    state = load_state()

    tf_candles = []
    master_price = None
    for tf in TIMEFRAMES:
        try:
            candles = get_kraken_candles(tf["interval"])
            tf_candles.append((tf, candles))
            if master_price is None:
                master_price = candles[-1]["close"]
            print(f"  Kraken [{tf['label']}] {len(candles)} candles OK")
        except Exception as e:
            print(f"  Kraken [{tf['label']}] failed: {e} — trying Coinbase...")
            try:
                candles = get_coinbase_candles(tf["interval"])
                tf_candles.append((tf, candles))
                if master_price is None:
                    master_price = candles[-1]["close"]
                print(f"  Coinbase [{tf['label']}] {len(candles)} candles OK")
            except Exception as e2:
                print(f"  [{tf['label']}] both sources failed: {e2} — skipping")

    if not tf_candles or master_price is None:
        notify("Scanner Error", "All data sources failed", priority="high")
        return

    if DEBUG:
        print(f"\n{'#'*60}\nDEBUG MODE\n{'#'*60}")
        for tf, candles in tf_candles:
            debug_timeframe(candles, tf)
        print("\nDEBUG DONE — no alerts sent, no state changed.")
        return

    check_open_trade(state, master_price)

    if state.get("open_trade"):
        print("  Skipping new signals — trade already open.")
        return

    all_alerts = []
    for tf, candles in tf_candles:
        all_alerts.extend(scan_timeframe(candles, tf))

    if not all_alerts:
        print("  No setup detected.")
        return

    priority_order = {"1m": 0, "5m": 1, "15m": 2, "60m": 3}
    all_alerts.sort(key=lambda x: priority_order.get(x.get("timeframe", ""), 99))

    for a in all_alerts:
        direction = a["direction"]
        label     = a["timeframe"]
        blocked, mins_left = in_cooldown(state, direction, label)
        if blocked:
            print(f"  [COOLDOWN] {direction.upper()} [{label}] — {mins_left} min remaining")
            continue

        notify(a["title"], a["msg"], a.get("priority", "urgent"))
        print(f"  ALERT: {a['title']}")

        state["open_trade"] = {
            "direction": direction,
            "signal":    a["signal"],
            "entry":     a["entry"],
            "tp":        a["tp"],
            "sl":        a["sl"],
            "timeframe": label,
            "time":      now_str,
        }
        state["last_signal"][f"{direction}_{label}"] = datetime.now().isoformat()
        save_state(state)
        break

    print("Done.")


if __name__ == "__main__":
    main()
