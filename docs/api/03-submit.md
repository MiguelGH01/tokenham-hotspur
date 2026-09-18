# Submit API

Report the write the agent **would** have made. Window and status semantics → [requirements/03-call-and-submission.md](../requirements/03-call-and-submission.md) and [01-conventions.md](01-conventions.md).

All submit routes: `POST`, JSON body, `X-Api-Key`, success → `SubmitResponse`.

## Shared success response

**`SubmitResponse`**

| Field | Type | Notes |
|---|---|---|
| `call_id` | string | Call this record belongs to |
| `received_at` | date-time | When this action was accepted (**UTC**) |
| `record` | `SubmittedOutcome` | Every action accepted so far for this call |

**`SubmittedOutcome`:** `{ "actions": [ ... ] }` with `minItems: 1`. Each action is discriminated on `action`:

| `action` | Schema | Payload |
|---|---|---|
| `REGISTER` | `RegisterAction` | `new_patient: NewPatient` |
| `BOOK` | `BookAction` | `patient_id`, `provider_id`, `location_id`, `appointment_type_id`, `slot`, `policy_id` |
| `RESCHEDULE` | `RescheduleAction` | `appointment_id`, `provider_id`, `location_id`, `slot`, `policy_id` |
| `CANCEL` | `CancelAction` | `appointment_id` |
| `NO_ACTION` | `NoAction` | `reason: OutcomeReason` |
| `ESCALATE` | `EscalateAction` | `reason: OutcomeReason` |

---

## `POST /api/v1/submit/register`

Caller not on file; demographics are the answer. Nothing booked.

**`RegisterRequest`** (all required)

| Field | Type | Notes / example |
|---|---|---|
| `call_id` | string | = `start.callSid` — never mint |
| `given_name` | string | e.g. `Ana` |
| `first_surname` | string | e.g. `García` |
| `second_surname` | string | e.g. `López` |
| `national_id` | string | Check letter re-derived; mismatch → `422`. e.g. `12345678Z` |
| `date_of_birth` | date | e.g. `1988-03-14` |
| `phone` | string | Normalized on score. e.g. `+34612345678` |
| `email` | string | Normalized on score; no check digit. e.g. `ana.garcia@gmail.com` |
| `insurer` | `Insurer` | Plan registered under. e.g. `adeslas` |

Readback nests the same fields under `new_patient` on a `REGISTER` action.

---

## `POST /api/v1/submit/book`

Slot for a patient already on file.

**`BookRequest`** (all required)

| Field | Type | Notes / example |
|---|---|---|
| `call_id` | string | |
| `patient_id` | string | From directory — never from speech. e.g. `P00042` |
| `provider_id` | string | e.g. `PR05` |
| `location_id` | string | From availability. e.g. `sur` |
| `appointment_type_id` | string | From availability. e.g. `review` |
| `slot` | date-time | Explicit offset; Europe/Madrid exact minute. e.g. `2026-09-24T16:30:00+02:00` |
| `policy_id` | `Insurer` | Plan billed; patient may hold two. e.g. `sanitas` |

---

## `POST /api/v1/submit/reschedule`

Existing appointment moved.

**`RescheduleRequest`** (all required)

| Field | Type | Notes |
|---|---|---|
| `call_id` | string | |
| `appointment_id` | string | From appointments lookup only. e.g. `A000123` |
| `provider_id` | string | |
| `location_id` | string | |
| `slot` | date-time | Explicit offset |
| `policy_id` | `Insurer` | |

Note: reschedule body has **no** `appointment_type_id` (unlike book).

---

## `POST /api/v1/submit/cancel`

One existing appointment cancelled. Two cancellations = two requests.

**`CancelRequest`:** `call_id`, `appointment_id` (both required).

---

## `POST /api/v1/submit/no-action`

Call ended with no write; reason is the answer.

**`NoActionRequest`:** `call_id`, `reason` (`OutcomeReason`) — both required.

---

## `POST /api/v1/submit/escalate`

Call handed to a human; reason is the answer.

**`EscalateRequest`:** `call_id`, `reason` (`OutcomeReason`) — both required.  
Typical medical case: `medical_emergency`.

---

## `GET /api/v1/submissions` — List Submissions

Own team’s recent records (health check / tooling).

| Query | Required | Type | Description |
|---|---|---|---|
| `limit` | no | integer | How many most recent records |

**Response `RecordsResponse`:** `{ "submissions": RecordResponse[] }`

**`RecordResponse`:** `call_id`, `record` (`SubmittedOutcome`), `received_at` (date-time)

---

## Agent checklist

| ID | Requirement |
|---|---|
| `API-submit-call-id` | Always use harness `start.callSid`. |
| `API-submit-one` | One HTTP request = one action; multi-intent calls POST multiple times. |
| `API-submit-policy` | `policy_id` / `insurer` values must be from the `Insurer` enum. |
| `API-submit-reason` | `reason` values must be from `OutcomeReason` (see [04-enums.md](04-enums.md)). |
