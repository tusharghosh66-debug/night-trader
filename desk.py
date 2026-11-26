# -*- coding: utf-8 -*-
"""Manual practice desk: paper-trade any listed company at the live market price.

State lives in manual_book.json next to arena_state.json, so the normal git sync
carries it between Render restarts. Prices are Yahoo quotes (same feed as the bots),
converted to INR with the live USDINR rate so one book has one currency.
"""
import io, json, os, threading, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import arena

HERE = os.path.dirname(os.path.abspath(__file__))
BOOK_FILE = os.path.join(HERE, "manual_book.json")
CO_FILE = os.path.join(HERE, "companies.json")
IST = timezone(timedelta(hours=5, minutes=30))
FEE = 0.001          # 0.1% per side, same as the bot books
START_CASH = 1000000.0
LOCK = threading.Lock()

_quotes = {}         # symbol -> (ts, payload)
_fx = {"ts": 0.0, "v": 88.0}
_companies = None


def _load_book():
    try:
        with io.open(BOOK_FILE, encoding="utf-8") as f:
            b = json.load(f)
    except Exception:
        b = {}
    b.setdefault("cash", START_CASH)
    b.setdefault("start_cash", START_CASH)
    b.setdefault("positions", {})
    b.setdefault("trades", [])
    b.setdefault("realised", 0.0)
    b.setdefault("started", datetime.now(IST).isoformat(timespec="seconds"))
    return b


BOOK = _load_book()


def _save():
    tmp = BOOK_FILE + ".tmp"
    with io.open(tmp, "w", encoding="utf-8") as f:
        json.dump(BOOK, f, indent=1, ensure_ascii=False)
    os.replace(tmp, BOOK_FILE)


def companies():
    global _companies
    if _companies is None:
        with io.open(CO_FILE, encoding="utf-8") as f:
            _companies = json.load(f)
    return _companies


def _yahoo(sym):
    s = sym.strip().upper()
    if s.endswith(".NS") or s.endswith(".BO") or "." in s:
        return s
    for c in companies():
        if c["s"] == s:
            return s if c["m"] == "US" else s + ".NS"
    return s + ".NS"


def fx():
    now = time.time()
    if now - _fx["ts"] > 600:
        try:
            _fx["v"] = float(arena.get_usdinr(None)) or _fx["v"]
            _fx["ts"] = now
        except Exception:
            pass
    return _fx["v"]


def quote(sym, max_age=20.0):
    s = sym.strip().upper()
    hit = _quotes.get(s)
    if hit and time.time() - hit[0] < max_age:
        return hit[1]
    y = _yahoo(s)
    d = arena.http_json("https://query1.finance.yahoo.com/v8/finance/chart/%s?interval=1d&range=5d" % y.replace("^", "%5E").replace("&", "%26"))
    res = d["chart"]["result"][0]
    m = res["meta"]
    px = float(m.get("regularMarketPrice") or 0)
    closes = [c for c in ((res.get("indicators") or {}).get("quote") or [{}])[0].get("close") or [] if c]
    prev = float(closes[-2]) if len(closes) > 1 else float(m.get("chartPreviousClose") or px or 0)
    try:
        asof = datetime.fromtimestamp(int(m.get("regularMarketTime") or 0), IST).strftime("%d %b %H:%M")
    except Exception:
        asof = ""
    cur = (m.get("currency") or "INR").upper()
    f = fx() if cur == "USD" else 1.0
    q = {"s": s, "y": y, "px": round(px * f, 2), "prev": round(prev * f, 2),
         "cur": cur, "inr": round(px * f, 2), "usdinr": f if cur == "USD" else None,
         "name": m.get("shortName") or m.get("longName") or s,
         "asof": asof, "state": m.get("marketState") or "", "t": int(time.time())}
    _quotes[s] = (time.time(), q)
    return q


def quotes(syms, max_age=20.0):
    syms = [s for s in syms if s][:40]
    out = {}
    if not syms:
        return out
    with ThreadPoolExecutor(max_workers=8) as ex:
        futs = {s: ex.submit(quote, s, max_age) for s in syms}
        for s, f in futs.items():
            try:
                out[s] = f.result(timeout=25)
            except Exception as e:
                out[s] = {"s": s, "error": str(e)[:80]}
    return out


def quote_many(syms):
    return quotes(syms)


def _pos_px(p):
    try:
        return quote(p["y"])["px"]
    except Exception:
        return p.get("last", p["avg"])


def state():
    pos, mtm, cost = [], 0.0, 0.0
    live = quotes(list(BOOK["positions"].keys()), max_age=60.0)
    for s, p in BOOK["positions"].items():
        px = (live.get(s) or {}).get("px") or _pos_px(p)
        val = px * p["qty"]
        mtm += val
        cost += p["avg"] * p["qty"]
        pos.append({"sym": s, "name": p.get("name", s), "qty": p["qty"], "avg": round(p["avg"], 2),
                    "px": round(px, 2), "value": round(val, 2),
                    "pnl": round(val - p["avg"] * p["qty"], 2),
                    "pnl_pct": round(100.0 * (px / p["avg"] - 1) if p["avg"] else 0.0, 2),
                    "opened": p.get("opened", "")})
    equity = BOOK["cash"] + mtm
    return {"cash": round(BOOK["cash"], 2), "start_cash": BOOK["start_cash"],
            "positions_value": round(mtm, 2), "invested": round(cost, 2),
            "equity": round(equity, 2), "pnl": round(equity - BOOK["start_cash"], 2),
            "pnl_pct": round(100.0 * (equity / BOOK["start_cash"] - 1), 3),
            "realised": round(BOOK["realised"], 2), "fee_rate": FEE,
            "positions": sorted(pos, key=lambda x: -x["value"]),
            "trades": BOOK["trades"][-40:][::-1], "started": BOOK["started"],
            "now": datetime.now(IST).strftime("%Y-%m-%d %H:%M:%S")}


def order(sym, side, qty):
    side = (side or "").upper()
    qty = int(qty or 0)
    if side not in ("BUY", "SELL"):
        return {"error": "side must be BUY or SELL"}
    if qty <= 0:
        return {"error": "quantity must be positive"}
    q = quote(sym, max_age=0.0)
    px = q["px"]
    if px <= 0:
        return {"error": "no live price for " + sym}
    key = q["s"]
    with LOCK:
        p = BOOK["positions"].get(key)
        if side == "BUY":
            cost = px * qty * (1 + FEE)
            if cost > BOOK["cash"] + 1e-6:
                return {"error": "not enough cash: need ₹%.0f, have ₹%.0f" % (cost, BOOK["cash"])}
            if p:
                tot = p["qty"] + qty
                p["avg"] = (p["avg"] * p["qty"] + px * qty) / tot
                p["qty"] = tot
                p["last"] = px
            else:
                p = {"qty": qty, "avg": px, "last": px, "name": q["name"], "y": q["y"],
                     "opened": datetime.now(IST).strftime("%Y-%m-%d %H:%M")}
                BOOK["positions"][key] = p
            BOOK["cash"] -= cost
            fill = {"sym": key, "side": "BUY", "qty": qty, "px": px, "fee": round(px * qty * FEE, 2),
                    "note": "add to position" if p["qty"] > qty else "open position"}
        else:
            if not p or p["qty"] < qty:
                return {"error": "you hold %s, cannot sell %d" % (p["qty"] if p else 0, qty)}
            proceeds = px * qty * (1 - FEE)
            BOOK["cash"] += proceeds
            BOOK["realised"] += (px - p["avg"]) * qty - px * qty * FEE - p["avg"] * qty * FEE
            p["qty"] -= qty
            if p["qty"] == 0:
                BOOK["positions"].pop(key, None)
            else:
                p["last"] = px
            fill = {"sym": key, "side": "SELL", "qty": qty, "px": px, "fee": round(px * qty * FEE, 2),
                    "note": ("close position" if p["qty"] == 0 else "partial exit") if p else "close position"}
        fill["t"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        fill["value"] = round(px * qty, 2)
        BOOK["trades"].append(fill)
        del BOOK["trades"][:-400]
        _save()
    out = state()
    out["fill"] = fill
    return out


def reset():
    global BOOK
    with LOCK:
        BOOK = _load_book()
        BOOK.update({"cash": START_CASH, "start_cash": START_CASH, "positions": {},
                     "trades": [], "realised": 0.0,
                     "started": datetime.now().isoformat(timespec="seconds")})
        _save()
    return state()