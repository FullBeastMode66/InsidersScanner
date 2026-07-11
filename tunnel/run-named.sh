#!/usr/bin/env bash
#
# run-named.sh — run the scanner behind a STABLE Cloudflare named tunnel.
#
# This is the setup you keep: a fixed https://scanner.yourdomain.com that survives
# restarts, so the installed PWA and its push subscription keep working.
#
# One-time setup lives in TUNNEL.md. Two ways to point this script at your tunnel:
#
#   A) Token mode (simplest). Create the tunnel in the Cloudflare dashboard, copy
#      its token, and set it in .env:   CLOUDFLARED_TOKEN=eyJ...
#
#   B) Config mode. Copy cloudflared.example.yml -> cloudflared.yml, fill in your
#      tunnel name/UUID, credentials-file path, and hostname (see TUNNEL.md).
#
# Usage:   cd <project root>;  ./tunnel/run-named.sh

set -euo pipefail

PORT="${PORT:-8000}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

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

for _ in $(seq 1 20); do
  if curl -sf "http://127.0.0.1:${PORT}/api/health" >/dev/null 2>&1; then break; fi
  sleep 0.5
done

CONFIG="${ROOT}/tunnel/cloudflared.yml"

if [ -n "${CLOUDFLARED_TOKEN:-}" ]; then
  echo "Running named tunnel in token mode…"
  cloudflared tunnel run --token "${CLOUDFLARED_TOKEN}"
elif [ -f "${CONFIG}" ]; then
  echo "Running named tunnel from ${CONFIG} …"
  cloudflared tunnel --config "${CONFIG}" run
else
  echo "No tunnel configured. Set CLOUDFLARED_TOKEN in .env, or create tunnel/cloudflared.yml."
  echo "See tunnel/TUNNEL.md for the one-time setup."
  exit 1
fi
