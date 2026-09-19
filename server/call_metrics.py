"""What a call consumed, written to the same per-call trail as its decisions.

Pipecat already measures what each service spends — audio seconds transcribed,
tokens in and out, characters synthesised, time to first byte — and reports it
in ``MetricsFrame``s. Nothing was listening. This attaches pipecat's own
:class:`ServiceMetricsObserver` to the call and appends every record to the
call's audit file, so ``call_cost`` can price a call afterwards.

An observer watches frames from outside the pipeline: it adds no processor to
the audio path. Records carry counts and model names, never caller text, so the
audit file's privacy contract holds. ``audit.audit`` swallows its own failures,
which is what keeps bookkeeping from ever reaching a scored call.
"""

from pipecat.observers.service_metrics_observer import ServiceMetricsObserver

import audit


def call_started(call_id: str) -> None:
    audit.audit(call_id, "call_started")


def call_ended(call_id: str) -> None:
    audit.audit(call_id, "call_ended")


def build_observer(call_id: str) -> ServiceMetricsObserver:
    """An observer that appends this call's service usage and latency to its audit trail."""
    observer = ServiceMetricsObserver()

    @observer.event_handler("on_service_usage")
    async def on_service_usage(_observer, record):
        audit.audit(call_id, "service_usage", **record.model_dump(mode="json", exclude_none=True))

    @observer.event_handler("on_service_latency")
    async def on_service_latency(_observer, record):
        audit.audit(call_id, "service_latency", **record.model_dump(mode="json", exclude_none=True))

    return observer
