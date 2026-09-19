# Add to the agent: PR-04 / PR-05 / PR-06

Handoff for another session. **Do not start a new agent.** Same graph as [process-map.md](process-map.md) + [flow.yaml](flow.yaml): A identify → B constraints → C availability → D confirm/submit. Code owns ids, dates, types, `reason`, and every POST. The LLM talks.

**Public cases are fixtures, not the product.** They are how we score on the leaderboard this weekend. Run All and the jury pass will use **private / unseen** callers, wordings, and combinations. Implement **general rules driven by the live catalogue + `/availability` + connect clock**. Do not `if patient_id == P00009` / `if slot == 2026-09-19T11:00`. If a public name or slot appears below, it is a **regression check** that the rule fired, not a branch to code.

Walkthroughs (expected JSON for those fixtures): [the_new_patient.md](../requirements/scenarios/the_new_patient.md), [when_exactly.md](../requirements/scenarios/when_exactly.md), [the_rules.md](../requirements/scenarios/the_rules.md).

---

## Design bar

| Do | Do not |
|---|---|
| Read age windows, `referral_required`, plan coverage, site hours, `closure_days`, provider `refused_insurers` from the clinic bundle | Hard-code Sonia, Teresa, 12 Oct, Vilar, `mapfre` as the only answers |
| Resolve **whatever** date the caller said against Europe/Madrid at connect + hours + closures | Only accept the five public phrases |
| `blocked[].restriction` → `OutcomeReason` when the API names the rule | Guess `referral_required` vs `specialty_not_covered` from problem_id |
| Map any spoken plan **display name** → catalogue `id` | Special-case “Mapfre Salud” only |
| Treat directory exact fields as filters; nid/DOB miss → new person | Book the first fuzzy name hit |

The LLM may hear new wording. Code must still: identify patient vs caller, resolve when, apply catalogue rules, POST something (never empty).

---

## 1. Register (`PR-04`) — unknown person, not a list of four

`lookup_patient` → `none` → `unknown_patient` → `register_patient` → `submit_register`.

- Identity is **nid / DOB / phone**, not name. Name-only can return 10 people **and** a legal namesake who is someone else. Name hit + nid miss = `none` → `REGISTER`, never `BOOK` that row.
- Any new DNI **or** NIE (letter-first included). Both surnames. Email local-part is what they said (dots, digits, underscores) — do not “fix” it.
- POST `/submit/register` is **flat**. Dashboard nests `new_patient`. These cases are register-**only**: no `BOOK` unless a later problem says otherwise.
- `insurer` = catalogue id. Spoken labels come from the plan list (`Mapfre Salud` → `mapfre` is one example of the general map).

Leaderboard fixtures that must pass if the rule is right: four public nids currently 0 matches; two of those names already exist as other DOBs.

---

## 2. When (`PR-05`) — a date engine, not five calendars

Clock = **this call’s connect time**, Europe/Madrid. No same-day. Appointment type still from `/availability` / `has_visited_before`, not from the phrase.

Build one resolver:

1. Parse the ask to a **target day or window** (relative, weekday, named calendar day, morning `<14:00` / afternoon `≥14:00`, “first thing” = earliest that morning).
2. Intersect with **that site’s hours** (Saturday = Centro only is a catalogue fact, not a special case for Ignacio).
3. If that day is closed (Sunday, `closure_days`, site shut) → **next open day** that still matches the rest of the ask (same site if they named one). Do not `NO_ACTION` just because the named day is shut. Do not book a closed-day slot even if `/availability` returns rows with empty `payable_with`.

Public phrases (`tomorrow`, `this coming Thursday`, `Saturday morning`, Sunday, “Monday the twelfth of October”) are **the published leaderboard vocabulary**. The engine should survive other colloquial dates and another connect day. Recompute; do not paste `2026-09-21T09:15:00+02:00`.

---

## 3. Rules (`PR-06`) — a policy engine, not five stories

Run **in code**, from **this patient’s chart + catalogue**, in this order. Any patient, any specialty, any plan.

1. **Patient ≠ caller** when the visit is for someone else (child, parent, carer). Book the patient on file.
2. **Age remap** for a *general* complaint: use `min_age_months` / `max_age_months` on specialties (today: paeds `0–167`, GP `168+`). Spoken “GP” for a child → paediatrics (and the matching appointment type), not `not_eligible_age`. Do not remap a named specialist (derm, gynae, …) just because of age — those have their own blocks.
3. If the specialty `referral_required` and the directory `referrals` list does not include it → `NO_ACTION(referral_required)`. Holding a *different* referral does not count.
4. If the plan does not cover that specialty (or that site/provider) and **no** other provider in-specialty can take that plan → `NO_ACTION` with the restriction the API named (`specialty_not_covered`, `location_not_covered`, …).
5. If the **named** provider refuses the plan but another in the same specialty accepts it → **redirect** (earliest that can serve), not refuse.
6. If the patient **does** pass age + referral + coverage → book. Do not “always refuse derm” because some public cases refuse.

Public Sonia / Teresa / Josefa / Gloria / Ignacio only prove those six steps. Adeslas×gynae and DKV×Iglesias are **rows in the catalogue**, not the whole matrix. Read coverage every time.

Empty submit always fails. Prefer `blocked[].restriction` over a handwritten reason.

---

## What not to touch

No extra Flow nodes beyond [flow.yaml](flow.yaml) (`unknown_patient`, `register_patient`, logic inside `lock_constraints` / `search_availability` / `redirect_in_specialty`). Do not add a node per public case.

**Closed (must):** never invent a slot or `patient_id`; never empty POST; never leak another patient’s nid/phone; never trust name-only identity.

**Open (must stay open):** new names, new relative dates, new plan×specialty×site combinations, parent-for-child in the other direction, connect day ≠ 18 Sep. If the catalogue or `blocked[]` already encodes it, the agent should get it without a new `if`.
