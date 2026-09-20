"""Switch between the Pipecat bot (carloslabs) and the ElevenLabs bridge (elevenagent).

When ``VOICE_AGENT=elevenagent``, Prosper Twilio Media Streams on ``/ws`` are
proxied to a local Node sidecar, tool webhooks are reverse-proxied under
``/tools/*``, and the sidecar posts CallHub events to ``/internal/obs/events``
so the observability console looks the same as for carloslabs.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import secrets
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from loguru import logger
from pydantic import BaseModel, Field

from observability.emit import emit_node_entered
from observability.events import (
    make_event,
    safe_action_payload,
    safe_tool_args,
)
from observability.hub import get_hub

REPO_ROOT = Path(__file__).resolve().parent.parent
ELEVENAGENT_DIR = REPO_ROOT / "elevenagent"

VOICE_AGENT_CARLOSLABS = "carloslabs"
VOICE_AGENT_ELEVENAGENT = "elevenagent"
VALID_VOICE_AGENTS = frozenset({VOICE_AGENT_CARLOSLABS, VOICE_AGENT_ELEVENAGENT})

# Heuristic node trail when ElevenLabs has no Pipecat flow graph.
_TOOL_TO_NODE: dict[str, str] = {
    "search-patient": "identify",
    "search_patient": "identify",
    "nearest-site": "find_slot",
    "nearest_site": "find_slot",
    "find_nearest_site": "find_slot",
    "book": "confirm",
    "reschedule": "confirm",
    "cancel": "cancel_confirm",
    "register": "registration",
    "no-action": "no_booking",
    "no_action": "no_booking",
    "escalate": "refused",
}

_sidecar_proc: subprocess.Popen | None = None
_ingest_token: str = ""
_sidecar_port: int = 3000


def voice_agent() -> str:
    raw = (os.getenv("VOICE_AGENT") or VOICE_AGENT_CARLOSLABS).strip().lower()
    if raw not in VALID_VOICE_AGENTS:
        logger.warning("Unknown VOICE_AGENT={!r}; falling back to {}", raw, VOICE_AGENT_CARLOSLABS)
        return VOICE_AGENT_CARLOSLABS
    return raw


def is_elevenagent() -> bool:
    return voice_agent() == VOICE_AGENT_ELEVENAGENT


def _cli_transport() -> str | None:
    """Return the ``-t`` / ``--transport`` value from argv, if any."""
    argv = sys.argv[1:]
    for i, arg in enumerate(argv):
        if arg in {"-t", "--transport"} and i + 1 < len(argv):
            return argv[i + 1].strip().lower()
        if arg.startswith("--transport="):
            return arg.split("=", 1)[1].strip().lower()
    return None


def _should_enable_elevenagent_bridge() -> bool:
    """Enable sidecar for Prosper telephony and console Place-test-call WebRTC.

    Eval stays on the Pipecat cascade regardless of VOICE_AGENT.
    """
    if not is_elevenagent():
        return False
    transport = _cli_transport()
    if transport is None:
        # No -t: runner serves every transport — bridge /ws and WebRTC Place-test-call.
        return True
    return transport in {"twilio", "telnyx", "plivo", "exotel", "webrtc"}


def _sidecar_port_from_env() -> int:
    return int(os.getenv("ELEVENAGENT_PORT") or "3000")


def _agent_id() -> str:
    return (os.getenv("ELEVEN_AGENT_ID") or os.getenv("AGENT_ID") or "").strip()


def _ensure_elevenagent_config() -> None:
    if not _agent_id():
        raise RuntimeError(
            "VOICE_AGENT=elevenagent requires ELEVEN_AGENT_ID (or AGENT_ID) in the environment"
        )
    if not os.getenv("ELEVENLABS_API_KEY"):
        raise RuntimeError("VOICE_AGENT=elevenagent requires ELEVENLABS_API_KEY")
    if not os.getenv("CLINIC_API_KEY"):
        raise RuntimeError("VOICE_AGENT=elevenagent requires CLINIC_API_KEY")
    if not (ELEVENAGENT_DIR / "src" / "server.js").is_file():
        raise RuntimeError(f"elevenagent bridge not found at {ELEVENAGENT_DIR}")


async def _wait_for_health(port: int, *, timeout_secs: float = 20.0) -> None:
    deadline = time.monotonic() + timeout_secs
    url = f"http://127.0.0.1:{port}/health"
    async with httpx.AsyncClient(timeout=2.0) as client:
        while time.monotonic() < deadline:
            try:
                response = await client.get(url)
                if response.status_code == 200:
                    return
            except (httpx.HTTPError, OSError):
                pass
            await asyncio.sleep(0.25)
    raise RuntimeError(f"elevenagent sidecar did not become healthy on port {port}")


def _spawn_sidecar(port: int, ingest_url: str, ingest_token: str) -> subprocess.Popen:
    env = os.environ.copy()
    env["PORT"] = str(port)
    env["AGENT_ID"] = _agent_id()
    env["OBS_INGEST_URL"] = ingest_url
    env["OBS_INGEST_TOKEN"] = ingest_token
    # Prefer the server's .env clinic settings; Node also loads its own dotenv if present.
    if "CLINIC_API_BASE_URL" not in env or not env["CLINIC_API_BASE_URL"]:
        env["CLINIC_API_BASE_URL"] = "https://hackspain.getprosperapp.com/api"

    node_modules = ELEVENAGENT_DIR / "node_modules"
    if not node_modules.is_dir():
        logger.info("Installing elevenagent npm dependencies…")
        subprocess.run(
            ["npm", "install", "--omit=dev"],
            cwd=str(ELEVENAGENT_DIR),
            check=True,
            env=env,
        )

    logger.info("Starting elevenagent sidecar on 127.0.0.1:{}", port)
    return subprocess.Popen(
        ["node", "src/server.js"],
        cwd=str(ELEVENAGENT_DIR),
        env=env,
        stdout=None,
        stderr=None,
        start_new_session=True,
    )


def _stop_sidecar(proc: subprocess.Popen | None) -> None:
    if proc is None:
        return
    if proc.poll() is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except (ProcessLookupError, PermissionError, OSError):
        proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            proc.kill()
        proc.wait(timeout=2)


class IngestEventBody(BaseModel):
    kind: str
    call_id: str = Field(min_length=1)
    ts: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


async def ingest_obs_event(body: IngestEventBody) -> dict[str, str]:
    """Apply one observability event from the elevenagent sidecar into the CallHub."""
    kind = body.kind
    call_id = body.call_id
    payload = dict(body.payload or {})
    hub = get_hub()

    if kind == "call.started":
        await hub.start_call(
            call_id,
            transport=payload.get("transport") or "twilio",  # type: ignore[arg-type]
            from_number=payload.get("from_number"),
            is_test=bool(payload.get("is_test")),
        )
        if payload.get("emit_reception", True):
            await emit_node_entered(call_id, to="reception")
        return {"ok": "started"}

    if kind == "call.ended":
        conversation_id = payload.get("conversation_id")
        await hub.end_call(call_id)
        try:
            from observability.elevenlabs_history import get_elevenlabs_history

            get_elevenlabs_history().schedule_final_fetch(call_id, conversation_id)
        except Exception:
            logger.debug("elevenlabs final fetch not scheduled")
        return {"ok": "ended"}

    if kind == "eleven.bound":
        conversation_id = str(payload.get("conversation_id") or "").strip()
        if not conversation_id:
            raise HTTPException(status_code=400, detail="missing conversation_id")
        store = await hub.ensure_ready()
        await store.set_eleven_conversation_id(call_id, conversation_id)
        try:
            from observability.elevenlabs_history import get_elevenlabs_history

            get_elevenlabs_history().bind(call_id, conversation_id)
        except Exception:
            logger.debug("elevenlabs history bind skipped")
        return {"ok": "bound"}

    # Validate kind against the known set (TypedDict Literal is not runtime-checkable).
    known: set[str] = {
        "call.started",
        "call.ended",
        "transcript.user",
        "transcript.bot",
        "transcript.user_interim",
        "node.entered",
        "tool.called",
        "tool.returned",
        "state.patched",
        "action.queued",
        "submit.posted",
        "metrics.first_word",
    }
    if kind not in known:
        raise HTTPException(status_code=400, detail=f"unknown event kind: {kind}")

    if kind == "tool.called":
        if "args" in payload and isinstance(payload["args"], dict):
            payload["args"] = safe_tool_args(payload["args"])
        tool_name = str(payload.get("name") or "")
        node = _TOOL_TO_NODE.get(tool_name) or _TOOL_TO_NODE.get(tool_name.replace("_", "-"))
        if node:
            await emit_node_entered(call_id, to=node)

    if kind == "action.queued":
        raw_payload = payload.get("payload")
        if isinstance(raw_payload, dict):
            payload["payload"] = safe_action_payload(raw_payload)
        elif payload:
            # Allow flat bodies; wrap for the store's doctor-inbox projection.
            action_body = {k: v for k, v in payload.items() if k not in {"action", "verb", "seq", "summary", "reason"}}
            if action_body and "payload" not in payload:
                payload["payload"] = safe_action_payload(action_body)

    event = make_event(kind, call_id, **payload)  # type: ignore[arg-type]
    if body.ts:
        event["ts"] = body.ts
    await hub.emit(event)
    return {"ok": "emitted"}


async def _proxy_websocket_to_sidecar(client_ws: WebSocket, port: int) -> None:
    """Bidirectional proxy: Prosper ↔ localhost elevenagent /ws."""
    import websockets
    from websockets.exceptions import ConnectionClosed

    upstream_url = f"ws://127.0.0.1:{port}/ws"
    try:
        async with websockets.connect(upstream_url, open_timeout=10, max_size=8 * 1024 * 1024) as upstream:
            async def client_to_upstream() -> None:
                try:
                    while True:
                        message = await client_ws.receive()
                        if message.get("type") == "websocket.disconnect":
                            break
                        text = message.get("text")
                        data = message.get("bytes")
                        if text is not None:
                            await upstream.send(text)
                        elif data is not None:
                            await upstream.send(data)
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.debug("client→sidecar closed: {}", exc)

            async def upstream_to_client() -> None:
                try:
                    async for message in upstream:
                        if isinstance(message, bytes):
                            await client_ws.send_bytes(message)
                        else:
                            await client_ws.send_text(message)
                except ConnectionClosed:
                    pass
                except Exception as exc:
                    logger.debug("sidecar→client closed: {}", exc)

            done, pending = await asyncio.wait(
                [
                    asyncio.create_task(client_to_upstream()),
                    asyncio.create_task(upstream_to_client()),
                ],
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            for task in done:
                with contextlib.suppress(Exception):
                    task.result()
    except Exception as exc:
        logger.error("elevenagent websocket proxy failed: {}", exc)
        try:
            await client_ws.close(code=1011)
        except Exception:
            pass


async def _proxy_tools_http(request: Request, path: str, port: int) -> Response:
    target = f"http://127.0.0.1:{port}/tools/{path}"
    if request.url.query:
        target = f"{target}?{request.url.query}"
    body = await request.body()
    headers = {
        key: value
        for key, value in request.headers.items()
        if key.lower() not in {"host", "content-length", "transfer-encoding", "connection"}
    }
    async with httpx.AsyncClient(timeout=30.0) as client:
        upstream = await client.request(
            request.method,
            target,
            content=body,
            headers=headers,
        )
    excluded = {"content-encoding", "transfer-encoding", "connection"}
    response_headers = {
        key: value for key, value in upstream.headers.items() if key.lower() not in excluded
    }
    return Response(
        content=upstream.content,
        status_code=upstream.status_code,
        headers=response_headers,
        media_type=upstream.headers.get("content-type"),
    )


def _patch_telephony_runner(port: int) -> None:
    """Replace Pipecat's telephony bot entry with a WS proxy to the sidecar."""
    import pipecat.runner.run as runner_mod

    async def _run_telephony_bot_proxy(websocket: WebSocket, args: Any) -> None:
        logger.info("VOICE_AGENT=elevenagent — proxying /ws to sidecar :{}", port)
        await _proxy_websocket_to_sidecar(websocket, port)

    runner_mod._run_telephony_bot = _run_telephony_bot_proxy  # type: ignore[attr-defined]
    logger.info("Patched pipecat.runner.run._run_telephony_bot → elevenagent proxy")


def mount_voice_agent(app: FastAPI) -> None:
    """Wire the voice-agent switch onto the Pipecat FastAPI app.

    Call this after ``mount_observability_routes`` and before ``main()``.
    """
    global _ingest_token, _sidecar_port

    agent = voice_agent()
    logger.info("VOICE_AGENT={}", agent)
    if not _should_enable_elevenagent_bridge():
        if agent == VOICE_AGENT_ELEVENAGENT:
            logger.info(
                "VOICE_AGENT=elevenagent but transport={!r} — keeping Pipecat for this process",
                _cli_transport(),
            )
        return

    _ensure_elevenagent_config()
    _sidecar_port = _sidecar_port_from_env()
    _ingest_token = os.getenv("OBS_INGEST_TOKEN") or secrets.token_urlsafe(24)
    # So a manually-started sidecar can share the token if needed.
    os.environ["OBS_INGEST_TOKEN"] = _ingest_token

    _patch_telephony_runner(_sidecar_port)

    @app.post("/internal/obs/events")
    async def internal_obs_events(request: Request, body: IngestEventBody):
        token = request.headers.get("x-obs-token") or ""
        if not _ingest_token or len(token) != len(_ingest_token) or not secrets.compare_digest(
            token, _ingest_token
        ):
            raise HTTPException(status_code=401, detail="invalid_token")
        try:
            return await ingest_obs_event(body)
        except HTTPException:
            raise
        except Exception as exc:
            # Never let observation break the call path on the Node side — still
            # surface a 500 so the sidecar can log, but swallow nothing critical.
            logger.warning("obs ingest failed: {}", exc)
            raise HTTPException(status_code=500, detail="ingest_failed") from exc

    @app.api_route("/tools/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"])
    async def tools_proxy(path: str, request: Request):
        return await _proxy_tools_http(request, path, _sidecar_port)

    # Startup/shutdown around the existing FastAPI app. Pipecat already has
    # routes; we layer startup via on_event so we do not replace its lifespan.
    @app.on_event("startup")
    async def _start_elevenagent_sidecar():
        global _sidecar_proc
        port = _sidecar_port
        # Pipecat's main() sets RUNNER_PORT before uvicorn starts; fall back to 7860.
        try:
            from pipecat.runner import run as runner_mod

            runner_port = int(getattr(runner_mod, "RUNNER_PORT", None) or 7860)
        except Exception:
            runner_port = 7860
        ingest_url = f"http://127.0.0.1:{runner_port}/internal/obs/events"

        _sidecar_proc = _spawn_sidecar(port, ingest_url, _ingest_token)
        try:
            await _wait_for_health(port)
            logger.info("elevenagent sidecar healthy; Prosper /ws and Place-test-call WebRTC bridge ready")
        except Exception:
            _stop_sidecar(_sidecar_proc)
            _sidecar_proc = None
            raise
        from observability.elevenlabs_history import get_elevenlabs_history

        get_elevenlabs_history().start()
        logger.info("ElevenLabs conversation history poller started")

    @app.on_event("shutdown")
    async def _stop_elevenagent_sidecar():
        global _sidecar_proc
        from observability.elevenlabs_history import get_elevenlabs_history

        await get_elevenlabs_history().aclose()
        _stop_sidecar(_sidecar_proc)
        _sidecar_proc = None
        logger.info("elevenagent sidecar stopped")
