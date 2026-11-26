"""
serve_all.py — ONE Render free web service running BOTH engines:
  * Night-Trader (bot.py)   : 3 markets, Rs 30,000 paper
  * Strategy Arena (arena.py): 13 bots, Rs 1,30,000 paper
60-second cycles, /health HTTP endpoint (keep-alive target), GitHub state
sync every 5 cycles so Pages dashboards stay fresh and restarts lose nothing.
Why combined: Render free = 750 instance-hours/month; one 24/7 service ~744h.
Two separate services would exceed the quota mid-month.
Run:  python serve_all.py   (PORT env, default 10000; GITHUB_TOKEN env for sync)
"""
import os, json, time, threading, traceback, subprocess
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import bot
import arena
import events
import desk
from urllib.parse import urlparse, parse_qs

BASE = os.path.dirname(os.path.abspath(__file__))
CYCLE_SEC = int(os.environ.get("CYCLE_SEC", "60"))   # Render env: 30
SYNC_EVERY = 5
FILES = ["state.json", "trades.csv", "report.md", "dashboard.html", "news.json",
         "arena_state.json", "arena_trades.csv", "arena_report.md", "arena_dashboard.html",
         "council_dashboard.html",
         "arena_trades_archive.csv", "trades_archive.csv", "events.json",
         "manual_book.json", "companies.json"]
# Who is keeping the free instance awake? UptimeRobot sends HEAD / every few
# minutes; Render spins a free web service down after 15 min with no inbound hit.
PINGS = {"total": 0, "head": 0, "uptimerobot": 0, "first": None, "last": [], "by_ua": {}}


def _hit(path, ua, method):
    now = datetime.now(bot.IST)
    ua = (ua or "no-ua")[:60]
    PINGS["total"] += 1
    if method == "HEAD":
        PINGS["head"] += 1
    if "uptimerobot" in ua.lower():
        PINGS["uptimerobot"] += 1
    PINGS["by_ua"][ua] = PINGS["by_ua"].get(ua, 0) + 1
    PINGS["first"] = PINGS["first"] or now.isoformat(timespec="seconds")
    PINGS["last"].append({"t": now.strftime("%H:%M:%S"), "m": method, "p": path[:30], "ua": ua[:32]})
    del PINGS["last"][:-25]


STATUS = {"started": None, "last_cycle": None, "cycles": 0,
          "night_trader": {"equity": 0.0, "error": None},
          "arena": {"equity": 0.0, "error": None},
          "sync": "not-run", "last_sync": None}


def say(msg):
    print(f"[serve_all {datetime.now(bot.IST).strftime('%H:%M:%S')}] {msg}", flush=True)


def boot():
    """Pull latest state from GitHub before trading (Render disk is ephemeral)."""
    if not os.path.isdir(os.path.join(BASE, ".git")):
        say("no .git here — skipping boot pull (local test)")
        return
    try:
        say(f"boot: pulled latest state from GitHub ({arena._boot_pull()})")
    except Exception as e:
        say(f"boot pull failed (using local state): {e}")


def git_sync():
    if not os.environ.get("GITHUB_TOKEN", "").strip():
        STATUS["sync"] = "no-token (state local only)"
        return
    try:
        # logs are tracked but never committed from serve mode; keep rebase clean
        for f in ("arena.log", "decisions.log"):
            subprocess.run(["git", "-C", BASE, "checkout", "--", f], capture_output=True)
        # rebase with local-state-wins + abort/retry: a half-finished rebase from a
        # killed instance used to block every later commit ("unmerged files")
        # only add files that exist: arena_trades_archive.csv appears at the
        # 2026-11-26 fresh start, and 'git add' on a missing path aborted the whole
        # sync (health showed sync error, state stopped reaching GitHub).
        files = [f for f in FILES if os.path.exists(os.path.join(BASE, f))]
        arena._rebase_and_push(
            files, f"serve all {datetime.now(bot.IST).strftime('%Y-%m-%dT%H:%M:%S%z')} [skip ci][skip render]")
        STATUS["sync"] = "ok"
        STATUS["last_sync"] = datetime.now(bot.IST).isoformat()
    except Exception as e:
        STATUS["sync"] = f"error: {e}"
        say(f"git sync failed: {e}")


class HealthHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        try:
            _hit(self.path, self.headers.get("User-Agent"), "HEAD")
        except Exception:
            pass
        self.send_response(200)
        content_type = "application/json" if self.path == "/health" else "text/html; charset=utf-8"
        self.send_header("Content-Type", content_type)
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _desk_get(self):
        """Practice desk API: company list, live quotes, book state."""
        try:
            u = urlparse(self.path)
            if u.path == "/api/companies":
                path = os.path.join(BASE, "companies.json")
                with open(path, "rb") as f:
                    body = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Cache-Control", "public, max-age=3600")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
            if u.path == "/api/quote":
                raw = (parse_qs(u.query).get("symbols") or [""])[0]
                syms = [x.strip().upper() for x in raw.split(",") if x.strip()][:25]
                return self._json({"quotes": desk.quotes(syms, max_age=15.0),
                                   "at": datetime.now(bot.IST).strftime("%H:%M:%S")})
            if u.path == "/api/desk":
                return self._json(desk.state())
            return self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            return self._json({"error": str(e)[:200]}, 500)

    def do_POST(self):
        try:
            _hit(self.path, self.headers.get("User-Agent"), "POST")
        except Exception:
            pass
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n) if n > 0 else b"{}"
            data = json.loads(raw.decode("utf-8") or "{}")
            u = urlparse(self.path)
            if u.path == "/api/order":
                out = desk.order(data.get("sym"), data.get("side"), data.get("qty"))
                return self._json(out, 200 if "error" not in out else 400)
            if u.path == "/api/reset":
                return self._json(desk.reset())
            return self._json({"error": "unknown endpoint"}, 404)
        except Exception as e:
            return self._json({"error": str(e)[:200]}, 500)

    def do_GET(self):
        try:
            _hit(self.path, self.headers.get("User-Agent"), "GET")
        except Exception:
            pass
        # Practice desk (manual paper trading) - JSON only, no HTML here
        if self.path.startswith("/api/desk") or self.path.startswith("/api/quote") \
                or self.path.startswith("/api/companies"):
            return self._desk_get()

        # Health check endpoint for uptime monitors
        if self.path in ["/health", "/api/pings"]:
            out = {"status": "ok", **STATUS}
            try:
                up_min = round((datetime.now(bot.IST) - datetime.fromisoformat(STATUS["started"])).total_seconds() / 60, 1)
            except Exception:
                up_min = None
            out["pings"] = {"total": PINGS["total"], "head": PINGS["head"],
                            "uptimerobot": PINGS["uptimerobot"], "first": PINGS["first"],
                            "uptime_min": up_min, "unique_ua": len(PINGS["by_ua"]),
                            "last": PINGS["last"][-6:], "by_ua_top": sorted(
                                PINGS["by_ua"].items(), key=lambda kv: -kv[1])[:3]}
            body = json.dumps(out, default=str).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # New grouped site for all bots at root; old board keeps /arena
        if self.path in ["/", "/site", "/site.html"]:
            site_file = os.path.join(BASE, "site.html")
            if os.path.exists(site_file):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(site_file, "rb") as f:
                    self.wfile.write(f.read())
                return

        # Serve Strategy Arena visual dashboard at /arena
        if self.path in ["/", "/arena", "/arena_dashboard.html", "/index.html"]:
            dash_file = os.path.join(BASE, "arena_dashboard.html")
            if os.path.exists(dash_file):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(dash_file, "rb") as f:
                    self.wfile.write(f.read())
                return

        # Separate dashboard for the three council bots
        if self.path in ["/council", "/council_dashboard.html"]:
            dash_file = os.path.join(BASE, "council_dashboard.html")
            if os.path.exists(dash_file):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(dash_file, "rb") as f:
                    self.wfile.write(f.read())
                return

        # Machine-readable council block, so the agent can answer without the page
        if self.path == "/api/council":
            try:
                st = arena.load_state()
                ids = set(arena.COUNCIL_IDS)
                out = {"updated": STATUS.get("last_cycle"), "bots": {}}
                for r in arena.leaderboard(st):
                    if r["id"] not in ids:
                        continue
                    bs = st["bots"][r["id"]]
                    out["bots"][r["id"]] = {
                        "name": r["name"], "equity": round(r["equity"], 2),
                        "pnl": round(r["pnl"], 2), "trades": r["trades"],
                        "win_rate": round(r["win_rate"], 1),
                        "positions": {s: {"side": p["side"], "qty": round(p["qty"], 6),
                                          "entry": round(p["entry"], 2), "stop": round(p["stop"], 2),
                                          "now": round(bs["last_prices"].get(s, p["entry"]), 2)}
                                      for s, p in bs["positions"].items()},
                        "gates": bs.get("gates", {}),
                        "last_trades": bs["closed_trades"][-5:]}
                out["total_equity"] = round(sum(v["equity"] for v in out["bots"].values()), 2)
                body = json.dumps(out, default=str).encode()
            except Exception as e:
                body = json.dumps({"error": str(e)[:200]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return

        # Serve Night-Trader dashboard at /night
        if self.path in ["/night", "/dashboard.html"]:
            dash_file = os.path.join(BASE, "dashboard.html")
            if os.path.exists(dash_file):
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                with open(dash_file, "rb") as f:
                    self.wfile.write(f.read())
                return

        # Fallback JSON response
        body = json.dumps({"status": "ok", **STATUS}, default=str).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass


def loop():
    ast = arena.load_state()
    STATUS["started"] = datetime.now(bot.IST).isoformat()
    n = 0
    try:                      # calendar collector: data only, first fill at boot
        events.collect()
    except Exception as e:
        say(f"events collect failed: {e}")
    while True:
        t0 = time.time()
        # --- Night-Trader (old bot): one cycle, hourly news cache inside ---
        try:
            bst = bot.cycle_once()
            STATUS["night_trader"] = {
                "equity": round(sum(bot.equity(bst["markets"][m]) for m in bot.MARKETS), 2),
                "error": None}
        except Exception as e:
            STATUS["night_trader"]["error"] = str(e)[:200]
            say(f"night-trader cycle error: {e}\n{traceback.format_exc()}")
        # --- Strategy Arena: one cycle ---
        try:
            arena.run_cycle(ast)
            STATUS["arena"] = {
                "equity": round(sum(arena.bot_equity(ast["bots"][b["id"]]) for b in arena.BOTS), 2),
                "error": None}
        except Exception as e:
            STATUS["arena"]["error"] = str(e)[:200]
            say(f"arena cycle error: {e}\n{traceback.format_exc()}")
        n += 1
        STATUS.update(cycles=n, last_cycle=datetime.now(bot.IST).isoformat())
        # calendar collector: data only (nothing reads it yet), one fetch per 6h
        if n % 30 == 0:
            try:
                events.collect()
            except Exception as e:
                say(f"events collect failed: {e}")
        if n % SYNC_EVERY == 0:
            git_sync()
        time.sleep(max(5, CYCLE_SEC - (time.time() - t0)))


if __name__ == "__main__":
    boot()
    port = int(os.environ.get("PORT", "10000"))
    srv = ThreadingHTTPServer(("0.0.0.0", port), HealthHandler)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    say(f"HTTP on :{port} | both engines every {CYCLE_SEC}s | GitHub sync every {SYNC_EVERY} cycles")
    loop()