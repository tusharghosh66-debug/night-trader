"""Weekly check-up for the three council bots - decided by numbers, not mood.

Reads arena_trades.csv and reports, per bot:
  n          closed trades
  mean R     average result measured in RISK units (x-bots risk 0.6% of equity)
  t          mean R / standard error; |t| < 1.65 means the edge is inside noise
  fee/gross  how much of the gross profit the toll ate (the arena's real killer)
  equity     where the Rs 10,000 book stands

Kill rules (council review: MiMo 2.6 Pro, Muse Ember, GPT-6 Luna), any one fires:

  x01  mean R <= 0 with n >= 15        no edge per trade
       |t| < 1.65 with n >= 30        edge cannot be told apart from luck
       equity <= Rs 9,400             6% of the book gone
  x02  mean R < 0.25 with n >= 20      too small to pay for the turnover
       fee/gross > 0.50 with n >= 20   the toll is eating half the profit
       equity <= Rs 9,250
  x03  mean R <= 0 with n >= 10
       n < 10 after 30 days            strategy inactive, not a strategy
       equity <= Rs 9,400

Run:  python council_check.py
"""
import csv
import os
from datetime import datetime, timezone

RISK = 0.006          # the x-bots size at 0.6% of equity per trade
FEE = 0.001           # per side
IDS = ("x01", "x02", "x03")
F = os.path.join(os.path.dirname(os.path.abspath(__file__)), "arena_trades.csv")


def stats(rows):
    if not rows:
        return None
    r_mult, fees, gross, pnl = [], 0.0, 0.0, 0.0
    for r in rows:
        qty, entry, exit_, p = float(r[4]), float(r[5]), float(r[6]), float(r[7])
        eq_after = float(r[9]) if len(r) > 9 and r[9] else 10000.0
        eq_before = eq_after - p
        fee = FEE * qty * (entry + exit_)
        fees += fee
        gross += p + fee
        pnl += p
        r_mult.append(p / (RISK * eq_before) if eq_before > 0 else 0.0)
    n = len(r_mult)
    mean = sum(r_mult) / n
    sd = (sum((x - mean) ** 2 for x in r_mult) / (n - 1)) ** 0.5 if n > 1 else 0.0
    t = mean / (sd / n ** 0.5) if n > 1 and sd else 0.0
    return {"n": n, "mean_r": mean, "t": t, "pnl": pnl, "fees": fees,
            "gross": gross, "rows": rows, "wins": sum(1 for x in r_mult if x > 0)}


def verdict(bot, s):
    if s is None:
        return "WAIT", "no closed trade yet"
    n, mr, t, pnl = s["n"], s["mean_r"], s["t"], s["pnl"]
    loads = s["fees"] / s["gross"] if s["gross"] > 0 else float("inf")
    load_txt = f"{loads:.2f}" if s["gross"] > 0 else "n/a (gross <= 0)"
    eq = 10000.0 + pnl     # close enough: no fees on the book itself
    if bot == "x01":
        if n >= 15 and mr <= 0:
            return "KILL", f"{n} trades, mean {mr:+.2f}R <= 0"
        if n >= 30 and abs(t) < 1.65:
            return "KILL", f"{n} trades, t={t:+.2f} - edge is luck so far"
        if eq <= 9400:
            return "KILL", f"equity Rs {eq:,.0f} <= 9,400"
        return "KEEP", f"{n} trades, mean {mr:+.2f}R, t={t:+.2f}, fee/gross {loads:.2f}"
    if bot == "x02":
        if n >= 20 and mr < 0.25:
            return "KILL", f"{n} trades, mean {mr:+.2f}R < 0.25R"
        if n >= 20 and loads > 0.5:
            return "KILL", f"fee/gross {loads:.2f} > 0.50"
        if eq <= 9250:
            return "KILL", f"equity Rs {eq:,.0f} <= 9,250"
        return "KEEP", f"{n} trades, mean {mr:+.2f}R, fee/gross {load_txt}"
    if bot == "x03":
        if n >= 10 and mr <= 0:
            return "KILL", f"{n} trades, mean {mr:+.2f}R <= 0"
        if eq <= 9400:
            return "KILL", f"equity Rs {eq:,.0f} <= 9,400"
        return "KEEP", f"{n} trades, mean {mr:+.2f}R, t={t:+.2f}"
    return "KEEP", ""


def main():
    if not os.path.exists(F):
        print("arena_trades.csv not found - the arena has not written a trade yet")
        return
    with open(F, encoding="utf-8") as fh:
        rows = [r for r in csv.reader(fh) if len(r) > 9][1:]
    print(f"{len(rows)} closed trades in arena_trades.csv "
          f"({datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')})")
    for bot in IDS:
        mine = [r for r in rows if r[1] == bot]
        s = stats(mine)
        v, why = verdict(bot, s)
        print(f"\n{bot}: {v} - {why}")
        if s:
            wr = 100.0 * s["wins"] / s["n"]
            print(f"   win {wr:.0f}% | net Rs {s['pnl']:+.2f} | fees Rs {s['fees']:.2f} "
                  f"| gross Rs {s['gross']:+.2f} | mean {s['mean_r']:+.2f}R +/- "
                  f"{(s['mean_r']/s['t'] if s['t'] else 0):.2f}R")


if __name__ == "__main__":
    main()
