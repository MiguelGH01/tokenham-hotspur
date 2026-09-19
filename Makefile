SERVER_DIR := server
# Reserve a free static domain at dashboard.ngrok.com -> Domains so the
# dashboard Endpoint stays fixed across restarts (OP-tunnel), then either
# export NGROK_DOMAIN in your shell or pass it inline: make tunnel NGROK_DOMAIN=...
NGROK_DOMAIN ?= grooving-april-subzero.ngrok-free.dev

.PHONY: help run-webrtc run-twilio run-eval evals tunnel

help:
	@echo "make run-webrtc   - run the bot with the local browser test UI (http://localhost:7860)"
	@echo "                    (full log written to server/run-logs/webrtc-<timestamp>.log)"
	@echo "make run-twilio   - run the bot as a Twilio Media Streams WebSocket server (ws://localhost:7860/ws)"
	@echo "                    (full log written to server/run-logs/twilio-<timestamp>.log)"
	@echo "make evals        - run every scenario under server/evals/PR-*, restarting the bot fresh before each"
	@echo "                    one so Flow/context state never leaks between scenarios"
	@echo "                    (full logs written to server/eval-runs/<scenario>.eval.log + .debug.log)"
	@echo "make tunnel       - ngrok the bot's port and print the ready-to-paste wss:// dashboard endpoint"
	@echo "                    (set NGROK_DOMAIN=your-reserved-domain to keep the URL fixed across restarts)"

run-webrtc:
	@mkdir -p $(SERVER_DIR)/run-logs
	cd $(SERVER_DIR) && uv run bot.py -t webrtc 2>&1 | tee run-logs/webrtc-$$(date +%Y%m%d-%H%M%S).log

run-twilio:
	@mkdir -p $(SERVER_DIR)/run-logs
	cd $(SERVER_DIR) && uv run bot.py -t twilio 2>&1 | tee run-logs/twilio-$$(date +%Y%m%d-%H%M%S).log

evals:
	@cd $(SERVER_DIR) && for f in evals/PR-*/*.yaml; do \
		pkill -f "bot.py -t eval" 2>/dev/null; \
		sleep 1; \
		override=$$(grep -m1 -oE '^# CALL_CLOCK_OVERRIDE=\S+' "$$f" | cut -d= -f2); \
		env $${override:+CALL_CLOCK_OVERRIDE=$$override} nohup uv run bot.py -t eval > /tmp/pipecat-eval-server.log 2>&1 & \
		disown; \
		for i in $$(seq 1 30); do \
			lsof -nP -iTCP:7860 -sTCP:LISTEN >/dev/null 2>&1 && break; \
			sleep 1; \
		done; \
		echo "=================== $$f ==================="; \
		PYTHONPATH=. uv run pipecat eval run "$$f" -v -d --logs-dir eval-runs || true; \
		echo; \
	done; \
	pkill -f "bot.py -t eval" 2>/dev/null; \
	true

tunnel:
	NGROK_DOMAIN=$(NGROK_DOMAIN) bash scripts/tunnel.sh 7860 /ws
