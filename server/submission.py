"""What gets POSTed when the call ends. Always something; never twice."""

from loguru import logger

DEFAULT_PENDING = {"action": "NO_ACTION", "reason": "patient_not_found"}


class CallSubmission:
    def __init__(self, call_id: str, client):
        self.call_id = call_id
        self._client = client
        self.pending: dict = dict(DEFAULT_PENDING)
        self.offered: dict | None = None  # offered to the caller, neither accepted nor declined
        self._flushed = False

    def set_book(self, offer: dict) -> None:
        self.pending = {"action": "BOOK", **offer}

    def set_offer(self, offer: dict) -> None:
        self.offered = offer

    def clear_offer(self) -> None:
        self.offered = None

    def set_no_action(self, reason: str) -> None:
        self.pending = {"action": "NO_ACTION", "reason": reason}

    async def flush(self) -> None:
        if self._flushed:
            return
        self._flushed = True
        pending = self.pending
        if pending == DEFAULT_PENDING and self.offered:
            # The call ended (cut off, hung up) after a real slot was offered and before the
            # caller answered. The offer is the earliest valid slot, so it beats NO_ACTION.
            logger.warning("Call {} ended on an unconfirmed offer: submitting it", self.call_id)
            pending = {"action": "BOOK", **self.offered}
        action = {"call_id": self.call_id, **pending}
        try:
            result = await self._client.post_submission(action)
            logger.info("Submitted {} for call {}: {}", action["action"], self.call_id, result)
        except Exception as exc:
            logger.error("Submission failed for call {} ({}): {}", self.call_id, action, exc)
