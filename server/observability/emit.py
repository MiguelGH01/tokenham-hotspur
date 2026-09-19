"""Helpers for emitting justified tool/node events from flow handlers."""

from __future__ import annotations

import functools
from typing import Any, Callable

from pipecat.flows import FlowManager, NodeConfig

from observability.events import make_event, safe_tool_args
from observability.hub import get_hub


def current_node_name(flow_manager: FlowManager) -> str | None:
    node = getattr(flow_manager, "_current_node", None)
    if isinstance(node, str):
        return node
    if isinstance(node, dict):
        return node.get("name")
    return getattr(node, "name", None)


def node_name(node: NodeConfig | dict | None) -> str | None:
    if node is None:
        return None
    if isinstance(node, dict):
        return node.get("name")
    return getattr(node, "name", None)


async def emit_tool_call(flow_manager: FlowManager, name: str, args: dict[str, Any] | None) -> None:
    call_id = flow_manager.state.get("call_id")
    if not call_id:
        return
    await get_hub().emit(
        make_event(
            "tool.called",
            call_id,
            name=name,
            args=safe_tool_args(args),
            node=current_node_name(flow_manager),
        )
    )


async def emit_tool_result(
    flow_manager: FlowManager,
    *,
    name: str,
    status: str,
    next_node: NodeConfig | dict | None,
    justification: str,
    extra: dict[str, Any] | None = None,
) -> None:
    call_id = flow_manager.state.get("call_id")
    if not call_id:
        return
    hub = get_hub()
    nxt = node_name(next_node)
    payload: dict[str, Any] = {
        "name": name,
        "status": status,
        "next_node": nxt,
        "justification": justification,
        "from_node": current_node_name(flow_manager),
    }
    if extra:
        payload.update(extra)
    await hub.emit(make_event("tool.returned", call_id, **payload))
    if nxt:
        await hub.emit(
            make_event(
                "node.entered",
                call_id,
                **{"from": current_node_name(flow_manager), "to": nxt},
            )
        )


async def emit_state_patch(flow_manager: FlowManager, **fields: Any) -> None:
    call_id = flow_manager.state.get("call_id")
    if not call_id:
        return
    await get_hub().emit(make_event("state.patched", call_id, **fields))


async def emit_node_entered(call_id: str, *, to: str, from_node: str | None = None) -> None:
    await get_hub().emit(make_event("node.entered", call_id, **{"from": from_node, "to": to}))


async def emit_action_queued(
    call_id: str,
    *,
    action: str,
    reason: str | None = None,
    summary: str | None = None,
    seq: int | None = None,
    payload: dict[str, Any] | None = None,
) -> None:
    kwargs: dict[str, Any] = {"action": action}
    if reason is not None:
        kwargs["reason"] = reason
    if summary is not None:
        kwargs["summary"] = summary
    if seq is not None:
        kwargs["seq"] = seq
    if payload is not None:
        kwargs["payload"] = payload
    await get_hub().emit(make_event("action.queued", call_id, **kwargs))


async def emit_submit_posted(
    call_id: str,
    *,
    action: str,
    http_status: int | None = None,
    ok: bool = True,
    reason: str | None = None,
    error: str | None = None,
) -> None:
    kwargs: dict[str, Any] = {"action": action, "ok": ok}
    if http_status is not None:
        kwargs["http_status"] = http_status
    if reason is not None:
        kwargs["reason"] = reason
    if error is not None:
        kwargs["error"] = error
    await get_hub().emit(make_event("submit.posted", call_id, **kwargs))


def trace_tool(name: str | None = None) -> Callable:
    """Decorator: emit tool.called / tool.returned around a flow handler.

    Handlers return ``(result, next_node)`` or just a result. When ``result`` is a
    dict, ``status`` and optional ``reason_codes`` / ``justification`` are lifted
    into the returned event.
    """

    def decorator(fn: Callable) -> Callable:
        tool_name = name or fn.__name__

        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            # Flow handlers are ``(args, flow_manager)`` or ``(flow_manager,)``.
            flow_manager: FlowManager | None = None
            call_args: dict[str, Any] | None = None
            for arg in args:
                if isinstance(arg, FlowManager):
                    flow_manager = arg
                elif isinstance(arg, dict):
                    call_args = arg
            if flow_manager is None:
                flow_manager = kwargs.get("flow_manager")
            if call_args is None and "args" in kwargs and isinstance(kwargs["args"], dict):
                call_args = kwargs["args"]

            if flow_manager is not None:
                await emit_tool_call(flow_manager, tool_name, call_args)

            result = await fn(*args, **kwargs)

            if flow_manager is None:
                return result

            next_node = None
            payload: dict[str, Any] = {}
            if isinstance(result, tuple) and len(result) == 2:
                payload_or_result, next_node = result
                if isinstance(payload_or_result, dict):
                    payload = payload_or_result
            elif isinstance(result, dict):
                payload = result

            status = str(payload.get("status") or ("ok" if next_node or payload else "done"))
            reason_codes = payload.get("reason_codes") or []
            justification = payload.get("justification") or (
                f"{tool_name} returned {status}"
                + (f"; next={node_name(next_node)}" if next_node else "")
            )
            await emit_tool_result(
                flow_manager,
                name=tool_name,
                status=status,
                next_node=next_node if not isinstance(next_node, type(None)) else None,
                justification=justification,
                extra={"reason_codes": reason_codes} if reason_codes else None,
            )
            return result

        return wrapper

    return decorator
