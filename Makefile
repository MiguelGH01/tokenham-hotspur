SERVER_DIR := server
# Reserve a free static domain at dashboard.ngrok.com -> Domains so the
# dashboard Endpoint stays fixed across restarts (OP-tunnel), then either
# export NGROK_DOMAIN in your shell or pass it inline: make tunnel NGROK_DOMAIN=...
NGROK_DOMAIN ?= grooving-april-subzero.ngrok-free.dev

.PHONY: help run-webrtc run-twilio tunnel

help:
	@echo "make run-webrtc   - run the bot with the local browser test UI (http://localhost:7860)"
	@echo "make run-twilio   - run the bot as a Twilio Media Streams WebSocket server (ws://localhost:7860/ws)"
	@echo "make tunnel       - ngrok the bot's port and print the ready-to-paste wss:// dashboard endpoint"
	@echo "                    (set NGROK_DOMAIN=your-reserved-domain to keep the URL fixed across restarts)"

run-webrtc:
	cd $(SERVER_DIR) && uv run bot.py -t webrtc

run-twilio:
	cd $(SERVER_DIR) && uv run bot.py -t twilio

tunnel:
	NGROK_DOMAIN=$(NGROK_DOMAIN) bash scripts/tunnel.sh 7860 /ws

run-eval:
	cd $(SERVER_DIR) && uv run bot.py -t eval

evals:
	cd $(SERVER_DIR) && PYTHONPATH=. uv run pipecat eval run evals/PR-* -v -d --logs-dir eval-runs
