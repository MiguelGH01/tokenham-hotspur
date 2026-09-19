# Reception, booking and registration (P1–P4)

The bot starts with an open question, then routes to booking or registration. This is one voice pipeline with per-call flow state, not separate voice agents.

## Active modules

- `flows/reception.py`: intent routing; no repeated collection of known details.
- `flows/identification.py`: name plus exact identifier; new patients can switch to registration.
- `flows/booking.py`: provider resolution, site constraints, offer and confirmation.
- `flows/registration.py`: demographic validation, readback and registration-only confirmation.
- `flows/common.py`: voice instructions and terminal nodes.
- `booking.py`: deterministic slot filtering; `submission.py`: immutable retryable delivery.
- `clients/clinic_client.py`: canonical HTTP client. Root compatibility imports contain no duplicate implementation.

One call owns its patient, offers and outcome. Registration never books an appointment. A confirmed outcome is frozen on first delivery attempt, serialized and retried unchanged; no provisional timer submits it early. HTTP acceptance is not scoring success.

## Offline verification

From the server directory:

```sh
PYTHONDONTWRITEBYTECODE=1 .venv/bin/python -m pytest -q -p no:cacheprovider
.venv/bin/ruff check bot.py booking.py handlers.py submission.py clinic_client.py clients flows tests/acceptance/test_flows.py tests/acceptance/test_booking_compile.py
```

The tests interleave 20 booking and 20 registration sessions with fake APIs. They do not establish real audio throughput, provider capacity or Prosper scoring. Eval YAML scenarios require a running test bot; they can send real submissions, so use only an authorized test environment.

## Deliberate limits

P5+ conversation types, multi-action calls, cancellations and rescheduling are not implemented. Current booking searches one 14-day window. Caller-supplied spoken identity and confirmation still depend on speech recognition and LLM tool use; perform real P1/P3/P4 public calls before claiming completion against the evaluator.
