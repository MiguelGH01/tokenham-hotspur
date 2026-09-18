# Add to the agent: PR-04 / PR-05 / PR-06

Handoff for another session. **Do not start a new agent.** Same graph as [process-map.md](process-map.md) + [flow.yaml](flow.yaml): A identify → B constraints → C availability → D confirm/submit. Code owns ids, dates, types, `reason`, and every POST. The LLM talks.

Public answers and traps: [the_new_patient.md](../requirements/scenarios/the_new_patient.md), [when_exactly.md](../requirements/scenarios/when_exactly.md), [the_rules.md](../requirements/scenarios/the_rules.md).

---

## 1. Register only (`PR-04`)

Wire `lookup_patient` → `none` → `unknown_patient` → `register_patient` → `submit_register` → `goodbye`.

- Query **`national_id` (or DOB)**, not name alone. A name hit plus nid miss is `none`, not `unique`.
- Homonyms already on file: Natalia Muñoz González `P01239` (other person) and Sergio Martínez Ramírez `P01988` (other person). Public nids `18921027P`, `X0500252W`, `50454876Y`, `31426012P` → 0 matches → `REGISTER`.
- POST **`/submit/register` is flat** (`call_id` + demographics). Dashboard nests `new_patient`. No `BOOK`.
- NIE as well as DNI (letter-first, e.g. `X0500252W`). Both surnames. Email local-part is literal (dots, digits, underscores).
- Spoken **Mapfre Salud** → `mapfre`. Same for other display names → catalogue id.

---

## 2. Requested date (`PR-05`)

Clock = **connect time, Europe/Madrid**. No same-day. Type still from the record (`review` / `first_visit` / `orthopaedic_review`).

| Phrase | Rule |
|---|---|
| `tomorrow` | Next calendar day. **May be Saturday** (Centro only). |
| `this coming <weekday>` | First that weekday **strictly after** the call day. Fri → Thursday = **24 Sep**, not 17. |
| `Saturday morning` | Centro, before 14:00. |
| Closed day (Sunday, Fiesta **12 Oct**, site shut) | Earliest on the **next open day**, **same site** and rest of filters. Do not `NO_ACTION`. Do not book the closed day even if `/availability` lists slots (`payable_with: []` on 12 Oct). |

Public rolls (connect assumed Fri 18 Sep 2026): Sunday Centro → Mon 21 09:15; “first thing Monday 12 Oct” → Tue 13 09:45 `first_visit`.

---

## 3. Clinic rules (`PR-06`) — run in this order, in code

1. **Caller ≠ patient.** “Your daughter’s check-up” books the child, not the parent.
2. **Age remap** (general complaint only): paeds `0–167` months, GP `168+`. Spoken “GP” for a child → **paediatrics**, not `NO_ACTION(not_eligible_age)`. Public: Sonia `P00009` (DOB 2017-05-12) → `paediatric_review` `PR08` Norte `privado`.
3. Derm/physio **without** a held referral → `NO_ACTION(referral_required)`. Teresa `P00004` (ASISA, physio ref only) asking derm.
4. Plan × specialty dead-end, nowhere to send → `NO_ACTION(specialty_not_covered)`. Josefa `P00015` Adeslas + gynae.
5. Named doctor refuses the plan but another in the **same specialty** takes it → **redirect**, do not refuse. Gloria DKV + Dra. Iglesias + derm referral → Vilar `PR12`.
6. **Control:** adult **with** the referral books. Ignacio derm → Vilar. Do not learn “always refuse derm”.

Take `reason` from `blocked[].restriction` when the API names it. Empty submit always fails.

---

## What not to touch

No new Flow nodes beyond what [flow.yaml](flow.yaml) already names (`unknown_patient`, `register_patient`, closed-day / age logic inside `lock_constraints` + `search_availability`, `redirect_in_specialty`). Do not book a fuzzy namesake, Sunday, Fiesta, GP for a child, or the parent instead of the child.
