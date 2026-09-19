SERVER_DIR := server
# Reserve a free static domain at dashboard.ngrok.com -> Domains so the
# dashboard Endpoint stays fixed across restarts (OP-tunnel), then either
# export NGROK_DOMAIN in your shell or pass it inline: make tunnel NGROK_DOMAIN=...
NGROK_DOMAIN ?= grooving-april-subzero.ngrok-free.dev

.PHONY: help run-webrtc run-twilio run-eval evals tunnel

help:
	@echo "make run-webrtc   - run the bot with the local browser test UI (http://localhost:7860)"
	@echo "make run-twilio   - run the bot as a Twilio Media Streams WebSocket server (ws://localhost:7860/ws)"
	@echo "make run-eval     - run the bot as a headless eval server (ws://localhost:7860), for running one scenario yourself"
	@echo "make evals        - run every scenario under server/evals/PR-*. Keep the eval bot up separately."
	@echo "                    (full logs written to server/eval-runs/<scenario>.eval.log + .debug.log)"
	@echo "make tunnel       - ngrok the bot's port and print the ready-to-paste wss:// dashboard endpoint"
	@echo "                    (set NGROK_DOMAIN=your-reserved-domain to keep the URL fixed across restarts)"

run-webrtc:
	cd $(SERVER_DIR) && uv run bot.py -t webrtc

run-twilio:
	cd $(SERVER_DIR) && uv run bot.py -t twilio

run-eval:
	cd $(SERVER_DIR) && uv run bot.py -t eval

evals:
	cd $(SERVER_DIR) && PYTHONUTF8=1 PYTHONPATH=. uv run clinic-eval evals/ -v -d --logs-dir eval-runs

tunnel:
	NGROK_DOMAIN=$(NGROK_DOMAIN) bash scripts/tunnel.sh 7860 /ws
