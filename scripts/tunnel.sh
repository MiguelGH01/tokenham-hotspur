#!/usr/bin/env bash
# Starts ngrok on the bot's port and prints the ready-to-paste wss:// dashboard
# endpoint, instead of making you convert the https:// URL by hand.
set -euo pipefail

PORT="${1:-7860}"
WS_PATH="${2:-/ws}"
# Set NGROK_DOMAIN to a domain reserved at dashboard.ngrok.com -> Domains so
# the dashboard Endpoint stays fixed across restarts (OP-tunnel).
DOMAIN="${NGROK_DOMAIN:-}"

if ! command -v ngrok >/dev/null 2>&1; then
  echo "ngrok not found. Install it: brew install ngrok" >&2
  echo "Then authenticate once: ngrok config add-authtoken <your-token>  (free account at ngrok.com)" >&2
  exit 1
fi

if [ -n "$DOMAIN" ]; then
  echo "Using reserved domain: $DOMAIN"
  ngrok http --domain="$DOMAIN" "$PORT" --log=stdout >/tmp/ngrok.log 2>&1 &
else
  echo "No NGROK_DOMAIN set — using an ephemeral URL that changes on restart." >&2
  ngrok http "$PORT" --log=stdout >/tmp/ngrok.log 2>&1 &
fi
NGROK_PID=$!
trap 'kill "$NGROK_PID" 2>/dev/null || true' EXIT

echo "Starting ngrok on port $PORT..."
URL=""
for _ in $(seq 1 20); do
  URL=$(curl -s http://127.0.0.1:4040/api/tunnels 2>/dev/null \
    | grep -o '"public_url":"https:[^"]*"' | head -1 | cut -d'"' -f4 || true)
  [ -n "$URL" ] && break
  sleep 0.5
done

if [ -z "$URL" ]; then
  echo "Could not read ngrok's public URL automatically — check http://127.0.0.1:4040" >&2
else
  WSS_URL="${URL/https:/wss:}${WS_PATH}"
  echo
  echo "Dashboard endpoint (Settings -> Integration):"
  echo "  $WSS_URL"
  echo
fi

wait "$NGROK_PID"
