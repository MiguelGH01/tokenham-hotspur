# submit-retry

Feature: a failed final delivery must retry within the closing call instead of
losing a decided case. Evidence: `audit-622ace24-7e9a-4630-bc01-7fab93c5da63.ndjson`
(real platform call, 19 Sep 05:09) — offer prepared, plan frozen, one POST,
`submission_result ok=False error=ClinicApiError`, call ended. No second attempt
was ever logged.

## Diagnosis

1. `ClinicClient._request` retries once (`ATTEMPTS=2`, 1 s) but only for
   `status >= 500` and transport errors. A **429** (rate limit — plausible under
   a Run All's 20 concurrent calls) and a **408** are raised immediately as
   non-retryable `ClinicApiError`.
2. `CallSubmission._deliver` gives up on the first exception after the client's
   own attempts. The 5 s background loop (`deliver_until_accepted`) only helps
   while the call lives; when the failure happens during `close()`, the call
   ends with the record undelivered.
3. The audit log records only `type(exc).__name__` — no HTTP status — so a
   scored failure cannot be attributed afterwards (was it a 4xx we must not
   retry, or a 5xx that outlived the retries?).

## Tasks

- [x] T1: `server/clients/clinic_client.py` — treat 429 and 408 as retryable
      alongside 5xx; keep 4xx (400/404/409-handled) non-retryable.
- [x] T2: `server/submission.py` — `_deliver` retries the same payload a bounded
      number of times with a short backoff (env-configurable), per action, in
      order, before returning False. Retrying the same payload is explicitly
      safe (`delivery_attempted` freezes the plan to exactly that payload).
- [x] T3: `server/submission.py` — audit `submission_result` failure records carry
      the HTTP status when the error is a `ClinicApiError`.
- [x] T4: acceptance tests (`server/tests/acceptance/test_submit_retry.py`): transient failure then
      success delivers; permanent 4xx fails without infinite retry; partial
      delivery keeps ordering; close() retries land.
- [x] T5: `make test` and `make eval` verified; work-unit commit.

## Deliberate limits

- No exponential backoff sophistication: two bounded knobs, linear sleep.
- No change to the 5 s background loop; it stays the live-call safety net.
- No client-level HTTP test (httpx is constructed inside `_request`); the
  retryable-vocabulary test pins 429/408 in vs 400/404 out instead.

## Verification

- `make test`: 210 passed.
- `make eval`: 13/15 scenarios green. `pr06_age_redirect` failed once on a
  judge-strictness flake and passed on re-run. `simple_booking_ignacio` and
  `pr05_sunday_rolls_to_monday` fail identically **at HEAD without this
  change** (verified by stashing it): the LLM answers within 0.6 s but the
  `llm_response` event never reaches the harness after a tool-call turn, so
  the scenario times out at 60 s. Pre-existing regression, out of scope here —
  it needs its own investigation before the next scored run.

## Work-unit commits

(recorded per task at close)
