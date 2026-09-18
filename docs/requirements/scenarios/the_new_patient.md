# Scenarios — `PR-04` The New Patient (`the_new_patient`)

Weight **2**. Each case is one dial. Answer verb: `REGISTER` → `POST /api/v1/submit/register`. **Do not `BOOK`.**

Dashboard “accepted answer” nests fields under `new_patient`. The POST body is **flat** (`call_id` + the same demographics). Scoring reads the submitted `REGISTER` action.

Connect-day note does not apply; there is no slot.

## Scenario-derived requirements

| ID | Requirement | Priority |
|---|---|---|
| `PR-04-S1` | Capture **both** surnames plus given name; accents may be spoken (`Joaquín`, `González`, `Muñoz`, `Martínez`, `Ramírez`) — submit the legal spelling; scorer folds `ñ` (`SC-norm-name`). | must |
| `PR-04-S2` | Capture the full national id **including check letter**. Public cases include both DNI (digit-first) and NIE (letter-first, e.g. `X0500252W`). | must |
| `PR-04-S3` | Email has no check digit. Public local-parts mix digits, dots, and underscores (`joaquingonzalez24@…`, `natalia.munoz86@…`, `sergio_martinez77@…`). Fold case/spaces only (`SC-norm-email`); do not “fix” the local-part. | must |
| `PR-04-S4` | Spoken plan names map to the catalogue id. **“Mapfre Salud” → `mapfre`**. Other public spoken brands: Cigna → `cigna`, AXA → `axa`, Sanitas → `sanitas`. | must |
| `PR-04-S5` | Directory **name search is not identity**. Homonyms already on file (same spoken name, different DOB/nid) must not be booked. Zero hits on `national_id` → `REGISTER` the caller, never `BOOK` the namesake (`LIVE-11`). | must |
| `PR-04-S6` | Registration-only: caller declines an appointment if offered. A `BOOK` beside `REGISTER` fails the case. | must |

---

## Case `the_new_patient-cba21da07f8a` — Joaquín González Ortega

| | |
|---|---|
| **Ask** | Register; not on file; books nothing. Reads a **DNI**; dictates email; insured with **Cigna**. |
| **dials** | 1 |
| **Hits** | `FR-register`, `FR-national-id`, `PR-04-S1`…`S4`, `PR-04-S6`, `SC-norm-*` |

`POST /api/v1/submit/register`:

```json
{
  "call_id": "<start.callSid>",
  "given_name": "Joaquín",
  "first_surname": "González",
  "second_surname": "Ortega",
  "national_id": "18921027P",
  "date_of_birth": "1970-06-25",
  "phone": "783869132",
  "email": "joaquingonzalez24@hotmail.com",
  "insurer": "cigna"
}
```

Live: `national_id=18921027P` → 0 directory matches.

---

## Case `the_new_patient-3a76e6fabfba` — Elizabeth Jones Evans

| | |
|---|---|
| **Ask** | Register; not on file; books nothing. Reads a **NIE**; dictates email; insured with **AXA**. |
| **dials** | 1 |
| **Hits** | `FR-register`, `FR-national-id`, `PR-04-S2`, `PR-04-S3`, `PR-04-S6` |

```json
{
  "call_id": "<start.callSid>",
  "given_name": "Elizabeth",
  "first_surname": "Jones",
  "second_surname": "Evans",
  "national_id": "X0500252W",
  "date_of_birth": "2004-09-13",
  "phone": "756960522",
  "email": "elizabethjones12@icloud.com",
  "insurer": "axa"
}
```

---

## Case `the_new_patient-dbb48077d9df` — Natalia Muñoz González

| | |
|---|---|
| **Ask** | Register; not on file; books nothing. Reads a **DNI**; dictates email; insured with **Sanitas**. |
| **dials** | 1 |
| **Hits** | `FR-register`, `PR-04-S3`, `PR-04-S5`, `LIVE-11`, `CL-exact-exclude` |

```json
{
  "call_id": "<start.callSid>",
  "given_name": "Natalia",
  "first_surname": "Muñoz",
  "second_surname": "González",
  "national_id": "50454876Y",
  "date_of_birth": "1988-12-13",
  "phone": "797574941",
  "email": "natalia.munoz86@hotmail.com",
  "insurer": "sanitas"
}
```

Live trap: name-only search returns **P01239** Natalia Muñoz González, DOB `2005-11-21` (already a patient). New caller DOB `1988-12-13` + nid `50454876Y` → 0 matches. Booking P01239 fails.

---

## Case `the_new_patient-ec8a02146ecd` — Sergio Martínez Ramírez

| | |
|---|---|
| **Ask** | Register; not on file; books nothing. Reads a **DNI**; dictates email; insured with **Mapfre Salud**. |
| **dials** | 1 |
| **Hits** | `FR-register`, `PR-04-S4`, `PR-04-S5`, `LIVE-11` |

```json
{
  "call_id": "<start.callSid>",
  "given_name": "Sergio",
  "first_surname": "Martínez",
  "second_surname": "Ramírez",
  "national_id": "31426012P",
  "date_of_birth": "2005-08-10",
  "phone": "792919982",
  "email": "sergio_martinez77@gmail.com",
  "insurer": "mapfre"
}
```

Live trap: name-only search returns **P01988** Sergio Martínez Ramírez, DOB `1969-06-01`. New caller is 16 (`2005-08-10`). Same rule as Natalia — nid miss → register.
