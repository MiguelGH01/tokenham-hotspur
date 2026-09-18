# Clinic domain requirements

**Clínica Arenal** is a read-only EHR, generated once and identical for every team and every call for the whole event. Cache freely. This page captures **rules and traps** the schema cannot state.

Every clinic endpoint needs `X-Api-Key`. Field-level shapes are deferred to the API phase; endpoints and behaviours below are requirements.

## Catalogue and lookups

| ID | Requirement | Priority |
|---|---|---|
| `CL-readonly` | There is no booking endpoint on the clinic API. Nothing called here reserves a slot; report decisions via the contract. | must |
| `CL-cache` | Static catalogue endpoints never change during the event — pull once at start-up and hold. | should |
| `CL-directory` | `GET /api/v1/directory` — who is calling / patient match: `name`, `national_id`, `phone`, `date_of_birth`. | must |
| `CL-availability` | `GET /api/v1/availability` — what may be booked and when: `date_from`, `date_to`, plus `provider_id` or `specialty_id`, optional `location_id`, `patient_id`, repeated `insurer`. Span ≤ 14 days; outside bookable range → `422`. | must |
| `CL-appointments` | `GET /api/v1/patients/{patient_id}/appointments` — diary; `when` = `upcoming` (default), `past`, or `all`. **Only source of `appointment_id`**. | must |
| `CL-clinic-bundle` | `GET /api/v1/clinic` — full catalogue plus bookable window and standing restrictions with decline reasons. | should |
| `CL-providers` | `GET /api/v1/providers` — specialty, languages, types, sites/hours, plans taken/refused, leave. | must |
| `CL-locations` | `GET /api/v1/locations` — three sites: address, hours, who sits there, plan coverage. | must |
| `CL-specialties` | `GET /api/v1/specialties` — age window, referral required, plan coverage; ids for `availability?specialty_id=`. | must |
| `CL-types` | `GET /api/v1/appointment-types` — duration, specialty, which patient it is for; each has `guidance`. | must |
| `CL-plans` | `GET /api/v1/insurance-plans` — coverage, where, which providers take it. | must |

## World scale (fixed)

| ID | Fact |
|---|---|
| `CL-world` | Three sites, twelve providers, six specialties, eleven appointment types, ten insurance plans, ~3,000 patients with visit histories, fixed calendar. |

## Directory behaviour

| ID | Requirement | Priority |
|---|---|---|
| `CL-exact-exclude` | An exact query field that does not match **excludes** the patient. | must |
| `CL-submit-record-name` | Submit the record’s name and id, never the caller’s nickname/mishearing as the legal identity for writes. | must |
| `CL-phone-fold` | `phone` queries fold to nine national digits (`+34…`, `0034…`, national form are one query). | must |
| `CL-note` | Every patient record carries a `note` (how history runs, how to talk to them). Not scored on the board; material for jury personalisation. | should |
| `CL-referrals` | Held referrals live on the directory record. | must |

## Sites

| ID | Requirement | Priority |
|---|---|---|
| `CL-sites` | Sites: Centro, Norte, Sur (ids as published in catalogue). | must |
| `CL-saturday` | Only Centro opens on Saturday. Nothing opens Sunday. | must |
| `CL-coords` | Site coordinates appear in availability location data; ground truth for nearest-site (`PR-15`). | must |

## Specialties and age

| ID | Requirement | Priority |
|---|---|---|
| `CL-age-boundary` | 14th birthday is the age boundary (in months): every age has exactly one correct specialty for a general complaint; no gap/overlap. | must |

## Providers (traps)

| ID | Requirement | Priority |
|---|---|---|
| `CL-requena-leave` | Dr. Requena is on leave **14–30 September** (covers the whole event). Callers asking for him by name must be moved. | must |
| `CL-name-collision` | Near-miss pairs: **Sáez** (GP) / **Sáenz** (paediatrics); **Iglesias** (dermatology) / **Iglesia** (orthopaedics). Disambiguate. | must |
| `CL-cid-title` | D. Álvaro Cid is a physiotherapist — title is **D.**, not Dr.; title is part of the name used. | must |
| `CL-language-default` | Language constrains booking only where the problem/case sets it (`PR-11`); elsewhere assume any provider can take the call. | must |
| `CL-dkv-iglesias` | Dra. Iglesias does not take DKV; Dr. Vilar does — DKV patient asking for her by name is a **redirect**, not a refusal. Every other provider takes all ten plans. | must |

## Insurance

| ID | Requirement | Priority |
|---|---|---|
| `CL-asisa-physio` | ASISA covers physiotherapy only at Centro and Norte, but the only physiotherapist sits at Sur → ASISA patient can never book physio. | must |
| `CL-adeslas-gynae` | Adeslas covers no gynaecology; one gynaecologist → nowhere to redirect. | must |
| `CL-privado` | `privado` is self-pay and a plan the patient holds or not — not a fallback. Uncovered → refuse. | must |
| `CL-second-policy` | Patients hold one or two plans. Only the first is on the directory; a second exists to be asked for on the call (`PR-17`). | must |
| `CL-insurer-param` | Naming a plan on availability (`insurer`) is the only way to be quoted against it. Omitting prices against the single plan on the record. | must |

## Appointment types

| ID | Requirement | Priority |
|---|---|---|
| `CL-type-rule` | Exactly one type is right: from **specialty** + **`has_visited_before`**, never from conversation wording. Specialty-specific types win over universal `first_visit` / `review`. Gynaecology has its own review only → new gynae patient uses universal `first_visit`. | must |
| `CL-type-from-availability` | Every `/availability` response names the fitting type as `appointment_type`; every slot carries that type id. Submit that id. Wrong type on a real slot fails. | must |

## Appointments history

| ID | Requirement | Priority |
|---|---|---|
| `CL-upcoming-default` | Default `when=upcoming` — what the caller can still act on. | must |
| `CL-past` | `when=past`: visits in 2024–2025 (up to eight) for most returning patients. For read-back / jury only. | should |
| `CL-past-not-mutable` | Past visits cannot be cancelled or moved; their `appointment_id` is not a valid answer for change/cancel problems. | must |
| `CL-no-past-new` | `has_visited_before=false` (and some youngest patients) have no past visits. | must |

## Calendar and clock

| ID | Requirement | Priority |
|---|---|---|
| `CL-slot-range` | Bookable slots: **7 September – 16 October 2026**, 15-minute steps. | must |
| `CL-busy-spread` | Provider diaries intentionally uneven (~40–72% full); choice of doctor is a real decision. | should |
| `CL-fiesta` | Monday **12 October 2026** is Fiesta Nacional — whole network shut. Makes “first thing Monday” a trap when that Monday is the 12th. **Live probe:** `/availability` may still return slots that day with empty `payable_with` — do not book them (`LIVE-01`). | must |
| `CL-clock` | Dates resolve against **the moment the call connects**, Europe/Madrid — not the machine clock, not a fixed anchor. | must |
| `CL-no-same-day` | Nothing is booked for the same day. Availability may still list later-today free slots; they are never accepted answers. | must |

## Availability semantics

| ID | Requirement | Priority |
|---|---|---|
| `CL-blocked` | Restriction metadata / `blocked` returns whether or not there are slots and names the standing rule that stopped a provider. | must |
| `CL-empty-full` | Empty `slots` with empty `blocked` means the calendar is simply full — distinct from a restriction refusal. | must |

## Scheduling guidelines (board-unscored)

These are receptionist quality bars for the jury (`JR-*`), not leaderboard criteria:

| ID | Guideline |
|---|---|
| `CL-guide-register-first` | Register before booking when the directory does not know the caller. |
| `CL-guide-read-chart` | Read chart/note/history before asking whether they have visited. |
| `CL-guide-personal-offer` | Offer personal options (“Dra. Ortiz usually sees you…”) while still letting the caller decide. |
| `CL-guide-respect-rules` | Refuse with the named rule when a restriction bites. |
| `CL-guide-spread` | Spread load across capable providers when the caller’s ask allows. |
| `CL-guide-go-further` | Remember prior calls, read back appointments naturally, notice upcoming appointments, ask note-implied questions. |

## Related docs

- Submit shapes → [03-call-and-submission](03-call-and-submission.md)
- Functional use of these rules → [02-functional](02-functional.md)
- Problem traps → [06-problems](06-problems.md)
