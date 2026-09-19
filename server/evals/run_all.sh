#!/usr/bin/env bash
# Runs eval scenarios one by one, each against a fresh bot so no conversation state leaks.
#   cd server && evals/run_all.sh                 # every scenario under evals/PR-*
#   cd server && evals/run_all.sh evals/PR-01     # one folder, or list .yaml files
#
# Scenarios run on the real clock. One whose premise is a date ("tomorrow", a doctor's leave)
# declares it with a `# clock: <ISO datetime>` header; bot.py honours it on the eval transport only.
# PORT defaults to 7861 so a bot already serving calls on 7860 is left alone.
set -uo pipefail
cd "$(dirname "$0")/.."
mkdir -p eval-runs
PORT="${PORT:-7861}"

files=()
for target in "${@:-evals}"; do
  if [ -d "$target" ]; then
    while IFS= read -r f; do files+=("$f"); done < <(find "$target" -name '*.yaml' | sort)
  else
    files+=("$target")
  fi
done

stop_bot() { pkill -f "bot.py -t eval --port $PORT" 2>/dev/null; }
trap stop_bot EXIT

failed=0
for f in "${files[@]}"; do
  stop_bot
  sleep 1
  clock=$(sed -n 's/^# clock: //p' "$f")
  bot_log="eval-runs/$(basename "$f" .yaml).bot.log"
  CALL_CLOCK_OVERRIDE=$clock nohup uv run bot.py -t eval --port "$PORT" >"$bot_log" 2>&1 &
  for _ in $(seq 1 60); do  # boot includes fetching the live catalogue, up to ~20s
    lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1 && break
    sleep 1
  done
  echo "=================== $f (clock: ${clock:-real}) ==================="
  # --trigger-disconnect: the call ends as on the platform, so the bot submits what it has.
  ok=1
  PYTHONPATH=. uv run pipecat eval run "$f" --bot-url "ws://localhost:$PORT" -v -d --logs-dir eval-runs --trigger-disconnect || ok=0
  sleep 2  # let the bot write its submission
  uv run python evals/check_submit.py "$f" "$bot_log" || ok=0
  [ "$ok" = 1 ] || failed=$((failed + 1))
  echo
done
echo "$((${#files[@]} - failed))/${#files[@]} scenarios passed"
exit "$failed"
