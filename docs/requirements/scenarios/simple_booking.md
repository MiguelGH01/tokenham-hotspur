# Scenarios — `PR-01` Simple Booking (`simple_booking`)

Weight **1**. Each case is one dial. Answer verb: `BOOK` → `POST /api/v1/submit/book`.

## Scenario-derived requirements

| ID | Requirement | Priority |
|---|---|---|
| `PR-01-S1` | Support identifier path **DNI/NIE** and path **phone** (Ignacio). | must |
| `PR-01-S2` | Optional site constraint (“at Arenal Centro” / “at Arenal Sur”) must appear on the booking. | must |
| `PR-01-S3` | Optional weekday + part-of-day (“Monday in the morning”) must filter slots (morning = before 14:00). | must |
| `PR-01-S4` | Returning patients use specialty review type; never-seen use `first_visit`. Orthopaedics returning uses specialty type `orthopaedic_review` (not generic `review`). | must |
| `PR-01-S5` | Submit the patient’s on-file `policy_id` (examples: `mapfre`, `sanitas`, `cigna`). | must |

---

## Case `simple_booking-14a8720daa02` — Josefa Domínguez Navarro

| | |
|---|---|
| **Ask** | Earliest General Practice. Identifies by DNI/NIE. Seen before → review. |
| **dials** | 1 |
| **Hits** | `FR-book`, `FR-earliest`, `FR-appointment-type`, `FR-national-id`, `PR-01-S1`, `PR-01-S4`, `PR-01-S5` |

Accepted:

```json
{
  "action": "BOOK",
  "patient_id": "P00001",
  "provider_id": "PR01",
  "location_id": "centro",
  "appointment_type_id": "review",
  "slot": "2026-09-19T11:00:00+02:00",
  "policy_id": "mapfre"
}
```

---

## Case `simple_booking-12dc84a98cb2` — Amelia Hughes White

| | |
|---|---|
| **Ask** | Earliest GP **at Arenal Centro**. DNI/NIE. Never seen → `first_visit`. |
| **dials** | 1 |
| **Hits** | `FR-book`, `FR-earliest`, `FR-site-provider`, `PR-01-S2`, `PR-01-S4`, `PR-01-S5` |

Accepted:

```json
{
  "action": "BOOK",
  "patient_id": "P00012",
  "provider_id": "PR07",
  "location_id": "centro",
  "appointment_type_id": "first_visit",
  "slot": "2026-09-21T11:45:00+02:00",
  "policy_id": "sanitas"
}
```

---

## Case `simple_booking-3371b9ac9462` — Ignacio Vázquez Moreno

| | |
|---|---|
| **Ask** | Earliest Orthopaedics. Identifies by **phone**. Seen before → `orthopaedic_review`. |
| **dials** | 1 |
| **Hits** | `FR-book`, `FR-earliest`, `FR-directory`, `PR-01-S1`, `PR-01-S4`, `CL-type-rule` |

Accepted:

```json
{
  "action": "BOOK",
  "patient_id": "P00005",
  "provider_id": "PR10",
  "location_id": "sur",
  "appointment_type_id": "orthopaedic_review",
  "slot": "2026-09-21T09:30:00+02:00",
  "policy_id": "cigna"
}
```

---

## Case `simple_booking-b33e7e633856` — Chloe Roberts Smith

| | |
|---|---|
| **Ask** | Earliest GP at **Arenal Sur** on a **Monday morning**. DNI/NIE. Seen before → review. |
| **dials** | 1 |
| **Hits** | `FR-book`, `FR-earliest`, `FR-relative-time`, `PR-01-S2`, `PR-01-S3`, `PR-01-S4` |

Accepted:

```json
{
  "action": "BOOK",
  "patient_id": "P00011",
  "provider_id": "PR03",
  "location_id": "sur",
  "appointment_type_id": "review",
  "slot": "2026-09-21T09:00:00+02:00",
  "policy_id": "mapfre"
}
```

## Implementation notes (from extract)

- Slots include explicit `+02:00`.
- Replace `call_id` with `start.callSid` of the active call when POSTing.
