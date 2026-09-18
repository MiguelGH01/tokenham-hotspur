# Functional requirements

Agent behaviours required to pass cases and to support the jury demo. Each ID is stable so client examples can later cite them.

## Identification and patient resolution

| ID | Requirement | Priority |
|---|---|---|
| `FR-identify` | Establish who the **patient** is (caller is not always the patient). Ask for a second identifier when needed. | must |
| `FR-directory` | Resolve patients via `GET /api/v1/directory` using exact fields (`name`, `national_id`, `phone`, `date_of_birth`). An exact field that does not match **excludes** the patient (filter, not downrank). | must |
| `FR-record-ids` | Submit the **record’s** `patient_id`, names, and related ids — never invent ids from what the caller said. A nickname may find a patient; it is not a legal name for registration. | must |
| `FR-national-id` | Capture the complete national id (DNI/NIE) including check letter when required. One wrong character fails the case. | must |
| `FR-confirm-id` | Confirm identity on a second field when ids may collide (some ids differ by one digit). | must |
| `FR-from-number` | Treat `from_number` on the wire as a **hint** only: may be absent (withheld / unknown), and the line owner is not always the patient being booked. | must |
| `FR-third-party` | When a third party calls (parent, daughter, carer), book for the **patient**, not the caller. Callers often offer their own details first. Public `PR-06` “daughter’s check-up” books the child (`P00009`), not a parent GP slot. | must |
| `FR-register-homonym` | A fuzzy name hit is not the caller. If `national_id` (and DOB) match nobody, `REGISTER` — do not `BOOK` an existing namesake (`PR-04-S5`, `LIVE-11`). | must |
| `FR-spoken-plan` | Map spoken / catalogue display names to plan ids (e.g. “Mapfre Salud” → `mapfre`). Submit the id, not the marketing name (`PR-04-S4`). | must |

## Chart, notes, and personalisation

| ID | Requirement | Priority |
|---|---|---|
| `FR-chart` | Read the patient chart (directory `note`, upcoming and past appointments) before asking questions the chart already answers. | should |
| `FR-history` | Use visit history to confirm identification and to speak personally (“you last saw Dr. X…”). Past visits are not cancellable/movable. | should |
| `FR-caller-intent-wins` | Prefer the caller’s stated ask over silently booking the “usual” doctor when they asked for soonest (or similar). Notes are context, never a scheduling preference that outranks the ask. | must |

## Scheduling decisions

| ID | Requirement | Priority |
|---|---|---|
| `FR-availability` | Obtain bookable slots only from `GET /api/v1/availability` (and related catalogue). Never invent a slot, provider, or rule. | must |
| `FR-appointment-type` | Choose `appointment_type_id` from specialty + patient’s `has_visited_before` (and specialty-specific types). Prefer the type named on the availability response / slots. Hard-coding `review` for every follow-up fails specialties with their own type ids. | must |
| `FR-earliest` | “Earliest” / soonest means earliest from **the day after the call** (Europe/Madrid at connect time). Same-day slots are never accepted. | must |
| `FR-relative-time` | Resolve relative/colloquial times (“next Thursday”, “first thing Monday”, etc.) against connect time, site hours, and published closures. `tomorrow` is the next calendar day (may be Saturday). `this coming <weekday>` is the first such weekday **strictly after** the day of the call. | must |
| `FR-closed-day-roll` | If the requested calendar day is closed (Sunday, Fiesta 12 Oct, site shut), book the earliest slot on the **next open day** that still matches the rest of the ask. Do not refuse and do not book a closed-day slot even if the API lists one (`PR-05-S4`, `LIVE-01`). | must |
| `FR-age-remap` | For a general complaint, if the spoken specialty is age-ineligible, book the age-correct specialty (GP ↔ paediatrics at 14 / 168 months). Do not `NO_ACTION(not_eligible_age)` when the other side of the boundary can serve (`PR-06-S1`). | must |
| `FR-site-provider` | Honour named provider and/or site when requested. If provider is on leave: book earliest **same specialty at same site**. If provider is not at that site on the requested day: keep provider+site, take earliest slot there on another day. Fallbacks must not drop specialty or site when both were required. | must |
| `FR-provider-missing` | If the named provider does not exist and the caller refuses anyone else, submit `NO_ACTION(provider_not_found)` — do not invent a booking. | must |
| `FR-nearest-site` | When the caller gives a street address and asks for the closest clinic, book the nearest site that can **serve** the request (straight-line distance to published coordinates), not refusal and not closest if it cannot serve. | must |
| `FR-policy` | Submit the correct `policy_id` for the plan that covers the booking. Patients may hold a second plan not on the directory; ask for it when the first plan does not cover. Do not invent a second plan. | must |
| `FR-language-provider` | When the case constrains language (esp. Catalan), book a provider who speaks that language. | must |
| `FR-load` | When several providers tie, prefer spreading load over always picking the same first tied doctor — for jury quality; board accepts any acceptable member of the set. | should |

## Writes reported (actions)

| ID | Requirement | Priority |
|---|---|---|
| `FR-book` | Report a booking via `POST /submit/book` with `patient_id`, `provider_id`, `location_id`, `appointment_type_id`, `slot` (explicit TZ offset), `policy_id`. | must |
| `FR-register` | For unknown callers who want registration only: `POST /submit/register` with **flat** demographics; do **not** also `BOOK` if the case forbids it. A patient not on chart cannot be booked. Dashboard readback nests the same fields under `new_patient`. | must |
| `FR-reschedule` | Move an existing **upcoming** appointment via `POST /submit/reschedule` using `appointment_id` from appointments lookup only. | must |
| `FR-cancel` | Cancel via `POST /submit/cancel`; support multi-cancel (multiple posts) when the caller cancels more than one appointment. | must |
| `FR-no-action` | When the correct outcome is refusal / cannot book, submit `NO_ACTION` with a closed-vocabulary `reason` that names the rule (or situation). Empty submission always fails. | must |
| `FR-escalate` | For published medical red flags, submit `ESCALATE` with `reason=medical_emergency`; book nothing. | must |
| `FR-multi-action` | Support calls that require a **list** of actions (e.g. cancel + book). All must be correct; no partial credit inside a case. | must |
| `FR-final-intent` | On difficult callers, book the caller’s **final** stated request, not the first. | must |

## Rules and safety

| ID | Requirement | Priority |
|---|---|---|
| `FR-blocked` | Use restriction metadata from `/availability` (`blocked`) to choose the correct refuse reason rather than guessing. | must |
| `FR-clinic-rules` | Enforce age boundaries, referrals, insurance matrix, site coverage, opening hours, Fiesta Nacional closure, no same-day booking. | must |
| `FR-no-medicine` | Do not practise medicine; escalate before improvising clinical advice. | must |
| `FR-privacy` | Do not read out another patient’s protected data (national id, phone). Decline out-of-scope / adversarial asks with `NO_ACTION(out_of_scope)` and a clean transcript. | must |
| `FR-triage` | Route symptom descriptions to the correct specialty per published mappings; do not invent clinical judgement beyond the published red-flag list. | must |
| `FR-questions` | When callers ask factual questions about the clinic before booking, answer accurately from catalogue data — wrong facts cause wrong bookings and fail the case. | must |

## Conversation and concurrency

| ID | Requirement | Priority |
|---|---|---|
| `FR-state` | Keep per-call conversational state so corrections, interruptions, and mind-changes are reflected in the final submission. | must |
| `FR-turn-taking` | Implement barge-in / interruption handling on the agent side (harness does not provide server-side barge-in). | must |
| `FR-concurrency` | Handle many concurrent WebSocket calls with **isolated** pipelines (no shared conversation / `call_id` across sockets). Run All opens ten at once; switchboard bursts up to twenty. | must |
| `FR-noise` | Still identify and book correctly under noisy audio (published SNR conditions on noise problems). | must |
| `FR-languages` | Handle non-English callers (Spanish, Catalan, and private-case languages of Spain) including mid-call code-switching without requiring a restart. | must |
| `FR-audible` | Produce audible audio from the agent; streaming silence keeps the socket open but counts as saying nothing and can cut the call off. | must |
| `FR-time-budget` | Complete within the **three-minute** wall-clock call cap (and any turn caps the harness applies). | must |

## Related docs

- Call wire and submit window → [03-call-and-submission](03-call-and-submission.md)
- Domain rules that back these behaviours → [04-clinic-domain](04-clinic-domain.md)
- Problem-specific intents → [06-problems](06-problems.md)
