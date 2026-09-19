# PR-03 doctor_and_site — plan

Status: implemented (19 Sep) — 49 unit tests green, PR-03 evals 5/5 (Mario needed a 2nd run: gateway flake). Uncommitted. Spec: `docs/requirements/scenarios/doctor_and_site.md`.

## Decisions (Adolfo, 19 Sep)

1. Name → id: `provider` enum on `get_earliest_slot` (12 ids + `not_listed`), names+specialty in the description.
2. Decision tree in code, Flows graph unchanged except one close node.
3. Wrong weekday: code drops the weekday itself, same API response.
4. Generic `end_without_booking` tool → `close` node (reused by PR-06).

## Live facts (probed 19 Sep)

- ONE `/availability` call (specialty + optional site, no provider_id) serves every branch.
- Leave is NOT in `blocked` (empty) and Requena's post-leave slots (1 Oct) ARE listed →
  a naive provider filter books PR02 on 1 Oct. Leave must come from `clinic.json` `leave`.
- Mario: PR03@centro earliest = Fri 25 Sep 09:30. Andrés: PR07@norte Tue 22 Sep 09:00.

## Tree (get_earliest_slot)

```
provider == not_listed            -> NO_ACTION provider_not_found, status provider_not_found, no API call
provider on leave at call date    -> drop provider, keep specialty+site      note=on_leave
provider ok, slots match          -> offer
provider ok, 0 slots, had weekday -> retry without weekday (keep prov+site)  note=other_day
still nothing                     -> no_slots (as today)
```

## Tasks

1. `clinic_catalog.py`: `providers()`, `provider_on_leave(provider_id, day)`.
2. `booking.py`: `pick_offer(..., provider_id=None)` filter.
3. `tools.py`: tree above; `note` in the offer result; `end_without_booking`.
4. `nodes.py`: prompts for note/provider_not_found; `create_close_node`; tool in find_slot + confirm.
5. Tests first: unit per branch (booking + tools). Then PR-01 + PR-03 evals.

## Out of scope

DKV/Iglesias redirect (PR-06), Spanish, fuzzy name matching.
