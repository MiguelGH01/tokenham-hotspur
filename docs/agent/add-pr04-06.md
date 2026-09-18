# Add to the agent: PR-04 / PR-05 / PR-06

Handoff for another session. **Do not start a new agent** and **do not rebuild Simple Booking**. Same conversation loop already in the bot: identify → constraints → availability → confirm/submit. Code owns ids, dates, types, `reason`, and every POST. The LLM talks.

**These public cases are fixtures, not the product.** Leaderboard practice. Run All and the jury pass will use **unseen** callers and wordings. Implement **general rules** from the clinic bundle + `/availability` + connect clock. Names, `P00…`, and timestamps below are **regression checks**, not `if` branches.

Walkthroughs: [the_new_patient.md](../requirements/scenarios/the_new_patient.md), [when_exactly.md](../requirements/scenarios/when_exactly.md), [the_rules.md](../requirements/scenarios/the_rules.md).

---

## 1. Register (`PR-04`)

`lookup` miss on **nid/DOB** (not name) → register-only → flat `POST /submit/register`. No `BOOK`.

- Name hit + nid miss = new person, never the namesake.
- DNI **or** NIE + check letter. Both surnames. Email local-part literal.
- Spoken plan labels → catalogue **id** (e.g. Mapfre Salud → `mapfre` is one row of that map).

---

## 2. When (`PR-05`)

One date engine: parse the ask → target day/window → site hours → closures → **next open day** still matching the rest. Clock = **this call’s** connect time, Europe/Madrid. No same-day. Type still from `/availability`.

Public phrases (`tomorrow`, `this coming Thursday`, Saturday morning, Sunday, named 12 Oct) are the leaderboard vocabulary, not the parser’s only input. Do not book a closed-day slot even if the API lists it.

---

## 3. Rules (`PR-06`) — in code, from **this** chart + catalogue

1. Patient ≠ caller when the visit is for someone else.
2. Age remap for a *general* complaint: specialty `min_age_months` / `max_age_months` (GP ↔ paeds). Do not `not_eligible_age` when the other side can serve. Named specialists keep their own blocks.
3. Specialty `referral_required` and chart `referrals` missing it → `NO_ACTION(referral_required)`.
4. Plan does not cover and nobody in-specialty can take it → `NO_ACTION` with the API `blocked` reason (`specialty_not_covered`, …).
5. Named doctor refuses the plan but another in-specialty accepts → **redirect**, not refuse.
6. If they pass, **book**. Do not learn “always refuse derm”.

Empty submit always fails.

---

## What not to do

Do not add a node per public case. Do not paste ISO slots. Do not hard-code Sonia / Vilar / 19 Sep. If the catalogue already encodes it, an unseen private case should not need a new `if`.
