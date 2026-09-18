# Platform API — internal reference

Field-level documentation of the Prosper platform API used by El Turno agents. Behavioural musts stay in [requirements/03-call-and-submission.md](../requirements/03-call-and-submission.md) and [requirements/04-clinic-domain.md](../requirements/04-clinic-domain.md); this folder is the **schema surface**.

| | |
|---|---|
| **Live Swagger** | https://hackspain.getprosperapp.com/api/docs#/ |
| **Live ReDoc** | https://hackspain.getprosperapp.com/api/redoc |
| **Live OpenAPI** | https://hackspain.getprosperapp.com/api/openapi.json |
| **Snapshot** | [openapi.snapshot.json](openapi.snapshot.json) (captured 18 Sep 2026) |
| **Declared version** | `0.1.0` (OpenAPI 3.1.0) — title *Prosper — platform API* |
| **Base host** | Desk issues `PLATFORM_API_BASE_URL` (public docs site mounts API at `https://hackspain.getprosperapp.com`) |

## Doc set

| File | Contents |
|---|---|
| [01-conventions.md](01-conventions.md) | Auth, errors, snake_case, what OpenAPI omits |
| [02-clinic.md](02-clinic.md) | Read-only EHR endpoints + response shapes |
| [03-submit.md](03-submit.md) | Submit + submissions readback |
| [04-enums.md](04-enums.md) | `Insurer`, `OutcomeReason`, `AppointmentWindow` |

## Surface map (17 routes)

| Method | Path | Tag | Auth |
|---|---|---|---|
| `GET` | `/api/v1/health` | Status | none |
| `GET` | `/api/v1/clinic` | Clinic | `X-Api-Key` |
| `GET` | `/api/v1/directory` | Clinic | `X-Api-Key` |
| `GET` | `/api/v1/availability` | Clinic | `X-Api-Key` |
| `GET` | `/api/v1/patients/{patient_id}/appointments` | Clinic | `X-Api-Key` |
| `GET` | `/api/v1/providers` | Clinic | `X-Api-Key` |
| `GET` | `/api/v1/locations` | Clinic | `X-Api-Key` |
| `GET` | `/api/v1/specialties` | Clinic | `X-Api-Key` |
| `GET` | `/api/v1/appointment-types` | Clinic | `X-Api-Key` |
| `GET` | `/api/v1/insurance-plans` | Clinic | `X-Api-Key` |
| `POST` | `/api/v1/submit/register` | Submission | `X-Api-Key` |
| `POST` | `/api/v1/submit/book` | Submission | `X-Api-Key` |
| `POST` | `/api/v1/submit/reschedule` | Submission | `X-Api-Key` |
| `POST` | `/api/v1/submit/cancel` | Submission | `X-Api-Key` |
| `POST` | `/api/v1/submit/no-action` | Submission | `X-Api-Key` |
| `POST` | `/api/v1/submit/escalate` | Submission | `X-Api-Key` |
| `GET` | `/api/v1/submissions` | Submission | `X-Api-Key` |

Security scheme name in OpenAPI: **`TeamApiKey`** → header **`X-Api-Key`**.
