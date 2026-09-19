"""One serialized immutable outcome per call, retryable until accepted."""

import asyncio
from copy import deepcopy

from loguru import logger

DEFAULT_PENDING = {"action": "NO_ACTION", "reason": "patient_not_found"}


class CallSubmission:
    def __init__(self, call_id, client):
        self.call_id = call_id
        self._client = client
        self.pending = dict(DEFAULT_PENDING)
        self._payload = None
        self._flushed = False
        self._lock = asyncio.Lock()

    @property
    def delivery_started(self):
        return self._payload is not None

    def _set(self, action):
        if self.delivery_started:
            raise RuntimeError("Delivery has already started")
        self.pending = deepcopy(action)

    def set_book(self, offer):
        self._set({"action": "BOOK", **offer})

    def set_register(self, patient):
        self._set({"action": "REGISTER", **patient})

    def set_no_action(self, reason):
        self._set({"action": "NO_ACTION", "reason": reason})

    async def flush(self):
        async with self._lock:
            if self._flushed:
                return True
            if self._payload is None:
                self._payload = {"call_id": self.call_id, **deepcopy(self.pending)}
            try:
                await self._client.post_submission(deepcopy(self._payload))
            except Exception as exc:
                logger.error("Submission failed ({})", type(exc).__name__)
                return False
            self._flushed = True
            logger.info("Submission accepted")
            return True
