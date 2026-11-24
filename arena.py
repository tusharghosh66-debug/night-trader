"""
Strategy Arena — 13 autonomous PAPER-trading bots, one per proven strategy.
Each bot starts with Rs 10,000 (total Rs 1,30,000). No real money, no keys.
Data: Yahoo v8 chart API (works from India + GitHub runners, no keys).
All P&L in INR (USD symbols converted via live USDINR). 0.1% fees per side.

The 10 bots (from the data-backed top-10 research):
  s01  St. Gallen Opening-Range Noise-Band Breakout   (30m, NQ=F, long+short)
  s02  Trend-Filtered Mean Reversion 200SMA+BB2.5     (daily, big US stocks)
  s03  Multi-Asset ETF Dual Momentum Rotation         (monthly, SPY/GLD/TLT/DBC)
  s04  Multi-Market Trend Following (Donchian+ATR)    (daily, multi-asset, L+S)
  s05  Larry Connors RSI(2) Pullback                  (daily, index ETFs)
  s06  Previous-Day High/Low Break & Retest           (15m, QQQ+stocks)
  s07  First-Candle ORB 15m Break & Retest            (15m, NY open)
  s08  Pre-Market High/Low Breakout & Retest          (15m, liquid stocks)
  s09  Statistical Arbitrage / Pairs Trading          (daily, cointegrated pairs)
  s10  Overnight Edge + Turnaround Tuesday            (daily, SPY)
  s11  Spike & VWAP Rejection Short                   (5m, top movers, Pine port)
  s12  OHL (Open=High) + CPR Breakdown Short          (5m, top movers, Pine port)
  s13  Afternoon Sell-the-Rip Short, 1h bear regime   (5m, top movers, Pine port)

Modes:  python arena.py once | run | backtest | report | dashboard
"""
import json, csv, time, sys, os, re, math, traceback
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.request import urlopen, Request
from urllib.parse import quote_plus

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_F = os.path.join(BASE, "arena_state.json")
TRADES_F = os.path.join(BASE, "arena_trades.csv")
LOG_F = os.path.join(BASE, "arena.log")
REPORT_F = os.path.join(BASE, "arena_report.md")
DASH_F = os.path.join(BASE, "arena_dashboard.html")

IST = timezone(timedelta(hours=5, minutes=30))
ET = ZoneInfo("America/New_York")
FEE = 0.001            # 0.1% per side
CAPITAL = 10000.0      # per bot
CYCLE_SEC = 5 * 60
VERSION = 1

# ---------------- logging / http ----------------
def log(msg):
    line = f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}] {msg}"
    print(line, flush=True)
    try:
        with open(LOG_F, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass

def http_json(url, timeout=25):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (strategy-arena)"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())

# ---------------- data ----------------
def get_candles(symbol, interval="1d", rng="1y", prepost=False, drop_last=True):
    """OHLC candles oldest first. interval: 1m,5m,15m,30m,60m,1d. drop_last removes open bar."""
    sym = symbol.replace("^", "%5E").replace("&", "%26")
    pp = "&includePrePost=true" if prepost else ""
    d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval={interval}&range={rng}{pp}")
    res = d["chart"]["result"][0]
    ts = res.get("timestamp") or []
    q = res["indicators"]["quote"][0]
    out = []
    for i, t in enumerate(ts):
        c = q["close"][i]
        if c is None:
            continue
        out.append({"t": int(t) * 1000, "o": q["open"][i] or c, "h": q["high"][i] or c,
                    "l": q["low"][i] or c, "c": float(c), "v": float(q["volume"][i] or 0)})
    if drop_last and out:
        out = out[:-1]
    return out

def get_live_price(symbol, fx=1.0):
    sym = symbol.replace("^", "%5E").replace("&", "%26")
    d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d")
    return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"]) * fx

def get_usdinr(st=None):
    try:
        d = http_json("https://query1.finance.yahoo.com/v8/finance/chart/INR=X?interval=1d&range=1d")
        fx = float(d["chart"]["result"][0]["meta"]["regularMarketPrice"])
        if fx > 0:
            if st is not None:
                st["usdinr"] = fx
            return fx
    except Exception as e:
        log(f"USDINR fetch failed: {e}")
    return (st or {}).get("usdinr", 88.0)

def to_inr(kl, fx):
    for x in kl:
        for k in ("o", "h", "l", "c"):
            x[k] *= fx
    return kl

def is_usd(symbol):
    return not symbol.endswith(".NS")

# ---------------- indicators ----------------
def sma(vals, n):
    out = [None] * len(vals)
    s = 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out

def ema(vals, n):
    if len(vals) < n:
        return [None] * len(vals)
    k = 2 / (n + 1)
    out = [None] * (n - 1)
    e = sum(vals[:n]) / n
    out.append(e)
    for v in vals[n:]:
        e = v * k + e * (1 - k)
        out.append(e)
    return out

def rsi(closes, n=14):
    if len(closes) < n + 1:
        return [None] * len(closes)
    out = [None] * n
    gains, losses = [], []
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(d, 0)); losses.append(max(-d, 0))
    ag = sum(gains[:n]) / n; al = sum(losses[:n]) / n
    out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    for i in range(n, len(gains)):
        ag = (ag * (n - 1) + gains[i]) / n
        al = (al * (n - 1) + losses[i]) / n
        out.append(100.0 if al == 0 else 100 - 100 / (1 + ag / al))
    return out

def atr(kl, n=14):
    if len(kl) < n + 1:
        return [None] * len(kl)
    trs = [kl[0]["h"] - kl[0]["l"]]
    for i in range(1, len(kl)):
        h, l, pc = kl[i]["h"], kl[i]["l"], kl[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    out = [None] * (n - 1)
    a = sum(trs[:n]) / n
    out.append(a)
    for t in trs[n:]:
        a = (a * (n - 1) + t) / n
        out.append(a)
    return out

def stdev(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1:i + 1]
        m = sum(w) / n
        out[i] = math.sqrt(sum((x - m) ** 2 for x in w) / n)
    return out

def bollinger(closes, n=20, k=2.5):
    mid = sma(closes, n)
    sd = stdev(closes, n)
    lower = [None if m is None or s is None else m - k * s for m, s in zip(mid, sd)]
    upper = [None if m is None or s is None else m + k * s for m, s in zip(mid, sd)]
    return mid, lower, upper

def roc(closes, n):
    out = [None] * len(closes)
    for i in range(n, len(closes)):
        if closes[i - n]:
            out[i] = (closes[i] / closes[i - n] - 1) * 100
    return out

def zscore(vals, n):
    out = [None] * len(vals)
    for i in range(n - 1, len(vals)):
        w = vals[i - n + 1:i + 1]
        m = sum(w) / n
        s = math.sqrt(sum((x - m) ** 2 for x in w) / n)
        out[i] = (vals[i] - m) / s if s > 0 else 0.0
    return out

def day_of(t_ms):
    return datetime.fromtimestamp(t_ms / 1000, ET).strftime("%Y-%m-%d")

def et_hm(t_ms):
    d = datetime.fromtimestamp(t_ms / 1000, ET)
    return d.hour * 60 + d.minute

def weekday_et(t_ms):
    return datetime.fromtimestamp(t_ms / 1000, ET).weekday()

# ---------------- per-market session helpers (US / India / crypto) ----------------
def is_india(sym):
    return sym.endswith(".NS") or sym.endswith(".BO")

def is_crypto(sym):
    return sym.endswith("-USD")

def mkt_tz(sym):
    if is_india(sym):
        return IST
    if is_crypto(sym):
        return timezone.utc
    return ET

def mkt_open(sym):
    if is_crypto(sym):
        return True   # 24x7
    n = datetime.now(mkt_tz(sym))
    if n.weekday() >= 5:
        return False
    hm = (n.hour, n.minute)
    if is_india(sym):
        return (9, 15) <= hm < (15, 30)
    return (9, 30) <= hm < (16, 0)

def mkt_day(t_ms, sym):
    return datetime.fromtimestamp(t_ms / 1000, mkt_tz(sym)).strftime("%Y-%m-%d")

def mkt_hm(t_ms, sym):
    d = datetime.fromtimestamp(t_ms / 1000, mkt_tz(sym))
    return d.hour * 60 + d.minute

def mkt_eod_min(sym):
    """minutes-from-midnight for end-of-day exit; None for 24x7 crypto"""
    if is_india(sym):
        return 15 * 60 + 15
    if is_crypto(sym):
        return None
    return 15 * 60 + 45

def mkt_entry_cutoff_min(sym):
    """minutes-from-midnight after which NO new position can be entered.
    Prevents entering right before or during EOD square-off (e.g. 15:00 IST for 15:15 EOD)."""
    em = mkt_eod_min(sym)
    if em is None:
        return None
    return em - 15

def mkt_orb_cutoff(sym):
    """ORB entry window ends (minutes from midnight, market tz)"""
    if is_india(sym):
        return 10 * 60 + 45   # first 90 min after 9:15
    if is_crypto(sym):
        return 90             # first 90 min of UTC day
    return 11 * 60            # first 90 min after 9:30 ET

# ---------------- market hours ----------------
def us_open():
    n = datetime.now(ET)
    return n.weekday() < 5 and (9, 30) <= (n.hour, n.minute) < (16, 0)

def us_prepost_open():
    n = datetime.now(ET)
    return n.weekday() < 5 and (4, 0) <= (n.hour, n.minute) < (20, 0)

# ---------------- state ----------------
def new_bot_state():
    return {"cash": CAPITAL, "start_equity": CAPITAL, "positions": {},
            "closed_trades": [], "cycles": 0, "last_prices": {}, "meta": {}}

def new_state():
    return {"created": datetime.now(IST).isoformat(), "version": VERSION,
            "usdinr": 88.0, "bots": {b["id"]: new_bot_state() for b in BOTS},
            "equity_history": []}

def load_state():
    try:
        with open(STATE_F, encoding="utf-8") as f:
            st = json.load(f)
        if st.get("version") != VERSION:
            st = new_state(); save_state(st); return st
        for b in BOTS:
            st["bots"].setdefault(b["id"], new_bot_state())
            st["bots"][b["id"]]["id"] = b["id"]
        return st
    except Exception:
        return new_state()

def save_state(st):
    tmp = STATE_F + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_F)

def bot_equity(bs):
    e = bs["cash"]
    for sym, p in bs["positions"].items():
        px = bs["last_prices"].get(sym, p["entry"])
        e += p["side"] * p["qty"] * (px - p["entry"])
    return e

def record_trade(bot_id, bs, row):
    actual_id = bs.get("id") or bot_id
    row[1] = actual_id
    bs["closed_trades"].append(row)
    new = not os.path.exists(TRADES_F)
    with open(TRADES_F, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "bot", "symbol", "side", "qty", "entry", "exit", "pnl", "reason", "equity_after"])
        w.writerow(row)

def open_position(bs, sym, side, qty, price, stop, reason, now):
    cost = qty * price * FEE
    bs["cash"] -= cost
    bs["positions"][sym] = {"side": side, "qty": qty, "entry": price, "stop": stop,
                            "opened": now, "reason": reason, "bars": 0}
    bs["last_prices"][sym] = price
    log(f"  OPEN {sym} {'LONG' if side>0 else 'SHORT'} qty={qty:.4f} @ {price:.2f} stop={stop:.2f} ({reason})")

def close_position(bs, sym, price, reason, now):
    p = bs["positions"].pop(sym)
    pnl = p["side"] * p["qty"] * (price - p["entry"]) - p["qty"] * price * FEE - p["qty"] * p["entry"] * FEE
    bs["cash"] += pnl
    bs["last_prices"][sym] = price
    eq = bot_equity(bs)
    row = [now, "", sym, "LONG" if p["side"] > 0 else "SHORT", round(p["qty"], 6),
           round(p["entry"], 4), round(price, 4), round(pnl, 2), reason, round(eq, 2)]
    log(f"  CLOSE {sym} @ {price:.2f} pnl={pnl:+.2f} ({reason})")
    return row

def size_by_risk(eq, price, stop_dist, max_notional_pct=0.30, risk=0.01):
    if stop_dist <= 0 or price <= 0:
        return 0.0
    qty = (eq * risk) / stop_dist
    return min(qty, (eq * max_notional_pct) / price)

def manage_stops(bs, prices, now, bot_id, trail_fn=None):
    """Generic stop / trailing-stop management. prices: {sym: live_px}."""
    rows = []
    for sym in list(bs["positions"]):
        p = bs["positions"][sym]
        px = prices.get(sym)
        if px is None:
            continue
        bs["last_prices"][sym] = px
        p["bars"] += 1
        if trail_fn:
            new_stop = trail_fn(p, px)
            if new_stop is not None:
                if p["side"] > 0:
                    p["stop"] = max(p["stop"], new_stop)
                else:
                    p["stop"] = min(p["stop"], new_stop)
        hit = (p["side"] > 0 and px <= p["stop"]) or (p["side"] < 0 and px >= p["stop"])
        if hit:
            row = close_position(bs, sym, px, "stop", now)
            row[1] = bot_id
            record_trade(bot_id, bs, row)
            rows.append(row)
    return rows

# =================================================================
#  STRATEGY DEFINITIONS
#  Each strategy exposes run(bs, ctx) where ctx has: candles cache,
#  fx, now, live(sym). It manages its own entries/exits.
# =================================================================

class Ctx:
    def __init__(self, fx, now):
        self.fx = fx
        self.now = now
        self._cache = {}
        self._live = {}
    def candles(self, sym, interval="1d", rng="1y", prepost=False, drop_last=True):
        key = (sym, interval, rng, prepost, drop_last)
        if key not in self._cache:
            try:
                kl = get_candles(sym, interval, rng, prepost, drop_last)
                if is_usd(sym):
                    to_inr(kl, self.fx)
                self._cache[key] = kl
            except Exception as e:
                log(f"  candle fetch failed {sym} {interval}: {e}")
                self._cache[key] = []
        return self._cache[key]
    def live(self, sym):
        if sym not in self._live:
            try:
                px = get_live_price(sym)
                if is_usd(sym):
                    px *= self.fx
                self._live[sym] = px
            except Exception:
                self._live[sym] = None
        return self._live[sym]

# ---- s01: St. Gallen Opening-Range Noise-Band Breakout (30m, US+India) ----
S01_SYMS = ["NQ=F", "NIFTYBEES.NS"]
def s01(bs, ctx):
    now_ms = time.time() * 1000
    for sym in S01_SYMS:
        if not mkt_open(sym):
            continue
        mins = mkt_hm(now_ms, sym)
        kl = ctx.candles(sym, "30m", "5d", drop_last=True)
        daily = ctx.candles(sym, "1d", "1mo")
        if len(kl) < 3 or len(daily) < 15:
            continue
        # noise band = avg daily range over last 14 days * 0.5, around today's open
        avg_rng = sum(d["h"] - d["l"] for d in daily[-14:]) / 14
        band = avg_rng * 0.5
        today = mkt_day(kl[-1]["t"], sym)
        today_bars = [k for k in kl if mkt_day(k["t"], sym) == today]
        if not today_bars:
            continue
        day_open = today_bars[0]["o"]
        upper, lower = day_open + band, day_open - band
        px = ctx.live(sym) or kl[-1]["c"]
        bs["last_prices"][sym] = px
        a = atr(kl, 14)[-1] or (avg_rng / 4)
        # manage this symbol's position: exit if price back inside band, stop, or EOD
        if sym in bs["positions"]:
            p = bs["positions"][sym]
            p["bars"] += 1
            inside = lower < px < upper
            em = mkt_eod_min(sym)
            eod = em is not None and mins >= em
            hit = (p["side"] > 0 and px <= p["stop"]) or (p["side"] < 0 and px >= p["stop"])
            time_exit = is_crypto(sym) and p["bars"] >= 16   # 8h max hold, 24x7 market
            if inside or eod or hit or time_exit:
                reason = "band-revert" if inside else ("stop" if hit else ("eod" if eod else "time-exit"))
                row = close_position(bs, sym, px, reason, ctx.now)
                row[1] = "s01"; record_trade("s01", bs, row)
            continue
        if len(bs["positions"]) >= 1:
            return
        cut = mkt_entry_cutoff_min(sym)
        if cut is not None and mins >= cut:
            continue
        eq = bot_equity(bs)
        last_c = kl[-1]["c"]
        if last_c > upper:
            qty = size_by_risk(eq, px, a, 0.95, 0.01)
            if qty > 0:
                open_position(bs, sym, 1, qty, px, px - 2 * a, "noise-band-break-up", ctx.now)
        elif last_c < lower:
            qty = size_by_risk(eq, px, a, 0.95, 0.01)
            if qty > 0:
                open_position(bs, sym, -1, qty, px, px + 2 * a, "noise-band-break-down", ctx.now)

# ---- s02: Trend-Filtered Mean Reversion (200SMA + BB2.5, daily) ----
S02_SYMS = ["AAPL", "MSFT", "NVDA", "GOOGL", "AMZN", "META",
            "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS", "BTC-USD", "ETH-USD"]
def s02(bs, ctx):
    for sym in S02_SYMS:
        kl = ctx.candles(sym, "1d", "2y")
        if len(kl) < 210:
            continue
        c = [k["c"] for k in kl]
        sma200 = sma(c, 200)
        _, lower, _ = bollinger(c, 20, 2.5)
        r2 = rsi(c, 2)
        sma5 = sma(c, 5)
        i = len(kl) - 1
        px = ctx.live(sym) or c[i]
        bs["last_prices"][sym] = px
        if sym in bs["positions"]:
            p = bs["positions"][sym]
            p["bars"] += 1
            exit_sig = (r2[i] is not None and r2[i] > 50) or (sma5[i] is not None and c[i] > sma5[i]) or p["bars"] >= 10
            stop_hit = px <= p["stop"]
            if exit_sig or stop_hit:
                row = close_position(bs, sym, px, "rsi2>50/sma5" if exit_sig else "stop", ctx.now)
                row[1] = "s02"; record_trade("s02", bs, row)
            continue
        if len(bs["positions"]) >= 5:
            continue
        if sma200[i] and c[i] > sma200[i] and lower[i] and c[i] < lower[i]:
            entry = c[i] * 0.97   # buy-limit 3% below close (approx fill at close)
            stop = entry * 0.92
            eq = bot_equity(bs)
            qty = (eq * 0.20) / entry
            if qty > 0:
                open_position(bs, sym, 1, qty, px, stop, "bb2.5-lower+200sma", ctx.now)

# ---- s03: Multi-Asset ETF Dual Momentum Rotation (monthly) ----
S03_SYMS = ["SPY", "GLD", "TLT", "DBC", "NIFTYBEES.NS", "BTC-USD"]
def s03(bs, ctx):
    data = {}
    for sym in S03_SYMS:
        kl = ctx.candles(sym, "1d", "2y")
        if len(kl) < 260:
            continue
        c = [k["c"] for k in kl]
        data[sym] = {"c": c, "sma200": sma(c, 200)[-1], "roc12": roc(c, 252)[-1], "px": c[-1]}
    if len(data) < 2:
        return
    # rebalance on first trading day of month (meta flag)
    month_key = datetime.now(ET).strftime("%Y-%m")
    if bs["meta"].get("rebal_month") == month_key:
        # still mark prices
        for sym in data:
            bs["last_prices"][sym] = ctx.live(sym) or data[sym]["px"]
        return
    eligible = [s for s, d in data.items() if d["sma200"] and d["px"] > d["sma200"] and d["roc12"] is not None]
    eligible.sort(key=lambda s: -data[s]["roc12"])
    targets = eligible[:2]
    # close positions not in targets
    for sym in list(bs["positions"]):
        if sym not in targets:
            px = ctx.live(sym) or data.get(sym, {}).get("px", bs["positions"][sym]["entry"])
            row = close_position(bs, sym, px, "rotation-out", ctx.now)
            row[1] = "s03"; record_trade("s03", bs, row)
    eq = bot_equity(bs)
    for sym in targets:
        px = ctx.live(sym) or data[sym]["px"]
        bs["last_prices"][sym] = px
        if sym in bs["positions"]:
            continue
        qty = (eq * 0.5) / px
        if qty > 0:
            open_position(bs, sym, 1, qty, px, px * 0.80, "dual-momentum-top2", ctx.now)
    bs["meta"]["rebal_month"] = month_key

# ---- s04: Multi-Market Trend Following (Donchian 200 + 6ATR chandelier, L+S) ----
S04_SYMS = ["SPY", "QQQ", "GLD", "SLV", "TLT", "DBC", "UUP", "BTC-USD", "ETH-USD",
            "RELIANCE.NS", "TCS.NS", "NIFTYBEES.NS"]
def s04(bs, ctx):
    for sym in S04_SYMS:
        kl = ctx.candles(sym, "1d", "2y")
        if len(kl) < 210:
            continue
        c = [k["c"] for k in kl]
        a = atr(kl, 14)
        i = len(kl) - 1
        px = ctx.live(sym) or c[i]
        bs["last_prices"][sym] = px
        hi200 = max(c[i - 200:i])
        lo200 = min(c[i - 200:i])
        if sym in bs["positions"]:
            p = bs["positions"][sym]
            # chandelier trail 6*ATR from extreme since entry
            ext = p.get("extreme", p["entry"])
            if p["side"] > 0:
                ext = max(ext, px); p["extreme"] = ext
                p["stop"] = max(p["stop"], ext - 6 * (a[i] or 0))
            else:
                ext = min(ext, px); p["extreme"] = ext
                p["stop"] = min(p["stop"], ext + 6 * (a[i] or 0))
            hit = (p["side"] > 0 and px <= p["stop"]) or (p["side"] < 0 and px >= p["stop"])
            if hit:
                row = close_position(bs, sym, px, "chandelier-stop", ctx.now)
                row[1] = "s04"; record_trade("s04", bs, row)
            continue
        if len(bs["positions"]) >= 4:
            continue
        eq = bot_equity(bs)
        av = a[i] or (c[i] * 0.02)
        if c[i] >= hi200:
            qty = size_by_risk(eq, px, 6 * av, 0.25, 0.01)
            if qty > 0:
                open_position(bs, sym, 1, qty, px, px - 6 * av, "donchian200-high", ctx.now)
                bs["positions"][sym]["extreme"] = px
        elif c[i] <= lo200:
            qty = size_by_risk(eq, px, 6 * av, 0.25, 0.01)
            if qty > 0:
                open_position(bs, sym, -1, qty, px, px + 6 * av, "donchian200-low", ctx.now)
                bs["positions"][sym]["extreme"] = px

# ---- s05: Larry Connors RSI(2) Pullback (daily, index ETFs) ----
S05_SYMS = ["SPY", "QQQ", "NIFTYBEES.NS", "BTC-USD"]
def s05(bs, ctx):
    for sym in S05_SYMS:
        kl = ctx.candles(sym, "1d", "2y")
        if len(kl) < 210:
            continue
        c = [k["c"] for k in kl]
        sma200 = sma(c, 200)
        sma5 = sma(c, 5)
        r2 = rsi(c, 2)
        i = len(kl) - 1
        px = ctx.live(sym) or c[i]
        bs["last_prices"][sym] = px
        if sym in bs["positions"]:
            p = bs["positions"][sym]
            p["bars"] += 1
            exit_sig = (sma5[i] is not None and c[i] > sma5[i]) or p["bars"] >= 7
            if exit_sig or px <= p["stop"]:
                row = close_position(bs, sym, px, "close>sma5" if exit_sig else "stop", ctx.now)
                row[1] = "s05"; record_trade("s05", bs, row)
            continue
        if len(bs["positions"]) >= 2:
            continue
        if sma200[i] and c[i] > sma200[i] and r2[i] is not None and r2[i] < 5:
            eq = bot_equity(bs)
            qty = (eq * 0.5) / px
            stop = px * 0.93
            if qty > 0:
                open_position(bs, sym, 1, qty, px, stop, "rsi2<5+200sma", ctx.now)

# ---- s06: Previous-Day High/Low Break & Retest (15m) ----
S06_SYMS = ["QQQ", "AAPL", "MSFT", "NVDA", "RELIANCE.NS", "TCS.NS", "BTC-USD"]
def _prev_day_hl(kl, sym):
    days = {}
    for k in kl:
        d = mkt_day(k["t"], sym)
        if d not in days:
            days[d] = {"h": k["h"], "l": k["l"]}
        else:
            days[d]["h"] = max(days[d]["h"], k["h"])
            days[d]["l"] = min(days[d]["l"], k["l"])
    ds = sorted(days)
    if len(ds) < 2:
        return None
    return days[ds[-2]]

def s06(bs, ctx):
    now_ms = time.time() * 1000
    for sym in S06_SYMS:
        if not mkt_open(sym):
            continue
        mins = mkt_hm(now_ms, sym)
        kl = ctx.candles(sym, "15m", "5d")
        if len(kl) < 10:
            continue
        pd = _prev_day_hl(kl, sym)
        if not pd:
            continue
        px = ctx.live(sym) or kl[-1]["c"]
        bs["last_prices"][sym] = px
        a = atr(kl, 14)[-1] or (px * 0.005)
        if sym in bs["positions"]:
            p = bs["positions"][sym]
            p["bars"] += 1
            hit = (p["side"] > 0 and px <= p["stop"]) or (p["side"] < 0 and px >= p["stop"])
            em = mkt_eod_min(sym)
            eod = em is not None and mins >= em
            time_exit = is_crypto(sym) and p["bars"] >= 26   # 6.5h max hold, 24x7
            if hit or eod or time_exit:
                reason = "stop" if hit else ("eod" if eod else "time-exit")
                row = close_position(bs, sym, px, reason, ctx.now)
                row[1] = "s06"; record_trade("s06", bs, row)
            continue
        if len(bs["positions"]) >= 3:
            continue
        cut = mkt_entry_cutoff_min(sym)
        if cut is not None and mins >= cut:
            continue
        eq = bot_equity(bs)
        last_c = kl[-1]["c"]
        prev_c = kl[-2]["c"]
        # break then retest: prev bar broke level, last bar pulled back toward it
        if prev_c > pd["h"] and last_c <= pd["h"] * 1.002 and last_c > pd["h"] * 0.997:
            qty = size_by_risk(eq, px, a, 0.30, 0.01)
            if qty > 0:
                open_position(bs, sym, 1, qty, px, pd["h"] - a, "pd-high-retest", ctx.now)
        elif prev_c < pd["l"] and last_c >= pd["l"] * 0.998 and last_c < pd["l"] * 1.003:
            qty = size_by_risk(eq, px, a, 0.30, 0.01)
            if qty > 0:
                open_position(bs, sym, -1, qty, px, pd["l"] + a, "pd-low-retest", ctx.now)

# ---- s07: First-Candle ORB 15m Break & Retest (NY open) ----
S07_SYMS = ["QQQ", "SPY", "AAPL", "NVDA", "NIFTYBEES.NS", "RELIANCE.NS"]
def s07(bs, ctx):
    now_ms = time.time() * 1000
    for sym in S07_SYMS:
        if not mkt_open(sym):
            continue
        mins = mkt_hm(now_ms, sym)
        kl = ctx.candles(sym, "15m", "5d")
        if len(kl) < 5:
            continue
        today = mkt_day(kl[-1]["t"], sym)
        tb = [k for k in kl if mkt_day(k["t"], sym) == today]
        if not tb:
            continue
        orb = tb[0]   # first 15m candle of the market's day
        px = ctx.live(sym) or kl[-1]["c"]
        bs["last_prices"][sym] = px
        a = atr(kl, 14)[-1] or (px * 0.004)
        if sym in bs["positions"]:
            p = bs["positions"][sym]
            p["bars"] += 1
            hit = (p["side"] > 0 and px <= p["stop"]) or (p["side"] < 0 and px >= p["stop"])
            em = mkt_eod_min(sym)
            eod = em is not None and mins >= em
            time_exit = is_crypto(sym) and p["bars"] >= 26   # 6.5h max hold, 24x7
            if hit or eod or time_exit:
                reason = "stop" if hit else ("eod" if eod else "time-exit")
                row = close_position(bs, sym, px, reason, ctx.now)
                row[1] = "s07"; record_trade("s07", bs, row)
            continue
        cut = mkt_entry_cutoff_min(sym)
        if len(bs["positions"]) >= 3 or mins > mkt_orb_cutoff(sym) or (cut is not None and mins >= cut):
            continue
        eq = bot_equity(bs)
        last_c = kl[-1]["c"]
        if last_c > orb["h"]:
            qty = size_by_risk(eq, px, a, 0.30, 0.01)
            if qty > 0:
                open_position(bs, sym, 1, qty, px, orb["l"], "orb15-break-up", ctx.now)
        elif last_c < orb["l"]:
            qty = size_by_risk(eq, px, a, 0.30, 0.01)
            if qty > 0:
                open_position(bs, sym, -1, qty, px, orb["h"], "orb15-break-down", ctx.now)

# ---- s08: Pre-Market High/Low Breakout & Retest (15m, prepost) ----
S08_SYMS = ["AAPL", "MSFT", "NVDA", "TSLA"]
def s08(bs, ctx):
    if not us_open():
        return
    now_ms = time.time() * 1000
    for sym in S08_SYMS:
        kl = ctx.candles(sym, "15m", "5d", prepost=True)
        if len(kl) < 10:
            continue
        today = day_of(kl[-1]["t"])
        # pre-market bars: today, before 9:30 ET
        pre = [k for k in kl if day_of(k["t"]) == today and et_hm(k["t"]) < 9 * 60 + 30]
        reg = [k for k in kl if day_of(k["t"]) == today and et_hm(k["t"]) >= 9 * 60 + 30]
        if len(pre) < 2 or not reg:
            continue
        pm_h = max(k["h"] for k in pre)
        pm_l = min(k["l"] for k in pre)
        px = ctx.live(sym) or kl[-1]["c"]
        bs["last_prices"][sym] = px
        a = atr(kl, 14)[-1] or (px * 0.004)
        if sym in bs["positions"]:
            p = bs["positions"][sym]
            p["bars"] += 1
            hit = (p["side"] > 0 and px <= p["stop"]) or (p["side"] < 0 and px >= p["stop"])
            eod = et_hm(now_ms) >= 15 * 60 + 45
            if hit or eod:
                row = close_position(bs, sym, px, "stop" if hit else "eod", ctx.now)
                row[1] = "s08"; record_trade("s08", bs, row)
            continue
        if len(bs["positions"]) >= 3:
            continue
        cut = mkt_entry_cutoff_min(sym)
        if cut is not None and mkt_hm(now_ms, sym) >= cut:
            continue
        eq = bot_equity(bs)
        last_c = reg[-1]["c"]
        if last_c > pm_h:
            qty = size_by_risk(eq, px, a, 0.30, 0.01)
            if qty > 0:
                open_position(bs, sym, 1, qty, px, pm_h - a, "pm-high-break", ctx.now)
        elif last_c < pm_l:
            qty = size_by_risk(eq, px, a, 0.30, 0.01)
            if qty > 0:
                open_position(bs, sym, -1, qty, px, pm_l + a, "pm-low-break", ctx.now)

# ---- s09: Statistical Arbitrage / Pairs Trading (daily, z-score) ----
S09_PAIRS = [("KO", "PEP"), ("XOM", "CVX"), ("MA", "V"), ("HD", "LOW"), ("JPM", "BAC"),
             ("HDFCBANK.NS", "ICICIBANK.NS"), ("TCS.NS", "INFY.NS"), ("BTC-USD", "ETH-USD")]
def s09(bs, ctx):
    for a_sym, b_sym in S09_PAIRS:
        ka = ctx.candles(a_sym, "1d", "1y")
        kb = ctx.candles(b_sym, "1d", "1y")
        if len(ka) < 70 or len(kb) < 70:
            continue
        n = min(len(ka), len(kb))
        ca = [k["c"] for k in ka[-n:]]
        cb = [k["c"] for k in kb[-n:]]
        spread = [math.log(x / y) for x, y in zip(ca, cb)]
        z = zscore(spread, 60)
        i = n - 1
        pa = ctx.live(a_sym) or ca[i]
        pb = ctx.live(b_sym) or cb[i]
        bs["last_prices"][a_sym] = pa
        bs["last_prices"][b_sym] = pb
        key = f"{a_sym}/{b_sym}"
        live_spread = math.log(pa / pb) if (pa and pb and pa > 0 and pb > 0) else spread[i]
        bs["last_prices"][key] = live_spread

        if key in bs["positions"]:
            p = bs["positions"][key]
            p["bars"] += 1
            cur = live_spread
            stop_hit = (p["side"] > 0 and cur <= p["stop"]) or (p["side"] < 0 and cur >= p["stop"])
            # Exit when spread mean-reverts to within 0.5 std dev, or stop hit, or max 10 days
            opened_dt = datetime.fromisoformat(p.get("opened", ctx.now))
            days_held = (datetime.fromisoformat(ctx.now) - opened_dt).total_seconds() / 86400.0
            if z[i] is not None and abs(z[i]) < 0.5:
                row = close_position(bs, key, cur, "z-revert", ctx.now)
                row[1] = "s09"; record_trade("s09", bs, row)
            elif stop_hit:
                row = close_position(bs, key, cur, "spread-stop", ctx.now)
                row[1] = "s09"; record_trade("s09", bs, row)
            elif days_held >= 10.0:
                row = close_position(bs, key, cur, "time-exit-10d", ctx.now)
                row[1] = "s09"; record_trade("s09", bs, row)
            continue
        if len(bs["positions"]) >= 3 or z[i] is None:
            continue
        eq = bot_equity(bs)
        notional = eq * 0.30
        if z[i] > 2:      # spread rich: short A, long B
            entry = live_spread
            qty = notional   # pnl = notional * delta(log-spread)
            open_position(bs, key, -1, qty, entry, entry + 0.10, "z>2-short-spread", ctx.now)
            bs["positions"][key]["legs"] = (a_sym, b_sym, -1)
            bs["positions"][key]["mark"] = entry
        elif z[i] < -2:   # spread cheap: long A, short B
            entry = live_spread
            qty = notional
            open_position(bs, key, 1, qty, entry, entry - 0.10, "z<-2-long-spread", ctx.now)
            bs["positions"][key]["legs"] = (a_sym, b_sym, 1)
            bs["positions"][key]["mark"] = entry
    # mark pair positions to current spread for equity
    for key, p in bs["positions"].items():
        if "legs" in p:
            a_sym, b_sym, _ = p["legs"]
            pa = bs["last_prices"].get(a_sym); pb = bs["last_prices"].get(b_sym)
            if pa and pb:
                cur = math.log(pa / pb)
                bs["last_prices"][key] = cur

# ---- s10: Overnight Edge + Turnaround Tuesday (daily, US + India) ----
S10_SYMS = ["SPY", "NIFTYBEES.NS"]
def s10(bs, ctx):
    for sym in S10_SYMS:
        kl = ctx.candles(sym, "1d", "1y")
        if len(kl) < 10:
            continue
        c = [k["c"] for k in kl]
        px = ctx.live(sym) or c[-1]
        bs["last_prices"][sym] = px
        today = datetime.now(mkt_tz(sym))
        today_key = today.strftime("%Y-%m-%d")
        day_meta = f"last_day_{sym}"
        # Overnight edge: buy at close, sell next open. Daily cadence: one entry per day,
        # exit on the next trading day's first cycle.
        if sym in bs["positions"]:
            p = bs["positions"][sym]
            # sell at next day's open (first cycle after market open on a later day)
            if p.get("day") != today_key and mkt_open(sym):
                row = close_position(bs, sym, px, "overnight-exit", ctx.now)
                row[1] = "s10"; record_trade("s10", bs, row)
            continue
        if len(bs["positions"]) >= 2 or bs["meta"].get(day_meta) == today_key:
            continue
        # enter only near the close to approximate buy-at-close
        entry_min = 15 * 60 + 15 if is_india(sym) else 15 * 60 + 45
        if not mkt_open(sym) or (today.hour * 60 + today.minute) < entry_min:
            continue
        eq = bot_equity(bs)
        # Turnaround Tuesday: Tuesday after >=2 consecutive down days
        consec_down = 0
        for j in range(len(c) - 1, 0, -1):
            if c[j] < c[j - 1]:
                consec_down += 1
            else:
                break
        is_tuesday = today.weekday() == 1
        overnight = True   # always run overnight edge
        if overnight or (is_tuesday and consec_down >= 2):
            qty = (eq * 0.45) / px
            reason = "turnaround-tuesday" if (is_tuesday and consec_down >= 2) else "overnight-edge"
            if qty > 0:
                open_position(bs, sym, 1, qty, px, px * 0.95, reason, ctx.now)
                bs["positions"][sym]["day"] = today_key
                bs["meta"][day_meta] = today_key

# =====================================================================
#  INTRADAY SHORT SUITE (s11-s13) — ported from TradingView Pine v5
#  Universe = top movers per OPEN market, ranked by RVOL x |day-move|
#  (port of top_movers_scanner.py, stdlib only). Pine params kept:
#  10% equity per trade, max 1 position, RR 2:1, entry sessions
#  09:30-15:00 / 13:00-15:05 exchange-local, EOD flatten 15:15.
#  Crypto = 24x7 "exchange" in UTC (same windows, UTC clock).
# =====================================================================
NIFTY_50 = [
    "RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "ICICIBANK.NS", "INFY.NS",
    "HINDUNILVR.NS", "ITC.NS", "SBIN.NS", "BHARTIARTL.NS", "KOTAKBANK.NS",
    "LT.NS", "AXISBANK.NS", "ASIANPAINT.NS", "MARUTI.NS", "TITAN.NS",
    "SUNPHARMA.NS", "BAJFINANCE.NS", "WIPRO.NS", "ULTRACEMCO.NS", "NTPC.NS",
    "POWERGRID.NS", "M&M.NS", "TATAMOTORS.NS", "TATASTEEL.NS", "ADANIENT.NS",
    "ADANIPORTS.NS", "JSWSTEEL.NS", "COALINDIA.NS", "HCLTECH.NS", "TECHM.NS",
    "ONGC.NS", "INDUSINDBK.NS", "BAJAJFINSV.NS", "GRASIM.NS", "DRREDDY.NS",
    "CIPLA.NS", "BRITANNIA.NS", "NESTLEIND.NS", "EICHERMOT.NS", "HEROMOTOCO.NS",
]
US_TOP_LIQUID = [
    "NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "META", "GOOGL", "AMD",
    "NFLX", "AVGO", "JPM", "BAC", "GS", "XOM", "CVX", "LLY",
    "UNH", "COIN", "MSTR", "PLTR", "SMCI", "ARM", "MU", "QCOM",
    "INTC", "BA", "DIS", "CRM", "ORCL", "ADBE",
]
CRYPTO_TOP_20 = [
    "BTC-USD", "ETH-USD", "SOL-USD", "BNB-USD", "XRP-USD",
    "DOGE-USD", "ADA-USD", "AVAX-USD", "LINK-USD", "DOT-USD",
    "TRX-USD", "MATIC-USD", "SHIB-USD", "LTC-USD", "BCH-USD",
    "UNI-USD", "ATOM-USD", "NEAR-USD", "APT-USD", "ARB-USD",
]
MOVER_UNIVERSES = {"nifty": NIFTY_50, "us": US_TOP_LIQUID, "crypto": CRYPTO_TOP_20}
_MOVERS_CACHE = {}      # market -> (ts, [sym,...])
_INTRADAY_CACHE = {}    # (sym, interval, rng) -> (ts, kl)
MOVERS_TTL = 300        # rescan movers every 5 min
INTRA_TTL = {"5m": 120, "60m": 3600}

def top_movers(market, ctx, top_n=8):
    """Mover_Score = RVOL x |Day_Move_%| on liquid universe (daily bars)."""
    hit = _MOVERS_CACHE.get(market)
    if hit and time.time() - hit[0] < MOVERS_TTL:
        return hit[1]
    rows = []
    for sym in MOVER_UNIVERSES[market]:
        try:
            kl = ctx.candles(sym, "1d", "2mo", drop_last=False)
            if len(kl) < 22:
                continue
            prev_c = kl[-2]["c"]
            cur = kl[-1]
            if prev_c <= 0:
                continue
            avg_vol = sum(k["v"] for k in kl[-21:-1]) / 20.0
            if avg_vol <= 0:
                continue
            rvol = cur["v"] / avg_vol
            move = (cur["c"] - prev_c) / prev_c * 100.0
            rows.append((rvol * abs(move), sym))
        except Exception:
            continue
    rows.sort(reverse=True)
    syms = [s for _, s in rows[:top_n]]
    _MOVERS_CACHE[market] = (time.time(), syms)
    return syms

def mover_universe(ctx):
    syms = []
    if mkt_open("RELIANCE.NS"):
        syms += top_movers("nifty", ctx)
    if us_open():
        syms += top_movers("us", ctx)
    syms += top_movers("crypto", ctx)
    return syms

def intraday(sym, interval, rng, ctx):
    """Cross-cycle TTL cache for intraday bars (Ctx cache is per-cycle)."""
    key = (sym, interval, rng)
    hit = _INTRADAY_CACHE.get(key)
    if hit and time.time() - hit[0] < INTRA_TTL.get(interval, 120):
        return hit[1]
    kl = ctx.candles(sym, interval, rng)
    if kl:
        _INTRADAY_CACHE[key] = (time.time(), kl)
    return kl

def vwap_series(kl, sym):
    """Day-anchored VWAP on hlc3 (Pine ta.vwap), resets each market-local day."""
    out = [None] * len(kl)
    day, pv, vv = None, 0.0, 0.0
    for i, k in enumerate(kl):
        d = mkt_day(k["t"], sym)
        if d != day:
            day, pv, vv = d, 0.0, 0.0
        pv += (k["h"] + k["l"] + k["c"]) / 3.0 * k["v"]
        vv += k["v"]
        out[i] = pv / vv if vv > 0 else k["c"]
    return out

def _pine_short_exit(bid, bs, sym, px, ctx, mean_exit=False):
    """Stop / 2R target / mean-revert / EOD-15:15 (exchange-local) exit."""
    p = bs["positions"][sym]
    p["bars"] += 1
    hit = px >= p["stop"]
    tgt = p.get("target") is not None and px <= p["target"]
    eod = mkt_hm(time.time() * 1000, sym) >= 15 * 60 + 15
    if hit or tgt or mean_exit or eod:
        reason = "stop" if hit else ("target-2R" if tgt else ("mean-revert" if mean_exit else "eod"))
        row = close_position(bs, sym, px, reason, ctx.now)
        row[1] = bid
        record_trade(bid, bs, row)
        return True
    return False

def _pine_short_entry(bs, sym, px, stop, reason, ctx, target=None):
    """10% of equity, single position (Pine default_qty_value=10)."""
    if stop <= px or len(bs["positions"]) >= 1:
        return False
    eq = bot_equity(bs)
    qty = (eq * 0.10) / px
    if qty <= 0:
        return False
    open_position(bs, sym, -1, qty, px, stop, reason, ctx.now)
    if target is not None:
        bs["positions"][sym]["target"] = target
    return True

def s11(bs, ctx):
    """01. Programmatic Spike & VWAP Rejection Short (5m, top movers).
    Short when a volume-spiked bar crosses back below VWAP after poking
    above VWAP/PDH; stop = 10-bar spike high, target = 2R."""
    for sym in mover_universe(ctx):
        if not mkt_open(sym):
            continue
        px = ctx.live(sym)
        if px is None:
            continue
        bs["last_prices"][sym] = px
        if sym in bs["positions"]:
            _pine_short_exit("s11", bs, sym, px, ctx)
            continue
        hm = mkt_hm(time.time() * 1000, sym)
        if not (9 * 60 + 30 <= hm < 15 * 60):
            continue
        kl = intraday(sym, "5m", "5d", ctx)
        daily = ctx.candles(sym, "1d", "1mo")
        if len(kl) < 30 or len(daily) < 2:
            continue
        vw = vwap_series(kl, sym)
        c = [k["c"] for k in kl]
        v = [k["v"] for k in kl]
        vs = sma(v, 20)
        i = len(kl) - 1
        if vw[i] is None or vw[i - 1] is None or vs[i] is None or vs[i - 1] is None:
            continue
        pdh = daily[-1]["h"]
        cross_dn = c[i] < vw[i] and c[i - 1] >= vw[i - 1]
        bearish = c[i] < kl[i]["o"]
        spiked = kl[i - 1]["h"] > vw[i - 1] or kl[i]["h"] > pdh
        vol_sp = v[i] > 1.5 * vs[i] or v[i - 1] > 1.5 * vs[i - 1]
        if cross_dn and bearish and spiked and vol_sp:
            stop = max(k["h"] for k in kl[-10:]) * 1.0005
            risk = max(stop - px, px * 0.001)
            tgt = px - 2 * risk
            if _pine_short_entry(bs, sym, px, stop, "vwap-reject-spike-short", ctx, target=tgt):
                log(f"s11 SHORT {sym} @ {px:.2f} stop {stop:.2f} tgt {tgt:.2f} (VWAP reject + vol spike)")

def s12(bs, ctx):
    """02. OHL (Open=High) + CPR Bearish Breakdown Short (5m, top movers).
    First bar Open=High (0.05% tol) arms the day; short on close below
    CPR bottom + prev close; stop = day high, target = 2R, 1 entry/day."""
    for sym in mover_universe(ctx):
        if not mkt_open(sym):
            continue
        px = ctx.live(sym)
        if px is None:
            continue
        bs["last_prices"][sym] = px
        if sym in bs["positions"]:
            _pine_short_exit("s12", bs, sym, px, ctx)
            continue
        if mkt_hm(time.time() * 1000, sym) >= 15 * 60 + 15:
            continue
        kl = intraday(sym, "5m", "5d", ctx)
        daily = ctx.candles(sym, "1d", "1mo")
        if len(kl) < 5 or len(daily) < 2:
            continue
        today = mkt_day(time.time() * 1000, sym)
        tb = [k for k in kl if mkt_day(k["t"], sym) == today]
        if not tb:
            continue
        meta = bs["meta"].setdefault("ohl", {}).setdefault(sym, {})
        if meta.get("day") != today:
            f = tb[0]
            meta.update(day=today, armed=abs(f["o"] - f["h"]) <= f["o"] * 0.0005, traded=False)
        meta["dayHigh"] = max(k["h"] for k in tb)
        if not meta.get("armed") or meta.get("traded"):
            continue
        pd = daily[-1]
        pivot = (pd["h"] + pd["l"] + pd["c"]) / 3.0
        bc = (pd["h"] + pd["l"]) / 2.0
        tc = 2.0 * pivot - bc
        cpr_bot = min(bc, tc)
        last = kl[-1]
        if mkt_day(last["t"], sym) != today:
            continue
        if last["c"] < cpr_bot and last["c"] < pd["c"] and last["c"] < last["o"]:
            stop = meta["dayHigh"] * 1.0005
            risk = max(stop - px, px * 0.001)
            tgt = px - 2 * risk
            if _pine_short_entry(bs, sym, px, stop, "ohl-cpr-breakdown-short", ctx, target=tgt):
                meta["traded"] = True
                log(f"s12 SHORT {sym} @ {px:.2f} stop {stop:.2f} tgt {tgt:.2f} (OHL + CPR breakdown)")

def s13(bs, ctx):
    """03. Afternoon Reversal / Sell-the-Rip Short (5m + 1h regime).
    Bear regime = 1h close < 1h EMA200. 13:00-15:05: RSI(3)>75 poke above
    BB(20,2) upper + bearish close -> short; stop = bar high + 1.5 ATR;
    exit at VWAP / BB-mid mean reversion, stop, or 15:15 EOD."""
    for sym in mover_universe(ctx):
        if not mkt_open(sym):
            continue
        px = ctx.live(sym)
        if px is None:
            continue
        bs["last_prices"][sym] = px
        if sym in bs["positions"]:
            mean_exit = False
            kl = intraday(sym, "5m", "5d", ctx)
            if len(kl) >= 21:
                vw = vwap_series(kl, sym)
                mid = sma([k["c"] for k in kl], 20)
                if (vw[-1] is not None and px <= vw[-1]) or (mid[-1] is not None and px <= mid[-1]):
                    mean_exit = True
            _pine_short_exit("s13", bs, sym, px, ctx, mean_exit=mean_exit)
            continue
        hm = mkt_hm(time.time() * 1000, sym)
        if not (13 * 60 <= hm < 15 * 60 + 5):
            continue
        h1 = intraday(sym, "60m", "3mo", ctx)
        if len(h1) < 201:
            continue
        e200 = ema([k["c"] for k in h1], 200)
        if e200[-1] is None or h1[-1]["c"] >= e200[-1]:
            continue    # bearish HTF regime only
        kl = intraday(sym, "5m", "5d", ctx)
        if len(kl) < 30:
            continue
        c = [k["c"] for k in kl]
        r3 = rsi(c, 3)
        _, _, up = bollinger(c, 20, 2.0)
        i = len(kl) - 1
        if r3[i] is None or up[i] is None:
            continue
        if r3[i] > 75 and kl[i]["h"] >= up[i] and c[i] < kl[i]["o"]:
            a = atr(kl, 14)[-1] or px * 0.004
            stop = max(kl[i]["h"], kl[i - 1]["h"]) + 1.5 * a
            if _pine_short_entry(bs, sym, px, stop, "afternoon-rip-short", ctx):
                log(f"s13 SHORT {sym} @ {px:.2f} stop {stop:.2f} (RSI3 {r3[i]:.0f} > BB upper, bear regime)")

# =====================================================================
# INTELLIGENT v2 SUITE (smart_01 - smart_13)
# Advanced Quant Enhancements:
# 1. Zero-Risk Breakeven Locker (stops moved to entry at +1.0R profit)
# 2. Dynamic Trailing Chandelier Stop (protects 70%+ of peak gains at +1.5R+)
# 3. Independent state and separate tracking from original s01-s13
# =====================================================================

def _smart_position_manager(bs, bot_id, ctx):
    """Monitors positions for smart bots: locks breakeven at +1R and trails at +1.5R."""
    for sym in list(bs["positions"].keys()):
        p = bs["positions"][sym]
        px = ctx.live(sym) or bs["last_prices"].get(sym, p["entry"])
        bs["last_prices"][sym] = px
        side = p["side"]
        entry = p["entry"]
        stop = p["stop"]

        # Track initial risk
        if "init_risk" not in p:
            risk = abs(entry - stop)
            p["init_risk"] = risk if risk > (entry * 0.002) else (entry * 0.01)
            p["extreme_px"] = entry
            p["be_locked"] = False

        init_risk = p["init_risk"]

        # Track peak favorable price
        if side > 0:
            p["extreme_px"] = max(p.get("extreme_px", entry), px)
            profit = px - entry
        else:
            p["extreme_px"] = min(p.get("extreme_px", entry), px)
            profit = entry - px

        r_mult = profit / init_risk

        # 1. Breakeven Lock at +1.0R (Risk becomes zero!)
        if r_mult >= 1.0 and not p.get("be_locked"):
            if side > 0 and p["stop"] < entry:
                p["stop"] = entry
                p["be_locked"] = True
                log(f"[{bot_id}] {sym} ZERO-RISK: Breakeven locked @ {entry:.2f} (Profit: +{r_mult:.1f}R)")
            elif side < 0 and p["stop"] > entry:
                p["stop"] = entry
                p["be_locked"] = True
                log(f"[{bot_id}] {sym} ZERO-RISK: Breakeven locked @ {entry:.2f} (Profit: +{r_mult:.1f}R)")

        # 2. Dynamic Chandelier Trailing at +1.5R+
        if r_mult >= 1.5:
            if side > 0:
                trail = p["extreme_px"] - (0.8 * init_risk)
                if trail > p["stop"]:
                    p["stop"] = trail
            else:
                trail = p["extreme_px"] + (0.8 * init_risk)
                if trail < p["stop"]:
                    p["stop"] = trail

        # 3. Check Stop / Breakeven Hit
        hit = (side > 0 and px <= p["stop"]) or (side < 0 and px >= p["stop"])
        if hit:
            reason = "smart-breakeven" if p.get("be_locked") and abs(p["stop"] - entry) <= (entry * 0.002) else "smart-trail-stop"
            row = close_position(bs, sym, px, reason, ctx.now)
            row[1] = bot_id
            record_trade(bot_id, bs, row)


def make_smart_bot(orig_fn, bot_id):
    def smart_fn(bs, ctx):
        bs["id"] = bot_id
        # Run smart position manager on existing open positions
        _smart_position_manager(bs, bot_id, ctx)
        # Execute underlying strategy logic
        orig_fn(bs, ctx)
        # Tag any newly opened positions with tracking metadata
        for sym, p in bs["positions"].items():
            if "init_risk" not in p:
                entry = p["entry"]
                stop = p["stop"]
                risk = abs(entry - stop)
                p["init_risk"] = risk if risk > (entry * 0.002) else (entry * 0.01)
                p["extreme_px"] = entry
                p["be_locked"] = False
    return smart_fn

smart_01 = make_smart_bot(s01, "smart_01")
smart_02 = make_smart_bot(s02, "smart_02")
smart_03 = make_smart_bot(s03, "smart_03")
smart_04 = make_smart_bot(s04, "smart_04")
smart_05 = make_smart_bot(s05, "smart_05")
smart_06 = make_smart_bot(s06, "smart_06")
smart_07 = make_smart_bot(s07, "smart_07")
smart_08 = make_smart_bot(s08, "smart_08")
smart_09 = make_smart_bot(s09, "smart_09")
smart_10 = make_smart_bot(s10, "smart_10")
smart_11 = make_smart_bot(s11, "smart_11")
smart_12 = make_smart_bot(s12, "smart_12")
smart_13 = make_smart_bot(s13, "smart_13")

# ---------------- bot registry ----------------
BOTS = [
    {"id": "s01", "name": "St. Gallen ORB Noise-Band", "tf": "30m", "market": "US + India + Crypto", "fn": s01},
    {"id": "s02", "name": "Trend-Filtered Mean Reversion", "tf": "daily", "market": "US + India + Crypto", "fn": s02},
    {"id": "s03", "name": "ETF Dual Momentum Rotation", "tf": "monthly", "market": "US + India + Crypto", "fn": s03},
    {"id": "s04", "name": "Multi-Market Trend Following", "tf": "daily", "market": "US + India + Crypto", "fn": s04},
    {"id": "s05", "name": "Connors RSI(2) Pullback", "tf": "daily", "market": "US + India + Crypto", "fn": s05},
    {"id": "s06", "name": "Prev-Day High/Low Retest", "tf": "15m", "market": "US + India + Crypto", "fn": s06},
    {"id": "s07", "name": "First-Candle ORB 15m", "tf": "15m", "market": "US + India + Crypto", "fn": s07},
    {"id": "s08", "name": "Pre-Market High/Low Break", "tf": "15m", "market": "US (pre-market)", "fn": s08},
    {"id": "s09", "name": "Statistical Arbitrage Pairs", "tf": "daily", "market": "US + India + Crypto", "fn": s09},
    {"id": "s10", "name": "Overnight Edge + Turnaround Tue", "tf": "daily", "market": "US + India", "fn": s10},
    {"id": "s11", "name": "Spike & VWAP Rejection Short", "tf": "5m intraday", "market": "Top movers (NSE+US+Crypto)", "fn": s11},
    {"id": "s12", "name": "OHL + CPR Breakdown Short", "tf": "5m intraday", "market": "Top movers (NSE+US+Crypto)", "fn": s12},
    {"id": "s13", "name": "Afternoon Sell-the-Rip Short", "tf": "5m + 1h regime", "market": "Top movers (NSE+US+Crypto)", "fn": s13},
    # --- Intelligent v2 Clones ---
    {"id": "smart_01", "name": "St. Gallen ORB (Smart v2)", "tf": "30m", "market": "US + India + Crypto", "fn": smart_01},
    {"id": "smart_02", "name": "Trend-Filtered Mean Rev (Smart v2)", "tf": "daily", "market": "US + India + Crypto", "fn": smart_02},
    {"id": "smart_03", "name": "ETF Dual Momentum (Smart v2)", "tf": "monthly", "market": "US + India + Crypto", "fn": smart_03},
    {"id": "smart_04", "name": "Multi-Market Trend (Smart v2)", "tf": "daily", "market": "US + India + Crypto", "fn": smart_04},
    {"id": "smart_05", "name": "Connors RSI(2) (Smart v2)", "tf": "daily", "market": "US + India + Crypto", "fn": smart_05},
    {"id": "smart_06", "name": "Prev-Day Retest (Smart v2)", "tf": "15m", "market": "US + India + Crypto", "fn": smart_06},
    {"id": "smart_07", "name": "First-Candle ORB (Smart v2)", "tf": "15m", "market": "US + India + Crypto", "fn": smart_07},
    {"id": "smart_08", "name": "Pre-Market Breakout (Smart v2)", "tf": "15m", "market": "US (pre-market)", "fn": smart_08},
    {"id": "smart_09", "name": "Statistical Arbitrage (Smart v2)", "tf": "daily", "market": "US + India + Crypto", "fn": smart_09},
    {"id": "smart_10", "name": "Overnight Edge (Smart v2)", "tf": "daily", "market": "US + India", "fn": smart_10},
    {"id": "smart_11", "name": "Spike & VWAP Short (Smart v2)", "tf": "5m intraday", "market": "Top movers (NSE+US+Crypto)", "fn": smart_11},
    {"id": "smart_12", "name": "OHL + CPR Short (Smart v2)", "tf": "5m intraday", "market": "Top movers (NSE+US+Crypto)", "fn": smart_12},
    {"id": "smart_13", "name": "Afternoon Sell-the-Rip Short (Smart v2)", "tf": "5m + 1h regime", "market": "Top movers (NSE+US+Crypto)", "fn": smart_13},
]

# ---------------- cycle ----------------
def run_cycle(st):
    fx = get_usdinr(st)
    now = datetime.now(IST).isoformat()
    ctx = Ctx(fx, now)
    log(f"=== ARENA CYCLE  USDINR={fx:.2f}  US_open={us_open()} ===")
    for b in BOTS:
        bs = st["bots"][b["id"]]
        bs["cycles"] += 1
        try:
            b["fn"](bs, ctx)
        except Exception as e:
            log(f"{b['id']} ERROR: {e}\n{traceback.format_exc()}")
    # snapshot equity history
    tot = sum(bot_equity(st["bots"][b["id"]]) for b in BOTS)
    st["equity_history"].append({"t": now, "total": round(tot, 2),
                                 **{b["id"]: round(bot_equity(st["bots"][b["id"]]), 2) for b in BOTS}})
    st["equity_history"] = st["equity_history"][-2000:]
    save_state(st)
    write_dashboard(st)
    write_report(st)
    log(f"cycle done. total equity = Rs {tot:,.2f}")

# ---------------- leaderboard / report ----------------
def leaderboard(st):
    rows = []
    for b in BOTS:
        bs = st["bots"][b["id"]]
        eq = bot_equity(bs)
        pnl = eq - bs["start_equity"]
        pct = pnl / bs["start_equity"] * 100
        n_closed = len(bs["closed_trades"])
        wins = sum(1 for t in bs["closed_trades"] if t[7] > 0)
        wr = (wins / n_closed * 100) if n_closed else 0.0
        rows.append({"id": b["id"], "name": b["name"], "equity": eq, "pnl": pnl,
                     "pct": pct, "trades": n_closed, "win_rate": wr,
                     "open": len(bs["positions"])})
    rows.sort(key=lambda r: -r["pnl"])
    return rows

def write_report(st):
    lb = leaderboard(st)
    total = sum(r["equity"] for r in lb)
    lines = [f"# Strategy Arena — Leaderboard",
             f"_Updated: {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}_",
             f"", f"**Total equity: Rs {total:,.2f}** / Rs {CAPITAL*len(BOTS):,.0f} start "
             f"({(total/(CAPITAL*len(BOTS))-1)*100:+.2f}%)", "",
             "| Rank | Bot | Strategy | Equity | P&L | % | Trades | Win% | Open |",
             "|---|---|---|---|---|---|---|---|---|"]
    for i, r in enumerate(lb, 1):
        lines.append(f"| {i} | {r['id']} | {r['name']} | Rs {r['equity']:,.2f} | "
                     f"{r['pnl']:+,.2f} | {r['pct']:+.2f}% | {r['trades']} | {r['win_rate']:.0f}% | {r['open']} |")
    best = lb[0]
    lines += ["", f"**Best so far: {best['id']} — {best['name']} ({best['pct']:+.2f}%)**"]
    with open(REPORT_F, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

def write_dashboard(st):
    lb = leaderboard(st)
    total = sum(r["equity"] for r in lb)
    start = CAPITAL * len(BOTS)
    hist = st.get("equity_history", [])
    # sparkline points for total
    pts = ""
    if len(hist) > 1:
        vals = [h["total"] for h in hist]
        lo, hi = min(vals), max(vals)
        rng = (hi - lo) or 1
        w, hgt = 600, 80
        coords = [f"{i/(len(vals)-1)*w:.1f},{hgt-(v-lo)/rng*hgt:.1f}" for i, v in enumerate(vals)]
        pts = " ".join(coords)
    rows_html = ""
    for i, r in enumerate(lb, 1):
        cls = "pos" if r["pnl"] >= 0 else "neg"
        medal = {1: "🥇", 2: "🥈", 3: "🥉"}.get(i, str(i))
        b = st["bots"][r["id"]]
        inv = sum(p["qty"] if "legs" in p else p["qty"] * p["entry"]
                  for p in b["positions"].values())
        rows_html += (f"<tr><td>{medal}</td><td><b>{r['id']}</b><br><span class='sub'>{r['name']}</span></td>"
                      f"<td class='num'>Rs {r['equity']:,.2f}</td>"
                      f"<td class='num {cls}'>{r['pnl']:+,.2f}</td>"
                      f"<td class='num {cls}'>{r['pct']:+.2f}%</td>"
                      f"<td class='num'>{r['trades']}</td><td class='num'>{r['win_rate']:.0f}%</td>"
                      f"<td class='num'>{r['open']}</td><td class='num'>Rs {inv:,.0f}</td></tr>")
    # ---- holdings: kon bot kothay koto invest koreche ----
    names = {b["id"]: b["name"] for b in BOTS}
    hold_html = ""
    for sid in sorted(st["bots"]):
        b = st["bots"][sid]
        pos = list(b["positions"].items())
        inv = sum(p["qty"] if "legs" in p else p["qty"] * p["entry"] for _, p in pos)
        hold_html += (f"<div class='hold'><div class='hhead'><b>{sid} · {names[sid]}</b>"
                      f"<span class='sub'>invested Rs {inv:,.0f} · cash Rs {b['cash']:,.0f}</span></div>")
        if not pos:
            hold_html += "<div class='sub' style='padding:6px 0'>kono open position nei — puro cash-e</div></div>"
            continue
        hold_html += ("<table><thead><tr><th>Symbol</th><th>Side</th><th>Qty</th><th>Entry</th>"
                      "<th>Invested</th><th>Now</th><th>Value</th><th>Unrealized</th><th>Since</th></tr></thead><tbody>")
        for sym, p in pos:
            px = b["last_prices"].get(sym, p["entry"])
            is_pair = "legs" in p
            inv_p = p["qty"] if is_pair else p["qty"] * p["entry"]
            upnl = p["side"] * p["qty"] * (px - p["entry"])
            val = p["qty"] + upnl if is_pair else p["qty"] * px
            ucls = "pos" if upnl >= 0 else "neg"
            entry_txt = f"{p['entry']:.3f} <span class='sub'>(spread)</span>" if is_pair else f"Rs {p['entry']:,.2f}"
            now_txt = f"{px:.3f}" if is_pair else f"Rs {px:,.2f}"
            hold_html += (f"<tr><td><b>{sym}</b></td>"
                          f"<td class='{'pos' if p['side'] > 0 else 'neg'}'>{'LONG' if p['side'] > 0 else 'SHORT'}</td>"
                          f"<td class='num'>{p['qty']:.4f}</td><td class='num'>{entry_txt}</td>"
                          f"<td class='num'>Rs {inv_p:,.0f}</td><td class='num'>{now_txt}</td>"
                          f"<td class='num'>Rs {val:,.0f}</td>"
                          f"<td class='num {ucls}'>{upnl:+,.2f}</td>"
                          f"<td class='sub'>{(p.get('opened') or '—')[5:16]}</td></tr>")
        hold_html += "</tbody></table></div>"
    best = lb[0]
    html = f"""<!DOCTYPE html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Strategy Arena — {len(BOTS)} Bot Leaderboard</title>
<style>
:root{{--bg:#0d1117;--card:#161b22;--ink:#e6edf3;--mute:#8b949e;--line:#30363d;--pos:#3fb950;--neg:#f85149;--acc:#58a6ff}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--ink);font-family:system-ui,'Segoe UI',sans-serif;padding:20px}}
h1{{font-size:22px;margin:0 0 4px}}.sub{{color:var(--mute);font-size:12px}}
.top{{display:flex;gap:16px;flex-wrap:wrap;margin:16px 0}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:14px 18px;min-width:150px}}
.kpi .v{{font-size:24px;font-weight:700}}.kpi .l{{color:var(--mute);font-size:12px}}
.pos{{color:var(--pos)}}.neg{{color:var(--neg)}}
table{{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:10px;overflow:hidden}}
th,td{{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);font-size:14px}}
th{{color:var(--mute);font-size:12px;text-transform:uppercase}}
.num{{font-variant-numeric:tabular-nums;text-align:right}}
svg{{width:100%;height:90px;background:var(--card);border:1px solid var(--line);border-radius:10px;margin:12px 0}}
.best{{background:var(--card);border:1px solid var(--acc);border-radius:10px;padding:14px 18px;margin:12px 0}}
.hold{{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;margin:10px 0}}
.hhead{{display:flex;justify-content:space-between;gap:10px;flex-wrap:wrap;margin-bottom:6px}}
.hold table{{border:none;margin:0}}
h2{{font-size:17px;margin:22px 0 8px}}
footer{{color:var(--mute);font-size:12px;margin-top:16px}}
</style></head><body>
<h1>📊 Strategy Arena — {len(BOTS)} Autonomous Paper Bots</h1>
<div class="sub">Each bot Rs 10,000 start · total Rs {start:,.0f} · auto-refresh 60s · {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}</div>
<div class="top">
<div class="kpi"><div class="v">Rs {total:,.2f}</div><div class="l">Total equity</div></div>
<div class="kpi"><div class="v {'pos' if total>=start else 'neg'}">{(total/start-1)*100:+.2f}%</div><div class="l">Overall return</div></div>
<div class="kpi"><div class="v">{best['id']}</div><div class="l">Leading bot</div></div>
</div>
<svg viewBox="0 0 600 90" preserveAspectRatio="none"><polyline points="{pts}" fill="none" stroke="var(--acc)" stroke-width="2"/></svg>
<div class="best">🏆 <b>Best so far: {best['id']} — {best['name']}</b> · {best['pct']:+.2f}% · Rs {best['pnl']:+,.2f}</div>
<table><thead><tr><th>#</th><th>Bot / Strategy</th><th>Equity</th><th>P&L</th><th>%</th><th>Trades</th><th>Win%</th><th>Open</th><th>Invested</th></tr></thead>
<tbody>{rows_html}</tbody></table>
<h2>💼 Holdings — kon bot kothay koto invest koreche</h2>
{hold_html}
<footer>Paper trading only — no real money. Data: Yahoo (delayed). Fees 0.1%/side. Not financial advice.</footer>
</body></html>"""
    with open(DASH_F, "w", encoding="utf-8") as f:
        f.write(html)

# ---------------- backtest (daily strategies, simplified) ----------------
def backtest():
    log("=== ARENA BACKTEST (daily strategies on available history) ===")
    fx = get_usdinr()
    results = {}
    # generic daily long-only backtest helper for s02/s05 style
    def bt_daily(sym, entry_fn, exit_fn, capital=CAPITAL, max_pos=1):
        kl = get_candles(sym, "1d", "2y")
        if is_usd(sym):
            to_inr(kl, fx)
        if len(kl) < 220:
            return None
        c = [k["c"] for k in kl]
        cash, pos, trades = capital, None, []
        for i in range(210, len(kl)):
            px = c[i]
            if pos:
                pos["bars"] += 1
                if exit_fn(c, i, pos) or px <= pos["stop"]:
                    pnl = pos["qty"] * (px - pos["entry"]) - pos["qty"] * (px + pos["entry"]) * FEE
                    cash += pnl; trades.append(pnl); pos = None
            elif entry_fn(c, i):
                qty = (cash * 0.95) / px
                pos = {"entry": px, "qty": qty, "stop": px * 0.93, "bars": 0}
                cash -= qty * px * FEE
        eq = cash + (pos["qty"] * c[-1] if pos else 0)
        wins = sum(1 for t in trades if t > 0)
        return {"sym": sym, "equity": eq, "pnl": eq - capital, "trades": len(trades),
                "win_rate": wins / len(trades) * 100 if trades else 0}
    # s05 RSI2 on SPY
    def s05_entry(c, i):
        s200 = sma(c[:i+1], 200)[-1]; r2 = rsi(c[:i+1], 2)[-1]
        return s200 and c[i] > s200 and r2 is not None and r2 < 5
    def s05_exit(c, i, pos):
        s5 = sma(c[:i+1], 5)[-1]
        return (s5 and c[i] > s5) or pos["bars"] >= 7
    for sym in ["SPY", "QQQ"]:
        r = bt_daily(sym, s05_entry, s05_exit)
        if r: results[f"s05:{sym}"] = r
    # s02 mean reversion on AAPL
    def s02_entry(c, i):
        s200 = sma(c[:i+1], 200)[-1]; _, lo, _ = bollinger(c[:i+1], 20, 2.5)
        return s200 and c[i] > s200 and lo[-1] and c[i] < lo[-1]
    def s02_exit(c, i, pos):
        r2 = rsi(c[:i+1], 2)[-1]; s5 = sma(c[:i+1], 5)[-1]
        return (r2 is not None and r2 > 50) or (s5 and c[i] > s5) or pos["bars"] >= 10
    for sym in ["AAPL", "MSFT"]:
        r = bt_daily(sym, s02_entry, s02_exit)
        if r: results[f"s02:{sym}"] = r
    for k, r in results.items():
        log(f"  {k}: equity Rs {r['equity']:,.2f}  pnl {r['pnl']:+,.2f}  "
            f"trades {r['trades']}  win {r['win_rate']:.0f}%")
    with open(os.path.join(BASE, "arena_backtest.json"), "w") as f:
        json.dump(results, f, indent=1)
    log("backtest saved -> arena_backtest.json")

# ---------------- main ----------------
# ---------------- serve mode: always-on web service (Render free + UptimeRobot keep-alive) ----------------
import subprocess, threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SERVE_CYCLE_SEC = 60      # 1-minute trading loop
SYNC_EVERY = 5            # push state/dashboard to GitHub every N cycles (~5 min)
REPO_URL = "https://github.com/tusharghosh66-debug/night-trader.git"
SERVE_FILES = ["arena_state.json", "arena_trades.csv", "arena_report.md", "arena_dashboard.html"]
SERVE_STATUS = {"started": None, "last_cycle": None, "cycles": 0,
                "equity": 0.0, "sync": "not-run", "last_sync": None}

def _auth_url():
    token = os.environ.get("GITHUB_TOKEN", "").strip()
    if token:
        return REPO_URL.replace("https://", f"https://x-access-token:{token}@")
    return REPO_URL

def _git(*args, timeout=120):
    r = subprocess.run(["git", "-C", BASE] + list(args),
                       capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError((r.stderr or r.stdout).strip()[:300])
    return r.stdout

def git_sync():
    """Commit state files and push to GitHub so Pages dashboard + daily verdict stay fresh.
    Without GITHUB_TOKEN the loop still trades, but state is local-only (lost on restart)."""
    if not os.environ.get("GITHUB_TOKEN", "").strip():
        SERVE_STATUS["sync"] = "no-token (state local only)"
        return
    try:
        _git("config", "user.email", "arena-bot@users.noreply.github.com")
        _git("config", "user.name", "strategy-arena")
        _git("add", *SERVE_FILES)
        r = subprocess.run(["git", "-C", BASE, "diff", "--cached", "--quiet"],
                           capture_output=True)
        if r.returncode != 0:
            _git("commit", "-m",
                 f"arena serve {datetime.now(IST).strftime('%Y-%m-%dT%H:%M:%S%z')} [skip ci][skip render]")
        # arena.log is tracked but never committed from serve mode; restore so rebase is clean
        subprocess.run(["git", "-C", BASE, "checkout", "--", "arena.log"],
                       capture_output=True)
        _git("pull", "--rebase", _auth_url(), "main")
        _git("push", _auth_url(), "HEAD:main")
        SERVE_STATUS["sync"] = "ok"
        SERVE_STATUS["last_sync"] = datetime.now(IST).isoformat()
    except Exception as e:
        SERVE_STATUS["sync"] = f"error: {e}"
        log(f"git sync failed: {e}")

def serve_boot():
    """Pull latest state from GitHub before trading (Render disk is ephemeral)."""
    if os.path.isdir(os.path.join(BASE, ".git")):
        try:
            subprocess.run(["git", "-C", BASE, "checkout", "--", "arena.log"],
                           capture_output=True)
            _git("pull", "--rebase", _auth_url(), "main")
            log("serve boot: pulled latest state from GitHub")
        except Exception as e:
            log(f"serve boot pull failed (using local state): {e}")

class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"status": "ok", **SERVE_STATUS}, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)
    def log_message(self, *a):
        pass

def serve_loop():
    st = load_state()
    SERVE_STATUS["started"] = datetime.now(IST).isoformat()
    n = 0
    while True:
        t0 = time.time()
        try:
            run_cycle(st)
            n += 1
            SERVE_STATUS.update(
                cycles=n, last_cycle=datetime.now(IST).isoformat(),
                equity=round(sum(bot_equity(st["bots"][b["id"]]) for b in BOTS), 2))
            if n % SYNC_EVERY == 0:
                git_sync()
        except Exception as e:
            log(f"serve cycle error: {e}\n{traceback.format_exc()}")
        time.sleep(max(5, SERVE_CYCLE_SEC - (time.time() - t0)))

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "once"
    if mode == "serve":
        serve_boot()
        port = int(os.environ.get("PORT", "10000"))
        srv = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        log(f"serve mode: HTTP on :{port}, cycle every {SERVE_CYCLE_SEC}s, "
            f"GitHub sync every {SYNC_EVERY} cycles")
        serve_loop()
        return
    if mode == "backtest":
        backtest(); return
    if mode == "dashboard":
        write_dashboard(load_state()); print("dashboard written"); return
    if mode == "report":
        write_report(load_state()); print(open(REPORT_F, encoding="utf-8").read()); return
    st = load_state()
    if mode == "once":
        run_cycle(st); return
    if mode == "run":
        while True:
            try:
                run_cycle(st)
            except Exception as e:
                log(f"cycle error: {e}\n{traceback.format_exc()}")
            time.sleep(CYCLE_SEC)

if __name__ == "__main__":
    main()
