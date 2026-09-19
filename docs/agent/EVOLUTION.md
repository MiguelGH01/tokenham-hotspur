# Eval optimization log — PR-01..06

Iterative loop: run `make evals` (all `server/evals/PR-*`), read the failure logs
(`server/eval-runs/<scenario>.eval.log` / `.debug.log`), fix the smallest thing that
explains the failure, re-run just that scenario, repeat. Session stopped by user request
before a final clean full-suite run completed — see "Status at stop" below.

## Iteration 0 — baseline

Full `make evals` before any change: **7/24 passed**.

Failures by category:
- PR-03 (`doctor_and_site_*`, 4/5 failed): `get_earliest_slot` had no way to target a
  named provider, so leave-fallback, same-provider/different-day, and unknown-provider
  cases couldn't work at all.
- PR-04 (`the_new_patient_*`, 4/4 failed): no registration flow existed — `search_patient`
  not-found just retried the identifier as if misheard, then gave up.
- PR-05 (`when_exactly_*`, 3/5 failed): `pick_offer` filtered strictly on a requested
  weekday with no closed-day rollover, and had no exact-calendar-date argument.
- PR-06 (`the_rules_*`, 4/5 failed): nothing read the `blocked[]` array from
  `/availability`, so referral/coverage/age-boundary/plan-network rules were invisible.

The scenario YAML comments and `docs/agent/{add-pr04-06,process-map}.md` already
specified the target design in detail (this was the load-bearing reference for every fix
below — not guessed from scratch).

## Iteration 1 — provider filter, registration, rules engine, date engine

**`server/clinic/clinic_catalog.py`** — added catalogue-driven helpers with no
hardcoded per-case logic: `provider_roster()`/`provider_ids_by_name()`/`provider_name()`
(name↔id, for the LLM to recognise/pass a named doctor), `provider_on_leave()`,
`provider_refuses_insurer()` (from each provider's static `refused_insurers`),
`age_in_months()` + `specialty_for_age()` (age-window lookup for the GP↔paediatrics
remap), `insurer_ids()`, `specialty_name()`, `closure_days()` (from `calendar.closure_days`).

**`server/booking.py`** — `pick_offer` now: (1) filters every candidate slot on
`patient["insurer"] in slot["payable_with"]` and drops rows on a catalogue closure day —
the live `/availability` still lists Fiesta-day rows with a real `payable_with`, so this
has to be a client-side filter, not the API's `blocked[]`; (2) resolves a cyclic
`weekday` to its **next concrete calendar date** up front and rolls forward through that
date exactly like an explicit `date` argument, instead of dropping the weekday
constraint entirely — dropping it outright can jump *backward* to an earlier open day
(see the chloe_sunday_rollover bug below).

**`server/flow/tools.py`** — `get_earliest_slot` gained `provider` and `date` args and:
resolves a named provider to an id, drops the provider filter (with a note) if they're
on leave or refuse the patient's insurer; drops any requested weekday/part_of_day once a
provider is set (the doctor's own schedule wins); on a redirect for age, retries with the
age-correct specialty and adds an explicit note stating the specialty changed; on a hard
`blocked[]` reason (referral_required / specialty_not_covered / location_not_covered /
not_eligible_age with no redirect) calls `submission.set_no_action(reason)` and routes to
a new `declined` node with a **templated explanation string** — the model was
paraphrasing the bare reason code wrong ("not offered at this clinic" instead of "plan
doesn't cover it") until the explanation was spelled out for it. Added `register_patient`
(flat POST body per `docs/api/03-submit.md`) and the transition-only tools
`refuse_unlisted_provider`, `begin_register`, `decline_register`.

**`server/flow/nodes.py`** — `find_slot` node now lists the provider roster and
the call's own resolved date/year (the model defaulted to the wrong year — 2025 instead
of 2026 — for an exact calendar date without this) and instructs when to use
`provider`/`date`/`weekday`. Split the `identify` node's "misheard_id or not_found ->
ask to repeat" instruction, since not_found and misheard_id need different next steps and
the model was defaulting to "repeat it" for both. Added `register_offer`, `register`,
`registered`, `declined` nodes. Removed "spell back to confirm" from the `register` node —
it made the model ask for confirmation before calling `register_patient`, which the
scenarios don't script a turn for.

**`server/clinic/clinic_client.py`** — `availability()` takes an optional `provider_id`.

**`server/submission.py`** — added `set_register()`.

**`Makefile`** + eval YAML comments — `make evals` now greps each scenario file for a
`# CALL_CLOCK_OVERRIDE=...` directive and exports it only for that scenario's bot
process. Added the directive to `amelia_fiesta_rollover` (needs the clock near end-Sept
to reach the Oct 12 closure at all) and converted the existing "Default
CALL_CLOCK_OVERRIDE=..." *documentation* comments on 8 other scenarios into the same
machine-readable form — those were previously just prose, so every run silently drifted
further from the fixture's assumed day as real time passed (see the josefa/Ortiz
regression below).

Result after this pass, scenario-by-scenario (see iteration 2 for the fixes these
triggered): most PR-03/04/06 cases and `amelia_fiesta_rollover` passed on the first or
second try.

## Iteration 2 — regressions found while verifying

1. **`when_exactly_amelia_fiesta_rollover`**: LLM passed `date: "2025-10-12"` — wrong
   year, made the date filter a no-op (all real slots are in 2026, so "on or after
   2025-10-12" matched everything). Fixed both ways: the node prompt now states the
   call's actual year, and `get_earliest_slot` snaps a past `date` arg forward to the
   next real occurrence of that month/day as a safety net.
2. Same scenario, second bug: the live API does **not** flag Fiesta-day rows via an empty
   `payable_with` (contradicts `docs/requirements/04-clinic-domain.md`'s `CL-fiesta` —
   apparently stale for this dataset instance); confirmed live that Oct-12 rows carry a
   normal `payable_with`. Switched to filtering on the catalogue's `calendar.closure_days`
   directly, which is deterministic.
3. **`the_rules_sonia_age_boundary`**: age redirect worked (booked paediatrics/Ocaña) but
   the model *said* "General Practice appointment with Doctor Ocaña" — right doctor, wrong
   spoken specialty, because nothing told it the specialty had changed. Added an explicit
   note on redirect.
4. **`the_rules_josefa_specialty_not_covered`**: declined correctly but said "gynaecology
   isn't offered at this clinic" instead of "your plan doesn't cover it" — the generic
   `declined` node prompt let the model invent its own reason text. Added the
   `DECLINE_EXPLANATIONS` template and told the node to use it verbatim.
5. **PR-04 registration, all four cases**: model asked "is that correct?" and read the
   details back instead of calling `register_patient` in the same turn — removed the
   "spell back to confirm" instruction (see iteration 1 note).
6. **`simple_booking_identify_fail`** (after fix #5's unrelated prompt edit touched the
   shared `identify` node): turn 2 got "repeat it slowly" instead of the not-found path,
   because `not_found` now transitions to a different node but the *previous* node's
   "misheard_id or not_found -> ask to repeat" instruction was still sitting earlier in
   context and won out. Split the instruction so `not_found` explicitly defers to
   whatever node comes next.
7. **`when_exactly_chloe_sunday_rollover`**: caught on the *second* verification run
   (passed once, then failed) — `pick_offer`'s weekday-relax fallback dropped the weekday
   constraint entirely on a miss, so "this coming Sunday" (closed) fell back to the
   **globally earliest slot**, which was the *preceding* Saturday (Centro opens
   Saturdays) — an earlier day than the one asked for, not a later one. Rewrote
   `pick_offer` to resolve `weekday` to its next concrete date up front and only ever roll
   *forward* from it (same code path as an explicit `date`). Fixed; not yet re-verified in
   a full suite run (see below).
8. **`simple_booking_josefa`** (pre-existing, not caused by this session's edits): real
   wall-clock time had moved one day past the scenarios' assumed baseline (today is really
   19 Sep 2026, a Saturday; the fixtures assume 18 Sep, a Friday), so "earliest GP" no
   longer lands on the hardcoded `text_contains: Ortiz`. Pinned with the same
   `CALL_CLOCK_OVERRIDE` directive as the other date-sensitive scenarios.
9. **Real submission POSTs always 422'd**, register included: `bot.py`'s `_call_id()`
   fallback for eval/local runs was `f"local-{uuid.uuid4().hex[:12]}"`, not a well-formed
   UUID, and the submit API's `CallId` type requires one. Fixed to `str(uuid.uuid4())`.
   Investigating further: even with a valid UUID the API now returns 404 "unknown call" —
   the real submit backend only recognises a `call_id` it issued for a live,
   platform-tracked call, so **local/eval submissions can never succeed against the real
   API by design**, regardless of call_id format. Not something fixable in this repo; the
   fix is still correct in isolation (a well-formed id is right regardless) and matters
   for any other transport where the fallback might ever be hit.

## Known flake (not a code bug)

Three failures across two verification runs showed the identical signature: after a
correct tool call, the model's next completion produces the `●` turn-complete marker with
**no text and no tool call**, and the harness times out waiting 60s for a `response` event
that never arrives (`simple_booking_chloe`, `when_exactly_chloe_sunday_rollover` on one
run, `when_exactly_josefa_tomorrow` on another). Every one of these scenarios passed
cleanly on a subsequent standalone re-run with no code changes. `bot.py`'s
`retry_on_timeout=True` on the Helmcode LLM service catches network-level hangs but not an
empty-but-"successful" completion, so this class of flake isn't currently retried. Not
investigated further this session — flagged for whoever picks this back up.

## Status at stop

User asked to stop mid-run. Last full-suite attempt (started **after** the provider/
register/rules fixes but **before** the weekday-rollover fix in item 7 above) got through
19 of 24 scenarios before being killed:

- **16 passed**
- **3 failed**: `simple_booking_chloe` (flake, see above), `when_exactly_chloe_sunday_rollover`
  (the real bug fixed in item 7, but this run predates that fix), `when_exactly_josefa_tomorrow`
  (flake, see above)
- **5 never ran**: `the_rules_ignacio_control` (in progress when stopped),
  `the_rules_josefa_specialty_not_covered`, `the_rules_sonia_age_boundary`,
  `the_rules_teresa_referral_required` — all four were individually verified passing
  earlier in the session (see iteration 1/2), just not re-confirmed in this final pass.

Every scenario that failed for a *code* reason (not the LLM-empty-completion flake) has a
fix already committed to the working tree, verified standalone. **Recommended next step:
run `make evals` once more, clean, to get an honest final tally** — not done here because
the user asked to stop first.

## Iteration 3 — real-call testing over the ngrok tunnel

Tested `make run-twilio` + `make tunnel` against the real Prosper dashboard (two live
calls). Two unrelated, real production bugs found this way that no local eval could have
caught (evals never open a real audio path):

1. **`RNNoiseFilter()` crashed on every real call, at connect.** `pyrnnoise~=0.4.3` calls
   `audiolab.av.Graph(rate=...)`, but the resolved `audiolab` (and transitively `av`)
   renamed that to `sample_rate`. The crash killed the audio input task for the whole
   call — the first real call's caller was never transcribed past the greeting, and the
   call correctly fell back to `NO_ACTION(patient_not_found)` on hangup (the "always
   submit something" contract held). Tried pinning `audiolab==0.4.9` back to a compatible
   API; that just exposed a second incompatibility one layer down (`audiolab` needs an
   `av` API that no longer exists in current `av` releases either). Not a one-line pin —
   disabled `RNNoiseFilter` in `bot.py` instead (commented out with the reason) until
   `pipecat-ai`'s `rnnoise` extra pins a compatible trio. Verified clean boot after.
2. **A mid-stream LLM stall left a second real call dead for ~30s with no recovery.**
   After the RNNoise fix, a second call got past the greeting, transcribed the caller's
   name/DNI correctly, then the completion's TTFB came back fine (~0.5s) but the stream
   never produced any further text or a tool call — `search_patient` was never invoked.
   The caller just sat in silence until they hung up 34s later.
   Root cause, confirmed by reading `pipecat.services.openai.base_llm`: `retry_on_timeout`
   only wraps the *initial* `chat.completions.create()` call in `asyncio.wait_for`,
   catching a connection/first-byte hang (the documented "~8% of gateway requests hang").
   It does not wrap iterating the stream afterward, so a stall *after* TTFB has no
   timeout at all short of the OpenAI SDK's own multi-minute default — Pipecat's
   `on_completion_timeout` event exists for exactly this, but nothing fired it because
   nothing made the underlying request time out quickly enough.
   Fix: added `extra={"timeout": 10.0}` to the Helmcode `Settings` — a per-request httpx
   timeout, which (per `pipecat.utils.http.TIMEOUT_EXCEPTIONS`, explicitly documented as
   catching "a timeout while iterating a response") fires on a *gap* between chunks, not
   on total response length, so it only catches a genuine stall, not a long-but-active
   reply. Registered an `on_completion_timeout` handler in `bot.py` that speaks "Sorry,
   could you say that again?" (same `TTSSpeakFrame`/`worker.queue_frames` pattern already
   used for the greeting) so a stall now recovers audibly within ~10s instead of leaving
   the caller in dead silence for 30+. Did not attempt to silently replay the exact same
   LLM turn — reconstructing that safely from inside a bare `on_completion_timeout(service)`
   handler (which gets no context object) would need either private-attribute access to
   the flow's context aggregator or a custom `process_frame` override, and isn't verified
   against a real reproduction; asking the caller to repeat is the safer, provable fix.
   Verified: bot boots clean, `ruff check` passes, `simple_booking_amelia` still passes.

3. **A third real call spoke its own chain-of-thought aloud for the whole call.** After a
   garbled/ambiguous transcription of the caller's DNI (Deepgram misheard part of it as
   "C n"), the bot produced ~55 separate TTS utterances working through the ambiguity out
   loud — literally computing the DNI check-digit by hand ("48064716 /23... remainder
   6... Letter Y is correct") — and never called `search_patient` at all; the call ended
   in `NO_ACTION(patient_not_found)` again. Confirmed by reading
   `pipecat.services.openai.base_llm`: the streaming loop only reads
   `chunk.choices[0].delta.content` and pushes every bit of it straight to TTS — there is
   no separate reasoning channel it filters out. `deepseek-v4-flash` is a reasoning model;
   most likely the Helmcode gateway's OpenAI-compat shim merges its reasoning tokens into
   the same `content` field pipecat speaks, rather than a `reasoning_content` field it
   would ignore, and the ambiguous input pushed the model into visibly working through it
   instead of quietly deciding and answering. Only reproduced on a real call with messy
   STT output — eval scenarios send clean text and never hit this path, so this can't be
   verified by `make evals`. No gateway-level reasoning toggle attempted (no documented
   Helmcode/deepseek param to check without guessing at one). Fix: added an explicit
   "decide silently, never speak your reasoning or working aloud" line to `ROLE_MESSAGE`
   in `flow/prompts.py`. Verified no regression on `simple_booking_amelia`; the underlying
   fix can only be confirmed by another real call with ambiguous input.

4. **The reported "10 seconds to answer" was mostly turn-detection, not LLM latency.**
   Measured the actual per-turn breakdown from the logs: STT ~0.3-0.5s, LLM think time
   ~0.7-1.8s, TTS ~0.2s — none of that adds up to 10s. The real cost was a turn that hit
   `on_user_turn_stop_timeout`: confirmed via the Pipecat docs that this is a hard 5.0s
   backstop that fires "when the user has stopped speaking according to VAD but no
   transcription-based stop has occurred." `bot.py` only configured one stop strategy
   (`TurnAnalyzerUserTurnStopStrategy`, the smart-turn ML model); when it didn't confidently
   fire on that turn's real 8kHz phone audio (`_on_user_turn_stopped ... strategy: None` in
   the log — no strategy claimed it, only the backstop), the caller sat in silence for the
   full 5s before any processing even started. This only shows up on real telephony audio;
   eval text-mode never exercises turn detection. Fix: added
   `SpeechTimeoutUserTurnStopStrategy(user_speech_timeout=1.2)` alongside the existing
   smart-turn strategy in `bot.py` — stop strategies race, first to fire wins, so normal
   turns are unaffected and a smart-turn stall now costs ~1.2s instead of 5s. Verified no
   regression on `simple_booking_amelia`; effectiveness on real stalled turns needs another
   real call to confirm.

5. **Root-caused the reasoning-leak from item 3, and it's the same short-utterance problem
   as item 4.** A bare `"Hello."` reply doesn't just confuse the smart-turn ML model — the
   LLM's own turn-completion marker judgment misjudges it too, replying with a bare `○`
   (needs more time). That arms Pipecat's built-in 10s "long incomplete" timeout, which
   auto-injects its own developer message ("The user has been quiet... generate a friendly
   check-in... You MUST respond with ● followed by your message"). Reconciling that
   injected nudge against the earlier ambiguous exchange is what triggers the model to
   reason out loud instead of complying cleanly. Confirmed in
   `pipecat.turns.user_turn_completion_mixin` that this is by design, not a framework bug:
   `_push_turn_text` correctly strips everything before a marker *when one is found*
   (`remaining_text = buffer[marker_end:]`); the leak only happens via `_turn_reset`'s
   documented safety net — "if no marker was found in this response, push the buffered
   text so it's not lost" (with its own warning log) — which exists so a malformed
   response isn't silently dropped, at the cost of speaking it raw when the model doesn't
   comply. Two fixes, both general rather than specific to this one trigger: added
   `max_completion_tokens=400` to the Helmcode settings in `bot.py` (bounds any future
   rambling regardless of cause; verified it doesn't clip a normal multi-field
   registration confirmation) and added a line to `ROLE_MESSAGE` stating that a short
   reply — a bare greeting, a single word, a yes/no — is a complete turn on its own, to
   fix the misjudgment at its source rather than only its downstream symptom. Verified no
   regression on `the_new_patient_joaquin` (register flow); confirming the fix requires
   another real call starting with a bare greeting.

6. **Added proactive "are you there?" idle handling** (feature request, not a bug fix).
   Looked this up via Pipecat's `detecting-user-idle.md` Fundamentals guide rather than
   guessing at a mechanism — it's a purpose-built, separate system from the
   `filter_incomplete_user_turns` marker machinery behind items 4/5 (it fires from
   silence after the bot stops speaking, not from an ambiguous turn-completion verdict),
   so it doesn't reuse or interact with that confusing check-in path. Set
   `user_idle_timeout=6.0` on `LLMUserAggregatorParams` and added an escalating
   `on_user_turn_idle` handler in `bot.py`, matching the guide's recommended pattern:
   attempt 1 is a gentle "are you still there?", attempt 2 is more direct and warns the
   call may end, attempt 3 speaks a deterministic goodbye and ends the call — deterministic
   rather than one more LLM turn, so the third strike can't itself get stuck rambling.
   `idle_retries` resets on `on_user_turn_started`. Verified no regression on
   `simple_booking_amelia`; the idle path itself needs a real call with a deliberate long
   silence to confirm.
