# pr01-06-record-and-liveness

Feature: make every decided outcome actually reach the platform, stop the call
from dying in silence, and tune the phone path so the turns are fast enough to
fit inside the limits the scorer enforces.

Lane value: **40 points** (PR-01 4×1, PR-02 0, PR-03 4×2, PR-04 4×2, PR-05 4×2,
PR-06 4×3). Last scored Run All before this work: 14.

Live scoring rule, 19 Sep 2026: points are **`passed cases × weight`**, not
`fraction × weight`; the repo mirror in `docs/requirements/05-scoring.md` still
carried the older formula. A harness failure now redials the case once and a run
is voided only when *every* call was voided.

## Evidence

Read from the scored Run All transcript of 19 Sep 2026, 04:54–04:58, or measured
against the live APIs. Nothing here is inferred from the code alone.

1. **A provisional `NO_ACTION` was delivered ~5 s after it was set, freezing the
   call's record.** Call `PipelineWorker#21` (patient Juan Ruiz, "Doctor
   Iglesias", DKV):

   ```
   04:55:51.279  get_earliest_slot {'provider_name': 'Doctor Iglesias'}
   04:55:51.280  Function handler completed            ← 1 ms, no network
   04:55:56.203  Submission accepted: NO_ACTION        ← +4.9 s (SUBMIT_RETRY_SECS=5)
   04:56:11.468  Offer prepared                        ← the real booking search
   04:56:31.634  confirm_offer completed               ← 0 ms, no network, no BOOK
   ```

   `confirm_offer` skipped `set_book` because `delivery_started` was already
   true, `flush()` had nothing outstanding, and it returned `{"status":
   "accepted"}` anyway. The caller heard "your appointment request has been
   received"; the platform recorded `NO_ACTION(provider_not_found)` where the
   case expects `BOOK PR12`.

2. **An LLM call hung 246 s and answered a closed socket.**

   ```
   04:54:34.934  OpenAILLMService#19: Generating chat
   04:54:36.645  pipeline cancelled, socket closed
   04:58:31.569  OpenAILLMService#19 TTFB: 246.148s
   04:58:32.467  exception sending data: WebSocketDisconnect
   04:58:39.199  TTS: "One moment please."   ← after EndFrame; leaked watchdog
   ```

   `retry_on_timeout` defaults to `False` on both OpenAI services and was never
   passed. The watchdog's monitor task also survived teardown (`dangling tasks
   detected: ['SilenceWatchdog#21::silence-watchdog']` on every worker).

3. **The watchdog manufactured duplicate turns.** Every nudge pushed
   `TTSSpeakFrame` **and** `LLMRunFrame`; when the LLM was merely slow the second
   frame produced a whole second generation ("How can I help you today?" three
   times in one call) and the caller answered the filler ("Okay. I'll wait.",
   "Sure. No."). Turns are capped at 24 per case.

4. **Spoken-name resolution threw away the cheapest discriminator.** `_fold`
   stripped `dr`/`dra`/`doctor`/`doctora`, and the patient was never used as
   context. Verified by execution:

   ```
   'Dr. Sáez'      -> [PR03 general_practice, PR04 paediatrics]
   'Dra. Iglesias' -> [PR05 dermatology, PR06 orthopaedics]
   ```

   The published roster already carries the title (`Dr. Martín Sáez` vs `Dra.
   Marta Sáenz`), and age separates the pair again for an adult.

5. **`no_slots` could not say why.** The result carried `blocked`
   (`[{"provider_id": "PR09", "restriction": "allowance_exhausted"}]`) but the
   node prompt only said "say nothing is available", so the agent answered
   "I don't have the details of why".

6. **A cancelled tool call made the model improvise.** A scripted scenario that
   sent its next turn while `get_earliest_slot` was in flight produced:

   ```
   {'role': 'tool', 'content': 'CANCELLED', 'tool_call_id': 'call_vLHEiYF…'}
   {'role': 'user', 'content': 'Yes, Arenal Centro, please, the earliest you can.'}
   ```

   `cancel_on_interruption=True` is deliberate — a synchronous tool is what
   stops the model answering from nothing — but the prompt said nothing about
   what to do with the cancellation, so the reply described a site and a day
   from memory and the judge rejected it. Same shape as the Run All failure in
   evidence 1.

7. **Concurrency, measured** with `make concurrency` (20 sockets, one process):

   | N | greeted | p50 | max |
   |---|---|---|---|
   | 1 | 1/1 | 1.37 s | 1.37 s |
   | 10 | 10/10 | 2.29 s | 2.49 s |
   | 20 | 20/20 | 3.79 s | 5.19 s |

   20 distinct call ids each took their own session. Krisp is off locally
   (`Krisp VIVA disabled: no API key and no local .kef`), so these numbers do not
   include noise suppression.

8. **Speech recognition, measured** by synthesising a caller and transcribing it
   back with each setting (19 Sep 2026):

   | model / language | English caller | Spanish caller |
   |---|---|---|
   | `nova-3-general` / `en` | `15750638P` | *(empty transcript)* |
   | `nova-3-general` / `multi` | `157-50-638P` | `15750638P`, `DKB` |
   | `nova-3-general` / `multi` + keyterm | `157-50-638P` | `15750638P`, **`DKV`** |

   `multi` is the only setting that hears Spanish at all, and it is no worse on
   English (`Dr. Elena Iglesias` against `en`'s stray `Doctor. Elena
   Iglesias`). Keyterm prompting fixed the plan name, which is a scored field.
   The hyphens are harmless: `national_id.normalize_national_id` strips `-` and
   whitespace before any comparison.

9. **LLM latency, measured** at 20 concurrent requests to `gpt-5.1`:
   `service_tier="fast"` answered 20/20 with zero errors and moved p50 from
   1.04 s to 0.74 s and the slowest call from 1.86 s to 0.95 s.
   `temperature=0.2` and explicit `effort="none"` both answered 200; the API
   only accepts `temperature` while reasoning is off, so the two are set
   together or not at all.

## Tasks

- [x] T1: submission — a provisional refusal is never delivered by the retry
  loop; `decide()` promotes it; only a contiguous decided prefix is delivered
  (`server/submission.py`, `server/flows/common.py`)
- [x] T2: confirm_offer — a booking that cannot be recorded says so instead of
  reporting success; a repeated confirmation is a retry, not a conflict
  (`server/flows/booking.py`)
- [x] T3: LLM — `retry_on_timeout=True` with `LLM_RETRY_TIMEOUT_SECS`
  (`server/bot.py`)
- [x] T4: watchdog — the first nudge only speaks, the re-run is deferred to
  `SILENCE_RERUN_SECS`, and the monitor stops at `EndFrame`/`CancelFrame`/
  `StopFrame` (`server/liveness.py`, `server/bot.py`)
- [x] T5: provider resolution — honour the spoken title and filter candidates by
  whether the specialty can serve this patient (`server/flows/booking.py`)
- [x] T6: prompts — `provider_name` and `specialty` together, plain words for the
  rule inside `blocked`, and what to do with a `CANCELLED` tool result
  (`server/flows/booking.py`)
- [x] T7: STT/TTS — `nova-3-general` / `multi` / keyterm / numerals, and 8 kHz
  synthesis on the telephony path (`server/bot.py`)
- [x] T8: LLM tuning — explicit `effort="none"`, `temperature`,
  `max_completion_tokens`, `service_tier` (`server/bot.py`)
- [x] T9: concurrency readiness — `scripts/concurrency_check.py` and
  `make concurrency`, plus the missing `run-twilio` target it needs
- [x] T10: verify — `make test` 74 passed, `make eval` 15/15 scenarios green,
  `make concurrency N=1|10|20` READY with 20/20 greeted

The scenario `doctor_site_clarification` was rewritten: it used to feed a
clarifying turn for Sáez/Sáenz, which T5 now settles by age, so its third turn
asserts the offer and the confirmation instead.

## Deliberate limits

- The title does **not** separate Iglesias/Iglesia when the caller says a neutral
  "Doctor", which is what STT produced in the trace. That case keeps its
  clarifying question; what T1 fixes is the record the question used to poison.
  `test_a_neutral_title_still_leaves_iglesias_ambiguous` pins the limit so nobody
  later mistakes it for a bug.
- `retry_on_timeout` re-issues once **without** a timeout
  (`pipecat/services/openai/base_llm.py:343`), so a second stall is still
  possible. T4's deferred re-run is the backstop, not a guarantee.
- Local evals cannot reproduce T1: an eval submit answers `404`, so `_delivered`
  never increments and `delivery_started` never becomes true. T1 is proven by
  unit tests over `CallSubmission`.
- Every call still builds its own ONNX sessions (`SileroVADAnalyzer`,
  `LocalSmartTurnAnalyzerV3`) and its own Deepgram sockets, and `ClinicClient`
  opens a fresh `httpx.AsyncClient` per request. The measurements in evidence 7
  say that is affordable at 20; they do not say it is free.
- `policy_id` is still `patient["insurer"]` (`server/booking.py:60`) rather than
  the resolved plan. That is PR-17's failure mode and is out of this lane.
- This commit carries the previous increment's uncommitted work as well. The two
  are edited inside the same files, so splitting them by hunk would produce a
  history where neither half runs.

## Notes

- Work-unit commit: **c8ee2d2** — `fix(run-all): deliver the decided record, stop
  the silence, tune the phone` (39 files, on branch `feature/flows`; not pushed).
  It carries the previous increment's uncommitted work as well, for the reason in
  *Deliberate limits*.
- `make test` 74 passed, `make eval` 15/15, `make concurrency N=20` 20/20 — all
  run against the working tree this commit records.
- Freeze: Sunday 20 September 2026, 06:00 Europe/Madrid.
- `SC-cooldown-run` is 15 minutes after a Run All finishes.
- Run `make concurrency` before the first scored run of the day. It costs a
  couple of minutes and it caught a startup crash (`NameError: load_catalog`)
  that would have failed all 20 calls of a wave.
