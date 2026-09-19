SERVER_DIR := server
# Reserve a free static domain at dashboard.ngrok.com -> Domains so the
# dashboard Endpoint stays fixed across restarts (OP-tunnel), then either
# export NGROK_DOMAIN in your shell or pass it inline: make tunnel NGROK_DOMAIN=...
NGROK_DOMAIN ?= grooving-april-subzero.ngrok-free.dev
JOBS ?= 4

.PHONY: help run-webrtc run-twilio run-eval evals evals-parallel tunnel dashboard

help:
	@echo "make run-webrtc   - run the bot with the local browser test UI (http://localhost:7860)"
	@echo "                    (full log written to server/run-logs/webrtc-<timestamp>/bot.log)"
	@echo "make run-twilio   - run the bot as a Twilio Media Streams WebSocket server (ws://localhost:7860/ws)"
	@echo "                    (full log written to server/run-logs/twilio-<timestamp>/bot.log)"
	@echo "                    (both default to LOG_LEVEL=INFO — no live transcript content on disk;"
	@echo "                    set RECORD_CALLS=1 to also save each call as a .wav under"
	@echo "                    server/run-logs/recordings/ — opt-in, since it's real patient audio)"
	@echo "make evals        - run every scenario under server/evals/PR-*, restarting the bot fresh before each"
	@echo "                    one so Flow/context state never leaks between scenarios"
	@echo "                    (all logs for the run — bot, eval, debug — grouped under"
	@echo "                    server/eval-runs/<run-timestamp>/<scenario>.{bot,eval,debug}.log)"
	@echo "make evals-parallel - same scenarios, split across JOBS concurrent bot instances on"
	@echo "                    separate ports (default JOBS=4; pass JOBS=N to change it). Each worker"
	@echo "                    keeps its own per-file bot restart, so isolation is unchanged — only the"
	@echo "                    number of scenarios running at once does."
	@echo "make tunnel       - ngrok the bot's port and print the ready-to-paste wss:// dashboard endpoint"
	@echo "                    (set NGROK_DOMAIN=your-reserved-domain to keep the URL fixed across restarts)"
	@echo "make dashboard    - local, live-reading web dashboard over your own server/eval-runs/ and"
	@echo "                    server/run-logs/ (http://localhost:8787 by default; PORT=N to change)."
	@echo "                    Each teammate runs this against their own checkout; it re-reads the log"
	@echo "                    files on every request, so re-running make evals / run-webrtc and hitting"
	@echo "                    Refresh in the page is all it takes. To show someone else your view live,"
	@echo "                    tunnel it the same way as the bot: ngrok http $${PORT:-8787}."

run-webrtc:
	@run_dir="run-logs/webrtc-$$(date +%Y%m%d-%H%M%S)"; \
	mkdir -p "$(SERVER_DIR)/$$run_dir"; \
	cd $(SERVER_DIR) && uv run bot.py -t webrtc 2>&1 | tee "$$run_dir/bot.log"; \
	echo "log: file://$$PWD/$$run_dir/bot.log"

run-twilio:
	@run_dir="run-logs/twilio-$$(date +%Y%m%d-%H%M%S)"; \
	mkdir -p "$(SERVER_DIR)/$$run_dir"; \
	cd $(SERVER_DIR) && uv run bot.py -t twilio 2>&1 | tee "$$run_dir/bot.log"; \
	echo "log: file://$$PWD/$$run_dir/bot.log"

evals:
	@run_dir="eval-runs/$$(date +%Y%m%d-%H%M%S)"; \
	mkdir -p "$(SERVER_DIR)/$$run_dir"; \
	cd $(SERVER_DIR) && total=0; passed=0; failed_list=""; \
	for f in evals/PR-*/*.yaml; do \
		pkill -f "bot.py -t eval" 2>/dev/null; \
		sleep 1; \
		override=$$(grep -m1 -oE '^# CALL_CLOCK_OVERRIDE=\S+' "$$f" | cut -d= -f2); \
		scenario=$$(grep -m1 -oE '^name: *\S+' "$$f" | sed -E 's/^name: *//'); \
		botlog="$$run_dir/$$scenario.bot.log"; \
		env LOG_LEVEL=DEBUG $${override:+CALL_CLOCK_OVERRIDE=$$override} nohup uv run bot.py -t eval > "$$botlog" 2>&1 & \
		disown; \
		for i in $$(seq 1 30); do \
			lsof -nP -iTCP:7860 -sTCP:LISTEN >/dev/null 2>&1 && break; \
			sleep 1; \
		done; \
		echo "=================== $$f ==================="; \
		total=$$((total + 1)); \
		if PYTHONPATH=. uv run pipecat eval run "$$f" -v -d --logs-dir "$$run_dir"; then \
			passed=$$((passed + 1)); \
		else \
			failed_list="$$failed_list $$scenario"; \
		fi; \
		echo "  bot log:   file://$$PWD/$$botlog"; \
		echo "  eval log:  file://$$PWD/$$run_dir/$$scenario.eval.log"; \
		echo "  debug log: file://$$PWD/$$run_dir/$$scenario.debug.log"; \
		echo; \
	done; \
	pkill -f "bot.py -t eval" 2>/dev/null; \
	echo "=================== Summary ==================="; \
	echo "  $$passed/$$total scenarios passed"; \
	if [ -n "$$failed_list" ]; then \
		echo "  Failed:"; \
		for s in $$failed_list; do echo "    - $$s"; done; \
	fi; \
	echo "All logs for this run: file://$$PWD/$$run_dir/"

evals-parallel:
	@bash scripts/eval_parallel.sh $(JOBS)

tunnel:
	NGROK_DOMAIN=$(NGROK_DOMAIN) bash scripts/tunnel.sh 7860 /ws

PORT ?= 8787
dashboard:
	cd $(SERVER_DIR)/scripts/dashboard && uv run --project ../.. python serve.py --port $(PORT)
