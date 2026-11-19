# Night Trader — Cloud Deployment (GitHub Actions, FREE)

Bot runs every 15 min on GitHub's servers — PC off thakleo cholbe.
Dashboard live thakbe GitHub Pages-e, mobile theke dekha jabe.

## Deploy steps (Hermes will do these once GitHub auth is available)
1. Create a repo named `night-trader` (public — only paper-trading data, no secrets).
2. Push the contents of this `cloud/` folder to `main`.
3. Enable Pages: Settings → Pages → Source: Deploy from branch → `main` / root.
4. Actions tab → enable workflows → run `night-trader` once (workflow_dispatch).
5. Dashboard: `https://<username>.github.io/night-trader/dashboard.html`

## Files
- `bot.py` — the bot (stdlib only). Modes: `once` (cloud cron), `run` (local daemon), `backtest`, `report`, `dashboard`.
- `.github/workflows/trader.yml` — runs `python bot.py once` every 15 min, commits state back.
- `state.json`, `news.json` — current paper state (₹10,000 x 3 markets), carried over from local.

## Notes
- GitHub scheduled cron can lag 5–20 min under load — fine for 1h-candle paper trading.
- Scheduled workflows auto-disable after 60 days of NO repo activity; our every-15-min commits keep it alive forever.
- All amounts INR; USD prices converted at live USDINR. PAPER TRADING — no real orders, no API keys anywhere.
- To stop: disable the workflow in the Actions tab (or delete the repo).
