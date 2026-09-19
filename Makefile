SERVER_DIR := server
# Reserve a free static domain at dashboard.ngrok.com -> Domains so the
# dashboard Endpoint stays fixed across restarts (OP-tunnel), then either
# export NGROK_DOMAIN in your shell or pass it inline: make tunnel NGROK_DOMAIN=...
NGROK_DOMAIN ?= grooving-april-subzero.ngrok-free.dev

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
JOBS ?= 4

.PHONY: help run-webrtc run-twilio tunnel guard oracle oracle-fetch oracle-check test concurrency concurrency-bot-stop eval eval-all eval-spec eval-one eval-bot-stop evals-parallel dashboard

# Concurrency readiness (PR-02). N is the burst size; Run All itself opens 10.
N ?= 20
CONCURRENCY_PORT ?= 7862

help:
	@echo "make run-webrtc   - run the bot with the local browser test UI (http://localhost:7860)"
	@echo "                    (full log written to server/run-logs/webrtc-<timestamp>/bot.log;"
	@echo "                    defaults to LOG_LEVEL=INFO so live transcripts stay off disk)"
	@echo "make run-twilio   - run the bot as a Twilio Media Streams WebSocket server (ws://localhost:7860/ws)"
	@echo "                    (full log written to server/run-logs/twilio-<timestamp>/bot.log;"
	@echo "                    set RECORD_CALLS=1 to also save each call as a .wav under"
	@echo "                    server/run-logs/recordings/)"
	@echo "make tunnel       - ngrok the bot's port and print the ready-to-paste wss:// dashboard endpoint"
	@echo "make guard        - refuse/wait if a scored run is dialling: run it before restarting the endpoint"
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
	@echo "                    (starts a headless bot on port $(EVAL_PORT), runs, then stops it;"
	@echo "                    logs under server/eval-runs/<run-timestamp>/)"
	@echo "make evals-parallel - same GREEN scenarios, split across JOBS concurrent bots (default 4)"
	@echo "make dashboard    - local live-reading dashboard over server/eval-runs/ and server/run-logs/"
	@echo "                    (http://localhost:8787; PORT=N to change)"
	@echo "make eval-bot-stop - kill any leftover eval bot on port $(EVAL_PORT)"

test:
	cd $(SERVER_DIR) && uv run pytest tests/

ifndef EVAL_RUN_DIR
EVAL_RUN_DIR := eval-runs/$(shell date +%Y%m%d-%H%M%S)
endif

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

evals-parallel:
	@bash scripts/eval_parallel.sh $(JOBS)

PORT ?= 8787
dashboard:
	cd $(SERVER_DIR)/scripts/dashboard && uv run --project ../.. python serve.py --port $(PORT)

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
	@scenario=$(EVALS_DIR)/$(S).yaml; \
	[ -f $$scenario ] || scenario=$(EVAL_SPEC_DIR)/$(S).yaml; \
	[ -f $$scenario ] || { echo "no such scenario: $(S)"; exit 2; }; \
	$(MAKE) --no-print-directory eval-bot-stop; \
	mkdir -p "$(SERVER_DIR)/$(EVAL_RUN_DIR)"; \
	echo ">> starting eval bot on port $(EVAL_PORT) (logs: $(SERVER_DIR)/$(EVAL_RUN_DIR)/$(S).bot.log)"; \
	clock=$$(cat $$scenario.clock 2>/dev/null || echo "$(CLOCK)"); \
	[ -n "$$clock" ] && echo ">> pinned clinic clock: $$clock"; \
	( cd $(SERVER_DIR) && DOTENV_PATH="$(DOTENV)" LOG_LEVEL=DEBUG CALL_CLOCK_OVERRIDE="$$clock" uv run bot.py -t eval --port $(EVAL_PORT) > "$(EVAL_RUN_DIR)/$(S).bot.log" 2>&1 & ); \
	ok=0; \
	for i in $$(seq 1 $(EVAL_BOOT_TIMEOUT)); do \
		nc -z localhost $(EVAL_PORT) 2>/dev/null && { ok=1; break; }; \
		sleep 1; \
	done; \
	if [ $$ok -ne 1 ]; then \
		echo ">> eval bot failed to listen on $(EVAL_PORT) after $(EVAL_BOOT_TIMEOUT)s"; \
		tail -20 "$(SERVER_DIR)/$(EVAL_RUN_DIR)/$(S).bot.log"; \
		$(MAKE) --no-print-directory eval-bot-stop; \
		exit 1; \
	fi; \
	( cd $(SERVER_DIR) && PYTHONPATH=. uv run pipecat eval run "$${scenario#$(SERVER_DIR)/}" -v -d --logs-dir $(EVAL_RUN_DIR) --bot-url $(EVAL_BOT_URL) ); \
	status=$$?; \
	echo "  bot log:   $(SERVER_DIR)/$(EVAL_RUN_DIR)/$(S).bot.log"; \
	echo "  eval log:  $(SERVER_DIR)/$(EVAL_RUN_DIR)/$(S).eval.log"; \
	pkill -f "bot.py -t eval --port $(EVAL_PORT)" 2>/dev/null || true; \
	exit $$status

eval-spec: $(patsubst %.yaml,eval-spec-%,$(notdir $(wildcard $(EVAL_SPEC_DIR)/*.yaml)))

eval-spec-%: $(EVAL_SPEC_DIR)/%.yaml
	@$(MAKE) --no-print-directory eval-one S=$* || true

eval-bot-stop:
	@pkill -f "bot.py -t eval --port $(EVAL_PORT)" 2>/dev/null || true
