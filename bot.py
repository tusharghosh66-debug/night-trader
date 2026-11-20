"""
Night Trader v2 — autonomous PAPER-trading bot, 3 markets (no real money, no keys).
  crypto : BTC/ETH/SOL (Binance public API, 24/7)                 $10,000
  india  : RELIANCE/TCS/HDFCBANK/INFY (Yahoo/NSE, 9:15-15:30 IST) Rs 5,00,000
  us     : AAPL/MSFT/NVDA/GOOGL (Yahoo, 9:30-16:00 ET)            $10,000
Strategy per market: 20-bar breakout + EMA20>EMA50 + RSI 50-75, index>EMA200
regime filter, per-market news sentiment gate, 2xATR stop / 3xATR trail,
1% equity risk per trade, max 3 positions, 30% notional cap, 0.1% fees.
Modes:  python bot.py backtest | run | report
"""
import json, csv, time, sys, os, re, traceback
from datetime import datetime, timezone, timedelta
from zoneinfo import ZoneInfo
from urllib.request import urlopen, Request
from urllib.parse import quote_plus

BASE = os.path.dirname(os.path.abspath(__file__))
STATE_F = os.path.join(BASE, "state.json")
TRADES_F = os.path.join(BASE, "trades.csv")
LOG_F = os.path.join(BASE, "decisions.log")
REPORT_F = os.path.join(BASE, "report.md")
NEWS_F = os.path.join(BASE, "news.json")

IST = timezone(timedelta(hours=5, minutes=30))
ET = ZoneInfo("America/New_York")
RISK_PER_TRADE = 0.01
MAX_POSITIONS = 3
MAX_NOTIONAL_PCT = 0.30
ATR_STOP_MULT = 2.0
ATR_TRAIL_MULT = 3.0
FEE = 0.001
CYCLE_SEC = 5 * 60
NEWS_EVERY_SEC = 60 * 60

MARKETS = {
    "crypto": {"source": "yahoo", "symbols": ["BTC-USD", "ETH-USD", "SOL-USD"],
               "regime": "BTC-USD", "ccy": "₹", "capital": 10000.0, "min_cost": 100, "usd_priced": True,
               "feeds": ["https://www.coindesk.com/arc/outboundfeeds/rss/",
                         "https://cointelegraph.com/rss",
                         "https://decrypt.co/feed",
                         "https://bitcoinmagazine.com/.rss/full/"],
               "gnews": ["bitcoin OR ethereum OR cryptocurrency market",
                         "crypto regulation OR bitcoin ETF OR crypto crash OR crypto rally"],
               "gnews_ed": ("en-IN", "IN", "IN:en")},
    "india": {"source": "yahoo", "symbols": ["RELIANCE.NS", "TCS.NS", "HDFCBANK.NS", "INFY.NS"],
              "regime": "^NSEI", "ccy": "\u20b9", "capital": 10000.0, "min_cost": 100,
              "feeds": ["https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms",
                        "https://www.livemint.com/rss/markets"],
              "gnews": ["NSE OR BSE OR sensex OR nifty",
                        "indian stock market OR RBI OR SEBI OR reliance OR TCS OR infosys",
                        "india economy OR GDP OR inflation OR rupee",
                        "site:moneycontrol.com OR site:business-standard.com market"],
              "gnews_ed": ("en-IN", "IN", "IN:en")},
    "us": {"source": "yahoo", "symbols": ["AAPL", "MSFT", "NVDA", "GOOGL"],
           "regime": "^GSPC", "ccy": "₹", "capital": 10000.0, "min_cost": 100, "usd_priced": True,
           "feeds": ["https://www.cnbc.com/id/100003114/device/rss/rss.html",
                     "https://feeds.marketwatch.com/marketwatch/topstories/"],
           "gnews": ["US stock market OR nasdaq OR S&P 500 OR dow jones",
                     "federal reserve OR wall street OR US economy OR interest rates",
                     "apple OR microsoft OR nvidia OR google stock",
                     "site:reuters.com OR site:bloomberg.com stocks OR market"],
           "gnews_ed": ("en-US", "US", "US:en")},
}

BULL_WORDS = ["surge", "rally", "bull", "soar", "jump", "gain", "record high", "all-time high",
              "ath", "etf inflow", "inflow", "approval", "approve", "adopt", "buy", "accumul",
              "upgrade", "breakout", "rebound", "recover", "rate cut", "dovish", "stimulus",
              "beat estimates", "beats estimates", "strong earnings", "order win", "expansion"]
BEAR_WORDS = ["crash", "plunge", "dump", "bear", "selloff", "sell-off", "drop", "fall", "tumble",
              "hack", "exploit", "lawsuit", "ban", "sec sues", "outflow", "liquidation", "fear",
              "rate hike", "hawkish", "recession", "bankrupt", "insolven", "fraud", "warning",
              "misses estimates", "miss estimates", "weak earnings", "layoff", "downgrade",
              "tariff", "probe", "penalty"]


def log(msg):
    line = f"[{datetime.now(IST).strftime('%Y-%m-%d %H:%M:%S IST')}] {msg}"
    print(line, flush=True)
    with open(LOG_F, "a", encoding="utf-8") as f:
        f.write(line + "\n")


def http_json(url, timeout=25):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (night-trader-bot)"})
    with urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_text(url, timeout=25):
    req = Request(url, headers={"User-Agent": "Mozilla/5.0 (night-trader-bot)"})
    with urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def get_klines(source, symbol, limit=300):
    """Closed 1h candles, oldest first."""
    if source == "binance":
        raw = http_json(f"https://api.binance.com/api/v3/klines?symbol={symbol}&interval=1h&limit={limit + 1}")
        out = [{"t": int(k[0]), "o": float(k[1]), "h": float(k[2]),
                "l": float(k[3]), "c": float(k[4]), "v": float(k[5])} for k in raw[:-1]]
        return out[-limit:]
    sym = symbol.replace("^", "%5E")
    d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1h&range=6mo")
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
    return out[:-1][-limit:]          # drop last (possibly open) bar


def market_open(mkt):
    if mkt == "crypto":
        return True
    if mkt == "india":
        n = datetime.now(IST)
        return n.weekday() < 5 and (9, 15) <= (n.hour, n.minute) < (15, 30)
    n = datetime.now(ET)
    return n.weekday() < 5 and (9, 30) <= (n.hour, n.minute) < (16, 0)


def get_usdinr(st=None):
    """Live USD->INR rate (Yahoo INR=X); fallback to last known / 88."""
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


def get_live_price(source, symbol, fx=1.0):
    """Latest trade price (Binance real-time; Yahoo ~15-min delayed on free tier)."""
    if source == "binance":
        d = http_json(f"https://api.binance.com/api/v3/ticker/price?symbol={symbol}")
        return float(d["price"]) * fx
    sym = symbol.replace("^", "%5E")
    d = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{sym}?interval=1d&range=1d")
    return float(d["chart"]["result"][0]["meta"]["regularMarketPrice"]) * fx


# ---------------- indicators ----------------
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


def indicators(kl):
    c = [x["c"] for x in kl]
    ind = {"ema20": ema(c, 20), "ema50": ema(c, 50), "ema200": ema(c, 200),
           "rsi": rsi(c), "atr": atr(kl)}
    ind["_closes"] = c
    ind["_highs"] = [x["h"] for x in kl]
    return ind


# ---------------- news (per market) ----------------
def score_titles(items):
    score, hits = 0.0, []
    for t in items:
        tl = t.lower()
        s = sum(1 for w in BULL_WORDS if w in tl) - sum(1 for w in BEAR_WORDS if w in tl)
        if s != 0:
            hits.append((s, t))
        score += s
    norm = max(-1.0, min(1.0, score / max(1, len(items)) * 3))
    return round(norm, 3), [t for _, t in sorted(hits, key=lambda x: -abs(x[0]))[:8]]


def fetch_news():
    """Aggregate: direct RSS feeds + Google News search (whole-internet coverage),
    dedupe headlines, blend crypto Fear & Greed index into sentiment."""
    data = {"ts": datetime.now(IST).isoformat()}
    for mkt, cfg in MARKETS.items():
        urls = list(cfg["feeds"])
        hl, gl, ceid = cfg.get("gnews_ed", ("en-US", "US", "US:en"))
        for q in cfg.get("gnews", []):
            urls.append(f"https://news.google.com/rss/search?q={quote_plus(q)}&hl={hl}&gl={gl}&ceid={ceid}")
        items, seen, n_sources = [], set(), 0
        for u in urls:
            try:
                xml = http_text(u)
                titles = re.findall(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", xml, re.S)
                got = 0
                for t in titles[1:41]:
                    t = re.sub(r"\s+", " ", t.replace("]]>", "")).strip()
                    key = re.sub(r"[^a-z0-9]", "", t.lower())[:80]
                    if t and key and key not in seen:
                        seen.add(key)
                        items.append(t)
                        got += 1
                if got:
                    n_sources += 1
            except Exception as e:
                log(f"news feed failed ({mkt}) {u[:70]}: {e}")
        sc, top = score_titles(items)
        entry = {"score": sc, "n_headlines": len(items), "n_sources": n_sources,
                 "top": top, "headlines": items[:60]}
        if mkt == "crypto":
            try:
                fng = int(http_json("https://api.alternative.me/fng/?limit=1")["data"][0]["value"])
                entry["fear_greed"] = fng
                entry["score"] = round(max(-1.0, min(1.0, 0.7 * sc + 0.3 * (fng - 50) / 50)), 3)
            except Exception as e:
                log(f"fear&greed fetch failed: {e}")
        data[mkt] = entry
        log(f"NEWS {mkt}: {len(items)} unique headlines from {n_sources} sources, "
            f"sentiment={entry['score']:+.2f}" + (f", F&G={entry.get('fear_greed')}" if mkt == "crypto" else ""))
    with open(NEWS_F, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=1)
    return data


def load_news(mkt):
    try:
        with open(NEWS_F, encoding="utf-8") as f:
            d = json.load(f)
        ts = datetime.fromisoformat(d.get("ts", ""))
        age = (datetime.now(IST) - ts).total_seconds()
        if age < 3 * 3600 and mkt in d:
            return d[mkt]
    except Exception:
        pass
    return {"score": 0.0, "top": [], "n_headlines": 0}


def news_age_sec():
    try:
        with open(NEWS_F, encoding="utf-8") as f:
            d = json.load(f)
        return (datetime.now(IST) - datetime.fromisoformat(d["ts"])).total_seconds()
    except Exception:
        return 1e9


# ---------------- state ----------------
def new_market_state(capital):
    return {"cash": capital, "start_equity": capital, "positions": {},
            "closed_trades": [], "cycles": 0, "last_prices": {}}


def new_state():
    return {"created": datetime.now(IST).isoformat(), "version": 3,
            "markets": {m: new_market_state(c["capital"]) for m, c in MARKETS.items()}}


def load_state():
    try:
        with open(STATE_F, encoding="utf-8") as f:
            st = json.load(f)
        if "markets" not in st:                      # migrate v1 (crypto-only)
            old = {k: st[k] for k in ("cash", "start_equity", "positions",
                                      "closed_trades", "cycles", "last_prices") if k in st}
            ms = new_market_state(MARKETS["crypto"]["capital"]); ms.update(old)
            st = {"created": st.get("created", datetime.now(IST).isoformat()),
                  "markets": {"crypto": ms,
                              "india": new_market_state(MARKETS["india"]["capital"]),
                              "us": new_market_state(MARKETS["us"]["capital"])}}
            log("state migrated v1 -> v2 (3 markets)")
            save_state(st)
        for m, c in MARKETS.items():
            st["markets"].setdefault(m, new_market_state(c["capital"]))
        if st.get("version") != 3:
            st["markets"] = {m: new_market_state(c["capital"]) for m, c in MARKETS.items()}
            st["equity_history"] = []
            st["version"] = 3
            log("state reset v3: INR 10,000 per market (total 30,000)")
            save_state(st)
        return st
    except Exception:
        return new_state()


def save_state(st):
    tmp = STATE_F + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=1)
    os.replace(tmp, STATE_F)


def equity(ms):
    e = ms["cash"]
    for sym, p in ms["positions"].items():
        e += p["qty"] * ms["last_prices"].get(sym, p["entry"])
    return e


def record_trade(mkt, ms, row):
    ms["closed_trades"].append(row)
    new = not os.path.exists(TRADES_F)
    with open(TRADES_F, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["time", "market", "symbol", "side", "qty", "price", "pnl", "reason", "equity_after"])
        w.writerow(row)


# ---------------- strategy ----------------
def signal(ind, i, regime_ok, news_score):
    e20, e50, r, a = ind["ema20"][i], ind["ema50"][i], ind["rsi"][i], ind["atr"][i]
    if None in (e20, e50, r, a) or i < 20 or not regime_ok or news_score < -0.35:
        return None
    hh = max(ind["_highs"][i - 20:i])
    if e20 > e50 and ind["_closes"][i] > hh and 50 <= r <= 75:
        return "buy"
    return None


def size_position(eq, price, a, news_score):
    stop_dist = ATR_STOP_MULT * a
    if stop_dist <= 0:
        return 0.0
    qty = (eq * RISK_PER_TRADE) / stop_dist
    if news_score < -0.15:
        qty *= 0.5
    return min(qty, (eq * MAX_NOTIONAL_PCT) / price)


def backtest():
    log("=== BACKTEST v3 (INR, 1h candles, ~6mo stocks / ~1000h crypto) ===")
    results = {}
    fx = get_usdinr()
    log(f"USDINR = {fx:.2f}")
    for mkt, cfg in MARKETS.items():
        try:
            rkl = get_klines(cfg["source"], cfg["regime"], 1000)
            if cfg.get("usd_priced"):
                to_inr(rkl, fx)
            rind = indicators(rkl)
        except Exception as e:
            log(f"{mkt}: regime fetch failed {e}")
            continue
        for sym in cfg["symbols"]:
            try:
                kl = get_klines(cfg["source"], sym, 1000)
                if cfg.get("usd_priced"):
                    to_inr(kl, fx)
                ind = indicators(kl)
                cash, pos, trades, peak = cfg["capital"], None, [], 0.0
                start_i = min(200, max(20, len(kl) - 50))
                for i in range(start_i, len(kl)):
                    px, a = kl[i]["c"], ind["atr"][i]
                    j = len(rkl) - (len(kl) - i)     # align regime from the end
                    regime_ok = (0 <= j < len(rkl) and rind["ema200"][j] is not None
                                 and rkl[j]["c"] > rind["ema200"][j])
                    if pos:
                        peak = max(peak, px)
                        stop = max(pos["stop"], peak - ATR_TRAIL_MULT * a)
                        why = None
                        if px <= stop:
                            why = "stop"
                        elif ind["ema50"][i] and px < ind["ema50"][i]:
                            why = "ema50"
                        elif ind["rsi"][i] and ind["rsi"][i] > 78:
                            why = "rsi"
                        if why:
                            pr = pos["qty"] * px * (1 - FEE)
                            trades.append(pr - pos["cost"]); cash += pr; pos = None
                    if pos is None and a and signal(ind, i, regime_ok, 0.0) == "buy":
                        qty = size_position(cash, px, a, 0.0)
                        cost = qty * px * (1 + FEE)
                        if cost >= cfg["min_cost"] and cost <= cash:
                            cash -= cost
                            pos = {"qty": qty, "cost": cost, "stop": px - ATR_STOP_MULT * a}
                            peak = px
                final = cash + (pos["qty"] * kl[-1]["c"] if pos else 0)
                w = sum(1 for t in trades if t > 0)
                key = f"{mkt}:{sym}"
                results[key] = {"trades": len(trades), "wins": w,
                                "pnl": round(final - cfg["capital"], 2),
                                "return_pct": round((final / cfg["capital"] - 1) * 100, 2)}
                log(f"{key}: {results[key]}")
            except Exception as e:
                log(f"{mkt}:{sym} backtest error: {e}")
    with open(os.path.join(BASE, "backtest.json"), "w") as f:
        json.dump(results, f, indent=1)
    return results


# ---------------- live paper cycle ----------------
def market_cycle(mkt, ms, fx):
    cfg = MARKETS[mkt]
    news = load_news(mkt)
    ns = news.get("score", 0.0)
    try:
        rkl = get_klines(cfg["source"], cfg["regime"], 300)
        if cfg.get("usd_priced"):
            to_inr(rkl, fx)
        rind = indicators(rkl)
        regime_ok = rind["ema200"][-1] is not None and rkl[-1]["c"] > rind["ema200"][-1]
    except Exception as e:
        log(f"{mkt} regime fetch failed: {e}")
        regime_ok = True
    for sym in cfg["symbols"]:
        try:
            kl = get_klines(cfg["source"], sym, 300)
            if cfg.get("usd_priced"):
                to_inr(kl, fx)
            ind = indicators(kl)
            i = len(kl) - 1
            px_candle, a = kl[i]["c"], ind["atr"][i]
            try:
                px = get_live_price(cfg["source"], sym, fx if cfg.get("usd_priced") else 1.0)
            except Exception:
                px = px_candle
            ms["last_prices"][sym] = px
            e20, e50, r = ind["ema20"][i], ind["ema50"][i], ind["rsi"][i]
            if sym in ms["positions"]:
                p = ms["positions"][sym]
                p["peak"] = max(p.get("peak", p["entry"]), px)
                trail = p["peak"] - ATR_TRAIL_MULT * a if a else p["stop"]
                stop = max(p["stop"], trail)
                reason = None
                if px <= stop:
                    reason = f"stop/trail hit ({px:.2f}<={stop:.2f})"
                elif e50 and px < e50:
                    reason = "trend lost: close < EMA50"
                elif r and r > 78:
                    reason = f"RSI {r:.0f} overbought take-profit"
                if reason:
                    proceeds = p["qty"] * px * (1 - FEE)
                    pnl = proceeds - p["cost"]
                    ms["cash"] += proceeds
                    row = [datetime.now(IST).isoformat(), mkt, sym, "SELL", round(p["qty"], 6),
                           round(px, 2), round(pnl, 2), reason, round(equity(ms), 2)]
                    record_trade(mkt, ms, row)
                    del ms["positions"][sym]
                    log(f"SELL [{mkt}] {sym} qty={p['qty']:.4f} @ {px:.2f} pnl={pnl:+.2f} — {reason}")
                else:
                    log(f"HOLD [{mkt}] {sym} px={px:.2f} stop={stop:.2f} rsi={r:.0f}")
            if sym not in ms["positions"] and len(ms["positions"]) < MAX_POSITIONS and a:
                if signal(ind, i, regime_ok, ns) == "buy":
                    eq = equity(ms)
                    qty = size_position(eq, px, a, ns)
                    cost = qty * px * (1 + FEE)
                    if cost >= cfg["min_cost"] and cost <= ms["cash"]:
                        ms["cash"] -= cost
                        ms["positions"][sym] = {"qty": qty, "entry": px, "cost": cost,
                                                "stop": px - ATR_STOP_MULT * a, "peak": px,
                                                "opened": datetime.now(IST).isoformat()}
                        row = [datetime.now(IST).isoformat(), mkt, sym, "BUY", round(qty, 6),
                               round(px, 2), 0.0,
                               f"breakout+EMA trend, RSI={r:.0f}, regime_ok={regime_ok}, news={ns:+.2f}",
                               round(equity(ms), 2)]
                        record_trade(mkt, ms, row)
                        log(f"BUY [{mkt}] {sym} qty={qty:.4f} @ {px:.2f} "
                            f"stop={px - ATR_STOP_MULT * a:.2f} (rsi={r:.0f}, news={ns:+.2f})")
                else:
                    log(f"WAIT [{mkt}] {sym} px={px:.2f} rsi={r and round(r)} regime_ok={regime_ok}")
        except Exception as e:
            log(f"[{mkt}] {sym} cycle error: {e}")
    ms["cycles"] += 1


def write_report(st):
    lines = ["# Night Trader v2 — Paper Trading Report (3 markets)",
             f"_Updated: {datetime.now(IST).strftime('%Y-%m-%d %H:%M IST')}_", ""]
    for mkt, cfg in MARKETS.items():
        ms = st["markets"][mkt]
        eq = equity(ms)
        pnl = eq - ms["start_equity"]
        c = cfg["ccy"]
        status = "OPEN" if market_open(mkt) else "CLOSED"
        lines += [f"## {mkt.upper()} ({status}) — {c}{eq:,.2f} | P&L {pnl:+,.2f} ({pnl / ms['start_equity'] * 100:+.2f}%)",
                  f"Cash {c}{ms['cash']:,.2f} | cycles {ms['cycles']} | capital {c}{ms['start_equity']:,.0f}"]
        if ms["positions"]:
            lines.append("| Symbol | Qty | Entry | Last | Unrealized |")
            lines.append("|---|---|---|---|---|")
            for sym, p in ms["positions"].items():
                last = ms["last_prices"].get(sym, p["entry"])
                lines.append(f"| {sym} | {p['qty']:.4f} | {p['entry']:.2f} | {last:.2f} | {p['qty'] * last - p['cost']:+.2f} |")
        else:
            lines.append("_No open positions — all cash._")
        ct = ms["closed_trades"]
        if ct:
            wins = sum(1 for r in ct if r[6] > 0)
            lines.append(f"Closed: {len(ct)} ({wins} wins), realized {sum(r[6] for r in ct):+,.2f}")
            for r in ct[-5:]:
                lines.append(f"- {r[0][11:16]} {r[3]} {r[2]} @ {r[5]} pnl {r[6]:+.2f} ({r[7]})")
        news = load_news(mkt)
        extra = f", Fear&Greed {news['fear_greed']}" if "fear_greed" in news else ""
        lines.append(f"News sentiment: {news.get('score', 0):+.2f} "
                     f"({news.get('n_headlines', 0)} headlines, {news.get('n_sources', '?')} sources{extra})")
        for t in news.get("top", [])[:3]:
            lines.append(f"- {t}")
        lines.append("")
    lines += ["Market hours: crypto 24/7 | NSE 9:15–15:30 IST Mon–Fri | US 9:30–16:00 ET (8:00 PM–2:30 AM IST) Mon–Fri.",
              "> PAPER TRADING ONLY — simulated capital, no real orders placed."]
    with open(REPORT_F, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))


def write_dashboard(st):
    hist = st.get("equity_history", [])
    css = """
    <style>
      .nt-wrap { display:flex; flex-direction:column; gap:14px; padding:4px 0; }
      .nt-head { display:flex; align-items:baseline; gap:10px; flex-wrap:wrap; }
      .nt-head h2 { margin:0; font-size:1.15em; }
      .nt-sub { color:var(--muted-foreground); font-size:.8em; }
      .nt-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(240px,1fr)); gap:10px; }
      .nt-card { border:1px solid var(--border); border-radius:10px; padding:12px; background:var(--card); }
      .nt-card h3 { margin:0 0 6px; font-size:.95em; display:flex; justify-content:space-between; align-items:center; }
      .nt-badge { font-size:.65em; padding:2px 8px; border-radius:99px; border:1px solid var(--border); color:var(--muted-foreground); }
      .nt-badge.open { color:#22c55e; border-color:#22c55e66; }
      .nt-eq { font-size:1.5em; font-weight:700; }
      .nt-pnl { font-size:.85em; font-weight:600; }
      .up { color:#22c55e; } .dn { color:#ef4444; }
      .nt-meta { color:var(--muted-foreground); font-size:.75em; margin-top:4px; }
      table.nt-t { width:100%; border-collapse:collapse; font-size:.78em; margin-top:8px; }
      .nt-t th { text-align:left; color:var(--muted-foreground); font-weight:500; padding:3px 6px; border-bottom:1px solid var(--border); }
      .nt-t td { padding:3px 6px; border-bottom:1px solid var(--border); }
      .nt-bar { height:6px; border-radius:3px; background:var(--border); overflow:hidden; margin:4px 0; }
      .nt-bar div { height:100%; border-radius:3px; }
      .nt-news { font-size:.75em; color:var(--muted-foreground); margin:2px 0; }
      .nt-sec { font-size:.85em; font-weight:600; margin-top:4px; }
      .nt-foot { color:var(--muted-foreground); font-size:.72em; }
      svg.nt-spark { width:100%; height:48px; display:block; }
    </style>
    """
    now = datetime.now(IST).strftime("%d %b %Y, %H:%M IST")
    cards = []
    for mkt, cfg in MARKETS.items():
        ms = st["markets"][mkt]
        eq = equity(ms); pnl = eq - ms["start_equity"]
        pct = pnl / ms["start_equity"] * 100
        c = cfg["ccy"]; cls = "up" if pnl >= 0 else "dn"
        op = market_open(mkt)
        badge = '<span class="nt-badge open">OPEN</span>' if op else '<span class="nt-badge">CLOSED</span>'
        news = load_news(mkt)
        sc = news.get("score", 0)
        barcol = "#22c55e" if sc >= 0 else "#ef4444"
        barw = min(100, abs(sc) * 100)
        fng = f" · Fear&amp;Greed {news['fear_greed']}" if "fear_greed" in news else ""
        pos_rows = ""
        if ms["positions"]:
            pos_rows = '<table class="nt-t"><tr><th>Symbol</th><th>Qty</th><th>Entry</th><th>Last</th><th>Unreal.</th></tr>'
            for sym, p in ms["positions"].items():
                last = ms["last_prices"].get(sym, p["entry"])
                u = p["qty"] * last - p["cost"]
                uc = "up" if u >= 0 else "dn"
                pos_rows += (f'<tr><td>{sym}</td><td>{p["qty"]:.4f}</td><td>{p["entry"]:.2f}</td>'
                             f'<td>{last:.2f}</td><td class="{uc}">{u:+.2f}</td></tr>')
            pos_rows += "</table>"
        # sparkline from equity history
        idx = list(MARKETS).index(mkt) + 1
        pts = [h[idx] for h in hist if len(h) > idx]
        spark = ""
        if len(pts) >= 2:
            lo, hi = min(pts), max(pts)
            rng = (hi - lo) or 1
            n = len(pts)
            coords = " ".join(f"{i / (n - 1) * 300:.1f},{44 - (v - lo) / rng * 40:.1f}" for i, v in enumerate(pts))
            col = "#22c55e" if pts[-1] >= pts[0] else "#ef4444"
            spark = (f'<svg class="nt-spark" viewBox="0 0 300 48" preserveAspectRatio="none">'
                     f'<polyline points="{coords}" fill="none" stroke="{col}" stroke-width="1.5"/></svg>')
        cards.append(f"""
        <div class="nt-card">
          <h3>{mkt.upper()} {badge}</h3>
          <div class="nt-eq">{c}{eq:,.2f}</div>
          <div class="nt-pnl {cls}">{pnl:+,.2f} ({pct:+.2f}%)</div>
          {spark}
          <div class="nt-meta">Cash {c}{ms['cash']:,.2f} · {len(ms['positions'])} positions · {ms['cycles']} cycles</div>
          <div class="nt-meta">News {sc:+.2f}{fng} · {news.get('n_headlines', 0)} headlines / {news.get('n_sources', '?')} sources</div>
          <div class="nt-bar"><div style="width:{barw:.0f}%;background:{barcol}"></div></div>
          {pos_rows}
        </div>""")
    # recent trades across markets
    all_tr = []
    for mkt, ms in st["markets"].items():
        for r in ms["closed_trades"][-6:]:
            all_tr.append(r)
    all_tr.sort(key=lambda r: r[0], reverse=True)
    tr_html = ""
    if all_tr:
        tr_html = '<div class="nt-sec">Recent trades</div><table class="nt-t"><tr><th>Time</th><th>Mkt</th><th>Side</th><th>Symbol</th><th>Price</th><th>PnL</th><th>Reason</th></tr>'
        for r in all_tr[:8]:
            pc = "up" if r[6] > 0 else ("dn" if r[6] < 0 else "")
            tr_html += (f'<tr><td>{r[0][11:16]}</td><td>{r[1]}</td><td>{r[3]}</td><td>{r[2]}</td>'
                        f'<td>{r[5]}</td><td class="{pc}">{r[6]:+.2f}</td><td>{r[7][:60]}</td></tr>')
        tr_html += "</table>"
    else:
        tr_html = '<div class="nt-meta">No trades yet — bot waiting for valid setups (regime + breakout + news filters).</div>'
    news_html = '<div class="nt-sec">Top headlines</div>'
    for mkt in MARKETS:
        for t in load_news(mkt).get("top", [])[:2]:
            news_html += f'<div class="nt-news">[{mkt}] {t[:130]}</div>'
    html = f"""<!DOCTYPE html><html><head><meta charset="utf-8">
<meta http-equiv="refresh" content="60">{css}</head><body>
<div class="nt-wrap">
  <div class="nt-head"><h2>📈 Night Trader</h2><span class="nt-sub">3-market paper trading · updated {now} · auto-refresh 60s</span></div>
  <div class="nt-grid">{''.join(cards)}</div>
  {tr_html}
  {news_html}
  <div class="nt-foot">PAPER TRADING ONLY — simulated capital, no real orders. Crypto 24/7 · NSE 9:15–15:30 IST · US 8:00 PM–2:30 AM IST.</div>
</div></body></html>"""
    with open(os.path.join(BASE, "dashboard.html"), "w", encoding="utf-8") as f:
        f.write(html)


def run():
    log("=== Night Trader v3 starting (PAPER: INR 10,000 x 3 markets = 30,000 total) ===")
    st = load_state()
    last_news = 0
    while True:
        try:
            if time.time() - last_news > NEWS_EVERY_SEC:
                try:
                    fetch_news()
                except Exception as e:
                    log(f"news error: {e}")
                last_news = time.time()
            fx = get_usdinr(st)
            for mkt in MARKETS:
                if market_open(mkt):
                    market_cycle(mkt, st["markets"][mkt], fx)
                else:
                    log(f"[{mkt}] market closed — skipping")
            save_state(st)
            write_report(st)
            hist = st.setdefault("equity_history", [])
            hist.append([datetime.now(IST).isoformat()] +
                        [round(equity(st["markets"][m]), 2) for m in MARKETS])
            del hist[:-672]                  # keep ~7 days at 15-min cycles
            save_state(st)
            write_dashboard(st)
        except Exception:
            log("cycle crash:\n" + traceback.format_exc())
        time.sleep(CYCLE_SEC)


def cycle_once(st=None):
    """One full cycle: news (hourly), open markets, save state/report/dashboard.
    Used by `once` mode (cloud cron fallback) and serve_all.py (Render 1-min loop)."""
    if st is None:
        st = load_state()
    try:                                   # keep decisions.log repo-friendly
        with open(LOG_F, encoding="utf-8") as f:
            lines = f.readlines()
        if len(lines) > 5000:
            with open(LOG_F, "w", encoding="utf-8") as f:
                f.writelines(lines[-3000:])
    except Exception:
        pass
    if news_age_sec() > NEWS_EVERY_SEC:
        try:
            fetch_news()
        except Exception as e:
            log(f"news error: {e}")
    fx = get_usdinr(st)
    for mkt in MARKETS:
        if market_open(mkt):
            market_cycle(mkt, st["markets"][mkt], fx)
        else:
            log(f"[{mkt}] market closed — skipping")
    hist = st.setdefault("equity_history", [])
    hist.append([datetime.now(IST).isoformat()] +
                [round(equity(st["markets"][m]), 2) for m in MARKETS])
    del hist[:-1440]                       # keep 24h at 1-min cycles
    save_state(st)
    write_report(st)
    write_dashboard(st)
    log("once: cycle complete")
    return st


if __name__ == "__main__":
    mode = sys.argv[1] if len(sys.argv) > 1 else "run"
    if mode == "backtest":
        backtest()
    elif mode == "report":
        write_report(load_state())
        print(open(REPORT_F, encoding="utf-8").read())
    elif mode == "dashboard":
        write_dashboard(load_state())
        print("dashboard.html written")
    elif mode == "once":
        # one cycle then exit — for cloud cron (GitHub Actions etc.)
        cycle_once()
    else:
        run()
