#!/usr/bin/env bash
# Runs server/evals/PR-*/*.yaml across N concurrent bot instances on separate ports,
# splitting the scenario list round-robin across them. Each worker keeps the same
# per-file bot restart as `make evals` (see scripts/eval_worker.sh) — only the number of
# simultaneous bots changes, not the isolation between scenarios.
#
# Usage: eval_parallel.sh [JOBS]   (JOBS defaults to 4)
set -uo pipefail

JOBS="${1:-4}"
BASE_PORT=7860
ROOT="$(cd "$(dirname "$0")/.." && pwd)"

FILES=()
while IFS= read -r line; do
	FILES+=("$line")
done < <(cd "$ROOT/server" && ls evals/PR-*/*.yaml)
if [ "${#FILES[@]}" -eq 0 ]; then
	echo "No scenario files found under server/evals/PR-*/*.yaml" >&2
	exit 1
fi

echo "Running ${#FILES[@]} scenarios across $JOBS worker(s) (ports $BASE_PORT-$((BASE_PORT + JOBS - 1)))"
echo

pids=()
ports=()
for ((i = 0; i < JOBS; i++)); do
	port=$((BASE_PORT + i))
	chunk=()
	for ((j = i; j < ${#FILES[@]}; j += JOBS)); do
		chunk+=("${FILES[j]}")
	done
	if [ "${#chunk[@]}" -eq 0 ]; then
		continue
	fi
	"$ROOT/scripts/eval_worker.sh" "$port" "${chunk[@]}" >"/tmp/eval-parallel-worker-$port.log" 2>&1 &
	pids+=("$!")
	ports+=("$port")
done

status=0
for pid in "${pids[@]}"; do
	wait "$pid" || status=1
done

echo "=== Combined output (in port order; interleaving between workers is lost, timing within each worker is preserved) ==="
for port in "${ports[@]}"; do
	cat "/tmp/eval-parallel-worker-$port.log"
done

exit "$status"
