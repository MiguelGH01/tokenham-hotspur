"""The deploy guard: restarting the endpoint must not kill a scored run.

A killed call is a scored call, so anything that restarts the bot asks the
dashboard first. The posture is asserted here in all three directions: hold back
while a run is active, allow the restart when the status cannot be read at all,
and refuse only when an active run outlasts the whole wait budget.
"""

import asyncio
import importlib

import httpx

from prosper_guard import EXIT_ALLOWED, EXIT_REFUSED, ProsperDashboard, wait_until_idle


def _client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _team(active):
    return httpx.Response(
        200, json={"eligibility": {"active_run": active}}, request=httpx.Request("GET", "http://x")
    )


def _dashboard(handler, **kwargs):
    return ProsperDashboard(
        base_url="https://dashboard.invalid/leaderboard",
        team_id="T1",
        cookie="prosper_dashboard=abc",
        client=_client(handler),
        **kwargs,
    )


def test_an_active_run_holds_the_restart_back():
    calls = []

    def handler(request):
        calls.append(str(request.url))
        return _team(True)

    sleeps = []
    now = {"t": 0.0}

    async def sleep(secs):
        sleeps.append(secs)
        now["t"] += secs

    status = asyncio.run(
        wait_until_idle(
            _dashboard(handler),
            wait_secs=60,
            poll_secs=30,
            sleep=sleep,
            clock=lambda: now["t"],
        )
    )
    assert status == EXIT_REFUSED
    assert sleeps == [30, 30]
    assert all(url.endswith("/api/teams/T1") for url in calls)


def test_an_idle_dashboard_allows_the_restart():
    def handler(request):
        return _team(False)

    assert asyncio.run(
        wait_until_idle(_dashboard(handler), wait_secs=60, sleep=_never)
    ) == EXIT_ALLOWED


def test_a_run_that_finishes_inside_the_budget_allows_the_restart():
    seen = {"count": 0}

    def handler(request):
        seen["count"] += 1
        return _team(seen["count"] == 1)

    sleeps = []
    now = {"t": 0.0}

    async def sleep(secs):
        sleeps.append(secs)
        now["t"] += secs

    assert asyncio.run(
        wait_until_idle(
            _dashboard(handler),
            wait_secs=120,
            poll_secs=30,
            sleep=sleep,
            clock=lambda: now["t"],
        )
    ) == EXIT_ALLOWED
    assert sleeps == [30]
    assert seen["count"] == 2


def test_an_unreadable_dashboard_allows_the_restart():
    """A guard that cannot see must not become a deploy lock."""

    def handler(request):
        raise httpx.ConnectError("no route to host")

    assert asyncio.run(
        wait_until_idle(_dashboard(handler), wait_secs=60, sleep=_never)
    ) == EXIT_ALLOWED


def test_a_non_boolean_status_reads_as_unknown():
    def handler(request):
        return httpx.Response(
            200, json={"eligibility": {}}, request=httpx.Request("GET", "http://x")
        )

    assert asyncio.run(_dashboard(handler).active_run()) is None


def test_a_missing_team_id_reads_as_unknown(monkeypatch):
    monkeypatch.delenv("PROSPER_TEAM_ID", raising=False)
    dashboard = ProsperDashboard(base_url="https://x.invalid", client=_client(_team(True)))
    assert asyncio.run(dashboard.active_run()) is None


def test_a_session_cookie_is_used_without_signing_in():
    requests = []

    def handler(request):
        requests.append((request.method, str(request.url), request.headers.get("cookie")))
        return _team(False)

    asyncio.run(_dashboard(handler).active_run())
    assert requests == [("GET", "https://dashboard.invalid/leaderboard/api/teams/T1", "prosper_dashboard=abc")]


def test_sign_in_is_attempted_when_only_credentials_are_given():
    requests = []

    def handler(request):
        requests.append((request.method, str(request.url)))
        if request.method == "POST":
            return httpx.Response(200, json={"ok": True}, request=request)
        return _team(False)

    dashboard = ProsperDashboard(
        base_url="https://dashboard.invalid/leaderboard",
        team_id="T1",
        email="team@example.com",
        password="secret",
        client=_client(handler),
    )
    assert asyncio.run(dashboard.active_run()) is False
    assert requests == [
        ("POST", "https://dashboard.invalid/leaderboard/api/session"),
        ("GET", "https://dashboard.invalid/leaderboard/api/teams/T1"),
    ]


def test_force_skips_the_check(monkeypatch):
    module = importlib.import_module("prosper_guard")

    def explode(*args, **kwargs):
        raise AssertionError("--force must not ask the dashboard anything")

    monkeypatch.setattr(module, "ProsperDashboard", explode)
    assert module.main(["--force"]) == EXIT_ALLOWED


async def _never(secs):
    raise AssertionError("this test should not need to wait")
