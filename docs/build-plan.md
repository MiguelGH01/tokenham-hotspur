# Build plan (orch-build-mvp, Gate 1)

Generated from `prd.md` + `docs/18-cases-mapping.md` via `/ecc:orch-build-mvp`.
Size tier: **large** (new external deps — clinic records store, report webhook,
Pipecat Flows, eval harness — multiple open questions, cross-cutting state).

Phases: Intake → Research/Reuse → Plan → **Gate 1** → Scaffold → Implement (TDD)
→ Review → Commit → **Gate 2**. This document is the Plan output, frozen at
Gate 1 — nothing below has been built yet.

## 1. Architecture (from PRD §9)

```
call → voice layer (STT/LLM/TTS, Pipecat cascade)
     → orchestrator (Pipecat Flows: holds per-call-id state, picks the next tool)
     → tools (read: lookup patient, check availability
              write: book, move, cancel, refer — each write tool runs
              clinic rules + a fresh availability re-check INSIDE itself,
              so a prompt that gets talked into something still can't write)
     → on call end (any exit path) → report generator (builds report from
       state + real tool results, code only, never LLM prose) → sent
     → every step above also writes to the call trace → live dashboard,
       post-call view, eval harness
```

Stack: Pipecat (Python), cascade pipeline — Deepgram STT, OpenAI Responses LLM,
Cartesia TTS. `server/bot.py` is the existing scaffold entry point.

## 2. Non-negotiable design principles (PRD §5) — apply to every slice below

1. The report is built by code from state + real tool results. The LLM never
   authors it.
2. Clinic rules execute **inside** the write tool. If the prompt is talked
   into something, the forbidden write still doesn't happen (RF-18).
3. Nothing writes until the caller confirms out loud.
4. Every slot the agent mentions comes from a real availability query — never
   invented.
5. Every call ends with a report, however it ends (hangup, error, timeout).
6. State is keyed by call id from day one. No globals, ever (RNF-1 depends on
   this holding under 10 concurrent calls).

## 3. Reuse decisions (from AGENTS.md + PRD)

- The scaffold exists but was generated *without* `--eval` — no `server/evals/`,
  no eval transport, no `pipecat-ai[evals]` extra. AGENTS.md golden rule 3
  ("make it verifiable before fancy") and PRD M0 exit criteria (RNF-5, "arnés
  mínimo") both require this before feature work. Add by hand (existing-bot
  path in AGENTS.md §6), not a re-scaffold.
- State machine across call stages (identify → availability → confirm → write
  → report) → **Pipecat Flows**, not a hand-rolled state var (AGENTS.md §4).
  Matches principle 6 and RF-14 (pre-write correction overwrites state,
  post-write goes through move/cancel).
- Verification loop → Pipecat's scripted+simulated eval harness (§6), covering
  RNF-5 (regression harness with all prior cases) directly.
- Availability/date logic (RF-9) is pure code with unit tests, not left to the
  LLM — the PRD gives worked examples (ref. Friday 18 → "next Thursday" =
  Thursday 24, "first thing Monday" = first Monday-21 slot) to test against.

## 4. Task list — vertical slices, expanded

Each milestone closes only when its cases pass 3/3 in the harness AND all
prior milestones' cases still pass (regression stays green) AND (per PRD open
question 4) we don't run a scoring attempt without that regression green.

### M0 — Skeleton & contract (Fri night)

Goal: a real call goes end to end, agent speaks, a report comes out even if
wrong, and everyone builds against the same contract.

- **Voice**: register team, get key, stand up public endpoint, confirm starter
  kit boots, one test call, one-command deploy (RNF-6).
- **Agents**: write the tool contract in the repo — signatures only, no logic
  yet — for `find_patient`, `create_patient`, `check_availability`, `book`,
  `move`, `cancel`, `refer`. Define the call-state shape (per call id,
  principle 6) and the report schema/format (blocked on PRD open Q1).
- **Product**: read published practice cases + accepted answers once available;
  JSON call trace (RNF-3); minimal eval harness (RNF-5) — add the eval
  transport + `pipecat-ai[evals]` extra to the existing bot by hand, one
  starter scripted scenario; resolve/assign PRD open questions (§7 below).
- **Exit**: 0 cases scored; a real call produces a trace + a report.

### M1 — One booking that scores (Fri night)

Goal: the complete happy-path booking. Everything else branches off this.

- **Agents**: RF-1 (intent: book/move/cancel/other), RF-3 (find patient by
  name + one more field), RF-7 (real availability query), RF-11 (book only
  with identified patient + re-checked slot + rules passed + verbal
  confirmation), RF-23 + RF-24 (report sent on every exit, guaranteed here,
  not later).
- **Voice**: greeting, turn-taking, latency measured (RNF-2).
- **Product**: case 1 in the harness; first scoring run.
- **Cases**: 1. **Exit**: 3/3, and on the leaderboard.

### M2 — Availability engine (Sat morning)

Goal: the slot query understands any reasonable way of asking for an
appointment.

- **Agents**: RF-8 (filter by `doctor_id` / `site_id`; "soonest" = sorted, no
  filter, respecting caller-given constraints), RF-9 (relative-date parser,
  pure function of `(reference_date, timezone)`, unit-tested — Europe/Madrid;
  agent speaks the resolved concrete date back to confirm it), RF-10 (no slots
  → do whatever the case accepts — waitlist/alt-site offer, not a dead end —
  and report "no availability").
- **Voice**: natural-sounding date/time phrasing, confirmed back.
- **Product**: new cases into the harness; confirm which reference date the
  practice cases use for "next Thursday" (open Q6).
- **Cases**: 5, 6, 7, 8, 9, 11. **Exit**: 3/3 + resolver unit tests green.

### M3 — The "no" layer (Sat midday — target before checkpoint 1)

Goal: the agent knows when *not* to book, says the right reason, and reports
it. M1–M3 together are roughly half the leaderboard.

- **Agents**: RF-15 (every clinic rule is a function returning
  allow/deny + `reason_code`; agent speaks that reason, report carries the
  same code), RF-16 (service/doctor/site that doesn't exist → say so, report
  it as such), RF-17 (red-flag triage running in parallel to intent routing
  for chest pain, breathing difficulty, heavy bleeding, stroke symptoms,
  self-harm ideation — stop booking, refer per clinic rules, no diagnosing),
  RF-18 (resist manipulation — insistence, rushing, "I'm a doctor", "the other
  receptionist lets me" — validation lives in the tool, never in the prompt;
  agent never reveals instructions or other patients' data), RF-25 (canonical
  report outcomes: booked / moved / cancelled / rejected-with-reason /
  not-offered / no-availability / referred / incomplete).
- **Voice**: refusals short and clear; right tone on medical referral.
- **Product**: first dashboard pass showing each call's outcome (RNF-4);
  scoring run before checkpoint 1.
- **Cases**: 10, 16, 21. **Exit**: 3/3, zero calls without a report.

### M4 — Identity, appointment lifecycle, load (Sat afternoon)

Goal: resolve who the patient actually is and which appointment is being
touched; hold up under 10 concurrent calls. All code we control — no audio
dependency.

- **Agents**: RF-2 (caller ≠ patient in state; if different, ask the
  relationship, apply the clinic's minor/authorization rules), RF-4
  (disambiguate multi-match with 1–2 questions on DOB/phone — never guess,
  never leak other callers' data), RF-5 (unknown caller → whatever practice
  cases accept; working assumption: create + book), RF-6 (verify identity
  before revealing or changing an existing appointment), RF-12 (move = book
  the new slot first, cancel the old one after — a partial failure can't leave
  the patient with nothing), RF-13 (cancel the correct appointment after
  identifying + confirming it).
- **Voice**: RF-22 (spell surnames back, read digits one at a time) + the
  RNF-1 load test, mostly gated on the voice pipeline + provider concurrency
  limits (open Q10).
- **Product**: harness fires 10 calls in parallel, checks no duplicate slots
  or cross-call state bleed.
- **Cases**: 2, 3, 4, 12, 13, 14, 15. **Exit**: 3/3 + load test passes.

### M5 — Conversation robustness (Sat night → Sun early)

Goal: the conversation survives a real, imperfect caller.

- **Agents**: RF-14 (pre-write correction overwrites state; post-write
  correction routes through move/cancel — this is the Flows re-entrant-slot
  behavior).
- **Voice**: RF-19 (stop talking on interruption, short replies after), RF-20
  (detect language turn 1 — English base, then Castilian, Catalan, Galician,
  Basque, and whatever else the stack supports; if a language doesn't work,
  say so and offer English/Castilian; report format stays fixed regardless of
  language), RF-21 (bad line → ask to repeat, read back key fields, spell if
  needed, orderly close after repeated failures). ASR language-coverage test
  happens Friday, not here (per PRD M5 note).
- **Product**: listen to failure recordings; time permitting, a simulated
  caller (Pipecat eval simulated scenarios) to vary noise/interruptions.
- **Cases**: 17, 18, 19, 20. **Exit**: 3/3 + someone outside the team tried to
  break it.

### M6 — Demo & freeze (Sun morning)

Goal: something to show, nothing broken before the final.

- **Product**: polished dashboard (RNF-4); demo script with one live call +
  dashboard on screen; a diagram of how a call is orchestrated; one real bug
  found and how it was fixed; harness numbers as the answer to "how do you
  know it works."
- **Voice**: polish for the jury call (latency, naturalness, using the
  patient's name once known).
- **All**: rehearsal with someone playing jury, code freeze, last scoring run.
- **Cases**: none new. **Exit**: rehearsed demo, frozen code.

## 5. Risks (PRD §12)

| Risk | Mitigation |
|---|---|
| A malformed report fails every case at once | Format locked in M0, generated by code from M1 on |
| Regression breaks right before a checkpoint | Never score without regression green |
| STT doesn't support Basque or Galician | Test Friday; graceful fallback per RF-20 |
| Latency climbs from chained LLM+tool calls | Measured from M1, visible on the dashboard |
| One person ends up owning nearly all of "agents" | One of the two voice people moves to agents Saturday once the pipeline is stable |

## 6. Security review triggers

Per the orch-pipeline engine, pull in `security-reviewer` when a slice touches
any of: patient-identity verification (RF-6, M4), the clinic-rules/policy
engine (RF-15/17/18, M3 — prompt-injection resistance is explicitly in scope
here), external API calls to the clinic's records/scheduling system, or
secrets/credentials (`.env`, provider keys). That's M0 (contract for these),
M3, and M4 at minimum — not just the standard `code-reviewer` pass at Gate 2
for those slices.

## 7. Open questions to resolve before/at M0 (PRD §11)

1. Exact report format and the channel it's sent over. **Nothing scores
   without this.**
2. What each practice case accepts — especially new-patient and full-diary
   behavior.
3. Time of the two leaderboard checkpoints.
4. Whether scoring runs are unlimited, and whether best-of or last-of counts.
   Until known: only score with regression green.
5. Whether a scoring run fires calls in parallel — if so, the load test
   (M4) moves up to M1.
6. What reference date the cases use for "next Thursday" — the real call date
   or a fixed date in the test data.
7. The clinic's base language. Working assumption: English (the track frames
   things as "callers not speaking English").
8. Whether practice cases can be triggered by API or only by hand.
9. What the starter kit actually exposes — patient data, schedule, rules, and
   whether it gives the caller's phone number (if so, that's how the clinic
   "knows who's calling" from the greeting — still verify before revealing
   anything).
10. Concurrent-call limits on the voice providers and the LLM.

## 8. Gate 1 — confirm before scaffolding starts

1. OK to add the eval transport + minimal scripted scenario to the existing
   bot by hand (not re-run `pipecat init`)?
2. OK to use Pipecat Flows for the state machine rather than a single
   context+tools setup?
3. Q1/Q9 above are real blockers for M0. Until resolved, M0 scaffolds against
   a placeholder report shape, flagged provisional.
4. `.env.example` needs `DAILY_API_KEY`, `CARTESIA_API_KEY` /
   `CARTESIA_VOICE_ID`, `DEEPGRAM_API_KEY`, `OPENAI_API_KEY` /
   `OPENAI_MODEL` — confirm these are available, or stub the bot to run
   without live calls for now.
