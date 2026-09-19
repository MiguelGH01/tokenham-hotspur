"""What gets POSTed when the call ends. Always something; never twice."""

from loguru import logger

DEFAULT_PENDING = {"action": "NO_ACTION", "reason": "patient_not_found"}


class CallSubmission:
    def __init__(self, call_id: str, client):
        self.call_id = call_id
        self._client = client
        self.pending: dict = dict(DEFAULT_PENDING)
        self._flushed = False

    def set_book(self, offer: dict) -> None:
        self.pending = {"action": "BOOK", **offer}

    def set_register(self, fields: dict) -> None:
        self.pending = {"action": "REGISTER", **fields}

    def set_no_action(self, reason: str) -> None:
        self.pending = {"action": "NO_ACTION", "reason": reason}

    async def flush(self) -> None:
        if self._flushed:
            return
        self._flushed = True
        action = {"call_id": self.call_id, **self.pending}
        try:
            result = await self._client.post_submission(action)
            logger.info("Submitted {} for call {}: {}", action["action"], self.call_id, result)
        except Exception as exc:
            logger.error("Submission failed for call {} ({}): {}", self.call_id, action, exc)
