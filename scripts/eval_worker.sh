#!/usr/bin/env bash
# Runs one worker's chunk of eval scenario files sequentially against a bot instance on
# its own port, restarting the bot fresh before each file — the same per-file isolation
# `make evals` uses (Flow/context state never leaks between scenarios), just parametrized
# by port so several of these can run at once. Not meant to be called directly; see
# scripts/eval_parallel.sh and `make evals-parallel`.
#
# Usage: eval_worker.sh <port> <scenario-file> [<scenario-file> ...]
set -uo pipefail

PORT="$1"
shift

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT/server"

for f in "$@"; do
	pkill -f "bot.py -t eval --port $PORT" 2>/dev/null
	sleep 1
	override=$(grep -m1 -oE '^# CALL_CLOCK_OVERRIDE=\S+' "$f" | cut -d= -f2)
	env ${override:+CALL_CLOCK_OVERRIDE=$override} nohup uv run bot.py -t eval --port "$PORT" >"/tmp/pipecat-eval-server-$PORT.log" 2>&1 &
	disown
	for i in $(seq 1 30); do
		lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && break
		sleep 1
	done
	echo "=================== [port $PORT] $f ==================="
	PYTHONPATH=. uv run pipecat eval run "$f" -v -d --logs-dir eval-runs --bot-url "ws://localhost:$PORT" || true
	echo
done

pkill -f "bot.py -t eval --port $PORT" 2>/dev/null
true
