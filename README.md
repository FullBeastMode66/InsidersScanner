# Insider / Politician Buy Scanner

A breakout-scanner-style tool for tracking two very different but often-correlated
signals:

- **Corporate insider buys** — SEC Form 4 filings (CEOs, CFOs, directors buying their own stock)
- **Congressional trades** — House & Senate financial disclosures

Instead of scanning price charts for breakout patterns, this scans filings and scores
each one 0–100 based on stacked "confirmations" (role seniority, dollar size, filing
speed, committee relevance) — the same mental model a breakout scanner uses for
volume/volatility confirmation.

> **Contributing / extending this project?** See [SKILL.md](SKILL.md) for development
> guidelines — architecture, the `Signal` contract, scoring rules, the "not real-time"
> data reality, and a per-change validation checklist.

## Quick start

```bash
pip install -r requirements.txt

# one-off scan, prints results + fires alerts above threshold
python scanner.py

# continuous scanning every 5 minutes
python scanner.py --loop --interval 300

# visual dashboard (separate terminal, scanner.py doesn't need to be running —
# the dashboard has its own "Run scan now" button)
streamlit run dashboard.py
```

## Phone app (installable PWA)

The phone app is a small web app that installs to your home screen and looks/behaves
like a native app. It's served by `api.py`, a thin FastAPI layer that reads the **same**
`scanner.db` and can trigger a scan on demand — the `scanner.py` engine and its scoring
are untouched; the app is simply a second UI onto the same database.

```bash
pip install -r requirements.txt

# 1. (optional) keep the engine polling in the background, as before
python scanner.py --loop --interval 300

# 2. start the app backend — binds to your whole network, not just localhost
uvicorn api:app --host 0.0.0.0 --port 8000
```

Then, on your phone **connected to the same Wi-Fi**, open:

```
http://<your-computer-LAN-ip>:8000
```

(Find the IP with `ipconfig` on Windows, or `ipconfig getifaddr en0` on macOS.)

**Install it:** on iOS Safari tap *Share → Add to Home Screen*; on Android Chrome tap
the *⋮ → Install app / Add to Home screen* prompt. It then launches full-screen with its
own icon and works offline (showing the last-seen signals).

The app has a source toggle (All / Insider / Congress), a minimum-signal slider, a ticker
filter, pull-to-refresh, and a **Run scan** button that calls `run_once()` on the backend.
It repeats the STOCK-Act disclosure caveat inline so the 45-day lag is never implied away.

### Push notifications (alerts while the app is closed)

The app can push a notification to your phone whenever a scan finds a **new**
signal at or above your alert threshold — the same trigger that already fires the
console/Pushover/Discord/Telegram alerts. It uses the standard Web Push + VAPID
flow and stores each device's subscription in the same `scanner.db`.

One-time setup:

```bash
# 1. generate a VAPID keypair and paste the printed lines into your .env / environment
python push.py --generate-keys

# 2. restart the backend so it picks up VAPID_PUBLIC_KEY / VAPID_PRIVATE_KEY / VAPID_CLAIM_EMAIL
uvicorn api:app --host 0.0.0.0 --port 8000
```

Then in the app tap the **bell** in the top-right and allow notifications. Tap it
again to turn alerts off. Expired subscriptions are pruned automatically.

Two caveats worth knowing up front:

- **HTTPS is required for push** (localhost is exempt for testing, but a phone
  reaching your machine over `http://<lan-ip>` is not). The quickest way to get
  real HTTPS on a home device is a Cloudflare Tunnel — see `tunnel/TUNNEL.md` for
  a copy-paste setup (`./tunnel/run-quick.sh` to test in five minutes, then a
  stable named tunnel to keep).
- **iOS** only delivers Web Push to a PWA that has been *installed* to the Home
  Screen (iOS 16.4+), not to a Safari tab. Add to Home Screen first, open the
  installed app, then enable the bell.

If the VAPID keys aren't set, push stays off and everything else runs unchanged.

### Going from home device to a cloud server

**Quick path: Deploy to Render** (free tier, 24/7 uptime, permanent URL)

Push your repo to GitHub and connect it to Render:

1. Create a free account at https://render.com
2. Click **+ New** → **Blueprint**, paste your GitHub repo URL
3. Set environment variables (VAPID keys, SEC_USER_AGENT) in the Render dashboard
4. Click **Deploy** — Render builds the Docker image and starts the app in ~2–5 minutes
5. You'll get a permanent URL like `https://insiders-scanner-abc123.onrender.com`

See `RENDER.md` for the full walkthrough. Key differences:

| | Home + Tunnel | Render |
|---|---|---|
| Setup time | ~5 minutes | ~10 minutes (includes GitHub push) |
| URL | Ephemeral (changes each run) | Permanent, stable (`*.onrender.com`) |
| Uptime | While your machine is on | 24/7 (free tier = ~99% uptime) |
| Costs | Free (cloudflared) | Free (750 hours/month) |

---

### Alternative: Self-managed cloud VPS

If you own a domain and want to host on your own VPS (DigitalOcean, Linode, etc.):

- Put it behind HTTPS (a reverse proxy like Caddy/Nginx, or the platform's built-in TLS)
  — iOS only lets a site register a service worker / install as a PWA over HTTPS.
- Restrict origins with `SCANNER_CORS_ORIGINS=https://yourapp.example` instead of `*`.
- Run the poller as its own always-on process (`python scanner.py --loop`) so signals
  keep accruing even when no phone is open.

No API key is required to get running. It uses:

| Data | Source | Key needed? |
|---|---|---|
| Insider Form 4 filings | SEC EDGAR "current filings" feed | No |
| House trades | House Stock Watcher dataset | No |
| Senate trades | Senate Stock Watcher dataset | No |

## Important limitation: this is not real-time for politicians

Corporate insiders must file Form 4 within **2 business days** of a trade — this
scanner catches those quickly. Members of Congress have up to **45 days** under the
STOCK Act to disclose a trade, so "new" congressional signals in this tool reflect
when a trade *became public*, not when it happened. The scoring engine rewards
faster-than-typical disclosures, but it can't make a 6-week-old trade "live."

## Upgrading past the free tier

The free feeds above are good enough to run continuously, but you can swap in paid
APIs for lower latency or richer fields by replacing the fetch functions in `scanner.py`:

- **SEC-API.io** — WebSocket stream of new filings (near-instant vs. polling)
- **Quiver Quantitative / Financial Modeling Prep / Finnhub** — congressional trade
  APIs with committee, party, and market-cap enrichment built in
- **Polygon.io / IEX Cloud** — real-time price/volume, if you want to correlate a
  buy signal with subsequent price action

## Alerts

Set any of these environment variables to get pushed alerts (console output always
works with no setup):

```bash
export PUSHOVER_TOKEN=...
export PUSHOVER_USER=...

export DISCORD_WEBHOOK_URL=...

export TELEGRAM_BOT_TOKEN=...
export TELEGRAM_CHAT_ID=...
```

Other tunables:

```bash
export SCANNER_MIN_SCORE=50       # 0-100, alert threshold
export SCANNER_INTERVAL=300       # seconds between scans in --loop mode
export SEC_USER_AGENT="YourName your-email@example.com"   # SEC requires this
export SCANNER_CORS_ORIGINS="*"   # phone-app API: lock to your domain in production
```

## Files

- `scanner.py` — scanning engine, scoring, SQLite storage, alert dispatch
- `dashboard.py` — Streamlit table view with sortable score bars and filters
- `api.py` — FastAPI backend for the phone app: reads the same `scanner.db`,
  serves signals as JSON, triggers a scan, handles push subscriptions, and hosts
  the installable PWA
- `push.py` — Web Push channel: VAPID keys, subscription storage, `send_push()`;
  wired into `scanner.py`'s existing alert dispatch
- `webapp/` — the installable phone app (PWA): `index.html`, `app.js`,
  `styles.css`, `manifest.webmanifest`, `sw.js`, and icons
- `tunnel/` — HTTPS tunnel setup for a home device (`TUNNEL.md` guide, plus
  `run-quick` / `run-named` launchers in `.sh` for macOS/Linux and `.bat`/`.ps1`
  for Windows, and a cloudflared config template)
- `.env.example` — every environment variable, ready to copy to `.env`
- `scanner.db` — created automatically on first run (SQLite)

## Scoring logic (summary)

**Insider (Form 4) score** = seniority weight (officer > director > other) +
dollar-size weight + filing-speed bonus, then scaled by the recency multiplier.

**Politician score** = disclosed dollar-range weight + committee/sector overlap
bonus + disclosure-speed bonus (faster than the 45-day max = higher score), then
normalized across chambers, then scaled by the recency multiplier.

**Cross-chamber normalization.** The free House and Senate feeds don't carry the same
fields, so raw scores aren't comparable. The House feed has transaction + disclosure
dates (disclosure speed is scorable); the Senate feed has only the transaction date —
no disclosure date. (Neither free feed currently carries committee data, so the
committee bonus is inert against both and reserved for a future richer source.) A
Senate signal could therefore only ever earn the dollar-size component, capping it far
below House regardless of trade size. Each chamber's score is scaled to a common
ceiling based on what its feed can structurally provide, so a large, fresh Senate buy
competes with a large, fresh House one. House already spans the full range, so House
scores are unchanged. This invents no disclosure or committee data — it only removes
the structural penalty, and the scaling is shown in the `reasons` string.

> **Senate data caveat:** the default Senate mirror stopped updating in late 2020, so
> in practice every Senate trade is stale and suppressed by recency regardless of
> normalization. Point `SENATE_TRADES_URL` at a maintained feed to get live Senate
> signals.

**Recency multiplier** (both sources) discounts the whole signal by how long ago the
*trade* happened — for an alerting tool, last week's buy should outrank a 2022 one:

| Age of trade | Multiplier |
| --- | --- |
| ≤ 30 days | ×1.00 |
| 31–90 days | ×0.85 |
| 91–180 days | ×0.60 |
| > 180 days | ×0.35 (stale) |

It multiplies rather than adds a flat bonus, so a stale signal is discounted in
proportion to its strength instead of merely missing points. Trades with no usable
date get ×1.00 (age can't be measured, so it isn't penalized). This is deliberately
separate from the disclosure-speed bonus: recency is *when the trade happened*,
disclosure speed is *the trade→public lag* — the two never double-count, and neither
implies the feed is real-time (see the limitation above). Because a stale trade caps
at 100 × 0.35 = 35 — below the default `SCANNER_MIN_SCORE` of 50 — old trades drop out
of alerts on their own while still appearing, ranked low, in the app. The score is
computed once when a signal is first seen and frozen (dedup never re-scores), so a
signal that's genuinely fresh at capture keeps that score as it ages; bulk historical
rows enter with their true age and are decayed correctly.

Both are capped at 100 and shown with a progress bar in the dashboard, same visual
language as a breakout scanner's strength meter. Every component above is emitted in
the signal's human-readable `reasons` string, so the dashboard and app always show
the "why" behind a score.
