# Deploy to Render

This guide gets the Insiders Scanner running 24/7 on Render with a permanent
HTTPS URL (`insiders-scanner-abc123.onrender.com`) and automatic deploys on push.

## Prerequisites

1. A GitHub account with this repo pushed to it
2. A free Render account (https://render.com)
3. Your VAPID keys (from `python push.py --generate-keys`)

## One-time setup

### 1. Push the repo to GitHub

If you haven't already:

```bash
git init
git add .
git commit -m "initial commit"
git remote add origin https://github.com/you/insiders-scanner
git push -u origin main
```

### 2. Connect to Render

1. Go to https://dashboard.render.com
2. Sign up / log in
3. Click **+ New** → **Blueprint** (or **Web Service**)
4. Paste your GitHub repo URL
5. Authorize Render to access your repo

### 3. Configure environment variables

Before deploying, click **Environment** and set:

```
VAPID_PUBLIC_KEY=<your-key-from-push.py>
VAPID_PRIVATE_KEY=<your-key-from-push.py>
VAPID_CLAIM_EMAIL=you@example.com
SEC_USER_AGENT=YourName you@example.com

# Optional: extra alert channels
# PUSHOVER_TOKEN=...
# DISCORD_WEBHOOK_URL=...
```

(Keep secrets like `VAPID_PRIVATE_KEY` **out of git** — set them in the Render
dashboard, not in `render.yaml`.)

### 4. Deploy

Click **Deploy**. Render will:

1. Clone your repo
2. Build the Docker image
3. Start the app + scanner poller
4. Assign a URL like `https://insiders-scanner-abc123.onrender.com`

First deploy takes ~2–5 minutes. Subsequent deploys (on `git push`) are automatic.

## After deployment

Your app is now live at `https://insiders-scanner-abc123.onrender.com`. Your phone's
installed PWA is tied to the old quick-tunnel URL, so:

1. Uninstall the old app from Home Screen
2. Open the new Render URL on your phone
3. Add to Home Screen → install the new version
4. Tap the bell and enable alerts — the subscription is fresh

The scanner poller runs in the background continuously, so signals keep accruing
24/7 whether anyone is looking at the phone or not.

## Key differences from home device

| | Home + Tunnel | Render |
|---|---|---|
| URL | Ephemeral quick tunnel | Permanent, stable |
| Uptime | While your computer is on | 24/7 |
| Database | Local file | Persistent disk (1GB) |
| Costs | Free (cloudflared + home device) | Free tier (Render's 750 free hours/month = ~99% uptime) |
| Maintenance | You keep it running | Automatic restarts on crash |

## Troubleshooting

**App won't deploy:**
- Check the build logs in Render dashboard
- Ensure all required Python files are in the repo (scanner.py, api.py, etc.)
- Make sure `Dockerfile` and `render.yaml` are in the root

**Push notifications not working:**
- Verify `VAPID_PUBLIC_KEY` and `VAPID_PRIVATE_KEY` are set in Render env vars (not just
  `.env` — Render doesn't see local `.env` files)
- Check app logs in Render dashboard: should say "push configured: True"

**Database lost after deploy:**
- Render's persistent disk survives app restarts. If the disk is recreated (rare),
  the signal history is lost but the app keeps working — just rescan.

## Custom domain

If you have a domain, you can point it at the Render URL:

1. In Render dashboard, under your app → Settings → Custom Domain
2. Add `scanner.yourdomain.com`
3. Update your DNS provider (Cloudflare, etc.) to point there

PWA installs work on custom domains too.

## Staying updated

Push changes to your GitHub repo — Render automatically redeploys:

```bash
# make a code change
git add .
git commit -m "update: change alert threshold"
git push origin main
# Render detects the push and redeploys in ~1-2 minutes
```
