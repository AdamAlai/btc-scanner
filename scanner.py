import requests
import json
import os
import sys
from datetime import datetime

# ── CONFIG ────────────────────────────────────────────────────────────
NTFY_TOPIC    = os.environ.get("NTFY_TOPIC", "btcwave554433")
STATE_FILE    = "scanner_state.json"
TRADE_LOG_FILE = "trade_log.json"
POSITION_USD  = 50000
PEAK_WINDOW   = 3
MOMENTUM_BARS = 2
STALE_HOURS   = 6
MIN_STOP_DIST = 100  # Minimum $ distance between entry and stop loss
MIN_TP_DIST   = 250  # Minimum $ distance to Take Profit
MAX_TP_DIST   = 400  # Maximum $ distance to Take Profit

TIMEFRAMES = [
    {"label": "1m",  "interval": 1,  "gap": 200, "size": 400, "recent": 15, "vol_confirm": True},
    {"label": "5m",  "interval": 5,  "gap": 300, "size": 600, "recent": 15, "vol_confirm": True},
    {"label": "15m", "interval": 15, "gap": 400, "size": 800, "recent": 15, "vol_confirm": True},
    {"label": "60m", "interval": 60, "gap": 800, "size": 1500, "recent": 10, "vol_confirm": True},
]
# ─────────────────────────────────────────────────────────────────────

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

def load_trade_log():
    if os.path.exists(TRADE_LOG_FILE):
        try:
            with open(TRADE_LOG_FILE) as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []

def log_trade(trade, result, exit_price, pnl):
    log = load_trade_log()
    log.append({
        "open_time":  trade.get("time", ""),
        "close_time": datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC"),
        "bot":        "ATOM",
        "signal":     trade.get("signal", ""),
        "timeframe":  trade.get("timeframe", ""),
        "direction":  trade.get("direction", ""),
        "entry":      trade.get("entry", 0),
        "tp":         trade.get("tp", 0),
        "sl":         trade.get("sl", 0),
        "exit":       exit_price,
        "pnl":        round(pnl, 2),
        "result":     result,
    })
    with open(TRADE_LOG_FILE, "w") as f:
        json.dump(log, f, indent=2)

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
    r = requests.get(
        "https://api.kraken.com/0/public/OHLC",
        params={"pair": "XBTUSD", "interval": interval},
        timeout=15
    )
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

def ema(candles, period=20):
    if len(candles) < period:
        return None
    k = 2 / (period + 1)
    val = sum(c["close"] for c in candles[:period]) / period
    for c in candles[period:]:
        val = c["close"] * k + val * (1 - k)
    return val

def ts(candle):
    if "time" in candle:
        return datetime.utcfromtimestamp(candle["time"]).strftime("%H:%M")
    return "??"

def get_market_context(candles_dict, price):
    ctx = {}
    c1m = candles_dict.get('1m', [])
    c15m = candles_dict.get('15m', [])
    c60m = candles_dict.get('60m', [])

    # 1-Hour Range (High/Low of the current 1h candle)
    if c60m:
        ctx['1h_high'] = c60m[-1]['high']
        ctx['1h_low'] = c60m[-1]['low']

    # 15m Trend (Price vs EMA50)
    if len(c15m) >= 50:
        ema50 = sum(c['close'] for c in c15m[-50:]) / 50
        ctx['15m_trend'] = "BULLISH" if price > ema50 else "BEARISH"
        ctx['15m_ema50'] = round(ema50)

    # 1m Volatility (Average candle move over last 10 candles)
    if len(c1m) >= 10:
        avg_move = sum(abs(c['close'] - c['open']) for c in c1m[-10:]) / 10
        ctx['1m_volatility'] = f"${avg_move:.0f}/candle"

    return ctx

def check_open_trade(state, price, candles_1m=None):
    trade = state.get("open_trade")
    if not trade:
        return

    # ── TIMESTAMP GUARD: prevent duplicate processing across simultaneous workflows ──
    last_check_str = trade.get("last_check")
    if last_check_str:
        try:
            last_check = datetime.fromisoformat(last_check_str)
            elapsed_sec = (datetime.now() - last_check).total_seconds()
            if elapsed_sec < 120:
                print(f"  Trade checked {int(elapsed_sec)}s ago — skipping")
                return
        except Exception:
            pass

    trade["last_check"] = datetime.now().isoformat()
    save_state(state)

    direction = trade["direction"]
    entry     = trade["entry"]
    tp        = trade["tp"]
    sl        = trade["sl"]

    # ── STALE TRADE DETECTION ─────────────────────────────────────────────────
    trade_time_str = trade.get("time", "")
    time_open_mins = 0
    if trade_time_str:
        try:
            trade_time = datetime.strptime(trade_time_str, "%Y-%m-%d %H:%M UTC")
            hours_open = (datetime.utcnow() - trade_time).total_seconds() / 3600
            time_open_mins = hours_open * 60
            if hours_open > STALE_HOURS:
                qty = POSITION_USD / entry
                pnl = ((entry - price) if direction == "short" else (price - entry)) * qty
                msg = "\n".join([
                    "STALE TRADE FORCE-CLOSED",
                    f"Trade was open {hours_open:.1f} hours — exceeded {STALE_HOURS}h limit",
                    f"Direction: {direction.upper()}",
                    f"Entry: ${entry:,.0f} | Exit: ${price:,.0f}",
                    f"PnL: ${pnl:+.0f} (qty {qty:.4f} BTC)",
                    "The bot missed TP/SL due to scan gaps. Consider a VPS for real-time tracking."
                ])
                notify("Trade STALE — Force Closed", msg, priority="high")
                print(f"  STALE TRADE closed after {hours_open:.1f}h | PnL: ${pnl:+.0f}")
                log_trade(trade, "stale", price, pnl)
                state["open_trade"] = None
                save_state(state)
                return
        except Exception:
            pass

    if trade.get("is_spike"):
        state["open_trade"] = None
        save_state(state)
        return

    qty = POSITION_USD / entry
    result = None
    exit_price = None

    if candles_1m and len(candles_1m) >= 3:
        recent = candles_1m[-3:]
        period_high = max(c["high"] for c in recent)
        period_low  = min(c["low"]  for c in recent)
    else:
        period_high = price
        period_low  = price

    if direction == "short":
        if period_low <= tp:
            result, exit_price = "won", tp
        elif period_high >= sl:
            result, exit_price = "lost", sl
    else:
        if period_high >= tp:
            result, exit_price = "won", tp
        elif period_low <= sl:
            result, exit_price = "lost", sl

    if not result:
        print(f"  Open {direction.upper()} | Entry: ${entry:,.0f} | TP: ${tp:,.0f} | SL: ${sl:,.0f} | Now: ${price:,.0f} | Range: ${period_low:,.0f}-${period_high:,.0f}")
        return

    pnl = ((entry - exit_price) if direction == "short" else (exit_price - entry)) * qty

    if result == "won":
        msg = f"PnL: ${pnl:+.0f} | Exit: ${exit_price:,.0f}\n{direction.upper()} | Entry: ${entry:,.0f} | TP: ${tp:,.0f}"
        notify("Trade WON", msg, priority="default")
        print(f"  Trade WON | PnL: ${pnl:+.0f}")
        log_trade(trade, "won", exit_price, pnl)
        state["last_signal"][f"{direction}_{trade.get('timeframe','')}"] = datetime.now().isoformat()
    else:
        move = abs(exit_price - entry)
        ema_str = f"${trade.get('ema20'):,.0f}" if trade.get('ema20') else "N/A"
        struct_str = "N/A"
        if trade.get("p1") and trade.get("p2"):
            struct_str = f"P1: ${trade.get('p1'):,.0f} | P2: ${trade.get('p2'):,.0f} | Trough: ${trade.get('trough'):,.0f}"
        elif trade.get("l1") and trade.get("l2"):
            struct_str = f"L1: ${trade.get('l1'):,.0f} | L2: ${trade.get('l2'):,.0f} | PeakB: ${trade.get('peak_b'):,.0f}"

        ctx = trade.get("context", {})
        ctx_str = f"15m Trend: {ctx.get('15m_trend', 'N/A')} | 1m Vol: {ctx.get('1m_volatility', 'N/A')}"

        loss_report = "\n".join([
            "TRADE LOSS REPORT",
            f"Signal: {trade.get('signal', 'N/A')}",
            f"Timeframe: {trade.get('timeframe', 'N/A')}",
            f"Direction: {direction.upper()}",
            f"Time Open: {time_open_mins:.0f} mins",
            f"Entry: ${entry:,.0f}",
            f"Target: ${tp:,.0f} (needed ${abs(tp-entry):,.0f} {'up' if direction=='long' else 'down'})",
            f"Stop: ${sl:,.0f} (risked ${abs(sl-entry):,.0f})",
            f"Exit: ${exit_price:,.0f} (moved ${move:,.0f} against you)",
            f"PnL: ${pnl:+.0f} (qty {qty:.4f} BTC)",
            f"EMA20 at Entry: {ema_str}",
            f"Structure: {struct_str}",
            f"Live Context: {ctx_str}",
            "Paste to Claude to diagnose."
        ])
        notify("Trade LOST", loss_report, priority="high")
        print(f"  Trade LOST | PnL: ${pnl:+.0f}")
        print(loss_report)
        log_trade(trade, "lost", exit_price, pnl)

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
    e20      = ema(candles, 20)

    # ── SHORT (double peak) ───────────────────────────────────────────────────
    if len(peaks) >= 2:
        for i in range(len(peaks) - 1):
            idx1, p1, v1 = peaks[i]
            idx2, p2, v2 = peaks[i + 1]
            if p2 - p1 < min_gap: continue
            trough = min(c["low"] for c in candles[idx1:idx2+1])
            if p2 - trough < min_size: continue
            if vol_confirm:
                avg_vol = sum(c["volume"] for c in candles[max(0,idx2-20):idx2]) / 20
                if v2 < avg_vol * 0.8: continue
            if not momentum_down(candles, idx2): continue
            if last_idx - idx2 > recent: continue
            if price >= p2: continue
            if p1 >= price: continue
            if p2 <= price: continue
            if price - p1 < 50: continue
            if abs(price - p2) < MIN_STOP_DIST: continue
            
            # TP CONSTRAINT: $250 - $400 distance
            tp_dist = abs(price - p1)
            if not (MIN_TP_DIST <= tp_dist <= MAX_TP_DIST): continue
            
            if e20 and price > e20 * 1.002: continue
            msg = "\n".join([
                f"Entry: ~${price:,.0f}",
                f"Target: ${p1:,.0f} | Stop: ${p2:,.0f}",
                f"2nd peak ${p2:,.0f} broke 1st ${p1:,.0f} (+${p2-p1:,.0f})",
                f"Wave: ${p2-trough:,.0f} tall",
            ])
            alerts.append({
                "direction": "short",
                "signal":    "SHORT SETUP - Wave Strategy",
                "title":     f"SELL SIGNAL [{label}]",
                "msg":       msg,
                "entry": price, "tp": p1, "sl": p2,
                "priority": "urgent", "timeframe": label,
                "p1": p1, "p2": p2, "trough": trough, "ema20": e20
            })

    # ── LONG (double low) ─────────────────────────────────────────────────────
    if len(lows) >= 2:
        for i in range(len(lows) - 1):
            idx1, l1, v1 = lows[i]
            idx2, l2, v2 = lows[i + 1]
            if l1 - l2 < min_gap: continue
            peak_b = max(c["high"] for c in candles[idx1:idx2+1])
            if peak_b - l2 < min_size: continue
            if vol_confirm:
                avg_vol = sum(c["volume"] for c in candles[max(0,idx2-20):idx2]) / 20
                if v2 < avg_vol * 0.8: continue
            if not momentum_up(candles, idx2): continue
            if last_idx - idx2 > recent: continue
            if price <= l2: continue
            if peak_b <= price: continue
            if l2 >= price: continue
            if peak_b - price < 50: continue
            if abs(price - l2) < MIN_STOP_DIST: continue

            # TP CONSTRAINT: $250 - $400 distance
            tp_dist = abs(peak_b - price)
            if not (MIN_TP_DIST <= tp_dist <= MAX_TP_DIST): continue

            if e20 and price < e20 * 0.998: continue
            msg = "\n".join([
                f"Entry: ~${price:,.0f}",
                f"Target: ${peak_b:,.0f} | Stop: ${l2:,.0f}",
                f"2nd low ${l2:,.0f} broke 1st ${l1:,.0f} (-${l1-l2:,.0f})",
                f"Wave: ${peak_b-l2:,.0f} tall",
            ])
            alerts.append({
                "direction": "long",
                "signal":    "LONG SETUP - Wave Strategy",
                "title":     f"BUY SIGNAL [{label}]",
                "msg":       msg,
                "entry": price, "tp": peak_b, "sl": l2,
                "priority": "urgent", "timeframe": label,
                "l1": l1, "l2": l2, "peak_b": peak_b, "ema20": e20
            })

    # ── SUPPORT BREAK SHORT ───────────────────────────────────────────────────
    if len(lows) >= 1:
        for idx1, l1, v1 in lows:
            if last_idx - idx1 > recent * 2: continue
            post_candles = candles[idx1:]
            if len(post_candles) < 4: continue
            peak_after = max(c["high"] for c in post_candles)
            if peak_after - l1 < min_gap: continue
            if price >= l1: continue
            if not momentum_down(candles, last_idx - 1): continue
            if vol_confirm:
                avg_vol = sum(c["volume"] for c in candles[max(0,last_idx-20):last_idx]) / 20
                if candles[last_idx]["volume"] < avg_vol * 0.8: continue
            drop = l1 - price
            tp   = price - drop * 1.5
            sl   = l1 + drop * 0.5
            if tp >= price: continue
            if sl <= price: continue
            if abs(price - sl) < MIN_STOP_DIST: continue
            
            # TP CONSTRAINT: $250 - $400 distance
            tp_dist = abs(price - tp)
            if not (MIN_TP_DIST <= tp_dist <= MAX_TP_DIST): continue

            msg = "\n".join([
                f"Entry: ~${price:,.0f}",
                f"Target: ${tp:,.0f} | Stop: ${sl:,.0f}",
                f"Broke support at ${l1:,.0f}",
                f"Bounce was ${peak_after-l1:,.0f} before breakdown",
            ])
            alerts.append({
                "direction": "short",
                "signal":    "SUPPORT BREAK - Wave Strategy",
                "title":     f"BREAKDOWN SELL [{label}]",
                "msg":       msg,
                "entry": price, "tp": tp, "sl": sl,
                "priority": "urgent", "timeframe": label,
                "l1": l1, "peak_after": peak_after, "ema20": e20
            })
            break

    # ── SPIKE DETECTOR ───────────────────────────────────────────────────────
    if label in ("1m", "5m") and len(candles) >= 22:
        last = candles[-1]
        candle_move = abs(last["close"] - last["open"])
        avg_move = sum(abs(c["close"] - c["open"]) for c in candles[-21:-1]) / 20
        spike_ratio = candle_move / avg_move if avg_move > 0 else 0
        if spike_ratio >= 3.0 and candle_move > min_size * 0.3:
            direction_word = "UP" if last["close"] > last["open"] else "DOWN"
            spike_kind = "long" if last["close"] > last["open"] else "short"
            msg = "\n".join([
                "Unusual candle detected!",
                f"Move: ${candle_move:,.0f} ({spike_ratio:.1f}x normal)",
                f"Price: ${price:,.0f}",
                "Watch for follow-through or reversal",
            ])
            alerts.append({
                "direction":  spike_kind,
                "signal":     f"SPIKE {direction_word} - Unusual Move",
                "title":      f"SPIKE {direction_word} [{label}]",
                "msg":        msg,
                "entry": price, "tp": price, "sl": price,
                "priority":   "high", "timeframe": label,
                "is_spike":   True, "ema20": e20
            })

    # ── MOMENTUM SURGE DETECTOR ───────────────────────────────────────────────
    surge_window = {"1m": 10, "5m": 6, "15m": 4}.get(label, 0)
    surge_threshold = {"1m": 300, "5m": 500, "15m": 800}.get(label, 9999)

    if surge_window and len(candles) >= surge_window + 20:
        window_candles = candles[-surge_window:]
        surge_open  = window_candles[0]["open"]
        surge_close = window_candles[-1]["close"]
        surge_move  = surge_close - surge_open

        historical = []
        for j in range(1, 21):
            start = -(surge_window + j)
            end   = -j
            chunk = candles[start:end]
            if len(chunk) == surge_window:
                historical.append(abs(chunk[-1]["close"] - chunk[0]["open"]))
        avg_surge = sum(historical) / len(historical) if historical else 0

        surge_abs = abs(surge_move)
        surge_mult = surge_abs / avg_surge if avg_surge > 0 else 0

        if surge_abs >= surge_threshold and surge_mult >= 2.5:
            direction_word = "UP" if surge_move > 0 else "DOWN"
            surge_kind = "long" if surge_move > 0 else "short"
            msg = "\n".join([
                f"BIG MOVE {direction_word} DETECTED",
                f"${surge_abs:,.0f} move in last {surge_window} candles ({surge_mult:.1f}x normal)",
                f"From ${surge_open:,.0f} to ${surge_close:,.0f}",
                f"Price now: ${price:,.0f}",
                "Watch for continuation or reversal",
            ])
            alerts.append({
                "direction":  surge_kind,
                "signal":     f"SURGE {direction_word} - Momentum",
                "title":      f"SURGE {direction_word} [{label}]",
                "msg":        msg,
                "entry": price, "tp": price, "sl": price,
                "priority":   "urgent", "timeframe": label,
                "is_spike":   True, "ema20": e20
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
        gap  = p2 - p1
        size = p2 - trough
        age  = last_idx - idx2
        mom  = momentum_down(candles, idx2)
        avg_vol = sum(c["volume"] for c in candles[max(0,idx2-20):idx2]) / 20
        vol_ok = v2 >= avg_vol * 0.8 if vol_confirm else True
        entry_ok = price < p2 and p1 < price and p2 > price
        stop_ok = abs(price - p2) >= MIN_STOP_DIST
        tp_dist = abs(price - p1)
        tp_ok = MIN_TP_DIST <= tp_dist <= MAX_TP_DIST
        
        print(f"\n     {ts(candles[idx1])} ${p1:,.0f} -> {ts(candles[idx2])} ${p2:,.0f}")
        print(f"      GAP   ${gap:,.0f} (>=${min_gap}) {'OK' if gap>=min_gap else 'FAIL'}")
        print(f"      SIZE  ${size:,.0f} (>=${min_size}) {'OK' if size>=min_size else 'FAIL'}")
        print(f"      VOL   v2={v2:.2f} avg={avg_vol:.2f} {'OK' if vol_ok else 'FAIL'}")
        print(f"      MOM   {'OK' if mom else 'FAIL'}  AGE {age} (<={recent}) {'OK' if age<=recent else 'FAIL'}")
        print(f"      STOP  ${abs(price-p2):,.0f} (>=$100) {'OK' if stop_ok else 'FAIL'}")
        print(f"      TP    ${tp_dist:,.0f} ($250-$400) {'OK' if tp_ok else 'FAIL'}")
        print(f"      ENTRY price=${price:,.0f} < p2=${p2:,.0f} and p1=${p1:,.0f} < price: {'OK' if entry_ok else 'FAIL'}")
        if gap>=min_gap and size>=min_size and vol_ok and mom and age<=recent and entry_ok and stop_ok and tp_ok:
            print(f"      >>> WOULD FIRE SELL at ${price:,.0f} TP=${p1:,.0f} SL=${p2:,.0f}")
        else:
            print(f"      >>> NO SIGNAL")

    print(f"\n  LONG checks:")
    for i in range(len(lows) - 1):
        idx1, l1, v1 = lows[i]
        idx2, l2, v2 = lows[i + 1]
        peak_b = max(c["high"] for c in candles[idx1:idx2+1])
        gap  = l1 - l2
        size = peak_b - l2
        age  = last_idx - idx2
        mom  = momentum_up(candles, idx2)
        avg_vol = sum(c["volume"] for c in candles[max(0,idx2-20):idx2]) / 20
        vol_ok = v2 >= avg_vol * 0.8 if vol_confirm else True
        entry_ok = price > l2 and peak_b > price and l2 < price
        stop_ok = abs(price - l2) >= MIN_STOP_DIST
        tp_dist = abs(peak_b - price)
        tp_ok = MIN_TP_DIST <= tp_dist <= MAX_TP_DIST
        
        print(f"\n    {ts(candles[idx1])} ${l1:,.0f} -> {ts(candles[idx2])} ${l2:,.0f}")
        print(f"      GAP   ${gap:,.0f} (>=${min_gap}) {'OK' if gap>=min_gap else 'FAIL'}")
        print(f"      SIZE  ${size:,.0f} (>=${min_size}) {'OK' if size>=min_size else 'FAIL'}")
        print(f"      VOL   v2={v2:.2f} avg={avg_vol:.2f} {'OK' if vol_ok else 'FAIL'}")
        print(f"      MOM   {'OK' if mom else 'FAIL'}  AGE {age} (<={recent}) {'OK' if age<=recent else 'FAIL'}")
        print(f"      STOP  ${abs(price-l2):,.0f} (>=$100) {'OK' if stop_ok else 'FAIL'}")
        print(f"      TP    ${tp_dist:,.0f} ($250-$400) {'OK' if tp_ok else 'FAIL'}")
        print(f"      ENTRY price=${price:,.0f} > l2=${l2:,.0f} and peak_b=${peak_b:,.0f} > price: {'OK' if entry_ok else 'FAIL'}")
        if gap>=min_gap and size>=min_size and vol_ok and mom and age<=recent and entry_ok and stop_ok and tp_ok:
            print(f"      >>> WOULD FIRE BUY at ${price:,.0f} TP=${peak_b:,.0f} SL=${l2:,.0f}")
        else:
            print(f"      >>> NO SIGNAL")

def main():
    now_str = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    print(f"[{now_str}] BTC Wave Scanner starting...")

    state = load_state()

    tf_candles = []
    master_price = None
    candles_dict = {}
    
    for tf in TIMEFRAMES:
        try:
            candles = get_kraken_candles(tf["interval"])
            tf_candles.append((tf, candles))
            candles_dict[tf['label']] = candles
            master_price = candles[-1]["close"]
            print(f"  [{tf['label']}] {len(candles)} candles | Price: ${master_price:,.0f}")
        except Exception as e:
            print(f"  [{tf['label']}] FAILED: {e}")
            continue

    if not tf_candles:
        print("  ERROR: No candle data acquired. Exiting.")
        return

    check_open_trade(state, master_price, candles_dict.get('1m'))

    if DEBUG:
        for tf, candles in tf_candles:
            debug_timeframe(candles, tf)
        return

    all_alerts = []
    for tf, candles in tf_candles:
        alerts = scan_timeframe(candles, tf)
        all_alerts.extend(alerts)

    if not all_alerts:
        print("  No signals detected.")
        return

    # ── HIGHER TIMEFRAME BIAS FILTER ──────────────────────────────────────────
    htf_bias = None
    for alert in all_alerts:
        if alert["timeframe"] in ("15m", "60m"):
            htf_bias = alert["direction"]
            print(f"  HTF Bias set to {htf_bias.upper()} based on {alert['timeframe']} signal")
            break

    context = get_market_context(candles_dict, master_price)

    for alert in all_alerts:
        direction = alert["direction"]
        label = alert["timeframe"]

        # Block lower timeframe signals that fight the higher timeframe bias
        if label in ("1m", "5m") and htf_bias is not None and direction != htf_bias:
            print(f"  [{label}] {direction.upper()}: Blocked by HTF Bias ({htf_bias.upper()})")
            continue

        # ── RACE CONDITION DEDUP ──────────────────────────────────────────────
        key = f"{direction}_{label}"
        last_str = state["last_signal"].get(key)
        if last_str:
            try:
                last = datetime.fromisoformat(last_str)
                elapsed = (datetime.now() - last).total_seconds()
                if elapsed < 300:  # 5 minutes
                    print(f"  [{label}] {direction.upper()}: duplicate suppressed ({int(elapsed)}s since last)")
                    continue
            except Exception:
                pass

        # Append context to Ntfy message
        ctx_msg = f"\n15m Trend: {context.get('15m_trend', 'N/A')} | 1m Vol: {context.get('1m_volatility', 'N/A')}"
        notify(alert["title"], alert["msg"] + ctx_msg, priority=alert["priority"])
        
        trade = {
            "time": now_str,
            "signal": alert["signal"],
            "timeframe": label,
            "direction": direction,
            "entry": alert["entry"],
            "tp": alert["tp"],
            "sl": alert["sl"],
            "is_spike": alert.get("is_spike", False),
            "context": context,
            "p1": alert.get("p1"),
            "p2": alert.get("p2"),
            "trough": alert.get("trough"),
            "l1": alert.get("l1"),
            "l2": alert.get("l2"),
            "peak_b": alert.get("peak_b"),
            "ema20": alert.get("ema20")
        }
        state["open_trade"] = trade
        state["last_signal"][key] = datetime.now().isoformat()
        save_state(state)
        print(f"  [{label}] {direction.upper()}: trade opened | Entry: ${alert['entry']:,.0f}")

        # STOP AFTER OPENING ONE TRADE TO PREVENT OVERWRITING
        break

if __name__ == "__main__":
    main()
