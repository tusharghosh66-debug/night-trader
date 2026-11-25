"""
events.py - economic-calendar collector. DATA ONLY (step 1): nothing here
changes trading. It fills events.json so that after 2-4 weeks we can test
whether entries near a US/IN release really are worse. First measurement on
the 599 arena trades: -12.17/trade inside +-30 min of a medium/high event vs
-6.32 outside (n=61/538) - a hypothesis, not proof.

Sources, both free and both unofficial, so every failure is swallowed:
  * ForexFactory weekly JSON (this week only, no key)
  * TradingView calendar JSON (needs Origin/Referer), used to backfill history
"""
import json, os, time, urllib.request
from datetime import datetime, timedelta, timezone

BASE = os.path.dirname(os.path.abspath(__file__))
F = os.path.join(BASE, "events.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
FF_URL = "https://nfs.faireconomy.media/ff_calendar_thisweek.json"
TV_URL = ("https://economic-calendar.tradingview.com/events"
          "?from={a}&to={b}&countries=US,IN")
FETCH_SEC = 6 * 3600      # refresh at most every 6h: 1 call each, no rate-limit risk
KEEP_DAYS = 400           # history window kept in the file
MAX_EVENTS = 6000
IMP = {"high": 1, "medium": 0, "med": 0}


def say(msg):
    print(f"[events {datetime.now().strftime('%H:%M:%S')}] {msg}", flush=True)


def _get(url, headers=None, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8", "replace"))


def _clean(t, c, imp, title, src):
    """One normalised event: UTC ISO minute, country, 1=high 0=medium."""
    if c not in ("US", "IN") or imp is None:
        return None
    title = (title or "").strip()[:80]
    if not title:
        return None
    return {"t": t.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
            "c": c, "i": int(imp), "title": title, "src": src}


def fetch_ff():
    out = []
    for e in _get(FF_URL):
        imp = IMP.get(str(e.get("impact", "")).strip().lower())
        try:
            t = datetime.fromisoformat(e["date"])
        except Exception:
            continue
        ev = _clean(t, e.get("country"), imp, e.get("title"), "ff")
        if ev:
            out.append(ev)
    return out


def fetch_tv(days_back=90):
    now = datetime.now(timezone.utc)
    a = (now - timedelta(days=days_back)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    b = (now + timedelta(days=2)).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    data = _get(TV_URL.format(a=a, b=b), headers={"Origin": "https://www.tradingview.com",
                                                 "Referer": "https://www.tradingview.com/"},
                timeout=45)
    rows = data.get("result", data) if isinstance(data, dict) else data
    out = []
    for e in rows or []:
        imp = IMP.get({1: "high", 0: "medium", -1: None}.get(e.get("importance")), None)
        try:
            t = datetime.fromisoformat(str(e["date"]).replace("Z", "+00:00"))
        except Exception:
            continue
        ev = _clean(t, e.get("country"), imp, e.get("title"), "tv")
        if ev:
            out.append(ev)
    return out


def _key(e):
    return (e["t"], e["c"], e["title"].lower()[:60])


def load():
    try:
        with open(F, encoding="utf-8") as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get("events"), list):
            return d
    except Exception:
        pass
    return {"ts": 0, "updated": None, "events": []}


def collect(force=False, days_back=90):
    """Merge this week (ForexFactory) + history (TradingView). Never raises."""
    st = load()
    if not force and time.time() - float(st.get("ts") or 0) < FETCH_SEC:
        return 0
    evs = list(st["events"])
    # full backfill only while the file is empty, then a rolling 10-day window
    # (actual values get revised after the release, so re-read recent days only)
    days_back = days_back if not evs else 10
    have = {_key(e) for e in evs}
    added = 0
    # TV first: it carries history and actual values; FF is a bonus feed that
    # rate-limits (HTTP 429) from shared datacenter IPs, so never depend on it.
    for fn in (lambda: fetch_tv(days_back), fetch_ff):
        try:
            rows = fn()
        except Exception as e:
            say(f"{getattr(fn, '__name__', 'source')} failed (kept old data): {e}")
            continue
        for e in rows:
            k = _key(e)
            if k in have:
                continue
            have.add(k)
            evs.append(e)
            added += 1
    cutoff = (datetime.now(timezone.utc) - timedelta(days=KEEP_DAYS)).strftime("%Y-%m-%d")
    evs = sorted([e for e in evs if e["t"] >= cutoff], key=lambda e: e["t"])[-MAX_EVENTS:]
    st = {"ts": time.time(),
          "updated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
          "events": evs}
    tmp = F + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, separators=(",", ":"))
    os.replace(tmp, F)
    if added:
        say(f"+{added} events ({len(evs)} stored, high={sum(1 for e in evs if e['i'] == 1)})")
    return added
