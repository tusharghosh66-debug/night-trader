# -*- coding: utf-8 -*-
"""Score advisory picks against real daily candles and publish them to the site.

picks.json  ->  pick.py score  ->  picks_scored.json  +  <script id="picks-data"> in site_template.html

Rules (deliberately conservative):
  * long: bar low <= stop  -> STOP at the stop, even if the same bar also touched the target
  * long: bar high >= target -> TARGET at the target
  * nothing inside "horizon" trading days -> OPEN, marked to the last close
  * R = (exit - entry) / (entry - stop);  MFE/MAE are the best and worst excursions in R
No fees, no slippage: an advisory call has no size and no fill, so R is the only
honest unit. Demo rows are scored but kept out of the headline statistics.
"""
import argparse, datetime, io, json, os, re, sys

HERE = os.path.dirname(os.path.abspath(__file__))
PICKS = os.path.join(HERE, "picks.json")
OUT = os.path.join(HERE, "picks_scored.json")
TPL = os.path.join(HERE, "site_template.html")
sys.path.insert(0, HERE)
import arena  # data helpers only; no state is touched


def load():
    with io.open(PICKS, encoding="utf-8") as f:
        return json.load(f)


def save(d):
    with io.open(PICKS, "w", encoding="utf-8") as f:
        json.dump(d, f, indent=2, ensure_ascii=False)


def candles(sym):
    kl = arena.get_candles(sym, "1d", "2y", prepost=False, drop_last=False)
    for k in kl:
        k["d"] = datetime.datetime.fromtimestamp(k["t"] / 1000).strftime("%Y-%m-%d")
    return kl


def score_one(p, kl):
    entry, stop, target = float(p["entry"]), float(p["stop"]), float(p["target"])
    side = 1 if str(p.get("side", "long")).lower().startswith("l") else -1
    risk = (entry - stop) * side
    horizon = int(p.get("horizon", 20))
    bars = [k for k in kl if k["d"] >= p["date"]]
    row = {"id": p["id"], "date": p["date"], "sym": p["sym"], "side": p.get("side", "long"),
           "entry": entry, "stop": stop, "target": target, "by": p.get("by", ""),
           "src": p.get("src", ""), "note": p.get("note", ""), "demo": bool(p.get("demo")),
           "status": "NO_DATA", "r": None, "mfe_r": None, "mae_r": None,
           "exit_date": "", "exit_px": None, "bars": 0, "last": None}
    if not bars or risk <= 0:
        return row
    best = worst = 0.0
    for i, k in enumerate(bars[:horizon]):
        hi, lo = k["h"] * 1.0, k["l"] * 1.0
        best = max(best, side * (hi - entry) / risk)
        worst = min(worst, side * (lo - entry) / risk)
        hit_t = (hi >= target) if side > 0 else (lo <= target)
        hit_s = (lo <= stop) if side > 0 else (hi >= stop)
        if hit_s:                       # conservative: the stop wins a two-touch bar
            row.update(status="STOP", exit_date=k["d"], exit_px=stop,
                       r=-1.0, bars=i + 1, last=k["c"])
            break
        if hit_t:
            row.update(status="TARGET", exit_date=k["d"], exit_px=target,
                       r=round((target - entry) / risk, 2), bars=i + 1, last=k["c"])
            break
    else:
        last = bars[min(horizon, len(bars)) - 1]
        row.update(status="OPEN", exit_date="", exit_px=None, last=last["c"],
                   r=round(side * (last["c"] - entry) / risk, 2), bars=min(horizon, len(bars)))
    row["mfe_r"] = round(best, 2)
    row["mae_r"] = round(worst, 2)
    return row


def summary(rows):
    live = [r for r in rows if not r["demo"]]
    done = [r for r in live if r["status"] in ("TARGET", "STOP")]
    wins = [r for r in done if r["status"] == "TARGET"]
    rs = [r["r"] for r in done if r["r"] is not None]
    mtm = [r["r"] for r in live if r.get("r") is not None]
    return {
        "tracked": len(live), "resolved": len(done), "targets": len(wins),
        "stops": len(done) - len(wins), "pending": len(live) - len(done),
        "hit_rate": round(100.0 * len(wins) / len(done), 1) if done else None,
        "avg_r": round(sum(rs) / len(rs), 2) if rs else None,
        "avg_r_mtm": round(sum(mtm) / len(mtm), 2) if mtm else None,
        "total_r_mtm": round(sum(mtm), 2) if mtm else None,
        "best": max(rs) if rs else None, "worst": min(rs) if rs else None,
    }


def publish(rows, s):
    payload = {"generated": datetime.datetime.now().strftime("%Y-%m-%d %H:%M IST"),
               "summary": s, "picks": rows}
    with io.open(OUT, "w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, ensure_ascii=False)
    blob = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).replace("<", "\\u003c")
    s_html = io.open(TPL, encoding="utf-8").read()
    new, n = re.subn(r'(<script id="picks-data"[^>]*>)([\s\S]*?)(</script>)',
                     lambda m: m.group(1) + blob + m.group(3), s_html, count=1)
    if n == 0:
        print("WARN: picks-data script tag not found in template; site not updated")
        return
    io.open(TPL, "w", encoding="utf-8").write(new)
    print("published:", len(rows), "picks |", json.dumps(s))


def cmd_score(args):
    d = load()
    rows = []
    for p in d["picks"]:
        try:
            rows.append(score_one(p, candles(p["sym"])))
        except Exception as e:
            print("FAIL", p.get("id"), e)
            rows.append({"id": p.get("id"), "sym": p.get("sym"), "status": "NO_DATA", "r": None,
                         "demo": bool(p.get("demo")), "note": "price fetch failed: %s" % e})
    publish(rows, summary(rows))


def cmd_add(args):
    d = load()
    pid = "%s-%s" % (args.date, args.sym.split(".")[0].lower())
    if any(p["id"] == pid for p in d["picks"]):
        print("already logged:", pid); return
    d["picks"].append({"id": pid, "date": args.date, "sym": args.sym,
                       "side": args.side, "entry": args.entry, "stop": args.stop,
                       "target": args.target, "horizon": args.horizon,
                       "by": args.by or "", "src": args.src or "", "note": args.note or "",
                       "demo": False})
    save(d)
    print("added", pid, "- now run: python pick.py score")


def cmd_list(args):
    for p in load()["picks"]:
        print("%-28s %-12s %8.2f %8.2f %8.2f  %s" % (p["id"], p["sym"], p["entry"], p["stop"],
                                                      p["target"], "DEMO" if p.get("demo") else ""))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    a = sub.add_parser("add"); a.add_argument("--sym", required=True)
    a.add_argument("--entry", type=float, required=True); a.add_argument("--stop", type=float, required=True)
    a.add_argument("--target", type=float, required=True); a.add_argument("--date", required=True)
    a.add_argument("--side", default="long"); a.add_argument("--horizon", type=int, default=20)
    a.add_argument("--by", default=""); a.add_argument("--src", default=""); a.add_argument("--note", default="")
    sub.add_parser("list"); sub.add_parser("score")
    args = ap.parse_args()
    {"add": cmd_add, "list": cmd_list, "score": cmd_score}.get(args.cmd or "score", cmd_score)(args)
