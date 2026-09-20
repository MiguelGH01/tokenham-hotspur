#!/usr/bin/env bash
# Runs one worker's chunk of eval scenario files sequentially against a bot instance on
# its own port, restarting the bot fresh before each file — the same per-file isolation
# `make evals` uses (Flow/context state never leaks between scenarios), just parametrized
# by port so several of these can run at once. Not meant to be called directly; see
# scripts/eval_parallel.sh and `make evals-parallel`.
#
# All logs for the run — bot, eval, debug — are grouped under one shared
# server/eval-runs/<run-timestamp>/ directory (passed in as RUN_DIR), same as `make evals`.
#
# Usage: eval_worker.sh <port> <run-dir> <scenario-file> [<scenario-file> ...]
set -uo pipefail

PORT="$1"
shift
RUN_DIR="$1"
shift

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/server"
mkdir -p "$RUN_DIR"

total=0
passed=0
failed=()

for f in "$@"; do
	pkill -f "bot.py -t eval --port $PORT" 2>/dev/null
	sleep 1
	override=$(grep -m1 -oE '^# CALL_CLOCK_OVERRIDE=\S+' "$f" | cut -d= -f2)
	# The YAML's own `name:` field, not the filename: pipecat eval run's --logs-dir names
	# .eval.log/.debug.log after it, and the two differ for most PR-05/PR-06 scenario files.
	scenario="$(grep -m1 -oE '^name: *\S+' "$f" | sed -E 's/^name: *//')"
	botlog="$RUN_DIR/$scenario.bot.log"
	env LOG_LEVEL=DEBUG ${override:+CALL_CLOCK_OVERRIDE=$override} nohup uv run bot.py -t eval --port "$PORT" >"$botlog" 2>&1 &
	disown
	for i in $(seq 1 30); do
		lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && break
		sleep 1
	done
	echo "=================== [port $PORT] $f ==================="
	total=$((total + 1))
	if PYTHONPATH=. uv run pipecat eval run "$f" -v -d --logs-dir "$RUN_DIR" --bot-url "ws://localhost:$PORT"; then
		passed=$((passed + 1))
		printf '%s\tPASS\n' "$scenario" >>"$RUN_DIR/results.txt"
	else
		failed+=("$scenario")
		printf '%s\tFAIL\n' "$scenario" >>"$RUN_DIR/results.txt"
	fi
	echo "  bot log:   file://$ROOT/server/$botlog"
	echo "  eval log:  file://$ROOT/server/$RUN_DIR/$scenario.eval.log"
	echo "  debug log: file://$ROOT/server/$RUN_DIR/$scenario.debug.log"
	echo
done

pkill -f "bot.py -t eval --port $PORT" 2>/dev/null

echo "--- [port $PORT] worker summary: $passed/$total passed ---"
if [ "${#failed[@]}" -gt 0 ]; then
	printf '  failed: %s\n' "${failed[*]}"
fi
true
