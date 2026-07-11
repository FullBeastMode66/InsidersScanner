#!/usr/bin/env bash
#
# run-quick.sh — expose the scanner over HTTPS in one command, no account needed.
#
# Starts the app (uvicorn) and a Cloudflare *quick tunnel*, which prints a public
# https://<random>.trycloudflare.com URL. Open that URL on your phone, install the
# app, and tap the bell to test push.
#
# The URL is EPHEMERAL: it changes every time you run this. That's fine for testing
# push today, but a new URL means re-installing the PWA and re-subscribing. For a
# stable URL you keep, use ./run-named.sh instead (see TUNNEL.md).
#
# Usage:   cd <project root>;  ./tunnel/run-quick.sh
# Requires: cloudflared (https://developers.cloudflare.com/cloudflare-one/... /downloads/)

set -euo pipefail

PORT="${PORT:-8000}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

# Load VAPID keys / config from .env if present (so push is configured).
if [ -f .env ]; then
  set -a; . ./.env; set +a
fi

command -v cloudflared >/dev/null 2>&1 || {
  echo "cloudflared not found. Install it: https://developers.cloudflare.com/cloudflare-one/networks/connectors/cloudflare-tunnel/downloads/"
  exit 1
}
command -v uvicorn >/dev/null 2>&1 || {
  echo "uvicorn not found. Run: pip install -r requirements.txt"
  exit 1
}

echo "Starting app on http://127.0.0.1:${PORT} …"
uvicorn api:app --host 127.0.0.1 --port "${PORT}" &
APP_PID=$!
trap 'echo; echo "Shutting down…"; kill "${APP_PID}" 2>/dev/null || true' EXIT INT TERM

# Give the app a moment to bind the port.
for _ in $(seq 1 20); do
  if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done

echo
echo "Opening a Cloudflare quick tunnel — look for the https://<...>.trycloudflare.com URL below."
echo "Open that URL on your phone, then Add to Home Screen and tap the bell to enable alerts."
echo
# Foreground: cloudflared prints the URL and streams logs until you Ctrl-C.
cloudflared tunnel --url "http://localhost:${PORT}"
