# API conventions

Captured from OpenAPI `0.1.0` + contract rules that the schema cannot fully express.

## Authentication

| ID | Rule |
|---|---|
| `API-auth` | Scheme `TeamApiKey`: send desk-issued key in header `X-Api-Key`. |
| `API-auth-health` | `GET /api/v1/health` has **no** security requirement. |
| `API-auth-fail` | Missing / invalid / revoked key → `403 {"detail":"Invalid API key"}` (from ops docs; not listed on every OpenAPI response). |
| `API-team` | Team identity comes from the key (and for submits, from the registered call session). Body never chooses the team. |

## Encoding

| ID | Rule |
|---|---|
| `API-json` | Request/response bodies are JSON, **snake_case** field names. |
| `API-twilio` | Twilio Media Streams WebSocket messages remain **camelCase** and are **not** part of this HTTP OpenAPI (see requirements contract). |
| `API-dates` | Calendar days are `string(date)` (`YYYY-MM-DD`). Instant slots are `string(date-time)` with **explicit offset**. |
| `API-ids` | Ids (`P00042`, `PR05`, `sur`, `A000123`, …) are opaque strings compared **exactly**. |

## HTTP statuses (submit window)

OpenAPI documents `200` and `422` on submit routes. Contract also defines:

| Status | Meaning |
|---|---|
| `200` | Action accepted into the call record (not a pass). |
| `404` | Unknown / other team’s `call_id`. |
| `409` | Identical action already accepted (retry). |
| `410` | Submit window closed (> 30 s after socket close). |
| `422` | Malformed body / validation (`HTTPValidationError`); also invalid national-id check letter on register. |

| ID | Rule |
|---|---|
| `API-status-contract` | Agents must handle `404` / `409` / `410` even when Swagger only lists `200`/`422`. |

## Validation errors

`HTTPValidationError`:

```json
{ "detail": [ { "loc": ["body", "field"], "msg": "...", "type": "..." } ] }
```

`loc` entries are `string | integer`.

## Health

`GET /api/v1/health` → `200` with an unconstrained JSON schema (`{}` in OpenAPI). Use as connectivity smoke only.
