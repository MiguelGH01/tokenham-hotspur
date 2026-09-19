#!/usr/bin/env bash
# Runs server/evals/PR-*/*.yaml across N concurrent bot instances on separate ports,
# splitting the scenario list round-robin across them. Each worker keeps the same
# per-file bot restart as `make evals` (see scripts/eval_worker.sh) — only the number of
# simultaneous bots changes, not the isolation between scenarios. All workers share one
# run folder (server/eval-runs/<run-timestamp>/) so every scenario's bot/eval/debug logs
# from this invocation end up grouped together.
#
# Usage: eval_parallel.sh [JOBS]   (JOBS defaults to 4)
set -uo pipefail

JOBS="${1:-4}"
BASE_PORT=7860
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
RUN_DIR="eval-runs/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$ROOT/server/$RUN_DIR"

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
	"$ROOT/scripts/eval_worker.sh" "$port" "$RUN_DIR" "${chunk[@]}" >"/tmp/eval-parallel-worker-$port.log" 2>&1 &
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

results_file="$ROOT/server/$RUN_DIR/results.txt"
echo
echo "=================== Summary ==================="
if [ -f "$results_file" ]; then
	total=$(wc -l <"$results_file" | tr -d ' ')
	passed=$(grep -c "$(printf '\t')PASS$" "$results_file" || true)
	echo "  $passed/$total scenarios passed"
	if grep -q "$(printf '\t')FAIL$" "$results_file"; then
		echo "  Failed:"
		grep "$(printf '\t')FAIL$" "$results_file" | cut -f1 | sed 's/^/    - /'
	fi
else
	echo "  (no results recorded)"
fi

echo "All logs for this run: file://$ROOT/server/$RUN_DIR/"

exit "$status"
