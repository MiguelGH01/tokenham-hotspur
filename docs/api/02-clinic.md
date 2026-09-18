# Clinic API (read-only)

All routes below require `X-Api-Key` except where noted. Nothing here mutates the clinic; booking is reported via [03-submit.md](03-submit.md).

Domain rules (exact-field exclusion, no same-day accept, etc.) → [requirements/04-clinic-domain.md](../requirements/04-clinic-domain.md).

---

## `GET /api/v1/clinic` — Clinic Overview

Full catalogue in one call: calendar window, standing restrictions, providers, specialties, appointment types, locations, plans.

**Response `ClinicResponse`**

| Field | Type | Notes |
|---|---|---|
| `clinic_name` | string | |
| `patient_count` | integer | Patients are **not** embedded; use directory. |
| `calendar` | `ClinicCalendarResponse` | Bookable window as availability accepts it |
| `restrictions` | `ClinicRestrictionResponse[]` | Standing rules + decline reasons |
| `providers` | `ClinicProviderResponse[]` | |
| `specialties` | `ClinicSpecialtyResponse[]` | |
| `appointment_types` | `ClinicAppointmentTypeResponse[]` | |
| `locations` | `ClinicLocationResponse[]` | |
| `plans` | `ClinicPlanResponse[]` | |

### Nested shapes

**`ClinicCalendarResponse`**

| Field | Type |
|---|---|
| `starts` / `ends` | date |
| `max_span_days` | integer |
| `slot_minutes` | integer |
| `closure_days` | date[] |
| `appointment_count` | integer |

**`ClinicRestrictionResponse`:** `id`, `title`, `explanation`

**`ClinicProviderResponse`:** `id`, `name`, `specialty_id`, `specialty_name`, `languages[]`, `appointment_type_names[]`, `location_names[]`, `schedules[]` (`ClinicScheduleResponse`: `location_id`, `location_name`, `days[]`), `accepted_insurers[]` / `refused_insurers[]` (`ClinicInsurerRef`: `id`, `name`), `leave` (`ClinicLeaveResponse` \| null: `start`, `end`, `reason`)

**`ClinicSpecialtyResponse`:** `id`, `name`, `min_age_months`, `max_age_months` (nullable), `referral_required`, `provider_names[]`, `covered_by[]` / `not_covered_by[]`

**`ClinicAppointmentTypeResponse`:** `id`, `name`, `duration_minutes`, `new_patient_requirement`, `guidance`, `provider_names[]`, `specialty_id` \| null, `specialty_name` \| null

**`ClinicLocationResponse`:** `id`, `name`, `address`, `latitude`, `longitude`, `hours[]` (`ClinicDayResponse`: `weekday`, `intervals[]` of strings), `provider_names[]`, `covered_by[]` / `not_covered_by[]`

**`ClinicPlanResponse`:** `id`, `name`, `covered_specialty_names[]`, `uncovered_specialty_names[]`, `covered_location_names[]`, `uncovered_location_names[]`, `accepted_by[]`, `refused_by[]`, `holders`

**`ClinicDayResponse.intervals`:** string intervals describing open hours for that weekday.

| ID | Requirement |
|---|---|
| `API-clinic-cache` | Static catalogue never changes during the event — safe to cache from `/clinic` or the split list endpoints. |

---

## Catalogue list endpoints

Same nested types as `/clinic`, returned under a single array key:

| Method | Path | Response wrapper |
|---|---|---|
| `GET` | `/api/v1/providers` | `{ "providers": ClinicProviderResponse[] }` |
| `GET` | `/api/v1/locations` | `{ "locations": ClinicLocationResponse[] }` |
| `GET` | `/api/v1/specialties` | `{ "specialties": ClinicSpecialtyResponse[] }` |
| `GET` | `/api/v1/appointment-types` | `{ "appointment_types": ClinicAppointmentTypeResponse[] }` |
| `GET` | `/api/v1/insurance-plans` | `{ "plans": ClinicPlanResponse[] }` |

No query parameters.

---

## `GET /api/v1/directory` — Search Patient

| Query | Required | Type | Description |
|---|---|---|---|
| `name` | no | string \| null | Full or partial name, compared after normalization |
| `national_id` | no | string \| null | DNI or NIE, as dictated |
| `phone` | no | string \| null | As dictated; digits compared |
| `date_of_birth` | no | string \| null | ISO date; separates namesakes |

**Response `DirectoryResponse`:** `{ "matches": PatientMatchOut[] }`

**`PatientMatchOut`**

| Field | Type | Notes |
|---|---|---|
| `patient_id` | string | Use this in book/availability/appointments |
| `given_name` | string | |
| `first_surname` | string | |
| `second_surname` | string | |
| `national_id` | string | |
| `date_of_birth` | date | |
| `phone` | string | |
| `sex` | string | |
| `has_visited_before` | boolean | Drives appointment type |
| `insurer` | string | First plan on file (second plan not here) |
| `referrals` | string[] | |
| `note` | string | Receptionist note (jury / personalisation) |
| `match_score` | number | |
| `matched_fields` | string[] | |

| ID | Requirement |
|---|---|
| `API-directory-exact` | An exact query field that does not match excludes the patient (domain rule). |
| `API-directory-submit-id` | Always submit `patient_id` from a match — never invent from speech. |

---

## `GET /api/v1/availability` — Search Availability

| Query | Required | Type | Description |
|---|---|---|---|
| `date_from` | **yes** | string | First day inclusive |
| `date_to` | **yes** | string | Last day inclusive |
| `provider_id` | no | string \| null | Only this provider |
| `specialty_id` | no | string \| null | Only this specialty’s providers |
| `location_id` | no | string \| null | Only this site |
| `patient_id` | no | string \| null | Eligibility applied (age, history, plans) — listed slots are ones they can take |
| `insurer` | no | `Insurer[]` \| null | Only slots these plans cover; **repeat** query param for several |

Errors: `422` validation (e.g. span / range — see domain calendar rules).

**Response `AvailabilityResponse`**

| Field | Type | Notes |
|---|---|---|
| `providers` | `ProviderOut[]` | Compact provider rows for the search |
| `appointment_type` | `AppointmentTypeOut` | The one type that fits patient+specialty for this search |
| `slots` | `SlotOut[]` | Each slot carries `appointment_type_id` |
| `blocked` | `BlockedOut[]` | Standing restriction that stopped a provider |

**`ProviderOut`:** `id`, `name`, `specialty_id`, `languages[]`, `accepted_insurers[]`, `locations[]`, `on_leave_until` (string \| null)

**`AppointmentTypeOut`:** `id`, `name`, `duration_minutes`, `new_patient_requirement`, `guidance`

**`SlotOut`:** `provider_id`, `provider_name`, `specialty_id`, `location_id`, `appointment_type_id`, `start_time` (date-time), `duration_minutes`, `payable_with[]` (plan ids)

**`BlockedOut`:** `provider_id`, `restriction` (string — maps to refuse reasons)

| ID | Requirement |
|---|---|
| `API-avail-type` | Prefer `appointment_type.id` / each slot’s `appointment_type_id` when submitting `BOOK`. |
| `API-avail-blocked` | Use `blocked` to choose `OutcomeReason` when refusing. |
| `API-avail-insurer` | Pass `insurer` to quote against a plan (needed for second-policy cases). |

---

## `GET /api/v1/patients/{patient_id}/appointments`

| Param | In | Required | Type | Description |
|---|---|---|---|---|
| `patient_id` | path | yes | string | From directory |
| `when` | query | no | `AppointmentWindow` | `upcoming` (default) \| `past` \| `all` |

**Response `AppointmentsResponse`:** `{ "appointments": AppointmentOut[] }`

**`AppointmentOut`:** `appointment_id`, `patient_id`, `provider_id`, `location_id`, `appointment_type_id`, `start_time` (date-time), `duration_minutes`

| ID | Requirement |
|---|---|
| `API-appointment-id` | Only source of `appointment_id` for cancel/reschedule. Past visits are not mutable answers. |
