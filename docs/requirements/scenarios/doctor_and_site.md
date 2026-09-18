# Scenarios — `PR-03` Doctor and the Site (`doctor_and_site`)

Weight **2**. Named provider ± named site, including near-miss surnames, leave, wrong weekday at site, and nonexistent provider.

Public JSON is a **leaderboard fixture**. Implement leave / hours / missing-name from the **provider catalogue**. Requena, Sáez-on-Monday-Centro, and Fuentes are tests of those functions, not `if` targets.

## Scenario-derived requirements

| ID | Requirement | Priority |
|---|---|---|
| `PR-03-S1` | When the named doctor consults at the named site, book **that** provider’s earliest slot there. | must |
| `PR-03-S2` | Disambiguate near-miss surnames (e.g. Dr. **Sáez** GP vs Dra. **Sáenz** paediatrics) and book the specialty the caller meant. | must |
| `PR-03-S3` | If the named doctor is **on leave**, book the earliest doctor of the **same specialty at the same site** (do not keep the leave provider). | must |
| `PR-03-S4` | If the named doctor is not at that site on the requested day, keep provider + site and take their **earliest slot there on any day** (do not switch doctor). | must |
| `PR-03-S5` | If the named doctor does not exist and the caller will see nobody else → `NO_ACTION` with `reason=provider_not_found` (book nothing). | must |
| `PR-03-S6` | `privado` is a valid `policy_id` when that is the patient’s plan. | must |

---

## Case `doctor_and_site-2fe62ca2872f` — Joaquín Ramírez Delgado

| | |
|---|---|
| **Ask** | Dra. Ortiz Vidal at Arenal Centro (she consults there) → earliest there. DNI; review. |
| **dials** | 1 |
| **Hits** | `PR-03-S1`, `FR-site-provider`, `FR-book`, `PR-03-S6` |

Accepted:

```json
{
  "action": "BOOK",
  "patient_id": "P01842",
  "provider_id": "PR01",
  "location_id": "centro",
  "appointment_type_id": "review",
  "slot": "2026-09-19T11:00:00+02:00",
  "policy_id": "privado"
}
```

---

## Case `doctor_and_site-057078bb5b44` — Emilio Rubio Jiménez

| | |
|---|---|
| **Ask** | Dr. **Sáez** the GP — not Dra. Sáenz the paediatrician. DNI; review. |
| **dials** | 1 |
| **Hits** | `PR-03-S2`, `CL-name-collision`, `FR-book` |

Accepted:

```json
{
  "action": "BOOK",
  "patient_id": "P01850",
  "provider_id": "PR03",
  "location_id": "sur",
  "appointment_type_id": "review",
  "slot": "2026-09-21T09:00:00+02:00",
  "policy_id": "mapfre"
}
```

---

## Case `doctor_and_site-d5b4f04886c5` — Andrés Rubio Vázquez

| | |
|---|---|
| **Ask** | Dr. Requena at Arenal Norte — on leave → earliest **same kind** at **same site**. DNI; review. |
| **dials** | 1 |
| **Hits** | `PR-03-S3`, `CL-requena-leave`, `FR-site-provider` |

Accepted (fallback provider `PR07` at `norte`):

```json
{
  "action": "BOOK",
  "patient_id": "P01843",
  "provider_id": "PR07",
  "location_id": "norte",
  "appointment_type_id": "review",
  "slot": "2026-09-22T09:00:00+02:00",
  "policy_id": "dkv"
}
```

---

## Case `doctor_and_site-9904a6d96cdd` — Mario Gómez Blanco

| | |
|---|---|
| **Ask** | Dr. Sáez at Arenal Centro on a Monday — not there that day → earliest slot **with Sáez at Centro on any day**. Never seen → `first_visit`. |
| **dials** | 1 |
| **Hits** | `PR-03-S4`, `FR-site-provider`, `FR-appointment-type` |

Accepted:

```json
{
  "action": "BOOK",
  "patient_id": "P01847",
  "provider_id": "PR03",
  "location_id": "centro",
  "appointment_type_id": "first_visit",
  "slot": "2026-09-25T09:30:00+02:00",
  "policy_id": "asisa"
}
```

---

## Case `doctor_and_site-570e40a3f718` — Vicente Álvarez Castro

| | |
|---|---|
| **Ask** | Dr. Fuentes, orthopaedic surgeon — **no such doctor**; caller will see nobody else. DNI; would have been review. |
| **dials** | 1 |
| **Hits** | `PR-03-S5`, `FR-no-action`, `CR-no-action` |

Accepted:

```json
{
  "action": "NO_ACTION",
  "reason": "provider_not_found"
}
```

Request: `POST /api/v1/submit/no-action` with `call_id` + `reason`.

## Provider schedules (verified live 18 Sep 2026)

Critical for `PR-03-S4` (Mario — Sáez at Centro on Monday):

| Provider | Site | Days |
|---|---|---|
| PR03 Sáez | **centro** | **Friday only** 09:00–14:00 |
| PR03 Sáez | **sur** | Mon–Thu 09:00–13:00 |
| PR02 Requena | norte | Mon–Fri 09:00–14:00 but **on leave through 30 Sep** |
| PR07 Benítez | norte | Tue, Thu 09:00–14:00 (leave fallback) |
| PR01 Ortiz | centro | Mon–Fri + **Saturday** 09:00–13:00 |

Full trap sheet: [10-live-probe-findings.md](../10-live-probe-findings.md).

## Implementation notes

- Do not book anything on the Fuentes case; silence fails, wrong booking fails.
- Leave fallback keeps **site**; wrong-weekday keeps **provider + site**.
- Sáez at Centro on a Monday → roll to his next Centro day (Friday), not to Sur.
