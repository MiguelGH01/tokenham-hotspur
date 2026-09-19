"""FastAPI routes for the local observability console (REST + live WebSocket)."""

from __future__ import annotations

import json
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from loguru import logger

from observability.events import ObsEvent
from observability.hub import get_hub
from observability.store import shift_start_iso

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
CONSOLE_DIR = Path(__file__).resolve().parent / "console"


def mount_observability_routes(app: FastAPI) -> None:
    """Register /observability/* on the Pipecat runner FastAPI app."""

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

    @app.get("/observability/health")
    async def observability_health():
        store = await hub.ensure_ready()
        shift = await store.shift_summary(since=shift_start_iso())
        return {
            "ok": True,
            "live_calls": shift["live_calls"],
            "shift_calls": shift["calls"],
        }

    @app.get("/observability/protocol")
    async def observability_protocol():
        return {"nodes": hub.protocol_nodes}

    @app.get("/observability/shift")
    async def get_shift(since: str | None = Query(default=None)):
        store = await hub.ensure_ready()
        return await store.shift_summary(since=since or shift_start_iso())

    @app.get("/observability/calls")
    async def list_calls(
        since: str | None = Query(default=None),
        include: str = Query(default="all", pattern="^(all|real|test)$"),
    ):
        store = await hub.ensure_ready()
        calls = await store.list_calls(since=since or shift_start_iso(), include=include)
        return {"calls": calls}

    @app.get("/observability/calls/{call_id}")
    async def get_call(call_id: str):
        store = await hub.ensure_ready()
        detail = await store.get_call(call_id)
        if not detail:
            raise HTTPException(status_code=404, detail="call_not_found")
        return detail

    @app.post("/observability/fixtures/{name}/load")
    async def load_fixture(name: str, clear: bool = True):
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
        await websocket.accept()
        store = await hub.ensure_ready()
        queue = hub.subscribe()
        try:
            shift = await store.shift_summary(since=shift_start_iso())
            await websocket.send_json(
                {
                    "kind": "snapshot",
                    "calls": await store.list_calls(since=shift_start_iso()),
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

    # Same origin as the API and the WebRTC offer endpoint: no CORS, no build.
    if CONSOLE_DIR.is_dir():
        app.mount("/console", _NoCacheStatic(directory=CONSOLE_DIR, html=True), name="console")


class _NoCacheStatic(StaticFiles):
    """Serve the console without caching.

    A reload has to pick up an edited app.js; a browser holding the previous
    one silently runs stale code, which looks like the fix never landed.
    """

    def file_response(self, *args, **kwargs) -> Response:
        response = super().file_response(*args, **kwargs)
        response.headers["Cache-Control"] = "no-store, must-revalidate"
        return response
