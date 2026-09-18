# Scenarios — `PR-06` The Rules (`the_rules`)

Weight **3**. Each case is one dial. Answer is either a redirected `BOOK` or `NO_ACTION` with the **rule that bit**. Caller does not name the rule.

Public Sonia / Teresa / Josefa / Gloria / Ignacio are **fixtures**. Implement age / referral / coverage / in-network redirect from the catalogue. Do not switch on those names.

## Scenario-derived requirements

| ID | Requirement | Priority |
|---|---|---|
| `PR-06-S1` | Age boundary **wins over the spoken specialty** for a general complaint. Catalogue: paediatrics `0–167` months, GP `168+` months (14th birthday). A parent asking for “GP” for a child → book **paediatrics** for the **child**, not GP, and not `NO_ACTION(not_eligible_age)`. | must |
| `PR-06-S2` | Parent/carer language (“your daughter’s check-up”) still books the **patient** on the chart (`FR-third-party`). Public: Sonia Álvarez Medina **P00009** (DOB 2017-05-12, age 9, `privado`, seen in paeds) → `paediatric_review` with Dr. Ocaña `PR08` at Norte. | must |
| `PR-06-S3` | Dermatology requires a held referral (`referral_required: true`). Adult **without** a derm referral who asks for earliest derm → `NO_ACTION(referral_required)`, even if the plan covers derm. Public: Teresa López García **P00004** (ASISA, refs = physio only). | must |
| `PR-06-S4` | Plan × specialty dead-end → `NO_ACTION(specialty_not_covered)`, no redirect. Public: Josefa Sánchez Gutiérrez **P00015** (Adeslas, never seen) asking gynae (`CL-adeslas-gynae`). Do not invent `first_visit` at another plan. | must |
| `PR-06-S5` | Named provider × plan mismatch is a **redirect**, not a refusal, when another provider in that specialty takes the plan. Public: Gloria González Blanco **P00057** (DKV, **has** derm referral) asks for Dra. Iglesias → book Vilar `PR12` Norte `dermatology_review` (`CL-dkv-iglesias`, `LIVE-06`). | must |
| `PR-06-S6` | **Control:** adult **with** the required referral books normally. Do not learn “always refuse derm”. Public: Ignacio **P00005** (Cigna, derm+ortho+physio refs) earliest derm → Vilar `PR12`. | must |

---

## Case `the_rules-8d92ce10f4a6` — Sonia Álvarez Medina

| | |
|---|---|
| **Ask** | Earliest **General Practice** because **your daughter’s** routine check-up is due. |
| **dials** | 1 |
| **Hits** | `CL-age-boundary`, `FR-third-party`, `PR-06-S1`, `PR-06-S2`, `LIVE-12` |

```json
{
  "action": "BOOK",
  "patient_id": "P00009",
  "provider_id": "PR08",
  "location_id": "norte",
  "appointment_type_id": "paediatric_review",
  "slot": "2026-09-21T10:00:00+02:00",
  "policy_id": "privado"
}
```

Live: P00009 DOB `2017-05-12`, `has_visited_before=true`, insurer `privado`, referrals `['orthopaedics']`, note: seen in paediatrics and orthopaedics. Booking GP would hit `not_eligible_age`. `privado` is the plan on the record — not a fallback invented at the desk.

---

## Case `the_rules-d760519fdfcd` — Teresa López García

| | |
|---|---|
| **Ask** | Earliest **Dermatology** (mole looks different). |
| **dials** | 1 |
| **Hits** | `FR-blocked`, `FR-no-action`, `PR-06-S3`, `CL-referrals` |

```json
{
  "action": "NO_ACTION",
  "reason": "referral_required"
}
```

Live: P00004 DOB `1996-08-14`, ASISA, referrals `['physiotherapy']` only. Derm is referral-gated for every plan; missing referral beats “plan covers derm”.

---

## Case `the_rules-460d9e84504a` — Josefa Sánchez Gutiérrez

| | |
|---|---|
| **Ask** | Earliest **Gynaecology** (yearly check-up). |
| **dials** | 1 |
| **Hits** | `FR-no-action`, `PR-06-S4`, `CL-adeslas-gynae` |

```json
{
  "action": "NO_ACTION",
  "reason": "specialty_not_covered"
}
```

Live: P00015 DOB `1967-03-20`, **Adeslas**, `has_visited_before=false`, no referrals. Catalogue: gynaecology `not_covered_by` includes Adeslas; one gynaecologist → nowhere to redirect. Wrong reasons: `referral_required` (gynae is not referral-gated), `not_eligible_age` (adult).

---

## Case `the_rules-fe23fca4aeb7` — Gloria González Blanco

| | |
|---|---|
| **Ask** | Appointment with **Dra. Iglesias** (eczema flare). |
| **dials** | 1 |
| **Hits** | `PR-06-S5`, `CL-dkv-iglesias`, `CL-name-collision`, `LIVE-06` |

```json
{
  "action": "BOOK",
  "patient_id": "P00057",
  "provider_id": "PR12",
  "location_id": "norte",
  "appointment_type_id": "dermatology_review",
  "slot": "2026-09-21T10:15:00+02:00",
  "policy_id": "dkv"
}
```

Live: P00057 DKV, referrals include `dermatology`. Iglesias refuses DKV → Vilar. Do not submit `provider_not_in_network`.

---

## Case `the_rules-04b2b5c51b40` — Ignacio Vázquez Moreno

| | |
|---|---|
| **Ask** | Earliest **Dermatology** (rash keeps coming back). Control case. |
| **dials** | 1 |
| **Hits** | `PR-06-S6`, `CL-referrals` |

```json
{
  "action": "BOOK",
  "patient_id": "P00005",
  "provider_id": "PR12",
  "location_id": "norte",
  "appointment_type_id": "dermatology_review",
  "slot": "2026-09-21T10:15:00+02:00",
  "policy_id": "cigna"
}
```

Live: P00005 Cigna, referrals `['dermatology', 'orthopaedics', 'physiotherapy']`. Same slot/provider as Gloria’s redirect — independent calls, read-only EHR.
