import requests
import json
import os
import sys
from datetime import datetime, timezone, timedelta

# ── CONFIG ────────────────────────────────────────────────────────────
NTFY_TOPIC     = os.environ.get("NTFY_TOPIC", "btcwave554433")
STATE_FILE     = "tjr_state.json"
TRADE_LOG_FILE = "tjr_trade_log.json"
POSITION_USD   = 50000
SWING_LOOKBACK = 30  # candles to look back for swing points
STALE_HOURS    = 6
MIN_BODY_RATIO = 0.60  # confirmation candle must have ≥60% body

# NY Killzone: 8:30 AM - 11:00 AM EST
KILLZONE_START = (8, 30)   # 8:30 AM
KILLZONE_END   = (11, 0)   # 11:00 AM

TIMEFRAMES = {
    "4h": 240,
    "1h": 60,
    "15m": 15,
}
# ─────────────────────────────────────────────────────────────────────

DEBUG = "--debug" in sys.argv

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE) as f: return json.load(f)
        except Exception: pass
    return {"last_signal": {}, "open_trade": None, "killzone_fired": False}

def save_state(state):
    with open(STATE_FILE, "w") as f: json.dump(state, f, indent=2)

def load_trade_log():
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE) as f:
                data = json.load(f)
                if isinstance(data, list): return data
        except Exception: pass
    return []

def log_trade(trade, result, exit_price, pnl):
    log = load_trade_log()
    log.append({
        "open_time": trade.get("time", ""), "close_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "bot": "TJR", "signal": trade.get("signal", ""), "timeframe": "15m",
        "direction": trade.get("direction", ""), "entry": trade.get("entry", 0),
        "tp": trade.get("tp", 0), "sl": trade.get("sl", 0), "exit": exit_price,
        "pnl": round(pnl, 2), "result": result,
    })
    with open(TRADE_LOG_FILE, "w") as f: json.dump(log, f, indent=2)

def notify(title, msg, priority="default"):
    safe_title = title.encode("ascii", "ignore").decode("ascii")
    safe_msg   = msg.encode("ascii", "ignore").decode("ascii")
    try:
        r = requests.post(f"https://ntfy.sh/{NTFY_TOPIC}", data=safe_msg.encode("utf-8"),
                          headers={"Title": safe_title, "Priority": priority}, timeout=10)
        print(f"  Ntfy sent ({r.status_code}): {safe_title}")
    except Exception as e: print(f"  Ntfy failed: {e}")

def get_kraken_candles(interval_minutes):
    r = requests.get("https://api.kraken.com/0/public/OHLC",
                     params={"pair": "XBTUSD", "interval": interval_minutes}, timeout=15)
    r.raise_for_status()
    data = r.json()
    if data.get("error") and data["error"]: raise Exception(f"Kraken error: {data['error']}")
    result = data["result"]
    pair_key = next(k for k in result.keys() if k != "last")
    raw = result[pair_key]
    if not raw: raise Exception("Kraken returned empty candle data")
    return [{"time": int(c[0]), "open": float(c[1]), "high": float(c[2]),
             "low": float(c[3]), "close": float(c[4]), "volume": float(c[6])} for c in raw]

def is_killzone():
    """Check if current time is within NY Killzone (8:30-11:00 AM EST)"""
    now_utc = datetime.now(timezone.utc)
    # EST is UTC-5, EDT is UTC-4. Use UTC-4 for daylight saving (March-Nov)
    # For simplicity, use UTC-5 (EST). Adjust if needed.
    est_offset = -5
    now_est = now_utc + timedelta(hours=est_offset)
    
    current_minutes = now_est.hour * 60 + now_est.minute
    start_minutes = KILLZONE_START[0] * 60 + KILLZONE_START[1]
    end_minutes = KILLZONE_END[0] * 60 + KILLZONE_END[1]
    
    return start_minutes <= current_minutes <= end_minutes

def find_swings(candles, lookback=SWING_LOOKBACK):
    """Find swing highs and lows in the last N candles"""
    if len(candles) < lookback: return [], []
    
    recent = candles[-lookback:]
    swing_highs = []
    swing_lows = []
    
    for i in range(2, len(recent) - 2):
        # Swing high: higher than 2 candles on each side
        if (recent[i]["high"] > recent[i-1]["high"] and 
            recent[i]["high"] > recent[i-2]["high"] and
            recent[i]["high"] > recent[i+1]["high"] and 
            recent[i]["high"] > recent[i+2]["high"]):
            swing_highs.append({"idx": i, "price": recent[i]["high"], "time": recent[i]["time"]})
        
        # Swing low: lower than 2 candles on each side
        if (recent[i]["low"] < recent[i-1]["low"] and 
            recent[i]["low"] < recent[i-2]["low"] and
            recent[i]["low"] < recent[i+1]["low"] and 
            recent[i]["low"] < recent[i+2]["low"]):
            swing_lows.append({"idx": i, "price": recent[i]["low"], "time": recent[i]["time"]})
    
    return swing_highs, swing_lows

def detect_htf_bias(candles_4h, candles_1h):
    """Determine trend bias using HH/HL or LL/LH on 4H and 1H"""
    if len(candles_4h) < 10 or len(candles_1h) < 10:
        return None
    
    # Check 4H structure
    highs_4h = [c["high"] for c in candles_4h[-5:]]
    lows_4h = [c["low"] for c in candles_4h[-5:]]
    
    # Simple structure: compare recent highs and lows
    hh_4h = highs_4h[-1] > highs_4h[-2]  # Higher High
    hl_4h = lows_4h[-1] > lows_4h[-2]    # Higher Low
    lh_4h = highs_4h[-1] < highs_4h[-2]  # Lower High
    ll_4h = lows_4h[-1] < lows_4h[-2]    # Lower Low
    
    if hh_4h and hl_4h:
        bias_4h = "bullish"
    elif lh_4h and ll_4h:
        bias_4h = "bearish"
    else:
        bias_4h = "neutral"
    
    # Check 1H structure for confirmation
    highs_1h = [c["high"] for c in candles_1h[-5:]]
    lows_1h = [c["low"] for c in candles_1h[-5:]]
    
    hh_1h = highs_1h[-1] > highs_1h[-2]
    hl_1h = lows_1h[-1] > lows_1h[-2]
    lh_1h = highs_1h[-1] < highs_1h[-2]
    ll_1h = lows_1h[-1] < lows_1h[-2]
    
    if hh_1h and hl_1h:
        bias_1h = "bullish"
    elif lh_1h and ll_1h:
        bias_1h = "bearish"
    else:
        bias_1h = "neutral"
    
    # If both agree, strong bias. If not, use 4H as primary.
    if bias_4h == bias_1h:
        return bias_4h
    return bias_4h if bias_4h != "neutral" else None

def detect_liquidity_sweep(candles_1h, bias):
    """
    Detect if price swept liquidity (wicked through a level and closed back).
    Returns sweep info or None.
    """
    if len(candles_1h) < SWING_LOOKBACK + 5: return None
    
    swing_highs, swing_lows = find_swings(candles_1h, SWING_LOOKBACK)
    recent = candles_1h[-5:]  # Last 5 candles to check for sweep
    
    if bias == "bullish" and swing_lows:
        # Look for sweep of a swing low (price wicked below, closed back up)
        last_swing_low = swing_lows[-1]["price"]
        for i, candle in enumerate(recent):
            if candle["low"] < last_swing_low and candle["close"] > last_swing_low:
                return {
                    "type": "sweep_low",
                    "level": last_swing_low,
                    "sweep_candle_idx": len(candles_1h) - 5 + i,
                    "extreme": candle["low"],
                    "direction": "long"
                }
    
    elif bias == "bearish" and swing_highs:
        # Look for sweep of a swing high (price wicked above, closed back down)
        last_swing_high = swing_highs[-1]["price"]
        for i, candle in enumerate(recent):
            if candle["high"] > last_swing_high and candle["close"] < last_swing_high:
                return {
                    "type": "sweep_high",
                    "level": last_swing_high,
                    "sweep_candle_idx": len(candles_1h) - 5 + i,
                    "extreme": candle["high"],
                    "direction": "short"
                }
    
    return None

def detect_bos_choch(candles_15m, sweep, bias):
    """
    Detect Break of Structure (BOS) or Change of Character (ChoCH) after sweep.
    Returns 'bos', 'choch', or None.
    """
    if len(candles_15m) < 10: return None
    
    recent = candles_15m[-10:]
    highs = [c["high"] for c in recent]
    lows = [c["low"] for c in recent]
    
    if sweep["direction"] == "long":
        # After sweeping low, look for break above recent high (BOS bullish)
        recent_high = max(highs[:-3])  # High before the last 3 candles
        if recent[-1]["close"] > recent_high:
            return "bos" if bias == "bullish" else "choch"
    
    elif sweep["direction"] == "short":
        # After sweeping high, look for break below recent low (BOS bearish)
        recent_low = min(lows[:-3])  # Low before the last 3 candles
        if recent[-1]["close"] < recent_low:
            return "bos" if bias == "bearish" else "choch"
    
    return None

def find_poi(candles_15m, direction):
    """
    Find Point of Interest (FVG, OB, or EQ).
    Priority: FVG > OB > EQ
    """
    if len(candles_15m) < 5: return None
    
    recent = candles_15m[-5:]
    
    # 1. Fair Value Gap (FVG)
    # Bullish FVG: candle[i-2].high < candle[i].low (gap between 1st and 3rd candle)
    # Bearish FVG: candle[i-2].low > candle[i].high
    for i in range(2, len(recent)):
        if direction == "long":
            # Bullish FVG: gap up
            if recent[i-2]["high"] < recent[i]["low"]:
                fvg_top = recent[i]["low"]
                fvg_bottom = recent[i-2]["high"]
                return {"type": "FVG", "level": (fvg_top + fvg_bottom) / 2, "top": fvg_top, "bottom": fvg_bottom}
        else:
            # Bearish FVG: gap down
            if recent[i-2]["low"] > recent[i]["high"]:
                fvg_top = recent[i-2]["low"]
                fvg_bottom = recent[i]["high"]
                return {"type": "FVG", "level": (fvg_top + fvg_bottom) / 2, "top": fvg_top, "bottom": fvg_bottom}
    
    # 2. Order Block (OB) - last opposing candle before strong move
    if direction == "long":
        # Bullish OB: last bearish candle before bullish move
        for i in range(len(recent)-2, -1, -1):
            if recent[i]["close"] < recent[i]["open"]:  # Bearish candle
                return {"type": "OB", "level": recent[i]["low"], "top": recent[i]["high"], "bottom": recent[i]["low"]}
    else:
        # Bearish OB: last bullish candle before bearish move
        for i in range(len(recent)-2, -1, -1):
            if recent[i]["close"] > recent[i]["open"]:  # Bullish candle
                return {"type": "OB", "level": recent[i]["high"], "top": recent[i]["high"], "bottom": recent[i]["low"]}
    
    # 3. Equilibrium (EQ) - 50% retrace of recent range
    range_high = max(c["high"] for c in recent)
    range_low = min(c["low"] for c in recent)
    eq_level = (range_high + range_low) / 2
    return {"type": "EQ", "level": eq_level, "top": range_high, "bottom": range_low}

def is_confirmation_candle(candle, direction):
    """
    Check if candle is a valid confirmation candle.
    Body must be ≥60% of total range and close in trade direction.
    """
    total_range = candle["high"] - candle["low"]
    if total_range == 0: return False
    
    body = abs(candle["close"] - candle["open"])
    body_ratio = body / total_range
    
    if body_ratio < MIN_BODY_RATIO:
        return False
    
    if direction == "long" and candle["close"] > candle["open"]:
        return True
    elif direction == "short" and candle["close"] < candle["open"]:
        return True
    
    return False

def find_unmitigated_liquidity(candles_1h, direction, lookback=50):
    """
    Find the next unmitigated swing high (for longs) or swing low (for shorts).
    'Unmitigated' means price hasn't returned to that level since it was created.
    """
    if len(candles_1h) < lookback: return None
    
    recent = candles_1h[-lookback:]
    current_price = candles_1h[-1]["close"]
    
    if direction == "long":
        # Find most recent swing high above current price that hasn't been retested
        swing_highs, _ = find_swings(recent, lookback)
        for sh in reversed(swing_highs):
            if sh["price"] > current_price:
                # Check if price has returned to this level since the swing
                mitigated = False
                for candle in recent[sh["idx"]+1:]:
                    if candle["low"] <= sh["price"]:
                        mitigated = True
                        break
                if not mitigated:
                    return sh["price"]
    
    elif direction == "short":
        # Find most recent swing low below current price that hasn't been retested
        _, swing_lows = find_swings(recent, lookback)
        for sl in reversed(swing_lows):
            if sl["price"] < current_price:
                # Check if price has returned to this level since the swing
                mitigated = False
                for candle in recent[sl["idx"]+1:]:
                    if candle["high"] >= sl["price"]:
                        mitigated = True
                        break
                if not mitigated:
                    return sl["price"]
    
    return None

def check_open_trade(state, price):
    trade = state.get("open_trade")
    if not trade: return
    
    last_check_str = trade.get("last_check")
    if last_check_str:
        try:
            last_check = datetime.fromisoformat(last_check_str)
            if (datetime.now() - last_check).total_seconds() < 120:
                return
        except Exception: pass
    
    trade["last_check"] = datetime.now().isoformat()
    save_state(state)
    
    direction, entry, tp, sl = trade["direction"], trade["entry"], trade["tp"], trade["sl"]
    
    # Stale trade detection
    trade_time_str = trade.get("time", "")
    if trade_time_str:
        try:
            trade_time = datetime.strptime(trade_time_str, "%Y-%m-%d %H:%M UTC")
            hours_open = (datetime.utcnow() - trade_time).total_seconds() / 3600
            if hours_open > STALE_HOURS:
                qty = POSITION_USD / entry
                pnl = ((entry - price) if direction == "short" else (price - entry)) * qty
                notify("TJR BOT — Trade STALE", f"Exceeded {STALE_HOURS}h limit\nPnL: ${pnl:+.0f}", priority="high")
                log_trade(trade, "stale", price, pnl)
                state["open_trade"] = None
                save_state(state)
                return
        except Exception: pass
    
    qty = POSITION_USD / entry
    result, exit_price = None, None
    
    if direction == "short":
        if price <= tp: result, exit_price = "won", tp
        elif price >= sl: result, exit_price = "lost", sl
    else:
        if price >= tp: result, exit_price = "won", tp
        elif price <= sl: result, exit_price = "lost", sl
    
    if not result:
        print(f"  Open {direction.upper()} | Entry: ${entry:,.0f} | TP: ${tp:,.0f} | SL: ${sl:,.0f} | Now: ${price:,.0f}")
        return
    
    pnl = ((entry - exit_price) if direction == "short" else (exit_price - entry)) * qty
    
    if result == "won":
        notify("TJR BOT — Trade WON", f"PnL: ${pnl:+.0f} | Exit: ${exit_price:,.0f}", priority="default")
        log_trade(trade, "won", exit_price, pnl)
    else:
        loss_report = "\n".join([
            "TJR BOT — TRADE LOSS REPORT",
            f"Direction: {direction.upper()}",
            f"Entry: ${entry:,.0f} | TP: ${tp:,.0f} | SL: ${sl:,.0f}",
            f"Exit: ${exit_price:,.0f}",
            f"PnL: ${pnl:+.0f}",
            f"POI Type: {trade.get('poi_type', 'N/A')}",
            f"Sweep Level: ${trade.get('sweep_level', 0):,.0f}",
            "Paste to Claude to diagnose."
        ])
        notify("TJR BOT — Trade LOST", loss_report, priority="high")
        log_trade(trade, "lost", exit_price, pnl)
    
    state["open_trade"] = None
    save_state(state)

def main():
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now_str}] TJR Scanner starting...")
    
    state = load_state()
    
    # ── TIME GATE: Only scan during NY Killzone ──
    if not is_killzone():
        print("  Outside NY Killzone (8:30-11:00 AM EST). Skipping scan.")
        return
    
    print("  ✓ Inside NY Killzone")
    
    # ── FETCH DATA ──
    try:
        candles_4h = get_kraken_candles(TIMEFRAMES["4h"])
        candles_1h = get_kraken_candles(TIMEFRAMES["1h"])
        candles_15m = get_kraken_candles(TIMEFRAMES["15m"])
        price = candles_15m[-1]["close"]
        print(f"  [4H] {len(candles_4h)} candles | [1H] {len(candles_1h)} candles | [15m] {len(candles_15m)} candles | Price: ${price:,.0f}")
    except Exception as e:
        print(f"  FAILED to fetch data: {e}")
        return
    
    # ── CHECK OPEN TRADE ──
    check_open_trade(state, price)
    
    # ── DON'T OPEN NEW TRADE IF ONE EXISTS ──
    if state.get("open_trade"):
        print("  Trade already open. Skipping new signals.")
        return
    
    # ── STEP 1: HTF BIAS ──
    bias = detect_htf_bias(candles_4h, candles_1h)
    if not bias:
        print("  No clear HTF bias. Skipping.")
        return
    print(f"  HTF Bias: {bias.upper()}")
    
    # ── STEP 2: LIQUIDITY SWEEP ──
    sweep = detect_liquidity_sweep(candles_1h, bias)
    if not sweep:
        print("  No liquidity sweep detected. Skipping.")
        return
    print(f"  Sweep detected: {sweep['type']} at ${sweep['level']:,.0f}")
    
    # ── STEP 3: BOS/CHOCH ──
    structure = detect_bos_choch(candles_15m, sweep, bias)
    if not structure:
        print("  No BOS/ChoCH confirmed. Skipping.")
        return
    print(f"  Structure: {structure.upper()}")
    
    # ── STEP 4: POI ──
    direction = sweep["direction"]
    poi = find_poi(candles_15m, direction)
    if not poi:
        print("  No POI found. Skipping.")
        return
    print(f"  POI: {poi['type']} at ${poi['level']:,.0f}")
    
    # ── STEP 5: CONFIRMATION CANDLE ──
    last_candle = candles_15m[-1]
    if not is_confirmation_candle(last_candle, direction):
        print("  No confirmation candle. Skipping.")
        return
    print(f"  ✓ Confirmation candle valid")
    
    # ── STEP 6: ENTRY/SL/TP ──
    entry = price
    sl = sweep["extreme"]  # SL beyond sweep extreme
    
    # TP at next unmitigated liquidity
    tp = find_unmitigated_liquidity(candles_1h, direction)
    if not tp:
        print("  No unmitigated liquidity target found. Skipping.")
        return
    
    # Validate TP direction
    if direction == "long" and tp <= entry:
        print(f"  TP ${tp:,.0f} below entry ${entry:,.0f}. Invalid. Skipping.")
        return
    elif direction == "short" and tp >= entry:
        print(f"  TP ${tp:,.0f} above entry ${entry:,.0f}. Invalid. Skipping.")
        return
    
    print(f"  Setup ready: {direction.upper()} | Entry: ${entry:,.0f} | TP: ${tp:,.0f} | SL: ${sl:,.0f}")
    
    # ── DEDUP CHECK ──
    key = f"{direction}_tjr"
    last_str = state["last_signal"].get(key)
    if last_str:
        try:
            last = datetime.fromisoformat(last_str)
            if (datetime.now() - last).total_seconds() < 300:
                print(f"  Duplicate suppressed ({int((datetime.now() - last).total_seconds())}s since last)")
                return
        except Exception: pass
    
    # ── SEND NOTIFICATION ──
    msg = "\n".join([
        f"TJR BOT — {direction.upper()} SIGNAL",
        f"Entry: ${entry:,.0f}",
        f"Target: ${tp:,.0f} | Stop: ${sl:,.0f}",
        f"HTF Bias: {bias.upper()}",
        f"Sweep: {sweep['type']} at ${sweep['level']:,.0f}",
        f"Structure: {structure.upper()}",
        f"POI: {poi['type']} at ${poi['level']:,.0f}",
        f"Confirmation: {MIN_BODY_RATIO*100:.0f}% body candle"
    ])
    notify(f"TJR BOT — {direction.upper()} SIGNAL", msg, priority="urgent")
    
    # ── OPEN TRADE ──
    trade = {
        "time": now_str,
        "signal": f"TJR {direction.upper()} — ICT Setup",
        "timeframe": "15m",
        "direction": direction,
        "entry": entry,
        "tp": tp,
        "sl": sl,
        "poi_type": poi["type"],
        "sweep_level": sweep["level"],
        "bias": bias,
        "structure": structure
    }
    state["open_trade"] = trade
    state["last_signal"][key] = datetime.now().isoformat()
    save_state(state)
    print(f"  Trade opened: {direction.upper()} | Entry: ${entry:,.0f}")

if __name__ == "__main__":
    main()
