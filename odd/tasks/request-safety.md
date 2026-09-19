# request-safety

Feature: make a request — not the model's memory — the unit that a confirmation,
a revision and a delivery are anchored to. The rule: a proposal is read back and
confirmed in a **later turn**; changed details require `revise_request` + a fresh
search + fresh consent; accepted actions are immutable; separate intents never
erase each other's drafts or effects. This is the continuation of the
proposal/revision/multi-request layer the gate needs to be safe.

In-flight work inherited from the previous agent (uncommitted, 19 Sep): the
proposal layer (`server/flows/requests.py`), the cancel/reschedule flows
(`server/flows/appointments.py`), per-intent child submissions
(`CallSubmission.new_request`), the affirmative-evidence gate
(`confirmation.is_affirmative`), the weekday/date conflict error
(`dates.resolve_named`), and `server/tests/acceptance/test_request_safety.py`.

## Baseline on pickup

`cd server && uv run pytest tests/` → **5 failed, 122 passed**:

| Test | Cause |
|---|---|
| `test_dates.py::test_a_named_date_already_past_means_next_year` | the new weekday conflict error fires on the next-year rollover, where the weekday matched the named *calendar* day |
| `test_flows.py::test_a_repeated_confirmation_is_a_retry_not_a_conflict` | the proposal gate needs a later caller turn, which this manager never had; the returned node also became `request_complete` |
| `test_flows.py::test_concurrent_call_isolation` | same, for `confirm_registration`: nothing was posted |
| `test_flows.py::test_twenty_concurrent_booking_calls_are_isolated` | same, for `confirm_offer`: nothing was posted |
| `test_request_safety.py::test_reschedule_keeps_existing_type_and_requires_later_consent` | `get_earliest_slot` offers any type; a moved `review` was offered a `first_visit` slot |

Two defects found by reading rather than by the suite:

- **Re-introduced settled-record bug** (`confirm_offer`): when delivery had
  already been attempted the flow skipped `set_book`, flushed nothing and
  returned `accepted`. That is evidence 1 of `pr01-06-record-and-liveness.md`
  verbatim — the caller hears "booked" while the platform holds `NO_ACTION`.
  Pointing the flow at `create_completion_node(False, …)` is not enough: the
  status itself was the lie.
- **`offer_id = f"offer-{revision}"`** breaks the eval contract: four scenarios
  assert `confirm_offer(offer_id: offer-1)`, and by the time `get_earliest_slot`
  runs, `route_request` + `search_patient` have already bumped the revision (so
  the first offer is `offer-3`). Handles must be numbered per call, not per
  revision.

## Tasks

- [x] T1: offer handles as per-call ids — `offer-{n}` counted per call (the eval
      contract), retained across a revision, read as `expired` when they no longer
      belong to the live proposal; the confirm node's enum lists only the live one.
  - Files: `server/flows/booking.py`, `server/flows/requests.py`
  - Evidence: `evals/simple_booking_{amelia,chloe,ignacio,josefa}.yaml` pin `offer-1`;
    `test_request_safety.py::test_reschedule_keeps_existing_type_and_requires_later_consent`
    asserts the old handle survives `revise_search`
- [x] T2: reschedule keeps the appointment — `get_earliest_slot` filters the
      availability to the existing appointment's `appointment_type_id`.
  - File: `server/flows/booking.py`
  - Evidence: `test_reschedule_keeps_existing_type_and_requires_later_consent`
- [x] T3: restore the settled-record guard — a confirmation after delivery was
      attempted is a retry only when the frozen plan already carries that exact
      action; otherwise `delivery_conflict` and an honest farewell, for BOOK,
      RESCHEDULE, CANCEL and REGISTER.
  - Files: `server/flows/booking.py`, `server/flows/registration.py`,
    `server/flows/appointments.py`, `server/flows/common.py`, `server/submission.py`
  - Evidence: `odd/tasks/pr01-06-record-and-liveness.md` evidence 1
- [x] T4: one fallback per call — a per-intent child never invents the call-level
      `NO_ACTION`; the root posts it once when nothing anywhere was delivered.
  - File: `server/submission.py`
  - Evidence: PR-08/PR-18 multi-intent calls (two requests, neither decided)
- [x] T5: the confirmation turn is contract — the three pre-existing tests that
      confirm with no conversation context now say "yes" in a later turn;
      `confirm_offer` returns `request_complete` because another request may
      follow (`docs/agent/flow.yaml::capture_next_job`).
  - File: `server/tests/acceptance/test_flows.py`
- [x] T6: the model is told the new statuses (`needs_confirmation`,
      `qualified_confirmation`, `expired`, `delivery_conflict`, `delivery_pending`)
      in the node prompts it has to act on.
  - Files: `server/flows/booking.py`, `server/flows/registration.py`,
    `server/flows/appointments.py`
- [x] T7: verification — `make test` green and `ruff check` clean for every file
      this change touches. `make eval` is deferred: it needs live provider keys
      and ~15 bot boots, so the offer-id contract was checked against the scenario
      files instead of by running them.
  - Result: `cd server && uv run pytest tests/` → **127 passed**;
    `uv run ruff check .` → only the two findings that already exist at HEAD
    (`audit.py` UP017, `test_registration_normalization.py` I001), both outside this
    change. The 7 findings the change introduced (import blocks) are fixed.

## Deliberate limits

- A reschedule still takes its specialty from the conversation, not from the
  appointment's `provider_id`; pre-filling it from the catalogue is a follow-up.
- The proposal layer does not re-verify identity evidence (≥2 corroborating
  fields); `search_patient` keeps its current rule.
- Second-policy negotiation (PR-17) and triage (PR-10) are untouched here.

## Error history

| Run | Observation | Cause | Correction |
|---|---|---|---|
| 19 Sep (pickup) | 5 acceptance failures, two of them silent records | proposal-turn gate added without the tests' conversation context; settled-record guard deleted | T3, T5 |
| 19 Sep (pickup) | 4 eval scenarios would fail on `offer-1` | offer id derived from the revision counter | T1 |

## Work-unit commits

(not committed yet: the working tree also carries the inherited uncommitted work
in the same files, so the split has to be decided with the user)
