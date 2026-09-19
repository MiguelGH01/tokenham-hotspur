"""Async client for the Clínica Arenal API (read-only lookups + submit routes)."""

import asyncio
import os
import threading

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
ATTEMPTS = 2
RETRY_DELAY_SECS = 1.0
_HTTP_LOCK = threading.Lock()
_SHARED: dict[tuple, httpx.AsyncClient] = {}


class ClinicClient:
    def __init__(self, base_url: str | None = None, api_key: str | None = None, timeout: float = 3.0):
        self._base_url = (base_url or os.environ["CLINIC_API_BASE_URL"]).rstrip("/")
        self._headers = {"X-Api-Key": api_key or os.environ["CLINIC_API_KEY"]}
        self._timeout = timeout

    def _http(self) -> httpx.AsyncClient:
        """One keepalive pool per (base, key, timeout) so 20 sockets do not open 20 TLS sessions."""
        key = (self._base_url, self._headers.get("X-Api-Key"), self._timeout)
        with _HTTP_LOCK:
            client = _SHARED.get(key)
            if client is None or client.is_closed:
                _SHARED[key] = client = httpx.AsyncClient(
                    base_url=self._base_url,
                    headers=self._headers,
                    timeout=self._timeout,
                    limits=httpx.Limits(max_connections=40, max_keepalive_connections=20),
                )
            return client

    async def _request(self, method: str, path: str, **kwargs) -> dict:
        client = self._http()
        for attempt in range(1, ATTEMPTS + 1):
            try:
                response = await client.request(method, path, **kwargs)
                if response.status_code == 409:
                    logger.info("{} {} -> 409 (already accepted)", method, path)
                    return response.json()
                if response.status_code >= 500:
                    raise httpx.HTTPStatusError(
                        f"{response.status_code} from {path}", request=response.request, response=response
                    )
                response.raise_for_status()
                return response.json()
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if attempt == ATTEMPTS or (
                    isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code < 500
                ):
                    raise
                logger.warning("{} {} failed (attempt {}): {}", method, path, attempt, exc)
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
        specialty_id: str | None,
        patient_id: str,
        location_id: str | None = None,
        provider_id: str | None = None,
    ) -> dict:
        query = {
            "date_from": date_from,
            "date_to": date_to,
            "patient_id": patient_id,
        }
        if specialty_id:
            query["specialty_id"] = specialty_id
        if location_id:
            query["location_id"] = location_id
        if provider_id:
            query["provider_id"] = provider_id
        return await self._request("GET", "/v1/availability", params=query)

    async def post_submission(self, action: dict) -> dict:
        body = {k: v for k, v in action.items() if k != "action"}
        return await self._request("POST", SUBMIT_ROUTES[action["action"]], json=body)
