"""Weekly check-up for the three council bots.

Reads arena_trades.csv and applies the numeric kill rules from the council review
(Muse Ember / MiMo 2.6 Flash), so a decision needs no judgement call:

  x01  KILL if n >= 15 and mean <= 0        (no edge per trade after fees)
       KILL if sum <= -Rs 180               (3R of damage)
       KILL if n >= 10 and sum <= -Rs 60 and mean < 0
  x02  KILL if n >= 15 and mean <= 0
       KILL if sum <= -Rs 150
       KILL if rank-out exits >= 5 and sum <= 0   (churn that did not pay)
  x03  KILL if sum <= -Rs 50
       KILL if n >= 10 and mean <= 0

Run:  python council_check.py
"""
import csv
import os

FEE = 0.001
IDS = ("x01", "x02", "x03")
F = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arena_trades.csv")


def stats(rows):
    pnl = [float(r[7]) for r in rows]
    n = len(pnl)
    if not n:
        return None
    mean = sum(pnl) / n
    sd = (sum((x - mean) ** 2 for x in pnl) / (n - 1)) ** 0.5 if n > 1 else 0.0
    se = sd / (n ** 0.5) if n > 1 else 0.0
    fees = sum(2 * FEE * float(r[4]) * float(r[5]) for r in rows)
    return {"n": n, "sum": sum(pnl), "mean": mean, "se": se,
            "fees": fees, "wins": sum(1 for x in pnl if x > 0), "rows": rows}


def verdict(bot, s):
    if s is None:
        return "WAIT", "no closed trade yet"
    n, tot, mean = s["n"], s["sum"], s["mean"]
    if bot == "x01":
        if n >= 15 and mean <= 0:
            return "KILL", f"{n} trades, mean Rs {mean:+.2f} <= 0"
        if tot <= -180:
            return "KILL", f"sum Rs {tot:+.0f} <= -180"
        if n >= 10 and tot <= -60 and mean < 0:
            return "KILL", f"{n} trades, sum Rs {tot:+.0f}, mean Rs {mean:+.2f}"
        return "KEEP", f"{n} trades, sum Rs {tot:+.0f}, mean Rs {mean:+.2f}"
    if bot == "x02":
        rank_exits = sum(1 for r in s["rows"] if "rotat" in (r[8] or "").lower())
        if n >= 15 and mean <= 0:
            return "KILL", f"{n} trades, mean Rs {mean:+.2f} <= 0"
        if tot <= -150:
            return "KILL", f"sum Rs {tot:+.0f} <= -150"
        if rank_exits >= 5 and tot <= 0:
            return "KILL", f"{rank_exits} rotation exits for sum Rs {tot:+.0f}"
        return "KEEP", f"{n} trades, sum Rs {tot:+.0f}, rotation exits {rank_exits}"
    if bot == "x03":
        if tot <= -50:
            return "KILL", f"sum Rs {tot:+.0f} <= -50"
        if n >= 10 and mean <= 0:
            return "KILL", f"{n} trades, mean Rs {mean:+.2f} <= 0"
        return "KEEP", f"{n} trades, sum Rs {tot:+.0f}, mean Rs {mean:+.2f}"
    return "KEEP", ""


def main():
    if not os.path.exists(F):
        print("arena_trades.csv not found - the arena has not written a trade yet")
        return
    with open(F, encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if len(r) > 9][1:]
    print(f"{len(rows)} closed trades in arena_trades.csv")
    for bot in IDS:
        mine = [r for r in rows if r[1] == bot]
        s = stats(mine)
        v, why = verdict(bot, s)
        print(f"\n{bot}: {v} - {why}")
        if s:
            wr = 100.0 * s["wins"] / s["n"]
            print(f"   fees paid Rs {s['fees']:.2f} | win {wr:.0f}% | "
                  f"mean+/-se Rs {s['mean']:+.2f} +/- {s['se']:.2f}")
            if s["n"] > 1:
                t = s["mean"] / s["se"] if s["se"] else 0.0
                print(f"   edge vs noise: t = {t:+.2f} "
                      f"({'inside noise' if abs(t) < 2 else 'beyond noise'})")


if __name__ == "__main__":
    main()
