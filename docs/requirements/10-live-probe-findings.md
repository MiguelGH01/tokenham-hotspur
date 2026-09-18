# Live clinic probe — findings

**Date:** 18 Sep 2026  
**Method:** Authenticated reads against `GET /api/v1/clinic`, `/directory`, `/availability`, `/patients/.../appointments` for the 12 open public cases.  
**Secrets:** API key used from shell env only — **not** stored in this repo.

All accepted `BOOK` answers for `PR-01` / `PR-03` were confirmed present as the earliest matching slot under the case constraints (ids + type + site + plan).

---

## Catalogue facts (verified)

| Fact | Live value |
|---|---|
| Clinic | Clínica Arenal · **2900** patients · **7440** appointments in window |
| Calendar | `2026-09-07` → `2026-10-16`, 15-min slots, max span **14** days |
| Closure day | `closure_days: ["2026-10-12"]` (Fiesta Nacional) |
| Age split | Paediatrics `0–167` months; GP & gynae from **168** (14th birthday) |
| Referral specialties | Dermatology, physiotherapy (`referral_required`) |
| Catalan speakers (exactly 4) | `PR01` Ortiz (GP Centro), `PR08` Ocaña (paeds Norte/Sur), `PR10` Peral (ortho Norte/Sur), `PR12` Vilar (derm Norte) |

### Providers (trap sheet)

| Id | Name | Specialty | Sites | Notes |
|---|---|---|---|---|
| PR01 | Dra. Carmen Ortiz Vidal | GP | Centro | Catalan; **only GP with Saturday** (`09:00–13:00`) |
| PR02 | Dr. Pablo Requena | GP | Norte | **Leave 2026-09-14 → 2026-09-30**; `/availability` → `provider_on_leave`, 0 slots |
| PR03 | Dr. Martín Sáez | GP | Centro **Fri only**; Sur Mon–Thu | Near-miss with Sáenz |
| PR04 | Dra. Marta Sáenz | Paediatrics | Centro | Near-miss with Sáez; adult patient → `not_eligible_age` |
| PR05 | Dra. Elena Iglesias | Dermatology | Centro, Sur | **Refuses DKV** → `provider_not_in_network` |
| PR06 | Dr. Emilio Iglesia | Orthopaedics | Centro | Near-miss with Iglesias |
| PR07 | Dra. Laura Benítez Roca | GP | Centro Mon/Wed/Fri; Norte Tue/Thu | **Spanish only** (no `en`/`ca`); leave fallback for Requena at Norte |
| PR08 | Dr. Javier Ocaña | Paediatrics | Norte, Sur | Catalan |
| PR09 | D. Álvaro Cid | Physiotherapy | **Sur only** | Title **D.** not Dr. |
| PR10 | Dra. Nuria Peral | Orthopaedics | Norte Fri; Sur Mon/Wed | Catalan |
| PR11 | Dra. Isabel Montoro | Gynaecology | Centro | Sole gynae |
| PR12 | Dr. Tomás Vilar | Dermatology | Norte | Catalan; takes DKV (redirect target from Iglesias) |

### Sites / hours

| Site | Hours trap |
|---|---|
| Centro | Mon–Fri 08–20; **Sat 09–14**; coords `(40.4178, -3.7075)` |
| Norte | Mon–Fri 09–19; **no Saturday**; not covered by `nueva_mutua` |
| Sur | Mon–Thu 08–18; **Fri 08–14 only**; **no Saturday**; not covered by `asisa` |

### Plans (coverage dead-ends)

| Plan | Uncovered specialties | Uncovered sites | Extra |
|---|---|---|---|
| adeslas | Gynaecology | — | Sole gynae → nowhere to redirect |
| dkv | Physiotherapy | — | Iglesias refuses DKV |
| asisa | — | **Arenal Sur** | Physio only at Sur → ASISA physio impossible (`location_not_covered` on PR09) |
| mapfre | Dermatology | — | |
| caser | Dermatology, Gynaecology, Physiotherapy | — | |
| nueva_mutua | Orthopaedics | **Arenal Norte** | |
| privado / sanitas / cigna / axa | — | — | Full coverage in catalogue |

### Appointment types (ids that fail if swapped)

Universal: `first_visit` (30m), `review` (15m).  
Specialty pairs (same minutes as universal where noted — **id must still match**):

- Paediatrics: `paediatric_first_visit` / `paediatric_review` (both 30m)
- Gynaecology: **`gynaecology_review` only** + universal `first_visit` for new
- Physio: `physiotherapy_assessment` (45m) / `physiotherapy_session` (30m)
- Dermatology: `dermatology_first_visit` / `dermatology_review` (15m like `review`)
- Orthopaedics: `orthopaedic_first_visit` (45m) / `orthopaedic_review` (15m like `review`)

Guidance on `dermatology_review` / `orthopaedic_review` explicitly warns that booking generic `review` fails on an otherwise correct slot.

---

## Directory behaviour (verified)

| Trap | Evidence |
|---|---|
| Name-only search returns **10 fuzzy matches** | Every case name returned score-1 target + 9 near-misses (swapped surnames, relatives). |
| Exact field excludes | Josefa + wrong DOB → **0** matches; wrong NID → **0**; name+correct NID → only `P00001`. |
| Phone forms fold | Ignacio `731169716`, `+34731169716`, `0034731169716` → same `P00005`. |
| Notes are actionable | e.g. Ignacio already has upcoming derm appointment `A001101`; Amelia `has_visited_before=false` + empty diary. |

**Requirement reminder:** always add a second exact identifier before trusting a name hit (`FR-identify`, `FR-confirm-id`).

---

## Case-by-case verification

### PR-01

| Case | Live check |
|---|---|
| Josefa `P00001` | Earliest GP = `PR01@centro` `2026-09-19T11:00+02` `review` `mapfre`. PR02 blocked `provider_on_leave`. Has derm referral but mapfre → derm specialty_not_covered. |
| Amelia `P00012` | Never seen → `first_visit`. Earliest **at Centro** = `PR07` `2026-09-21T11:45` (not PR01). |
| Ignacio `P00005` | Phone id path OK. Ortho → **`orthopaedic_review`** (not `review`). Earliest `PR10@sur` `2026-09-21T09:30`. Upcoming derm appt on file — do not cancel unless asked. |
| Chloe `P00011` | Sur + Monday morning → `PR03@sur` `2026-09-21T09:00` `review`. |

### PR-03

| Case | Live check |
|---|---|
| Joaquín / Ortiz Centro | Exact `PR01@centro` earliest matches accepted; `privado` plan. |
| Emilio / Sáez not Sáenz | `PR03` earliest at Sur Mon. Booking `PR04` Sáenz → type `paediatric_review` + `not_eligible_age` (adult). |
| Andrés / Requena Norte | `PR02` 0 slots + `provider_on_leave`. Earliest **same specialty @ Norte** = `PR07` Tue `2026-09-22T09:00` `dkv`. |
| Mario / Sáez Centro Monday | Sáez **not at Centro on Monday** (Centro = Friday only). Keep provider+site → Fri `2026-09-25T09:30` `first_visit` `asisa`. |
| Vicente / Fuentes | Patient exists (`P01846`); doctor does not → `NO_ACTION(provider_not_found)`. |

### PR-02

No per-burst catalogue difference — concurrency isolation only. Each dial is a PR-01-class booking.

---

## New traps to build for (beyond case answers)

| ID | Trap | Detail |
|---|---|---|
| `LIVE-01` | **Closure day still lists slots** | `GET /availability` for `2026-10-12` returns many GP slots across all sites, but `payable_with: []` and calendar `closure_days` includes that date. **Do not book Fiesta Nacional** even if slots appear. |
| `LIVE-02` | **Saturday GP scarcity** | Only Ortiz sits Saturday; early Saturdays in window can be fully booked (0 Centro GP slots on 19 & 26 Sep). “On Saturday” may need the next free Saturday or negotiation (`PR-05`/`PR-07` territory). |
| `LIVE-03` | **No GP at Sur on Friday** | Sáez is at Centro Fridays; Sur Fri afternoon clinic closes at 14:00 and morning has no GP in probe. Friday+Sur GP asks are fragile. |
| `LIVE-04` | **ASISA × Sur** | Any Sur search for ASISA patient → all providers `location_not_covered`. |
| `LIVE-05` | **ASISA × physio** | Only physio is Sur → ASISA physio → `location_not_covered` on PR09 (dead end). |
| `LIVE-06` | **DKV × Iglesias** | `provider_not_in_network` on PR05 when quoting DKV; Vilar PR12 still has DKV slots. |
| `LIVE-07` | **`insurer` query rewrites payable_with** | Passing `insurer=mapfre` for a Sanitas patient still returned Centro slots labeled `payable_with: ["mapfre"]`. Agent must not invent a second plan; only submit a plan the patient actually holds (`PR-17`). |
| `LIVE-08` | **Blocked reason priority** | Same provider can fail for different reasons depending on params (leave vs referral vs network). Prefer the `blocked[].restriction` string as `OutcomeReason`. |
| `LIVE-09` | **Amelia earliest ≠ Ortiz** | First free Centro first-visit was Benítez (`PR07`), not Ortiz — “usual doctor” must not override soonest when asked. |
| `LIVE-10` | **Name collision is specialty collision** | Sáez↔Sáenz and Iglesias↔Iglesia sit in different specialties; wrong pick fails age or specialty rules, not just “wrong id”. |

---

## Requirement ID cross-links

| Finding | Existing IDs |
|---|---|
| Fuzzy name list | `FR-identify`, `FR-confirm-id`, `CL-exact-exclude` |
| Leave redirect same site | `PR-03-S3`, `CL-requena-leave`, `FR-site-provider` |
| Wrong weekday keep provider+site | `PR-03-S4` |
| Specialty type ids | `FR-appointment-type`, `CL-type-from-availability`, `PR-01-S4` |
| Closure / relative dates | `CL-fiesta`, `FR-relative-time`, **`LIVE-01`** |
| Catalan constraint | `FR-language-provider`, `PR-11` |
| Plan dead-ends | `CL-asisa-physio`, `CL-adeslas-gynae`, `CL-dkv-iglesias`, `LIVE-04`…`06` |

---

## Follow-ups when more problems open

- Re-probe private-case templates are impossible (answers unpublished); re-run this playbook on new **public** cases as they appear.
- Confirm with a practice call whether Oct 12 slots are scorer-rejected or API bug — treat as **unbookable** until proven otherwise.
- Rotate the team API key if this chat is shared beyond the team (`OP-lost-key`).
