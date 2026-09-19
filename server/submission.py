"""What gets POSTed when the call ends. Always something; never twice."""

from loguru import logger

DEFAULT_PENDING = {"action": "NO_ACTION", "reason": "patient_not_found"}
# The patient is on file but the call ended with nothing settled (hung up, cut off, never
# answered an offer). OutcomeReason has no "abandoned" value. "no_availability" is the documented
# ending for "no appointment could be established" (PR-07); "out_of_scope" is reserved for
# adversarial asks (PR-14) and would be a false claim here.
NO_OUTCOME_REASON = "no_availability"


class CallSubmission:
    def __init__(self, call_id: str, client):
        self.call_id = call_id
        self._client = client
        self.pending: dict = dict(DEFAULT_PENDING)
        self._flushed = False

    def set_book(self, offer: dict) -> None:
        """Only on the caller's explicit yes: an offer nobody accepted is never booked."""
        self.pending = {"action": "BOOK", **offer}

    def set_no_outcome(self) -> None:
        """The patient is known (or a slot now exists): any earlier refusal reason is stale."""
        if self.pending["action"] == "NO_ACTION":
            self.pending = {"action": "NO_ACTION", "reason": NO_OUTCOME_REASON}

    def set_no_action(self, reason: str) -> None:
        self.pending = {"action": "NO_ACTION", "reason": reason}

    async def flush(self) -> None:
        if self._flushed:
            return
        action = {"call_id": self.call_id, **self.pending}
        try:
            result = await self._client.post_submission(action)
            # Only a POST that landed counts: a failed one stays retryable by the next flush()
            # (run_bot's finally), inside the 30s submit window. A duplicate is a harmless 409.
            self._flushed = True
            logger.info("Submitted {} for call {}: {}", action["action"], self.call_id, result)
        except Exception as exc:
            logger.error("Submission failed for call {} ({}): {}", self.call_id, action, exc)
