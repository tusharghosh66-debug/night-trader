"""
serve_all.py — ONE Render free web service running BOTH engines:
  * Night-Trader (bot.py)   : 3 markets, Rs 30,000 paper
  * Strategy Arena (arena.py): 10 bots, Rs 1,00,000 paper
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

BASE = os.path.dirname(os.path.abspath(__file__))
CYCLE_SEC = 60
SYNC_EVERY = 5
FILES = ["state.json", "trades.csv", "report.md", "dashboard.html", "news.json",
         "arena_state.json", "arena_trades.csv", "arena_report.md", "arena_dashboard.html"]
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
        for f in ("arena.log", "decisions.log"):
            subprocess.run(["git", "-C", BASE, "checkout", "--", f], capture_output=True)
        arena._git("pull", "--rebase", arena._auth_url(), "main")
        say("boot: pulled latest state from GitHub")
    except Exception as e:
        say(f"boot pull failed (using local state): {e}")


def git_sync():
    if not os.environ.get("GITHUB_TOKEN", "").strip():
        STATUS["sync"] = "no-token (state local only)"
        return
    try:
        arena._git("config", "user.email", "arena-bot@users.noreply.github.com")
        arena._git("config", "user.name", "night-trader-serve")
        arena._git("add", *FILES)
        r = subprocess.run(["git", "-C", BASE, "diff", "--cached", "--quiet"],
                           capture_output=True)
        if r.returncode != 0:
            arena._git("commit", "-m",
                       f"serve all {datetime.now(bot.IST).strftime('%Y-%m-%dT%H:%M:%S%z')} [skip ci][skip render]")
        # logs are tracked but never committed from serve mode; keep rebase clean
        for f in ("arena.log", "decisions.log"):
            subprocess.run(["git", "-C", BASE, "checkout", "--", f], capture_output=True)
        arena._git("pull", "--rebase", arena._auth_url(), "main")
        arena._git("push", arena._auth_url(), "HEAD:main")
        STATUS["sync"] = "ok"
        STATUS["last_sync"] = datetime.now(bot.IST).isoformat()
    except Exception as e:
        STATUS["sync"] = f"error: {e}"
        say(f"git sync failed: {e}")


class HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
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
