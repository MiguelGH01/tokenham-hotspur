# Scoring design notes: how a call ends, when it submits, and what protects it

Five decisions in this codebase exist because of how the board scores a call, and
each one is worth points on its own. This is the reasoning, with the numbers
taken from our own requirements docs and from the organisers' published roster
(`server/evals/corpus/public-cases.json`, 73 cases, 80 accepted action mentions).

The shape of the problem first, because everything below follows from it:

- **Binary per case** (`SC-binary`): no partial credit for a field, a slot one
  minute out, or three fields of four.
- **Membership** (`SC-membership`): the submitted action *list* must match one of
  the accepted lists. A wrong action costs exactly what silence costs, and
  silence always fails (`SC-silence-cut`, `SC-no-silence`).
- **What the roster actually accepts**: BOOK 57 times, NO_ACTION 9, CANCEL 5,
  REGISTER 4, RESCHEDULE 4, ESCALATE 1. `out_of_scope` appears in 4 of those
  mentions and all four are `adversarial` cases — one problem out of seventeen.

So a refusal that names nothing is not a safe default; it is a wrong answer in
sixteen problems out of seventeen, and an empty submission is worse.

## 1. The end of a call is resolved, never defaulted

`server/resolution.py` answers one question when a call ends without having
decided anything: what does this call still stand behind? In order:

1. the action the conversation already drew up and read back to the caller — an
   offer awaiting their yes, a verified appointment they asked to cancel or move,
   a completed registration readback;
2. the first slot the platform will offer the patient the call identified, using
   the doctor and site the patient's own diary points at (unanimous history or
   nothing), then general practice anywhere;
3. `NO_ACTION/out_of_scope` — the ending that wins nothing except the adversarial
   cases.

Nothing in that list is invented. Every id comes from a response this call
received, which is the same rule `booking.pick_offer` keeps for ordinary
bookings: the platform owns patient, provider, slot, type and plan, and the plan
label is not the plan id (`CL-plan-display-names`).

`CallSubmission.close()` asks for this ending **once per call**, not once per
intent, and never when a plan already exists: a rule the conversation named is a
better answer than anything a last-second guess can produce. The search is bounded
(`COLD_BOOKING_TIMEOUT_SECS`, default 6 s) because a booking nobody sends is worth
less than a refusal that leaves inside the 30-second post-hangup window.

## 2. The affirmation submits, not a later model turn

The caller's yes is the moment the case is decided, so it is also the moment to
submit. Waiting for the model to call `confirm_offer` costs a full LLM round trip
on a three-minute cap, and it is a turn the model can skip entirely when the
conversation is already long.

`server/affirmation_watch.py` watches the turns instead: a short, unqualified yes
(`confirmation.is_short_clean_yes`) landing on the agent's read-back question runs
**the flow's own confirmation handler** — the same code the model would have
called, so the deterministic gate in `flows/common.gated_confirmation` still
judges the utterance. This is not a second, weaker consent path; it is the same
path, earlier. The model's own later call finds the plan already sent and is
accepted as a retry (the platform answers a duplicate with 409, which counts as
accepted).

Two implementation details that turned out to be load-bearing:

- **The trigger is the user aggregator's `on_user_turn_message_added` event.**
  A pipeline `FrameProcessor` cannot do this job: the aggregator *consumes* the
  final `TranscriptionFrame` instead of forwarding it, and an agent turn that is
  a bare tool call emits no text frame at all. The first version of this watcher
  was a frame processor and it silently never fired — found by running the eval
  lane and seeing no `affirmation_submit` in the audit trail.
- **The turn is claimed synchronously**, before anything is awaited, so the frames
  and events of one utterance cannot submit twice.

## 3. The clock is a budget, and the line is never mute

`server/llm_deadline.py` exists because of our own incident (`SC-silence-cut`, and
evidence 2 of `odd/tasks/pr01-06-record-and-liveness.md`): a provider accepted a
request and never streamed a token, the call produced no audio for 36 seconds, and
the platform cut it. Pipecat's own guard binds only the await that ends when the
response *headers* arrive, and its retry is re-issued with no deadline at all, so
it would not have fired.

The guard here therefore:

- puts the **first chunk** under the deadline, not the headers;
- abandons and closes a stalled attempt, and re-issues it up to `LLM_ATTEMPTS`;
- drops the deadline once the first chunk is in hand, so a slow but streaming
  answer is never cut;
- when every attempt stalls, speaks one short line as an `LLMTextFrame` — audible
  through the TTS and recorded in the context as the agent's turn, so the caller
  hears a sentence instead of dead air and the next turn starts from a context
  that knows the agent spoke.

`LLM_FIRST_TOKEN_GUARD=0` restores the stock behaviour.

## 4. The restart guard

A scored run dials the endpoint ten calls at a time. Restarting the bot process,
or re-opening the tunnel, while a run is in flight kills calls that are already
being scored, and a killed call scores nothing. `server/prosper_guard.py` (and
`make guard`) asks the dashboard first:

- waits while a run is active (a full run is around 18 minutes, so the default
  budget is 20);
- refuses when an active run outlasts the budget;
- allows the restart when the status cannot be read at all — a guard that cannot
  see must not become a deploy lock.

## 5. The offline oracle

The leaderboard reports a verdict, an attribution and a fixed signal code, and
nothing about *which* field was lost; a scored run is otherwise undiagnosable.
`server/evals/corpus/` fixes that locally:

- `roster.py` keeps the organisers' published cases — each with the caller's own
  script and `expected.acceptable`, the literal action lists the scorer accepts —
  and refreshes them on demand (`--fetch`, digest printed);
- `normalize.py` implements the published normalization table field by field
  (`SC-norm-*`): ids compare exactly, names fold and drop accent, the two
  surnames are unordered, phone loses the country code, email loses whitespace,
  a slot is the exact minute in Europe/Madrid, and a slot without an offset is
  refused rather than assumed;
- `judge.py` scores by membership, exactly as the platform does, and reports the
  field-level diff against the *closest* accepted member;
- `--coverage` prints where the 196 board points are, per problem.

It tests itself: `--selfcheck` replays every accepted answer in the roster through
the judge, and CI should treat a rejection there as a broken judge, not a bot
regression.

## What this does not cover

- **Caller identity from the number.** `FR-from-number` is not implemented, so a
  call that never got a word in cannot be booked by the cold-booking branch: it
  needs the patient, and only the conversation can name one today.
- **The affirmation watcher on the voice path.** In the text-mode eval lane the
  harness writes the caller's turn straight into the context, so the aggregator's
  turn event never fires. The watcher's decision logic is covered by unit tests;
  an audio-mode scenario is what would exercise it end to end.
- **Mapping a live call to its published case.** The oracle scores the action list
  it is handed. Pulling that list from `audit-logs/audit-<call>.ndjson` is
  automatic (`--audit`); knowing *which* case the call was is a dashboard
  question.
