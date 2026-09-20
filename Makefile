SERVER_DIR := server
# Reserve a free static domain at dashboard.ngrok.com -> Domains so the
# dashboard Endpoint stays fixed across restarts (OP-tunnel), then either
# export NGROK_DOMAIN in your shell or pass it inline: make tunnel NGROK_DOMAIN=...
NGROK_DOMAIN ?= grooving-april-subzero.ngrok-free.dev
# The eval scenarios expect slots computed for a call on this date (see their headers).
# bot.py honours it on the eval transport only, never on a real call.
EVAL_CLOCK ?= 2026-09-18T10:00:00+02:00

# Observability console (make console). It rides on the same process as the
# WebRTC bot, so the page, the API and the /api/offer endpoint share an origin.
# CONSOLE_DB keeps demo and test traffic out of whatever the bot has been
# writing all day; point it at data/centralita.sqlite to read the real thing.
CONSOLE_PORT ?= 7860
CONSOLE_DB ?= data/console.sqlite
CONSOLE_URL := http://localhost:$(CONSOLE_PORT)/console/

# Eval harness settings (make eval-one S=simple_booking_amelia)
EVAL_PORT ?= 7861
EVAL_BOT_URL := ws://localhost:$(EVAL_PORT)
EVALS_DIR := $(SERVER_DIR)/evals
EVAL_SPEC_DIR := $(EVALS_DIR)/spec
EVAL_BOOT_TIMEOUT ?= 40

# Pin the clinic clock for date-sensitive scenarios (PR-05, PR-07). The bot
# reads CALL_CLOCK_OVERRIDE at call start, so relative phrases like "tomorrow"
# resolve against a fixed instant instead of the wall clock. Without this, a
# scenario asserting "tomorrow" only passes on the day it was written.
#   make eval-one S=pr05_tomorrow CLOCK=2026-09-18T10:00:00+02:00
# A scenario that needs a fixed clock carries it in a sidecar file next to itself,
# <scenario>.yaml.clock, so `make eval` stays correct without anyone remembering the flag.
CLOCK ?=

# Point a run at a different env file to A/B the phone model without touching the
# live .env (load_dotenv uses override=True, so .env always wins otherwise).
#   make eval-one S=pr06_age_redirect DOTENV=/tmp/env-helmcode
DOTENV ?=

# Full local platform (make run): one bot on RUN_PORT, ngrok tunnel, console open.
RUN_PORT ?= 7860
RUN_URL := http://localhost:$(RUN_PORT)/console/
TUNNEL_LOG ?= /tmp/hackspain-tunnel.log
TUNNEL_PID_FILE ?= /tmp/hackspain-tunnel.pid

.PHONY: help console console-seed console-reset run run-webrtc run-twilio run-eval evals tunnel guard cost oracle oracle-fetch oracle-check test concurrency concurrency-bot-stop eval eval-all eval-spec eval-one eval-bot-stop

# Concurrency readiness (PR-02). N is the burst size; Run All itself opens 10.
N ?= 20
CONCURRENCY_PORT ?= 7862

help:
	@echo "make run          - full platform: bot + ngrok tunnel + open console ($(RUN_URL))"
	@echo "                    Prosper /ws + Place-test-call WebRTC on :$(RUN_PORT); Ctrl-C stops all"
	@echo "make console      - run the bot and open the oversight console ($(CONSOLE_URL))"
	@echo "                    CONSOLE_DB=data/centralita.sqlite to read the live database"
	@echo "make console-seed - fill the console database with a synthetic shift to look at"
	@echo "make console-reset - delete the console database and start the shift empty"
	@echo "make run-webrtc   - run the bot with the local browser test UI (http://localhost:7860)"
	@echo "make run-twilio   - run the bot as a Twilio Media Streams WebSocket server (ws://localhost:7860/ws)"
	@echo "                    VOICE_AGENT=carloslabs (default) or elevenagent in server/.env"
	@echo "make run-eval     - run the bot as a headless eval server (ws://localhost:7860), for running one scenario yourself"
	@echo "make evals        - run every scenario under server/evals/PR-*, restarting the bot fresh before each"
	@echo "                    one so Flow/context state never leaks between scenarios"
	@echo "                    (full logs written to server/eval-runs/<scenario>.eval.log + .debug.log)"
	@echo "make tunnel       - ngrok the bot's port and print the ready-to-paste wss:// dashboard endpoint"
	@echo "make guard        - refuse/wait if a scored run is dialling: run it before restarting the endpoint"
	@echo "make cost         - euros and seconds per recorded call, with p50/p95 (list prices, see call_cost.py)"
	@echo "make oracle       - offline scoring: where the 196 points are and what each problem expects"
	@echo "make oracle-fetch - refresh the organisers' published roster (do it each morning)"
	@echo "make oracle-check - run the judge against every published answer (must reject none)"
	@echo "                    score one call's trail: cd server && uv run python -m evals.corpus \\"
	@echo "                      --case <case-id> --audit audit-logs/audit-<call>.ndjson"
	@echo "                    (set NGROK_DOMAIN=your-reserved-domain to keep the URL fixed across restarts)"
	@echo "make test         - run the server's pytest suite (unit + acceptance)"
	@echo "make concurrency  - dial $(N) concurrent sockets at a local telephony bot and report"
	@echo "                    (N=20 default; Run All opens 10, PR-02's biggest burst 20)"
	@echo "make eval         - run every GREEN eval scenario in $(EVALS_DIR)/"
	@echo "make eval-spec    - run the spec scenarios in $(EVALS_DIR)/spec/ (capabilities not built yet)"
	@echo "make eval-one S=<scenario> - run one scenario from $(EVALS_DIR)/, e.g. S=pr06_age_redirect"
	@echo "                    add CLOCK=<iso> to pin the clinic clock for date-sensitive scenarios"
	@echo "                    (starts a headless bot on port $(EVAL_PORT), runs, then stops it)"
	@echo "make eval-bot-stop - kill any leftover eval bot on port $(EVAL_PORT)"

# Foreground on purpose, so Ctrl-C stops the bot. The browser is opened from a
# subshell once the port answers, so the page never loads before the server does.
console:
	@echo ">> console  $(CONSOLE_URL)"
	@echo ">> database $(SERVER_DIR)/$(CONSOLE_DB)"
	@( for i in $$(seq 1 40); do \
		if nc -z localhost $(CONSOLE_PORT) 2>/dev/null; then \
			if command -v open >/dev/null 2>&1; then open "$(CONSOLE_URL)"; \
			elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$(CONSOLE_URL)"; \
			else echo ">> open $(CONSOLE_URL)"; fi; \
			exit 0; \
		fi; \
		sleep 1; \
	done ) &
	@cd $(SERVER_DIR) && OBSERVABILITY_DB=$(CONSOLE_DB) uv run bot.py -t webrtc --port $(CONSOLE_PORT)

# ~120 calls of synthetic traffic, so the Overview has something to show
# without waiting for a real shift to happen.
console-seed:
	cd $(SERVER_DIR) && OBSERVABILITY_DB=$(CONSOLE_DB) \
		uv run python -m observability.seed_shift -n 120 --load --db $(CONSOLE_DB)

console-reset:
	@rm -f $(SERVER_DIR)/$(CONSOLE_DB) $(SERVER_DIR)/$(CONSOLE_DB)-wal $(SERVER_DIR)/$(CONSOLE_DB)-shm
	@echo ">> removed $(SERVER_DIR)/$(CONSOLE_DB)"

# One process for Prosper telephony (/ws), WebRTC Place-test-call, and /console/.
# Tunnel runs in the background; Ctrl-C stops the bot and tears the tunnel down.
run:
	@echo ">> platform  $(RUN_URL)"
	@echo ">> tunnel    ngrok → :$(RUN_PORT)/ws  (log: $(TUNNEL_LOG))"
	@echo ">> VOICE_AGENT from server/.env (carloslabs | elevenagent)"
	@rm -f $(TUNNEL_PID_FILE)
	@( NGROK_DOMAIN=$(NGROK_DOMAIN) bash scripts/tunnel.sh $(RUN_PORT) /ws >$(TUNNEL_LOG) 2>&1 & \
		echo $$! > $(TUNNEL_PID_FILE) )
	@( for i in $$(seq 1 40); do \
		if nc -z localhost $(RUN_PORT) 2>/dev/null; then \
			if command -v open >/dev/null 2>&1; then open "$(RUN_URL)"; \
			elif command -v xdg-open >/dev/null 2>&1; then xdg-open "$(RUN_URL)"; \
			else echo ">> open $(RUN_URL)"; fi; \
			exit 0; \
		fi; \
		sleep 1; \
	done ) &
	@trap 'echo; echo ">> stopping tunnel"; \
		if [ -f $(TUNNEL_PID_FILE) ]; then kill $$(cat $(TUNNEL_PID_FILE)) 2>/dev/null || true; fi; \
		pkill -f "ngrok http.*$(RUN_PORT)" 2>/dev/null || true; \
		rm -f $(TUNNEL_PID_FILE)' EXIT INT TERM; \
	cd $(SERVER_DIR) && uv run bot.py --port $(RUN_PORT)

run-webrtc:
	cd $(SERVER_DIR) && uv run bot.py -t webrtc

run-twilio:
	cd $(SERVER_DIR) && uv run bot.py -t twilio

run-eval:
	cd $(SERVER_DIR) && uv run bot.py -t eval

test:
	cd $(SERVER_DIR) && uv run pytest tests/

# A scored run dials the endpoint ten calls at a time and scores each call's
# record, so restarting the bot (or re-opening the tunnel) mid-run kills cases
# that are already being scored. Run this first, or wire it into whatever
# restarts the endpoint. Waits ~20 min for a run to finish, refuses after that,
# and allows the restart when the dashboard cannot be read at all.
#   make guard                 # wait/refuse as needed
#   make guard FORCE=1         # skip the check
FORCE ?=
guard:
	cd $(SERVER_DIR) && uv run python -m prosper_guard $(if $(filter 1,$(FORCE)),--force,)

# What the recorded calls cost, in euros and seconds (JR-rigour). Reads the audit
# trails call_metrics.py writes; list prices and their sources live in call_cost.py.
#   make cost                        # server/audit-logs
#   make cost AUDIT=path/to/trails
AUDIT ?= $(or $(AUDIT_DIR),audit-logs)
cost:
	@cd $(SERVER_DIR) && uv run python -m call_cost $(AUDIT)

# The offline oracle: score a submission against the organisers' published cases
# without spending a scored run (see docs/scoring-design-notes.md).
oracle:
	cd $(SERVER_DIR) && uv run python -m evals.corpus --coverage

oracle-fetch:
	cd $(SERVER_DIR) && uv run python -m evals.corpus --fetch

oracle-check:
	cd $(SERVER_DIR) && uv run python -m evals.corpus --selfcheck

# PR-02 readiness: hold N concurrent Twilio-shaped sockets against a local
# telephony bot. Not scored — it measures whether the endpoint can carry the
# wave Run All opens before a scored run is spent finding out.
concurrency:
	@$(MAKE) --no-print-directory concurrency-bot-stop
	@echo ">> starting telephony bot on port $(CONCURRENCY_PORT) (logs: /tmp/pipecat-twilio.log)"; \
	( cd $(SERVER_DIR) && uv run bot.py -t twilio --port $(CONCURRENCY_PORT) > /tmp/pipecat-twilio.log 2>&1 & ); \
	ok=0; \
	for i in $$(seq 1 $(EVAL_BOOT_TIMEOUT)); do \
		nc -z localhost $(CONCURRENCY_PORT) 2>/dev/null && { ok=1; break; }; \
		sleep 1; \
	done; \
	if [ $$ok -ne 1 ]; then \
		echo ">> bot failed to listen on $(CONCURRENCY_PORT) after $(EVAL_BOOT_TIMEOUT)s"; \
		tail -20 /tmp/pipecat-twilio.log; \
		$(MAKE) --no-print-directory concurrency-bot-stop; \
		exit 1; \
	fi; \
	( cd $(SERVER_DIR) && uv run python ../scripts/concurrency_check.py \
		--connections $(N) --url ws://localhost:$(CONCURRENCY_PORT)/ws \
		--bot-log /tmp/pipecat-twilio.log ); \
	status=$$?; \
	$(MAKE) --no-print-directory concurrency-bot-stop; \
	exit $$status

concurrency-bot-stop:
	@pkill -f "bot.py -t twilio --port $(CONCURRENCY_PORT)" 2>/dev/null || true

# -k so a pre-Run-All check reports every failure, not just the first one.
eval:
	@$(MAKE) -k --no-print-directory $(patsubst %.yaml,eval-%,$(notdir $(wildcard $(EVALS_DIR)/*.yaml)))

eval-%: $(EVALS_DIR)/%.yaml
	@$(MAKE) --no-print-directory eval-one S=$*

# One fresh eval bot per scenario: the pipecat runner serves exactly one
# session per process, so reuse across scenarios deadlocks the second one.
eval-one:
	@test -n "$(S)" || { echo "usage: make eval-one S=<scenario-name>"; exit 2; }
	@scenario="$(firstword $(wildcard $(EVALS_DIR)/$(S).yaml $(EVAL_SPEC_DIR)/$(S).yaml $(EVALS_DIR)/PR-*/$(S).yaml))"; \
	[ -n "$$scenario" ] || { echo "no such scenario: $(S)"; exit 2; }; \
	$(MAKE) --no-print-directory eval-bot-stop; \
	echo ">> starting eval bot on port $(EVAL_PORT) (logs: /tmp/pipecat-eval-$(S).log)"; \
	clock=$$(cat $$scenario.clock 2>/dev/null || echo "$(CLOCK)"); \
	[ -n "$$clock" ] && echo ">> pinned clinic clock: $$clock"; \
	( cd $(SERVER_DIR) && DOTENV_PATH="$(DOTENV)" CALL_CLOCK_OVERRIDE="$$clock" uv run bot.py -t eval --port $(EVAL_PORT) > /tmp/pipecat-eval-$(S).log 2>&1 & ); \
	ok=0; \
	for i in $$(seq 1 $(EVAL_BOOT_TIMEOUT)); do \
		nc -z localhost $(EVAL_PORT) 2>/dev/null && { ok=1; break; }; \
		sleep 1; \
	done; \
	if [ $$ok -ne 1 ]; then \
		echo ">> eval bot failed to listen on $(EVAL_PORT) after $(EVAL_BOOT_TIMEOUT)s"; \
		tail -20 /tmp/pipecat-eval-$(S).log; \
		$(MAKE) --no-print-directory eval-bot-stop; \
		exit 1; \
	fi; \
	( cd $(SERVER_DIR) && PYTHONPATH=. uv run pipecat eval run "$${scenario#$(SERVER_DIR)/}" -v --bot-url $(EVAL_BOT_URL) ); \
	status=$$?; \
	pkill -f "bot.py -t eval --port $(EVAL_PORT)" 2>/dev/null || true; \
	exit $$status

eval-spec: $(patsubst %.yaml,eval-spec-%,$(notdir $(wildcard $(EVAL_SPEC_DIR)/*.yaml)))

eval-spec-%: $(EVAL_SPEC_DIR)/%.yaml
	@$(MAKE) --no-print-directory eval-one S=$* || true

eval-bot-stop:
	@pkill -f "bot.py -t eval --port $(EVAL_PORT)" 2>/dev/null || true

evals:
	@cd $(SERVER_DIR) && for f in evals/PR-*/*.yaml; do \
		pkill -f "bot.py -t eval" 2>/dev/null; \
		sleep 1; \
		CALL_CLOCK_OVERRIDE=$(EVAL_CLOCK) nohup uv run bot.py -t eval > /tmp/pipecat-eval-server.log 2>&1 & \
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
