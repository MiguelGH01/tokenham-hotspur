# Operations requirements

How the team gets credentials, exposes an endpoint, and runs practice vs scored lanes. Not a technology choice document.

## Account and credentials

| ID | Requirement | Priority |
|---|---|---|
| `OP-desk-account` | Organisers open the account at the registration desk. No self sign-up, no event code form. | must |
| `OP-credentials` | Desk shows once: **email**, generated **dashboard password**, **API key** (`pk-…`). Copy before the screen is gone — neither key nor password is readable again. | must |
| `OP-key-team` | API key belongs to the **team**, not a person. One key; every agent uses it. Extra dashboard logins added at desk against the same team. | must |
| `OP-lost-key` | Lost key → rotated at desk; old key stops working. Lost password → desk revokes and opens a new account (no reset email). | must |
| `OP-api-key-header` | Every route except `/api/v1/health` and the OpenAPI schema itself requires `X-Api-Key`. Missing/invalid/revoked → `403 {"detail":"Invalid API key"}`. | must |
| `OP-isolation` | Another team’s call/run is indistinguishable from nonexistent (`404`). Request body never chooses which team you are. | must |

## Budget

| ID | Requirement | Priority |
|---|---|---|
| `OP-card` | Desk hands one prepaid debit card with **€100** per team for models, speech, telephony, tunnels. Not topped up; nothing to claim back. | must |

## Endpoint integration

| ID | Requirement | Priority |
|---|---|---|
| `OP-build-ws` | Build a WebSocket server that answers the phone per [03-call-and-submission](03-call-and-submission.md). No starter kit — building the answerer is part of the challenge. | must |
| `OP-tunnel` | Expose localhost to the internet (WebSocket-capable tunnel). Free tunnel URLs that change on restart are a risk. | must |
| `OP-settings` | Set endpoint on dashboard **Settings → Integration** (not a desk trip). Fields: **Endpoint** (`wss://host/path`), optional **Headers** (one per line, e.g. `Authorization: Bearer …`). | must |
| `OP-settings-replace` | Saving replaces the whole configuration; clearing headers means none. Header values write-only (names listed back, values never). | must |
| `OP-header-reject` | Headers owned by WebSocket (`Host`, `Connection`, `Upgrade`, `Sec-WebSocket-*`) rejected. Endpoint must be `wss://` or `ws://`. | must |
| `OP-next-run` | Endpoint change applies to the **next** run; a queued run keeps the endpoint snapshotted at admit time. | must |
| `OP-tunnel-up` | Keep the tunnel up for the whole run. Dropped connection = failed case. Run All holds ten sockets open at once. | must |

## Smoke and tooling

| ID | Requirement | Priority |
|---|---|---|
| `OP-health` | `GET /api/v1/health` reachable without key. | must |
| `OP-directory-smoke` | Authenticated directory query works (e.g. name search) before judged calls. | must |
| `OP-submissions-read` | Agent/tooling can read own submissions: `GET /api/v1/submissions?limit=50` with API key. | should |
| `OP-api-ref` | Live schema: ReDoc `/api/redoc`, Swagger `/api/docs`, raw `/api/openapi.json` on the API host — Authorize with `X-Api-Key`. Internal field docs: [../api/README.md](../api/README.md). | should |

## Dashboard run loop

| ID | Requirement | Priority |
|---|---|---|
| `OP-call-button` | **Call** beside each published case = one practice call; scores nothing. | must |
| `OP-run-all` | **Run All** = one scored run across private cases for every open scored problem. | must |
| `OP-cancel-safe` | Cancelling a run is safe at any point. | must |
| `OP-rate-limits` | Honour practice 30 s cooldown and 15 min post–Run All cooldown (`SC-cooldown-*`). UI counts down. | must |

## Upstream hints (not our stack decisions)

Recorded only so we do not lose organiser recommendations; choosing them is deferred:

| Hint | Source note |
|---|---|
| Pipecat for voice pipeline + Twilio Media Streams serializer | Official quickstart recommendation |
| ngrok as recommended tunnel; prefer European region; prefer static domain | Official quickstart |
| Verify with `wscat` before handing over endpoint | Official quickstart |

## Related docs

- Wire contract → [03-call-and-submission](03-call-and-submission.md)
- Scoring lanes / limits → [05-scoring](05-scoring.md)
- Platform API fields → [../api/README.md](../api/README.md)
- Deferred tech → [09-open-and-deferred](09-open-and-deferred.md)
