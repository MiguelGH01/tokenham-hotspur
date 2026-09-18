# Prosper Track reference (verified from official docs)

Scraped from `https://hackspain.getprosperapp.com/leaderboard/docs` on 2026-09-18.
This supersedes any earlier reconstructed/guessed version of the rules in this
repo (`18-cases-mapping.md`, parts of `build-plan.md`, `prd.md`) — those were
written before the real docs were available and disagree with this in places
(notably: there is no "report" webhook, submission is
`POST /api/v1/submit/<action>`; the real 18-problem list and weights differ
from the earlier 21-case guess). See `prosper-docs-navigation.md` for how to
re-pull this if the organizers publish a rules change.

## What you build

A WebSocket server that answers inbound scheduling calls for a fake clinic
("Clínica Arenal") the way a receptionist would, speaking Twilio's Media
Streams wire protocol. No real Twilio account or phone number needed — the
harness dials your `wss://` endpoint directly. Recommended stack: **Pipecat**,
cascade pipeline (STT → LLM → TTS).

Core loop: identify caller → look up patient via the clinic's read-only EHR
API → find real availability → submit one of
`register | book | reschedule | cancel | no-action | escalate` to
`POST /api/v1/submit/<action>`, keyed by `call_id` (= Twilio `callSid`),
within 30s of the call ending. The API never mutates anything — you report
what you *would* have done.

## Event logistics

- Fri 18 – Sun 20 September 2026 (HackSpain '26).
- Organisers open your team account at the registration desk (no self
  sign-up): you get an email, a dashboard password, and one team API key
  (`pk-...`), all shown once. Lost key → rotated at the desk. Lost password →
  desk revokes and reopens the account.
- Each team gets a **prepaid €100 debit card**, one per team, for whatever the
  agent runs on (models, speech, telephony, tunnels). Not topped up, nothing
  claimed back.
- Board freezes twice during the weekend (checkpoint prizes to whoever leads
  at that moment) and finally **Sunday 20 Sep, 06:00 Europe/Madrid**.
- Sunday: the jury calls every team's agent themselves ("the final boss").
- Reveal of private-case answers/transcripts: **Monday 21 Sep, 00:00
  Europe/Madrid** — after the event ends, so nothing leaks during it.

## Getting on the phone (quickstart)

- No starter kit — building the WebSocket server is part of the challenge.
- Expose it via ngrok (or any WebSocket-forwarding tunnel), EU region
  recommended for latency. Endpoint = `wss://<host>/<path>`, scheme and path
  both required (forgetting the path is the most common mistake).
- Set the real endpoint yourself on the dashboard's **Settings → Integration**
  page (every team starts on a placeholder). Optional custom headers
  supported, but not the ones a WebSocket handshake owns
  (`Host`, `Connection`, `Upgrade`, `Sec-WebSocket-*`).
- Problems page has two buttons:
  - **Call** — one practice call on a published case, unlimited (30s
    cooldown), scores nothing, gives full transcript + recording + which
    fields your record lost.
  - **Run All** — the scored lane: private cases across every open scored
    problem, 10 sockets at a time, ~18 min per run, 15 min cooldown after the
    previous run finished. Only lane the leaderboard counts.
- One queued/active run at a time per lane.
- Every route but `/api/v1/health` and the OpenAPI schema needs
  `X-Api-Key`. Invalid/missing/revoked key → `403`. Another team's resource →
  `404` (indistinguishable from nonexistent).

## The call contract (wire protocol)

- We connect to your WebSocket URL and speak Twilio Media Streams exactly:
  `connected` → `start` (its `callSid` is your `call_id` — keep it;
  `customParameters` also carries `call_id` again and `from_number` in E.164,
  which may be absent if caller ID is withheld) → `media` frames (20ms, 8kHz
  µ-law, base64, camelCase keys, `sequenceNumber`/`chunk`/`timestamp` are
  strings not numbers) → `stop` when the call ends, then socket closes.
- Turn-taking and interruption handling are **entirely yours** — no
  server-side barge-in; `clear` messages have no effect on our side.
- **Everything is per-socket.** A Run All opens 10 sockets concurrently, each
  with its own `callSid`. Sharing state (conversation, `call_id`, session
  object) across sockets is the mistake this challenge is built to catch.
  Build a fresh pipeline per connection.
- Submission window: opens when the call opens, closes 30s after the call
  ends. `call_id` unknown/another team's → `404`. Still open or ≤30s closed →
  `200`. >30s closed → `410`. Duplicate identical action → `409` (expected,
  not a bug). Malformed body → `422`, nothing recorded.

### `POST /api/v1/submit/<action>`

One route per action, snake_case JSON body (camelCase only applies to the
Twilio handshake), `X-Api-Key` header, `call_id` always included:

| Route | Body fields (besides `call_id`) |
|---|---|
| `register` | `given_name`, `first_surname`, `second_surname`, `national_id`, `date_of_birth`, `phone`, `email`, `insurer` |
| `book` | `patient_id`, `provider_id`, `location_id`, `appointment_type_id`, `slot`, `policy_id` |
| `reschedule` | `appointment_id`, `provider_id`, `location_id`, `slot`, `policy_id` |
| `cancel` | `appointment_id` |
| `no-action` | `reason` |
| `escalate` | `reason` |

- `register` is for a caller the directory doesn't know — nothing is booked
  in the same call. `national_id` re-derives its own check letter; a
  mismatched letter is `422`.
- `book` names an existing patient by `patient_id` (from `/directory`, never
  from what the caller said). `policy_id` names which of the patient's plans
  is billed — required, not inferred.
- `slot` needs an explicit timezone offset, converts to Europe/Madrid, and
  must match to the exact minute.
- `reason` is a closed vocabulary. Rule-based refusals (map 1:1 to clinic
  restrictions): `not_eligible_age`, `referral_required`,
  `provider_not_in_network`, `specialty_not_covered`, `location_not_covered`,
  `insurer_referral_required`, `allowance_exhausted`, `provider_on_leave`,
  `location_hours`, `type_not_offered`, `patient_history`. Non-rule endings:
  `no_availability`, `clinic_closed`, `patient_not_found`,
  `provider_not_found`, `caller_not_authorised`, `out_of_scope`,
  `medical_emergency`.
- Response (`200`):
  `{"call_id": "...", "received_at": "...", "record": {"actions": [...]}}` —
  every action accepted for the call so far, each with an `action` verb
  (`REGISTER`, `BOOK`, `RESCHEDULE`, `CANCEL`, `NO_ACTION`, `ESCALATE`) and
  its fields (`REGISTER` nests under `new_patient`).
- Each request is one action; a call that does two things posts twice.

## The clinic (read-only EHR)

Clínica Arenal: 3 sites (Centro, Norte, Sur), 12 providers, 6 specialties, 11
appointment types, 10 insurance plans, ~3,000 patients. Fixed and identical
for every team all weekend — cache it.

Endpoints (`X-Api-Key` required, full schema in the API reference):
`GET /api/v1/directory` (name/national_id/phone/date_of_birth),
`GET /api/v1/availability` (date_from/date_to + provider_id or specialty_id,
optional location_id/patient_id/insurer),
`GET /api/v1/patients/{patient_id}/appointments` (when=upcoming|past|all),
plus static catalogue: `/clinic` (everything in one call, plus restrictions),
`/providers`, `/locations`, `/specialties`, `/appointment-types`,
`/insurance-plans`. No booking endpoint anywhere — you never mutate.

For exact field-by-field request/response shapes, `hackspain-prosper-api.md`
(built from the live `openapi.json`, v0.1.0) is more authoritative than this
page's narrative summary — consult it first for wire-level detail. Facts
worth pulling forward from it:

- On `/directory`, **`name` is the only approximate field** — `national_id`,
  `phone`, `date_of_birth` are all exact filters. The response also carries
  `match_score` and `matched_fields`, and `referrals` (specialty ids the
  patient holds, format unconfirmed).
- On `/availability`, each slot carries `payable_with` — the insurers that
  can actually pay for that specific slot. This is the real mechanism behind
  problem 17 (second policy): pass `insurer=` to see if a plan not on the
  patient's record would unlock it.
- Insurer ids are the exact lowercase tokens: `sanitas`, `adeslas`, `dkv`,
  `asisa`, `mapfre`, `caser`, `cigna`, `axa`, `nueva_mutua`, `privado` — not
  the display names.
- The catalogue (`/clinic`, `/providers`, `/locations`, etc.) cross-references
  entities **by name, not id** (`provider_names`, `location_names`,
  `covered_specialty_names`) — build a name→id index at startup.

### Known discrepancy: site coordinates

The narrative `/clinic-api` docs page says problem 15's site coordinates are
"published in `/availability`'s location data." The actual `openapi.json`
schema doesn't carry `latitude`/`longitude` on `/availability` at all — they
live on `/locations` (and inside `/clinic`'s `locations` block) instead. Use
`/locations` for problem 15's distance calculation, not `/availability`.

### Traps worth remembering

- **Identification is exclusionary, not fuzzy.** An exact field mismatch
  excludes the patient (it filters, never downranks). `name` + `date_of_birth`
  separates two same-name people. `phone` folds to 9 national digits (`+34...`,
  `0034...`, bare 9 digits all equal). Confirm on a second field — some
  national ids differ from another patient's by one digit.
- Submit the record's name/id, never what the caller said (a nickname can
  still find the record, but isn't the legal name to submit).
- `/availability` always returns restriction metadata (`blocked`) whether or
  not slots exist; empty `slots` + empty `blocked` means simply a full
  calendar, a different answer from a rule refusal.
- Naming a plan (`insurer` param) is the only way to be quoted against it — a
  second plan not on file is only found by asking the caller
  (see problem 17).
- Every patient record has a `note` (receptionist's freeform context) and
  visit history — not scored by the leaderboard, but central to the jury's
  personalization criterion.
- **Sites:** only Centro opens Saturdays; nothing opens Sundays; Sur shuts
  Friday lunchtime.
- **Providers:** Dr. Requena on leave 14–30 Sep (covers the whole event).
  Near-miss name pairs in different specialties: Sáez (GP) / Sáenz
  (paediatrics), Iglesias (dermatology) / Iglesia (orthopaedics). D. Álvaro
  Cid is a physiotherapist, not "Dr." — the title is part of the submitted
  name. Language only constrains booking in problem 11; elsewhere assume any
  provider can take the call.
- **Insurance:** ASISA covers physio only at Centro/Norte but the only
  physiotherapist sits at Sur → ASISA can never book physio. Adeslas covers
  no gynaecology and there's one gynaecologist → no redirect exists. Dra.
  Iglesias doesn't take DKV (Dr. Vilar does) — a DKV patient asking for her by
  name is a redirect, not a refusal. `privado` (self-pay) is a plan held or
  not, never a fallback.
- **Appointment types:** exactly one is correct per booking, determined by
  `specialty` + patient's `has_visited_before` — never by the conversation.
  Two specialty-specific types run the same time slots as the universal
  `first_visit`/`review` types but are different ids
  (`review` vs `dermatology_review`) — hard-coding `review` fails cases it
  otherwise understood. Always submit the `appointment_type` id that
  `/availability` names on the slot.
- **Calendar:** bookable window 7 Sep – 16 Oct 2026, 15-min steps.
  `date_from`/`date_to` outside this or a span >14 days is `422`. Monday 12
  Oct is Fiesta Nacional, whole network shut. Dates resolve against the
  moment the call connects (Europe/Madrid), not a fixed anchor. Nothing is
  ever booked same-day — "earliest" means earliest from the day after the
  call.
- Diaries are deliberately uneven (40–72% full) — spreading load across tied
  providers is a jury (not leaderboard) criterion.

## Scoring

- **Leaderboard (automatic, binary per case):** your submitted action list
  must match one of the case's accepted answers after normalization. No
  partial credit. A `NO_ACTION`/`ESCALATE` with the right reason is a correct
  answer — silence or no submission is always wrong. More than one submission
  can be correct (ties on "earliest slot" etc.) — the case defines a *set* of
  acceptable outcomes, matching any member passes.
- **Points:** each scored problem has a weight (1–5). Score =
  `Σ over problems (pass_fraction × weight)`, pass_fraction is 0/.25/.5/.75/1
  of that problem's 4 private cases. No percentage/denominator — sum only
  grows as problems open. A problem never attempted scores 0, same as one
  attempted and failed. Full roster (17 scored problems) = 49 points max.
  Problem 2 (Switchboard) carries 0 weight and is never dialled by Run All —
  it's a self-triggered concurrency readiness check only.
- Leaderboard rank uses your **best** Run All, not latest or cumulative.
- **Call limits:** 3 minutes max per call. No audible audio from your agent
  during the silence window = cut off, attributed as your failure.
- **Failure attribution** (deterministic, no LLM arbiter): matching record →
  pass. Missing/mismatched record or agent-side connectivity/audio issues →
  `agent_issue`, case fails. Wall-clock/turn-cap/unexplained disconnect →
  `inconclusive`, case fails (evidence kept for dispute). Confirmed harness
  defect → entire run voided (must be requested explicitly as a replacement,
  doesn't auto-rerun).
- **What's NOT scored by the leaderboard:** voice quality/accent/politeness,
  transcription accuracy alone, number/order of questions or tool calls,
  model choice/cost, speed. (Problem 14 is the one exception — its transcript
  is checked for leaked patient data.)
- **Practice vs scored transparency:** practice calls give full
  transcript+recording+field-diff immediately. Scored (Run All) calls only
  tell you pass/fail + failure signal until the Monday reveal — no transcript,
  audio, or per-field diff, and the *expected* answer is never published at
  all, before or after reveal.

## The 18 problems

3–6 public (practice, answers published) + private pool per problem. Problem
2 has no cases of its own (it's just problem-1 bursts). "Open" = releasable
progressively over the weekend as verified — check the problems page for
current status before assuming a problem is scorable.

| # | Problem | id | Weight | Public cases | Tests |
|---|---|---|---|---|---|
| 1 | The Simple Booking | `simple_booking` | 1 | 4 | Baseline happy path |
| 2 | The Switchboard | `switchboard` | 0 (ungraded) | — | Concurrency burst (5/10/20 at once) of problem 1 |
| 3 | The Doctor and the Site | `doctor_and_site` | 2 | 5 | Named provider+site, ambiguity/leave/fallback |
| 4 | The New Patient | `the_new_patient` | 2 | 4 | Registration only, no booking; DNI check-letter accuracy |
| 5 | When Exactly | `when_exactly` | 2 | 5 | Colloquial date resolution vs call time/hours/closures |
| 6 | The Rules | `the_rules` | 3 | 5 | Age/referral/insurance refusal matrix, exact `reason` |
| 7 | No Slot Free | `no_slot_free` | 2 | 4 | Negotiate alternative or correctly say no availability |
| 8 | Change and Cancel | `change_and_cancel` | 2 | 4 | Reschedule/cancel via `appointment_id` lookup only |
| 9 | The Third Party | `third_party` | 3 | 4 | Caller ≠ patient; must book for the patient |
| 10 | Triage | `triage` | 3 | 5 | Symptom → specialty routing + red-flag escalation |
| 11 | Languages | `languages` | 3 | 4 | Spanish/Catalan; private cases skew harder than public |
| 12 | Noise | `noise` | 3 | 4 | Background noise at fixed 5dB SNR, acoustic-only test |
| 13 | The Difficult Caller | `difficult_caller` | 4 | 5 | Interruptions/corrections; book the *final* stated request |
| 14 | Adversarial and Privacy | `adversarial` | 4 | 4 | Injection/PII requests; transcript substring-checked for leaks |
| 15 | The Nearest Site | `nearest_site` | 3 | 4 | Geo distance from caller address to site, constrained by capability |
| 16 | The Questions | `the_questions` | 3 | 5 | Caller interrogates first; wrong facts told → booking fails |
| 17 | The Second Policy | `second_policy` | 4 | 4 | Second insurance plan not on file, must be elicited |
| 18 | The Real Call | `the_real_call` | 5 | 3 | Multiple axes + multi-action in one call, no partial credit |

### Problem 10 red-flag list (must `ESCALATE(medical_emergency)`, never book)

Tight chest pain + can't catch breath; sudden facial droop/arm weakness/
slurred speech; can't get breath at all, sudden onset; heavy bleeding not
stopping after 10 min of pressure; head injury an hour ago with confusion/
vomiting since.

## Jury scoring (Sunday, "the final boss")

Separate score, added to the leaderboard total. Criteria: patient experience
(pace, warmth, interruption handling, no invented slots/doctors/rules);
personalization (chart used proactively — a regular isn't asked "have you
been here before"); the platform built around the model (live console,
observability, "why did it say that", 10-concurrent-call demo — deliberately
unscoped, "surprise us"); safety/boundaries (no medical practice, escalates
before improvising, resists being talked out of its rules); language quality
(code-switching mid-call, correct pronunciation of Spanish names/ids);
engineering rigour (own eval harness, variance across runs, named failure
modes, cost/latency per call); and jury discretion (unscoped wildcard).
Nothing the jury can't observe on a call or demo scores.
