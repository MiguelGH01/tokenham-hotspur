"""Async client for the Clínica Arenal API (read-only lookups + submit routes)."""

import asyncio
import os

import httpx
from loguru import logger

SUBMIT_ROUTES = {
    "BOOK": "/v1/submit/book",
    "REGISTER": "/v1/submit/register",
    "RESCHEDULE": "/v1/submit/reschedule",
    "CANCEL": "/v1/submit/cancel",
    "NO_ACTION": "/v1/submit/no-action",
    "ESCALATE": "/v1/submit/escalate",
}

ATTEMPTS = max(1, int(os.getenv("CLINIC_API_ATTEMPTS", "2")))
RETRY_DELAY_SECS = max(0.0, float(os.getenv("CLINIC_API_RETRY_DELAY_SECS", "1.0")))
MAX_LOGGED_BODY = 500

#: Client errors worth another attempt. A Run All dials twenty calls at once,
#: so the submit endpoint can answer 429 (rate limited) or 408 (timed out)
#: while still being perfectly willing to take the record a moment later.
#: Every other 4xx is a real refusal of the payload and repeats only waste the
#: seconds a closing call has left.
RETRYABLE_STATUSES = frozenset({408, 429})


class ClinicApiError(RuntimeError):
    """A non-success answer from the clinic API, with the evidence attached.

    A bare ``HTTPStatusError`` says only "4xx". A scored run that fails with
    ``Record mismatch`` is undiagnosable without the status, the path and the
    body, which is exactly the detail this carries into the log.
    """

    def __init__(self, method: str, path: str, status: int, body: str):
        super().__init__(f"{method} {path} -> HTTP {status}: {body}")
        self.method = method
        self.path = path
        self.status = status
        self.body = body


class ClinicClient:
    def __init__(
        self, base_url: str | None = None, api_key: str | None = None, timeout: float = 5.0
    ):
        self._base_url = (base_url or os.environ["CLINIC_API_BASE_URL"]).rstrip("/")
        self._headers = {"X-Api-Key": api_key or os.environ["CLINIC_API_KEY"]}
        self._timeout = timeout

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                async with httpx.AsyncClient(
                    base_url=self._base_url, headers=self._headers, timeout=self._timeout
                ) as client:
                    response = await client.request(method, path, **kwargs)
                if response.status_code == 409:
                    logger.info("{} {} -> 409 (already accepted)", method, path)
                    return response.json()
                if response.status_code >= 400:
                    raise ClinicApiError(
                        method,
                        path,
                        response.status_code,
                        response.text[:MAX_LOGGED_BODY],
                    )
                return response.json()
            except ClinicApiError as exc:
                retryable = exc.status >= 500 or exc.status in RETRYABLE_STATUSES
                if attempt == ATTEMPTS or not retryable:
                    logger.error("{} (attempt {}/{})", exc, attempt, ATTEMPTS)
                    raise
                logger.warning(
                    "{} {} failed (attempt {}): HTTP {}",
                    method,
                    path,
                    attempt,
                    exc.status,
                )
                await asyncio.sleep(RETRY_DELAY_SECS)
            except (httpx.TransportError, ValueError) as exc:
                if attempt == ATTEMPTS:
                    logger.error(
                        "{} {} failed after {} attempts: {}",
                        method,
                        path,
                        ATTEMPTS,
                        type(exc).__name__,
                    )
                    raise
                logger.warning(
                    "{} {} failed (attempt {}): {}", method, path, attempt, type(exc).__name__
                )
                await asyncio.sleep(RETRY_DELAY_SECS)
        raise RuntimeError("unreachable")

    async def search_directory(self, **params) -> list[dict]:
        query = {k: v for k, v in params.items() if v is not None}
        data = await self._request("GET", "/v1/directory", params=query)
        return data["matches"]

    async def availability(
        self,
        date_from: str,
        date_to: str,
        specialty_id: str,
        patient_id: str,
        location_id: str | None = None,
        insurer: list[str] | None = None,
    ) -> dict:
        """Slots for a patient.

        ``insurer`` is repeatable and is what quotes a search against a plan the
        record does not hold — the second policy of PR-17 (``API-avail-insurer``).
        It is a query parameter, so several plan ids are sent as repeats rather
        than as one comma-joined value.
        """
        query: dict = {
            "date_from": date_from,
            "date_to": date_to,
            "specialty_id": specialty_id,
            "patient_id": patient_id,
        }
        if location_id:
            query["location_id"] = location_id
        if insurer:
            query["insurer"] = list(insurer)
        return await self._request("GET", "/v1/availability", params=query)

    async def appointments(self, patient_id: str, when: str = "upcoming") -> list[dict]:
        """The patient's diary. ``when`` is ``upcoming``, ``past`` or ``all``.

        Past visits are what the end-of-call resolver mines for a habit (see
        ``resolution.py``); they are not mutable and are never an answer.
        """
        from urllib.parse import quote

        data = await self._request(
            "GET",
            f"/v1/patients/{quote(patient_id, safe='')}/appointments",
            params={"when": when},
        )
        return data["appointments"]

    async def post_submission(self, action: dict) -> dict:
        verb = action["action"]
        if verb not in SUBMIT_ROUTES:
            raise ValueError(f"unknown submission verb: {verb}")
        body = {k: v for k, v in action.items() if k != "action"}
        return await self._request("POST", SUBMIT_ROUTES[verb], json=body)
