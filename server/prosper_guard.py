"""Do not restart the endpoint while a scored run is dialling.

The scored lane dials our endpoint ten calls at a time and scores each call's
record inside that call's own window (``CR-concurrency``). Restarting the bot
process — or re-opening the tunnel — while a run is in flight kills those calls
mid-conversation, and a killed call is still an attempt: it scores nothing, or
worse, leaves the platform holding a partial record (`SC-cut-still-attempt`).
Nothing about the code causes that; the timing does, and timing is easy to get
wrong when the reload is automated.

So anything that restarts the endpoint asks this module first::

    uv run python -m prosper_guard || exit 1     # refuses while a run is active

Posture, in three directions:

- it **waits** while a run is active (a full Run All is about 18 minutes, so the
  default budget is 20);
- it **allows** the restart when the dashboard cannot be read at all — a guard
  that cannot see must not become a deploy lock;
- it **refuses** only when it can see an active run that outlasts the whole wait
  budget.

Needs a way in: ``PROSPER_TEAM_ID`` plus either ``PROSPER_DASHBOARD_COOKIE`` or
``PROSPER_DASHBOARD_EMAIL``/``PROSPER_DASHBOARD_PASSWORD``. Without them the
status is "unknown" and the guard allows the restart.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
import time

import httpx
from loguru import logger

DEFAULT_BASE_URL = "https://hackspain.getprosperapp.com/leaderboard"

#: How long the guard will hold a restart back, and how often it re-asks. A full
#: Run All is around 18 minutes (`SC-cooldown-run`), so 20 minutes covers it with
#: room for a queue.
DEFAULT_WAIT_SECS = 20 * 60
DEFAULT_POLL_SECS = 30

EXIT_ALLOWED = 0
EXIT_REFUSED = 3


def _env(name: str, default: str | None = None) -> str | None:
    value = os.getenv(name, default)
    return value or None


class ProsperDashboard:
    """The team's own dashboard view: enough of it to know if a run is dialling."""

    def __init__(self, *, base_url=None, team_id=None, cookie=None, email=None,
                 password=None, client=None, timeout=10.0):
        self.base_url = (base_url or _env("PROSPER_DASHBOARD_URL", DEFAULT_BASE_URL)).rstrip("/")
        self.team_id = team_id or _env("PROSPER_TEAM_ID")
        self.cookie = cookie or _env("PROSPER_DASHBOARD_COOKIE")
        self.email = email or _env("PROSPER_DASHBOARD_EMAIL")
        self.password = password or _env("PROSPER_DASHBOARD_PASSWORD")
        self._client = client
        self._timeout = timeout

    async def _session(self, client) -> bool:
        """Sign in, unless a session cookie was handed to us directly."""
        if self.cookie:
            client.headers["Cookie"] = self.cookie
            return True
        if not (self.email and self.password):
            return False
        response = await client.post(
            f"{self.base_url}/api/session",
            json={"email": self.email, "password": self.password},
        )
        if response.status_code >= 400:
            logger.warning("Prosper sign-in failed with HTTP {}", response.status_code)
            return False
        return True

    async def active_run(self) -> bool | None:
        """``True`` while a scored run is dialling, ``False`` when idle, else ``None``.

        ``None`` is "could not tell" and is never treated as an active run: the
        guard's job is to protect a run, not to become a reason the endpoint
        stops shipping.
        """
        if not self.team_id:
            logger.warning("PROSPER_TEAM_ID is not set: run status unknown")
            return None
        client = self._client or httpx.AsyncClient(timeout=self._timeout)
        try:
            if not await self._session(client):
                return None
            response = await client.get(f"{self.base_url}/api/teams/{self.team_id}")
            if response.status_code >= 400:
                logger.warning("Prosper team lookup failed with HTTP {}", response.status_code)
                return None
            payload = response.json()
        except Exception as exc:
            logger.warning("Prosper status unreadable: {}", type(exc).__name__)
            return None
        finally:
            if self._client is None:
                await client.aclose()
        eligibility = payload.get("eligibility") or {}
        active = eligibility.get("active_run")
        return bool(active) if isinstance(active, bool) else None


async def wait_until_idle(
    dashboard: ProsperDashboard,
    *,
    wait_secs: float = DEFAULT_WAIT_SECS,
    poll_secs: float = DEFAULT_POLL_SECS,
    sleep=asyncio.sleep,
    clock=time.monotonic,
) -> int:
    """Hold the restart back until no run is dialling, or the budget runs out."""
    deadline = clock() + wait_secs
    while True:
        active = await dashboard.active_run()
        if active is not True:
            if active is None:
                logger.warning("Run status unknown: allowing the restart")
            else:
                logger.info("No scored run active: restart may proceed")
            return EXIT_ALLOWED
        if clock() + poll_secs > deadline:
            logger.error("A scored run is still active after {}s: refusing", wait_secs)
            return EXIT_REFUSED
        logger.warning("A scored run is active: holding the restart for {}s", poll_secs)
        await sleep(poll_secs)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--force", action="store_true", help="skip the check (CI/deploy scripts only)")
    parser.add_argument("--wait-secs", type=float, default=DEFAULT_WAIT_SECS)
    parser.add_argument("--poll-secs", type=float, default=DEFAULT_POLL_SECS)
    parser.add_argument("--json", action="store_true", help="print the status as JSON")
    args = parser.parse_args(argv)
    if args.force:
        logger.warning("--force: restarting without asking whether a run is active")
        return EXIT_ALLOWED
    status = asyncio.run(
        wait_until_idle(
            ProsperDashboard(), wait_secs=args.wait_secs, poll_secs=args.poll_secs
        )
    )
    if args.json:
        print(json.dumps({"exit": status, "refused": status == EXIT_REFUSED}))
    return status


if __name__ == "__main__":
    sys.exit(main())
