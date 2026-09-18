# El Turno — internal requirements

Internal rewrite of the HackSpain / Prosper **El Turno** challenge docs as stable requirements. These are **our** specs (IDs, must/should, constraints), not a copy of the official site.

**Capture date:** 18 September 2026  
**Event:** HackSpain ’26 — Friday 18 → Sunday 20 September 2026

## Source map

| Official page | URL | Feeds |
|---|---|---|
| The short version | https://hackspain.getprosperapp.com/leaderboard/docs/overview | [01-product](requirements/01-product.md) |
| What Is the Challenge? | https://hackspain.getprosperapp.com/leaderboard/docs/challenge | [01-product](requirements/01-product.md), [07-jury](requirements/07-jury-and-platform.md) |
| Get on the phone | https://hackspain.getprosperapp.com/leaderboard/docs/quickstart | [08-operations](requirements/08-operations.md) |
| The call contract | https://hackspain.getprosperapp.com/leaderboard/docs/contract | [03-call-and-submission](requirements/03-call-and-submission.md) |
| The clinic | https://hackspain.getprosperapp.com/leaderboard/docs/clinic-api | [04-clinic-domain](requirements/04-clinic-domain.md) |
| Scoring | https://hackspain.getprosperapp.com/leaderboard/docs/rules | [05-scoring](requirements/05-scoring.md) |
| The 18 problems | https://hackspain.getprosperapp.com/leaderboard/docs/problems | [06-problems](requirements/06-problems.md) |
| API Documentation | https://hackspain.getprosperapp.com/leaderboard/docs/api | [api/](api/README.md) (live schema) |
| Swagger UI | https://hackspain.getprosperapp.com/api/docs#/ | [api/](api/README.md) |
| OpenAPI JSON | https://hackspain.getprosperapp.com/api/openapi.json | [api/openapi.snapshot.json](api/openapi.snapshot.json) |
| Normalization table | https://hackspain.getprosperapp.com/leaderboard/docs/scoring | [05-scoring](requirements/05-scoring.md) |

## Doc set

| File | Purpose | ID prefix |
|---|---|---|
| [requirements/01-product.md](requirements/01-product.md) | Product definition, weekend shape, what wins | `PD-` |
| [requirements/02-functional.md](requirements/02-functional.md) | Agent behaviours that must work | `FR-` |
| [requirements/03-call-and-submission.md](requirements/03-call-and-submission.md) | Wire protocol and submit routes | `CR-` |
| [requirements/04-clinic-domain.md](requirements/04-clinic-domain.md) | Clínica Arenal world rules | `CL-` |
| [requirements/05-scoring.md](requirements/05-scoring.md) | Pass/fail, points, limits, normalization | `SC-` |
| [requirements/06-problems.md](requirements/06-problems.md) | Eighteen problems | `PR-` |
| [requirements/07-jury-and-platform.md](requirements/07-jury-and-platform.md) | Final boss / demo criteria | `JR-` |
| [requirements/08-operations.md](requirements/08-operations.md) | Keys, endpoint, runs, budget | `OP-` |
| [requirements/09-open-and-deferred.md](requirements/09-open-and-deferred.md) | TBDs and next phases | `OD-` |
| [requirements/scenarios/](requirements/scenarios/README.md) | Public case walkthroughs for open problems | `PR-*-S*` |
| [requirements/10-live-probe-findings.md](requirements/10-live-probe-findings.md) | Authenticated clinic probe traps (no secrets) | `LIVE-*` |
| [agent/](agent/README.md) | Receptionist process map + Flows YAML | — |
| [api/](api/README.md) | Platform OpenAPI field reference | `API-*` |

## How to extend

1. **Client examples (in progress):** open problems `PR-01`…`PR-06` live under [requirements/scenarios/](requirements/scenarios/README.md). Append more when new problems open.
2. **API docs (done for v0.1.0):** [api/](api/README.md) from live OpenAPI; re-snapshot if the schema changes.
3. **Agent graph:** [agent/](agent/README.md) is the receptionist process map + Flows YAML (not loaded by the bot yet).

## Explicitly deferred

- Scenarios for `PR-07`…`PR-18` (not yet open / not in the shared extract).
- Full `public-cases.json` dump beyond the 26 dashboard cases already captured.
- Stack / model / telephony choices — wait for tech discussion.
- Upstream recommendations (Pipecat, ngrok EU) appear only as hints in [08-operations](requirements/08-operations.md) / [09-open-and-deferred](requirements/09-open-and-deferred.md), not as our decisions.

## Requirement ID conventions

| Prefix | Meaning |
|---|---|
| `PD-` | Product / weekend |
| `FR-` | Functional (agent behaviour) |
| `CR-` | Call wire + submission |
| `CL-` | Clinic domain |
| `SC-` | Scoring / normalization |
| `PR-01`…`PR-18` | Problems |
| `JR-` | Jury / platform |
| `OP-` | Operations |
| `OD-` | Open / deferred |

Cross-links between IDs are intentional so a later case note can say e.g. “hits `PR-09` + `FR-identify`”.
