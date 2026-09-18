# Call contract and submission requirements

How a call reaches the agent and what must be POSTed when it ends. Official contract is frozen for the event: only additive changes (new optional fields, new `reason` values), never breaking ones.

## Wire protocol

| ID | Requirement | Priority |
|---|---|---|
| `CR-endpoint` | Expose one WebSocket URL configured on the dashboard (`wss://…` or `ws://…`, scheme and path included). | must |
| `CR-twilio-shape` | Speak **Twilio Media Streams** wire format. No Twilio account or phone number required on our side. | must |
| `CR-message-order` | Expect, in order: `connected` → `start` → `media` frames → `stop`, then socket close. | must |
| `CR-call-id` | Use `start.callSid` as `call_id` for submissions. `start.customParameters.call_id` repeats the same id. Do not mint a local id. | must |
| `CR-from-number` | Read optional `start.customParameters.from_number` (E.164). Absent when caller id withheld. Hint only — see `FR-from-number`. | must |
| `CR-media-format` | Inbound `media`: 20 ms frames of **8 kHz µ-law** audio, base64-encoded, real time. | must |
| `CR-camelcase` | Twilio-shaped messages use camelCase; `sequenceNumber`, `chunk`, and `timestamp` are **strings** on the wire, not numbers. | must |
| `CR-outbound-media` | Reply with agent `media` on the same socket. May send `mark` / `clear`; harness implements no server-side barge-in and `clear` has no effect on their side today. Turn-taking is ours (`FR-turn-taking`). | must |
| `CR-concurrency` | One URL, many calls. Run All opens **ten** concurrent sockets, each with its own `callSid`. Problem 2 bursts open up to **twenty**. Isolate all state per socket. | must |
| `CR-dropped` | A refused or dropped connection fails that case; other calls in the wave continue and score normally. | must |

## Submission window

| ID | Requirement | Priority |
|---|---|---|
| `CR-window` | Window opens when the call opens and closes **30 seconds after** the harness socket to us closes. Early submit (while call still open) is allowed; late submit is not. | must |
| `CR-status-404` | Unknown / other team’s `call_id` → `404`. | must |
| `CR-status-200` | Call still open, or closed ≤ 30 s ago → `200` accepted. | must |
| `CR-status-410` | Call closed > 30 s ago → `410` window closed (checked before anything else). | must |
| `CR-status-409` | Identical action already accepted for this call → `409` (retry; expected). | must |
| `CR-status-422` | Malformed body → `422`; nothing recorded. Invalid `national_id` check letter on register → `422`. | must |
| `CR-record` | A call’s record is **every action accepted inside its window**. Multi-intent calls POST once per action. | must |
| `CR-ack-not-pass` | `200` acknowledges receipt, not a pass. | must |
| `CR-silence-fails` | Submitting nothing always fails scoring. | must |

## Submit routes

Auth: desk-issued key in `X-Api-Key`. Attribution comes from the registered call session, never a team id in the body. JSON is **snake_case**.

| ID | Route | Body besides `call_id` | Notes |
|---|---|---|---|
| `CR-register` | `POST /api/v1/submit/register` | `given_name`, `first_surname`, `second_surname`, `national_id`, `date_of_birth`, `phone`, `email`, `insurer` | Unknown patient; demographics are the answer. Fields scored after normalization. |
| `CR-book` | `POST /api/v1/submit/book` | `patient_id`, `provider_id`, `location_id`, `appointment_type_id`, `slot`, `policy_id` | `patient_id` from directory only. |
| `CR-reschedule` | `POST /api/v1/submit/reschedule` | `appointment_id`, `provider_id`, `location_id`, `slot`, `policy_id` | `appointment_id` from appointments lookup only. |
| `CR-cancel` | `POST /api/v1/submit/cancel` | `appointment_id` | |
| `CR-no-action` | `POST /api/v1/submit/no-action` | `reason` | Closed vocabulary. |
| `CR-escalate` | `POST /api/v1/submit/escalate` | `reason` | Closed vocabulary (incl. `medical_emergency`). |

| ID | Requirement | Priority |
|---|---|---|
| `CR-slot-tz` | `slot` must carry an explicit timezone offset; converts to Europe/Madrid and must match to the **exact minute**. | must |
| `CR-exact-ids` | Ids (`PR05`, location ids, etc.) compared **exactly** — no normalization. | must |
| `CR-policy-required` | `policy_id` is part of the answer when booking/rescheduling (patient may have two plans). | must |
| `CR-response-shape` | Success `200`: `{ call_id, received_at, record: { actions: [...] } }` with verbs `REGISTER`, `BOOK`, `RESCHEDULE`, `CANCEL`, `NO_ACTION`, `ESCALATE`; `REGISTER` nests fields under `new_patient`. | must |

## Closed `reason` vocabulary

### Clinic restriction mirrors (must cover every standing restriction)

`not_eligible_age` · `referral_required` · `provider_not_in_network` · `specialty_not_covered` · `location_not_covered` · `insurer_referral_required` · `allowance_exhausted` · `provider_on_leave` · `location_hours` · `type_not_offered` · `patient_history`

### Non-rule endings

`no_availability` · `clinic_closed` · `patient_not_found` · `provider_not_found` · `caller_not_authorised` · `out_of_scope` · `medical_emergency`

| ID | Requirement | Priority |
|---|---|---|
| `CR-reason-closed` | Use only the closed vocabulary above for `NO_ACTION` / `ESCALATE` reasons. | must |

## Gotchas checklist

| ID | Requirement | Priority |
|---|---|---|
| `CR-case-split` | `/submit/*` JSON snake_case; Twilio handshake camelCase. | must |
| `CR-one-action` | Each HTTP request is one action; multi-action calls post multiple times. | must |

## Related docs

- Functional behaviours → [02-functional](02-functional.md)
- Clinic lookups feeding these writes → [04-clinic-domain](04-clinic-domain.md)
- Pass/fail and normalization → [05-scoring](05-scoring.md)
- Field-level HTTP schemas → [../api/README.md](../api/README.md)
