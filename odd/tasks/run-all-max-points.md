# run-all-max-points

Feature: raise the scored Run All lane from **14 points (6/20 cases)** by fixing
liveness and record delivery first, then closing PR-01…PR-06 properly.

## Evidence (Run All, 19 Sep 2026, 20 cases)

| Signal | Cases | Points lost | Attribution | Owner |
|---|---|---|---|---|
| `Agent silence` | 6 | 9 | `agent_issue` | us |
| `Connection lost` | 5 | 11 | `inconclusive` | likely us |
| `Wall clock` | 2 | 4 | `inconclusive` | us |
| `Harness socket error` | 1 | — | `harness_issue` (excluded) | harness |

Passed: 6 cases, 14 points. **Every failure carried `Record mismatch` and no case
failed with a record that was delivered but wrong — the record was never made.**
That reframed the work: the decision engine was not the bottleneck, liveness was.

## Root causes, found in the installed pipecat 1.11.0 (source, not memory)

1. **The turn-completion marker protocol is a silence generator.**
   `filter_incomplete_user_turns` + `user_turn_completion_config` are deprecated
   since 1.2.0 and removed in 2.0.0
   (`processors/aggregators/llm_response_universal.py:151-200`), and an
   incomplete verdict **suppresses the whole response** for
   `incomplete_short_timeout` (5 s) or `incomplete_long_timeout` (10 s)
   (`turns/user_turn_completion_mixin.py:198-233`). On a call capped at three
   minutes and cut on silence, that is `Agent silence` by construction.
2. **`FlowsFunctionSchema.cancel_on_interruption` defaults to `False`**, so every
   tool ran as an **async task** and the model was told *"This tool is still
   running… do not invent a result in the meantime"* instead of receiving it
   (`processors/aggregators/async_tool_messages.py:79`). Observed live: the model
   answered *"another dermatologist"* with no data, and one scenario took 65 s
   instead of 24 s.
3. **`MinWordsUserTurnStartStrategy(min_words=3)` discards short utterances while
   the bot speaks** (`min_words = self._min_words if self._bot_speaking else 1`),
   so the one-word answers this agent lives on ("yes", "morning", "Tuesday")
   reset the aggregation instead of opening a turn.
4. **`submission.py` persisted nothing about failures**: `logger.error("Submission
   failed ({})", type(exc).__name__)` printed `HTTPStatusError` with no status and
   no body. A scored failure was undiagnosable.
5. **`call_id` is required on every submit request** (live OpenAPI, verified
   against the snapshot: 48 schemas, no drift). It must be the id the platform
   dialled with, never minted.

## Tasks

- [x] T1: liveness — remove the deprecated marker protocol; add a silence watchdog that
  guarantees audible speech instead of suppressing it (`server/liveness.py`, `server/bot.py`)
- [x] T2: liveness — startup watchdog so the greeting always speaks even if the
  client-ready event is missed (`server/bot.py`)
- [x] T3: tools — every flow tool is synchronous (`cancel_on_interruption=True`), so the
  model waits for the result instead of answering from nothing
- [x] T4: record — ordered multi-action submission, `flush` (never invents a decision)
  separated from `close` (the end of the call), `call_id` stamped, HTTP status and body
  logged (`server/submission.py`, `server/clients/clinic_client.py`)
- [x] T5: rules — deterministic eligibility engine over `clinic.json` (age window,
  referral, plan x specialty, plan x site, provider network, provider leave) returning the
  closed `reason` vocabulary plus `redirect_to` (`server/rules.py`)
- [x] T6: rules — wired into the slot node, including the age redirect and a terminal
  tool-free refusal node so a refusal cannot become a substitution
- [x] T7: PR-05 — `server/dates.py`: relative and named dates, `this coming <weekday>`
  strictly after the call day, rolling off closures and shut days while keeping
  part-of-day, and windows clamped to the published calendar
- [x] T8: PR-05 — `resolve_date` tool so the model never does calendar arithmetic
- [x] T9: suite — 8 new scenarios (PR-05 x3, PR-06 x5) plus a per-scenario pinned clock
  (`<scenario>.yaml.clock`) so `make eval` is correct without anyone remembering a flag
- [x] T10: verify — `make test` 60 passed, `make eval` 15/15 scenarios green

## Deliberate limits

- `PR-07`…`PR-18` are untouched: no cancellation/reschedule lifecycle, no triage table,
  no nearest-site tool, no clinic-facts tool, and the second-policy query parameter is
  passed but unexercised. Run All dials only problems that are open, so the value of each
  depends on the dashboard, which is authenticated.
- The local eval cannot obtain an accepted record: it mints a `call_id` the platform
  never dialled, so every local submit answers `404 unknown call`. That is why the local
  evidence is the tool-call chain, and why the delivery path is instrumented instead.

## Notes

- Freeze: Sunday 20 September 2026, 06:00 Europe/Madrid.
- `SC-cooldown-run` is 15 minutes after a Run All finishes, so a scored run is only worth
  spending once `make eval` is green.
