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

    def set_escalate(self, reason: str) -> None:
        self.pending = {"action": "ESCALATE", "reason": reason}

    def set_register(self, fields: dict) -> None:
        self.pending = {"action": "REGISTER", **fields}

    def ready_to_submit(self) -> bool:
        """True when we have a real answer, not the unused patient_not_found default."""
        if self.offered:
            return True
        return self.pending != DEFAULT_PENDING

    async def flush(self) -> None:
        if self._flushed:
            return
        self._flushed = True
        pending = self.pending
        if self.offered and pending.get("action") not in {"BOOK", "REGISTER", "ESCALATE"}:
            # Hang-up after a real slot was offered beats patient_not_found / asking
            # whether another doctor will do. Explicit refusals (referral, coverage) win.
            if pending.get("reason") in {DEFAULT_PENDING["reason"], "provider_not_found"}:
                logger.warning("Call {} ended on an unconfirmed offer: submitting it", self.call_id)
                pending = {"action": "BOOK", **self.offered}
        action = {"call_id": self.call_id, **pending}
        try:
            result = await self._client.post_submission(action)
            logger.info("Submitted {} for call {}: {}", action["action"], self.call_id, result)
        except Exception as exc:
            logger.error("Submission failed for call {} ({}): {}", self.call_id, action, exc)
