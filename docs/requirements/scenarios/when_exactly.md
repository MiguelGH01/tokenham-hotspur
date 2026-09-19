# Scenarios — `PR-05` When Exactly (`when_exactly`)

Weight **2**. Each case is one dial. Answer verb: `BOOK` → `POST /api/v1/submit/book`.

The extract’s confirmation dates assume the call connects on **Friday 18 September 2026**. Relative phrases resolve against **connect time** (`CL-clock`), not this file. If organisers re-anchor public “earliest” slots, keep the **calendar rule** (closed day → next open day that still matches the rest) and re-read `/availability`.

## Scenario-derived requirements

| ID | Requirement | Priority |
|---|---|---|
| `PR-05-S1` | **`tomorrow`** = next calendar day in Europe/Madrid. That day **may be Saturday**. Do not skip the weekend. Public: Fri 18 → Sat 19 Centro GP. | must |
| `PR-05-S2` | **`this coming <weekday>`** = first that weekday **strictly after** the day of the call (not “this week if still ahead”). Public: Friday → “this coming Thursday” = **24 Sep**, not 17. | must |
| `PR-05-S3` | **`on Saturday morning`** (and Saturday-only hours): only **Centro** is open (`CL-saturday`). Morning = before 14:00. | must |
| `PR-05-S4` | Requested day **closed** (Sunday, or a named Fiesta date) → book earliest on the **next open day** that still matches site / specialty / part-of-day. Do not `NO_ACTION` and do not book the closed day even if `/availability` lists slots (`LIVE-01`). | must |
| `PR-05-S5` | Named **Monday 12 October 2026** (“first thing on Monday the twelfth of October”) is Fiesta Nacional. Roll to Tuesday 13 Oct, still “first thing” (earliest morning slot). | must |
| `PR-05-S6` | Appointment type still from the record (`CL-type-rule`), not from the date phrase. Public: Amelia never-seen → `first_visit` on the rolled Tuesday; returning GP → `review`; returning ortho → `orthopaedic_review`. | must |

---

## Case `when_exactly-7d2467212026` — Josefa Domínguez Navarro

| | |
|---|---|
| **Ask** | GP **tomorrow** (hay fever). Confirm date = Saturday 19 Sep 2026. Identified patient **P00001**, Mapfre, returning → `review`. |
| **dials** | 1 |
| **Hits** | `FR-relative-time`, `PR-05-S1`, `CL-saturday`, `PR-01-S4` |

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

## Case `when_exactly-72cdb35b9682` — Ignacio Vázquez Moreno

| | |
|---|---|
| **Ask** | Orthopaedics **this coming Thursday** (hip). Confirm date = Thursday 24 Sep 2026. **P00005**, Cigna, returning → `orthopaedic_review`. |
| **dials** | 1 |
| **Hits** | `FR-relative-time`, `PR-05-S2`, `CL-type-rule` |

```json
{
  "action": "BOOK",
  "patient_id": "P00005",
  "provider_id": "PR06",
  "location_id": "centro",
  "appointment_type_id": "orthopaedic_review",
  "slot": "2026-09-24T09:15:00+02:00",
  "policy_id": "cigna"
}
```

`PR06` is Dr. Emilio **Iglesia** (orthopaedics) — not Dra. Iglesias (dermatology).

---

## Case `when_exactly-0ae0c03a3e6d` — Ignacio Vázquez Moreno

| | |
|---|---|
| **Ask** | GP **on Saturday morning** (cough). Confirm date = Saturday 19 Sep 2026. Same patient as above; **not** his usual ortho. Centro only. |
| **dials** | 1 |
| **Hits** | `FR-caller-intent-wins`, `PR-05-S3`, `CL-saturday` |

```json
{
  "action": "BOOK",
  "patient_id": "P00005",
  "provider_id": "PR01",
  "location_id": "centro",
  "appointment_type_id": "review",
  "slot": "2026-09-19T11:00:00+02:00",
  "policy_id": "cigna"
}
```

---

## Case `when_exactly-b1f6c257c34c` — Chloe Roberts Smith

| | |
|---|---|
| **Ask** | GP at **Arenal Centro this coming Sunday** (repeat prescription). Confirm date = Sunday 20 Sep 2026. **Nothing opens Sunday.** Next open day matching Centro + GP + morning = **Monday 21 Sep**. **P00011**, Mapfre, returning → `review`. |
| **dials** | 1 |
| **Hits** | `PR-05-S4`, `CL-saturday`, `LIVE-13` |

```json
{
  "action": "BOOK",
  "patient_id": "P00011",
  "provider_id": "PR01",
  "location_id": "centro",
  "appointment_type_id": "review",
  "slot": "2026-09-21T09:15:00+02:00",
  "policy_id": "mapfre"
}
```

Do not book Sunday. Do not jump to another site to “keep Sunday”.

---

## Case `when_exactly-34e8e0acadb6` — Amelia Hughes White

| | |
|---|---|
| **Ask** | GP at Arenal Centro **first thing on Monday the twelfth of October** (blood pressure). Confirm date = Monday 12 Oct 2026. Network shut (`CL-fiesta`, `LIVE-01`). Roll to **Tue 13 Oct**, first morning slot. **P00012** never seen → `first_visit`. |
| **dials** | 1 |
| **Hits** | `PR-05-S4`, `PR-05-S5`, `PR-05-S6`, `LIVE-01`, `LIVE-14` |

```json
{
  "action": "BOOK",
  "patient_id": "P00012",
  "provider_id": "PR01",
  "location_id": "centro",
  "appointment_type_id": "first_visit",
  "slot": "2026-10-13T09:45:00+02:00",
  "policy_id": "sanitas"
}
```

Contrast: unconstrained “earliest Centro GP” for Amelia in `PR-01` was Benítez (`PR07`) on a nearer weekday. The named-date + first-thing constraint picks Ortiz (`PR01`) after the closure.
