# Open items and deferred phases

What is still unknown from organisers, and what we intentionally have **not** documented yet.

## Organiser TBDs (from official scoring docs)

Until organisers confirm, nothing in our requirements implies an answer:

| ID | Open question |
|---|---|
| `OD-tiebreak` | How stage-final places are settled when qualifiers tie. |
| `OD-dispute-channel` | Announcement channel for corrections, and who owns a dispute. |
| `OD-checkpoint-windows` | Exact clock times of the two weekend checkpoint freezes (desk announces). |
| `OD-jury-weights` | Numeric weights for the seven final-boss criteria (desk announces). |
| `OD-api-host` | Concrete `PLATFORM_API_BASE_URL` / API host string (desk issues). |

Update this table when the desk publishes answers; do not invent them.

## Phase gate: client examples (in progress)

| ID | Status |
|---|---|
| `OD-client-examples` | **Partial.** Dashboard extract for open problems ingested into [scenarios/](scenarios/README.md) (`simple_booking`, `switchboard`, `doctor_and_site` — 12 cases, 18 Sep 2026). |
| `OD-public-cases` | **Partial.** The 12 visible public cases are documented; remaining problems and the full `public-cases.json` roster wait until those pages open / further extracts are shared. |

Preferred shape (in use):

1. One linked file per `problem_id` under `scenarios/`.
2. Each scenario cites requirement IDs it exercises (`PR-*-S*`, `FR-*`, `CL-*`).
3. Expected action JSON included for single-dial cases; bursts note per-call independence.

## Phase gate: API documentation (done — re-check if schema moves)

| ID | Status |
|---|---|
| `OD-openapi` | **Done for v0.1.0.** Internal reference in [../api/](../api/README.md); snapshot [openapi.snapshot.json](../api/openapi.snapshot.json) captured 18 Sep 2026 from https://hackspain.getprosperapp.com/api/openapi.json. |
| `OD-admin-routes` | Note only: organisers use `X-Admin-Key` evidence routes absent from public OpenAPI — not part of our agent surface. |

Keep [03-call-and-submission](03-call-and-submission.md) and [04-clinic-domain](04-clinic-domain.md) as the behavioural source of truth; API docs should not contradict them. Contract statuses `404`/`409`/`410` are documented under [api/01-conventions.md](../api/01-conventions.md) because Swagger only lists `200`/`422` on submit.

## Phase gate: technology discussion (next)

| ID | Status |
|---|---|
| `OD-tech` | **Deferred until we discuss.** No stack, model, STT/TTS, orchestration, or hosting decisions in this folder yet. |
| `OD-upstream-hints` | Pipecat / ngrok EU / static domain recorded as hints in [08-operations](08-operations.md) only. |

## Doc maintenance

| ID | Rule |
|---|---|
| `OD-capture-date` | Source capture date is **18 September 2026**. If official docs change, re-capture and bump a short changelog note here. |
| `OD-id-stability` | Do not renumber `FR-` / `PR-` / etc. once client examples start citing them; add new IDs instead. |

## Related docs

- Index → [../README.md](../README.md)
- Problems placeholders → [06-problems](06-problems.md)
