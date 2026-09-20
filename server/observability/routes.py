"""FastAPI routes for the local observability console (REST + live WebSocket)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger
from pydantic import BaseModel, Field
from starlette.responses import Response as StarletteResponse

import reception_notices
from observability.auth import (
    COOKIE_NAME,
    cookie_to_session,
    is_admin,
    resolve_key,
    session_response,
    session_to_cookie,
)
from observability.elevenlabs_history import (
    elevenlabs_history_configured,
    get_elevenlabs_history,
)
from observability.events import ObsEvent
from observability.hub import get_hub
from observability.insights import (
    MAX_INSIGHTS,
    extract_call_insights,
    validate_insight_body,
)
from observability.store import resolve_period_bound, shift_start_iso

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CONSOLE_DIR = Path(__file__).resolve().parent / "console"

_COOKIE_MAX_AGE = 60 * 60 * 12  # half a shift day


class LoginBody(BaseModel):
    key: str = Field(min_length=1)


class InsightCreateBody(BaseModel):
    name: str
    description: str
    values: list[str]


def _read_session(request: Request) -> dict[str, Any] | None:
    return cookie_to_session(request.cookies.get(COOKIE_NAME))


def require_admin(request: Request) -> dict[str, Any]:
    session = _read_session(request)
    if not is_admin(session):
        raise HTTPException(status_code=401, detail="admin_required")
    return session  # type: ignore[return-value]


def require_provider(request: Request) -> dict[str, Any]:
    session = _read_session(request)
    if session is None or session.get("role") != "provider":
        raise HTTPException(status_code=401, detail="provider_required")
    return session


def _set_session_cookie(response: Response, session: dict[str, Any]) -> None:
    response.set_cookie(
        key=COOKIE_NAME,
        value=session_to_cookie(session),
        httponly=True,
        samesite="lax",
        max_age=_COOKIE_MAX_AGE,
        path="/",
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(key=COOKIE_NAME, path="/")


def mount_observability_routes(app: FastAPI) -> None:
    """Register /auth/* and /observability/* on the Pipecat runner FastAPI app."""

    # Vite dev server is a different origin; keep this local-only.
    app.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://localhost:5173",
            "http://127.0.0.1:5173",
            "http://localhost:7860",
            "http://127.0.0.1:7860",
        ],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    hub = get_hub()

    @app.on_event("startup")
    async def _observability_startup():
        await hub.ensure_ready()

    @app.post("/auth/login")
    async def auth_login(body: LoginBody, response: Response):
        session = resolve_key(body.key)
        if session is None:
            raise HTTPException(status_code=401, detail="invalid_key")
        _set_session_cookie(response, session)
        return session_response(session)

    @app.post("/auth/logout")
    async def auth_logout(response: Response):
        _clear_session_cookie(response)
        return {"ok": True}

    @app.get("/auth/me")
    async def auth_me(request: Request):
        session = _read_session(request)
        if session is None:
            raise HTTPException(status_code=401, detail="not_authenticated")
        try:
            return session_response(session)
        except KeyError:
            raise HTTPException(status_code=401, detail="not_authenticated") from None

    @app.get("/auth/me/calendar")
    async def auth_me_calendar(
        request: Request,
        week_start: str | None = Query(default=None),
        days: int = Query(default=6, ge=1, le=14),
    ):
        """Week calendar for the logged-in provider (free vs booked)."""
        from datetime import date as date_cls
        from datetime import timedelta

        from clinic_catalog import load_catalog
        from observability.provider_calendar import (
            assemble_calendar,
            calendar_window,
            fetch_provider_free_slots,
        )
        from rules import provider_by_id

        session = require_provider(request)
        provider = provider_by_id(load_catalog(), session["id"])
        if provider is None:
            raise HTTPException(status_code=401, detail="not_authenticated")

        if week_start:
            try:
                anchor = date_cls.fromisoformat(week_start)
            except ValueError as exc:
                raise HTTPException(status_code=422, detail="bad_week_start") from exc
            date_from = anchor - timedelta(days=anchor.weekday())
            date_to = date_from + timedelta(days=days - 1)
        else:
            date_from, date_to = calendar_window(days=days)

        free_slots, source = await fetch_provider_free_slots(
            provider["id"], date_from, date_to
        )
        store = await hub.ensure_ready()
        diary = await store.list_provider_bookings(
            provider["id"],
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        )
        overlays = await store.list_provider_overlays(
            provider["id"],
            date_from=date_from.isoformat(),
            date_to=date_to.isoformat(),
        )
        return assemble_calendar(
            provider,
            date_from=date_from,
            date_to=date_to,
            free_slots=free_slots,
            bot_bookings=diary["bookings"],
            cancellations=diary["cancellations"],
            overlays=overlays,
            source=source,
        )

    @app.get("/auth/me/inbox")
    async def auth_me_inbox(request: Request):
        """Doctor mailbox: cancellations and emergencies for this provider."""
        session = require_provider(request)
        store = await hub.ensure_ready()
        notes = await store.list_notifications(session["id"])
        unread = await store.count_unread_notifications(session["id"])
        return {"unread": unread, "notifications": notes}

    @app.post("/auth/me/inbox/{notification_id}/read")
    async def auth_me_inbox_read(notification_id: int, request: Request):
        session = require_provider(request)
        store = await hub.ensure_ready()
        ok = await store.mark_notification_read(session["id"], notification_id)
        if not ok:
            # Already read or wrong owner — still 200 with current unread.
            pass
        unread = await store.count_unread_notifications(session["id"])
        return {"ok": True, "unread": unread}

    @app.post("/auth/me/inbox/read-all")
    async def auth_me_inbox_read_all(request: Request):
        session = require_provider(request)
        store = await hub.ensure_ready()
        marked = await store.mark_all_notifications_read(session["id"])
        return {"ok": True, "marked": marked, "unread": 0}

    @app.get("/observability/health")
    async def observability_health(_admin: dict = Depends(require_admin)):
        store = await hub.ensure_ready()
        shift = await store.shift_summary(since=shift_start_iso())
        return {
            "ok": True,
            "live_calls": shift["live_calls"],
            "shift_calls": shift["calls"],
        }

    @app.get("/observability/protocol")
    async def observability_protocol(_admin: dict = Depends(require_admin)):
        return {"nodes": hub.protocol_nodes}

    @app.get("/observability/shift")
    async def get_shift(
        _admin: dict = Depends(require_admin),
        period: str = Query(default="today", pattern="^(today|week|month|year|all)$"),
        since: str | None = Query(default=None),
    ):
        store = await hub.ensure_ready()
        resolved, bound = resolve_period_bound(period, since)
        return await store.shift_summary(since=bound, period=resolved)

    @app.get("/observability/calls")
    async def list_calls(
        _admin: dict = Depends(require_admin),
        period: str = Query(default="today", pattern="^(today|week|month|year|all)$"),
        since: str | None = Query(default=None),
        include: str = Query(default="all", pattern="^(all|real|test)$"),
    ):
        store = await hub.ensure_ready()
        _, bound = resolve_period_bound(period, since)
        if elevenlabs_history_configured():
            calls = await get_elevenlabs_history().list_console_calls(
                store, since=bound, include=include
            )
        else:
            calls = await store.list_calls(since=bound, include=include)
        return {"calls": calls}

    @app.get("/observability/calls/{call_id}")
    async def get_call(call_id: str, _admin: dict = Depends(require_admin)):
        store = await hub.ensure_ready()
        if elevenlabs_history_configured():
            detail = await get_elevenlabs_history().get_console_call(store, call_id)
        else:
            detail = await store.get_call(call_id)
        if not detail:
            raise HTTPException(status_code=404, detail="call_not_found")
        return detail

    @app.post("/observability/calls/{call_id}/insights")
    async def recompute_call_insights(
        call_id: str, _admin: dict = Depends(require_admin)
    ):
        store = await hub.ensure_ready()
        defs = await store.list_insight_defs()
        if not defs:
            raise HTTPException(status_code=422, detail="no_insights_defined")
        status = await extract_call_insights(call_id, store=store, emitter=hub)
        if status == "skipped_eval":
            raise HTTPException(status_code=422, detail="eval_transport")
        if elevenlabs_history_configured():
            detail = await get_elevenlabs_history().get_console_call(store, call_id)
        else:
            detail = await store.get_call(call_id)
        if not detail:
            raise HTTPException(status_code=404, detail="call_not_found")
        return {"status": status, **detail}

    @app.get("/observability/insights")
    async def list_insights(_admin: dict = Depends(require_admin)):
        store = await hub.ensure_ready()
        return {"insights": await store.list_insight_defs()}

    @app.post("/observability/insights")
    async def create_insight(body: InsightCreateBody, _admin: dict = Depends(require_admin)):
        normalized, error = validate_insight_body(body.model_dump())
        if error:
            raise HTTPException(status_code=422, detail=error)
        assert normalized is not None
        store = await hub.ensure_ready()
        count = await store.count_insight_defs()
        if count >= MAX_INSIGHTS:
            raise HTTPException(
                status_code=422, detail=f"at most {MAX_INSIGHTS} insights"
            )
        existing = await store.list_insight_defs()
        if any(d["name"] == normalized["name"] for d in existing):
            raise HTTPException(status_code=422, detail="name: already exists")
        try:
            created = await store.create_insight_def(
                name=normalized["name"],
                description=normalized["description"],
                values=normalized["values"],
            )
        except Exception as exc:
            # UNIQUE race on name
            if "UNIQUE" in str(exc).upper():
                raise HTTPException(status_code=422, detail="name: already exists") from exc
            raise
        return created

    @app.delete("/observability/insights/{insight_id}")
    async def delete_insight(insight_id: int, _admin: dict = Depends(require_admin)):
        store = await hub.ensure_ready()
        ok = await store.delete_insight_def(insight_id)
        if not ok:
            raise HTTPException(status_code=404, detail="insight_not_found")
        return {"ok": True, "id": insight_id}

    @app.post("/observability/fixtures/{name}/load")
    async def load_fixture(
        name: str,
        _admin: dict = Depends(require_admin),
        clear: bool = True,
    ):
        path = FIXTURES_DIR / f"{name}.jsonl"
        if not path.is_file():
            raise HTTPException(status_code=404, detail="fixture_not_found")
        events: list[ObsEvent] = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            events.append(json.loads(line))
        count = await hub.load_fixture_events(events, clear=clear)
        logger.info("Loaded fixture {} ({} events)", name, count)
        store = await hub.ensure_ready()
        return {
            "loaded": count,
            "name": name,
            "calls": await store.list_calls(since=None),
            "shift": await store.shift_summary(since=shift_start_iso()),
        }

    @app.websocket("/observability/live")
    async def observability_live(websocket: WebSocket):
        session = cookie_to_session(websocket.cookies.get(COOKIE_NAME))
        if not is_admin(session):
            await websocket.close(code=1008)
            return
        await websocket.accept()
        store = await hub.ensure_ready()
        queue = hub.subscribe()
        try:
            shift = await store.shift_summary(since=shift_start_iso())
            if elevenlabs_history_configured():
                snapshot_calls = await get_elevenlabs_history().list_console_calls(
                    store, since=shift_start_iso(), include="all"
                )
            else:
                snapshot_calls = await store.list_calls(since=shift_start_iso())
            await websocket.send_json(
                {
                    "kind": "snapshot",
                    "calls": snapshot_calls,
                    "shift": shift,
                    "protocol_nodes": hub.protocol_nodes,
                }
            )
            while True:
                event = await queue.get()
                await websocket.send_json(event)
        except WebSocketDisconnect:
            pass
        except Exception as exc:
            logger.warning("Observability WS closed: {}", exc)
        finally:
            hub.unsubscribe(queue)

    # Reception's notices ride on the console's app: same origin, same port, so
    # the page that shows them is the page that writes them. Writing is admin
    # only — this port is what `make tunnel` exposes, and a notice changes which
    # appointments the agent will offer.
    reception_notices.mount_notices_routes(app, write_guard=require_admin)

    # Same origin as the API and the WebRTC offer endpoint: no CORS, no build.
    # Auth routes above must be registered before this mount.
    if CONSOLE_DIR.is_dir():
        app.mount("/console", _NoCacheStatic(directory=CONSOLE_DIR, html=True), name="console")


class _NoCacheStatic(StaticFiles):
    """Serve the console without caching.

    A reload has to pick up an edited app.js; a browser holding the previous
    one silently runs stale code, which looks like the fix never landed.
    """

    def file_response(self, *args, **kwargs) -> StarletteResponse:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response
