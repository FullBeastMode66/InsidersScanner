# HTTPS tunnel for your home device

Push notifications and PWA install both require your phone to reach the app over
**HTTPS** — plain `http://<lan-ip>:8000` won't do it. A Cloudflare Tunnel gives you
HTTPS from a home machine without opening any router ports or owning a static IP:
`cloudflared` makes an outbound-only connection to Cloudflare's edge, and traffic
rides back down it.

There are two modes. Start with the quick tunnel to confirm push works on your
phone today; move to the named tunnel for the setup you keep.

| | Quick tunnel | Named tunnel |
|---|---|---|
| Account needed | none | free Cloudflare account |
| Domain needed | none | a domain on Cloudflare |
| URL | random, **changes every run** | **stable**, yours |
| Good for | testing push right now | the real, ongoing setup |

Why the stable URL matters: an installed PWA and its push subscription are tied to
the exact origin they were created on. When a quick-tunnel URL changes, the phone's
installed app and its subscription are orphaned and you'd re-install + re-subscribe.
The named tunnel fixes the URL so that never happens.

---

## 0. Install cloudflared (once)

- macOS: `brew install cloudflared`
- Windows: `winget install --id Cloudflare.cloudflared`
- Linux (Debian/Ubuntu): install from Cloudflare's apt repo — see
  https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/

> **Windows note:** the `.sh` launchers are for macOS/Linux. On Windows use the
> matching `.bat` (Command Prompt) or `.ps1` (PowerShell) scripts in this folder —
> e.g. `tunnel\run-quick.bat` instead of `./tunnel/run-quick.sh`. Everything else
> below is identical.

Also make sure the app's own deps are installed and your VAPID keys are set:

```bash
pip install -r requirements.txt
python push.py --generate-keys      # paste the printed lines into .env
```

---

## 1. Quick tunnel — test push in five minutes

From the project root:

```bash
./tunnel/run-quick.sh          # macOS / Linux
```
```bat
tunnel\run-quick.bat           REM Windows (Command Prompt)
```
```powershell
powershell -ExecutionPolicy Bypass -File tunnel\run-quick.ps1   # Windows (PowerShell)
```

It starts the app and prints a line like:

```
https://random-words-here.trycloudflare.com
```

On your phone (any network — it goes through Cloudflare, not your LAN):

1. Open that URL.
2. Add to Home Screen (iOS Safari: Share → Add to Home Screen; Android Chrome:
   ⋮ → Install app).
3. Open the installed app, tap the **bell**, allow notifications.
4. Tap **Run scan** — when a new signal lands above your threshold, your phone
   gets a notification even with the app closed.

Stop with Ctrl-C. Note: quick tunnels are capped at 200 in-flight requests and
carry no uptime guarantee — fine for one person testing, not for anything durable.
(If cloudflared complains, make sure there's no leftover `config.yaml` in your
`~/.cloudflared` directory — quick tunnels won't run alongside one.)

---

## 2. Named tunnel — the stable setup you keep

Prerequisite: a domain added to your Cloudflare account (its nameservers point to
Cloudflare). Then pick **one** of the two ways below.

### Option A — token mode (simplest, no local config files)

1. Go to `one.dash.cloudflare.com` → Networks → Tunnels → Create a tunnel →
   choose the `cloudflared` connector. Name it `scanner`.
2. Under Public Hostname, add e.g. `scanner.yourdomain.com` → service
   `http://localhost:8000`.
3. Copy the tunnel **token** the dashboard shows and put it in `.env`:

   ```bash
   CLOUDFLARED_TOKEN=eyJ...          # the long token string
   ```
4. Run it:

   ```bash
   ./tunnel/run-named.sh          # macOS / Linux
   ```
   ```bat
   tunnel\run-named.bat           REM Windows
   ```

Your app is now live at `https://scanner.yourdomain.com` — same URL every time.

### Option B — config mode (config lives in your repo)

```bash
cloudflared tunnel login                         # pick your domain in the browser
cloudflared tunnel create scanner                # writes ~/.cloudflared/<UUID>.json
cloudflared tunnel route dns scanner scanner.yourdomain.com

cp tunnel/cloudflared.example.yml tunnel/cloudflared.yml
# edit tunnel/cloudflared.yml: set credentials-file path + hostname

./tunnel/run-named.sh
```

`tunnel/cloudflared.yml` is git-ignored-worthy (it points at your private
credentials file) — keep the real one out of version control; the
`.example.yml` is the shareable template.

---

## 3. Keep it running (optional)

To have the tunnel come back after a reboot, install cloudflared as a service and
run the app under your process manager of choice:

```bash
# tunnel as a boot service (token or named tunnel):
sudo cloudflared service install                 # token mode
# or, config mode:
sudo cloudflared --config /full/path/tunnel/cloudflared.yml service install

# keep the poller accruing signals even when no phone is open:
python scanner.py --loop --interval 300
```

Run `uvicorn api:app --host 127.0.0.1 --port 8000` under systemd, `pm2`, `tmux`,
or a login item so the app itself restarts too.

---

## Notes specific to this app

- **Same-origin, so no CORS change needed.** The phone loads the app from the
  tunnel URL and calls `/api/...` on that same origin, so you can leave
  `SCANNER_CORS_ORIGINS` as is. (If you ever host the frontend separately, set it
  to your tunnel hostname.)
- **iOS quirk:** Web Push on iOS only reaches an *installed* PWA (iOS 16.4+), not a
  Safari tab. Install to the Home Screen first, then enable the bell inside it.
- **Bind to localhost.** The launch scripts run uvicorn on `127.0.0.1` on purpose —
  cloudflared reaches it locally, and nothing is exposed on your LAN directly.
