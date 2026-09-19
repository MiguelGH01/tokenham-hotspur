# scoring-hardening

Feature: make every ending of a call a stated, best-known answer instead of a
default refusal, submit it the moment the caller agrees, keep the line audible
under a stalled model, protect a scored run from a restart, and be able to judge
our own submissions offline. Rationale and numbers: `docs/scoring-design-notes.md`.

## Where the points leaked (all of it from our own checks)

| Leak | Ours before this change |
|---|---|
| A call that ended undecided posted `NO_ACTION/out_of_scope` | `submission.DEFAULT_ACTION`, triggered by `close()` |
| The plan waited for the model's own `confirm_offer` turn | one LLM round trip on a three-minute cap |
| A stalled model produced no first token | `retry_on_timeout` bounds only the header await; 36 s of dead air on record |
| Nothing checked for a running scored run before a restart | a restart during a run kills calls that are mid-scoring |
| No way to judge a submission locally | the board reports pass/fail and a signal code only |

## Tasks

- [x] T1: end-of-call resolution (`server/resolution.py`, `server/submission.py`,
      `server/bot.py`): the fallback asks the call what it still stands behind, in
      order — the action it drew up (live offer, verified appointment, registration
      draft) → the first slot the platform offers the identified patient (diary habits
      first, then general practice) → and only then `NO_ACTION/out_of_scope`. One ending
      per call, never one per request, and never over a rule the conversation named.
  - Tests: `tests/acceptance/test_resolution.py` (16)
- [x] T2: affirmation submits (`server/confirmation.py`, `server/affirmation_watch.py`,
      `server/bot.py`): a short, unqualified yes landing on the agent's read-back runs the
      flow's own confirmation handler immediately; the model's later `confirm_offer` finds
      the plan already sent and is accepted as a retry.
  - Trigger: the user aggregator's `on_user_turn_message_added` event, **not** a frame
    watcher — the aggregator consumes the final `TranscriptionFrame` instead of forwarding
    it, and a bare tool-call turn emits no text frame at all, so a pipeline watcher
    silently misses the very yes it exists for.
  - Tests: `tests/acceptance/test_affirmation_watch.py` (11)
- [x] T3: first-token deadline (`server/llm_deadline.py`, `server/bot.py`): one attempt
      opens the stream **and** pulls its first chunk under one deadline; a stalled attempt
      is closed and re-issued; when every attempt stalls the agent speaks one short line so
      the turn ends audibly instead of silently.
  - Evidence: evidence 2 of `pr01-06-record-and-liveness.md` (246 s stall, no TTFB)
  - Limit: the chat-completions path (`LLM_PROVIDER=helmcode`, our default); the Responses
    and Gemini services keep pipecat's own `retry_on_timeout`.
  - Tests: `tests/acceptance/test_llm_deadline.py` (12)
- [x] T4: restart guard (`server/prosper_guard.py`, `make guard`): wait while a scored run
      is dialling, refuse when it outlasts the budget, allow the restart when the status
      cannot be read at all.
  - Tests: `tests/acceptance/test_prosper_guard.py` (9)
- [x] T5: offline oracle (`server/evals/corpus/`, roster copy committed): fetch the
      published roster, normalise per `docs/requirements/05-scoring.md`, judge a submission
      by membership, score our own audit trail, and report where the 196 points are.
  - Tests: `tests/acceptance/test_corpus_judge.py` (31)
- [x] T6: verification — `cd server && uv run pytest tests/` → **204 passed**;
    `uv run ruff check .` → clean; `uv run python -m evals.corpus --selfcheck` → 75
    accepted answers checked, 0 rejected; the eval bot boots and the pipeline reaches
    `StartFrame` end-to-end; `make eval-one S=simple_booking_amelia` → **1/1 passed** (the
    booking path still calls `confirm_offer`; the honest "delivery did not go through" line
    is the eval lane's own 404, as `pr01-06-record-and-liveness.md` documents).

## Deliberate limits

- **Caller identity from the number** (`FR-from-number`) is not implemented: `_call_id`
  never reads `from_number`, so cold booking only fires when the conversation identified
  the patient. That is the branch that would cover a call where nobody got a word in.
- Habits for cold booking come from the patient's **past** appointments through the
  catalogue (provider → specialty, unanimous or nothing); no background preference mining.
- The affirmation watcher fires on the **voice** path only. In the text-mode eval lane the
  harness writes the caller's turn straight into the context, so the aggregator's turn
  event never fires and `make eval` cannot exercise the watcher; its decision logic is
  covered by unit tests and the eval run only proves the wiring does not break the
  pipeline. An audio-mode scenario is the cheap way to close that gap.
- `make run-webrtc`, `make run-twilio` and `make tunnel` are advertised in the Makefile's
  help and `.PHONY` but have no recipes (pre-existing), so `make guard` is the only wired
  entry point for the restart check; whoever restarts the endpoint still has to call it.
- The oracle scores the action list it is handed. `--audit` extracts ours from
  `audit-logs/`; knowing which published case a live call was remains a dashboard question.

## Error history

| Run | Observation | Cause | Correction |
|---|---|---|---|
| 18 Sep (ours) | a 246 s LLM stall with no TTFB, cut by the platform | `retry_on_timeout` binds only the header await | T3 |
| 19 Sep (ours) | a call that decided nothing submitted `NO_ACTION/out_of_scope`, an answer the roster accepts in one problem out of seventeen | a default refusal instead of a resolved ending | T1 |
| 19 Sep (ours) | the first watcher (a pipeline `FrameProcessor`) never fired; the audit trail had no `affirmation_submit` | the aggregator swallows the final transcription, and a tool-call turn emits no text frame | T2 |
| 19 Sep (ours) | the oracle's first scored submission failed on `policy_id: 'Mapfre Salud'` | the plan label is not the plan id (`CL-plan-display-names`) | T5 (the judge is right; the incident is the reminder) |

## Work-unit commits

(not committed: the working tree also carries the inherited uncommitted work in the same
files, so the split has to be decided with the user)
