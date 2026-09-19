"""Escalate a red flag the model did not notice.

``flag_emergency`` (``flows/rails.py``) is a tool, so it only fires when the model
chooses to call it, and a missed red flag ends in a booking. This watcher asks Jev
(TypeSafe AI's yes/no classifier) about every caller turn on its own, and on a yes
runs that same tool: one escalation path, reached a second way.

Same trigger as ``affirmation_watch.py``: the aggregator's
``on_user_turn_message_added`` event, wired in ``bot.py``. The check runs as a task
beside the model's own turn (~300 ms against the model's 1-2 s), so the caller
waits for nothing. Jev slow, down or unconfigured means the watcher does nothing
and the model alone decides, which is how the bot behaved before this file.

Measured 19-Sep on twelve Spanish and English phrases: red flags scored 0.95-0.98,
ordinary complaints ("me duele el pecho al toser") 0.01-0.07.
"""

from __future__ import annotations

import asyncio
import os

import httpx
from loguru import logger
from pipecat.frames.frames import InterruptionFrame

import audit
from flows.rails import flag_emergency
from llm_messages import chat_role, chat_text

#: The five published combinations (PR-10), the same ones the model is shown in
#: ``flows.common.RED_FLAG_EXAMPLES``.
QUESTION = {
    "type": "noul",
    "instructions": (
        "A caller is phoning a clinic. Is the caller describing a medical emergency happening "
        "now: chest tightness with trouble breathing; face droop, weak arm or slurred speech; "
        "sudden breathlessness that stops them mid-sentence; a cut still bleeding after ten "
        "minutes of pressure; or a bang to the head with confusion and vomiting?"
    ),
    "criteria": {
        "true": "one of those emergencies is described",
        "false": "anything else: fever, dizziness, a cough, a fall, wanting an appointment today",
    },
}

#: A red flag can arrive in pieces ("me aprieta el pecho" / "y me cuesta respirar").
_TURNS_ASKED = 3


def _threshold() -> float:
    return float(os.getenv("EMERGENCY_THRESHOLD", "0.5"))


async def ask_jev(client: httpx.AsyncClient, text: str) -> float | None:
    """How sure Jev is that ``text`` is a red flag, or ``None`` when it cannot say."""
    try:
        response = await client.post(
            "/v1/systemone",
            json={
                "state": text,
                "model": os.getenv("TYPESAFE_MODEL", "jev-latest"),
                "questions": {"emergency": QUESTION},
            },
        )
        response.raise_for_status()
        return float(response.json()["answers"]["emergency"]["noul"])
    except (httpx.HTTPError, KeyError, TypeError, ValueError) as exc:
        logger.warning("Jev gave no answer: {}", type(exc).__name__)
        return None


class EmergencyWatch:
    """One per call. Off when there is no ``TYPESAFE_API_KEY``."""

    def __init__(self, call_id: str, ask=None):
        self._call_id = call_id
        self._client: httpx.AsyncClient | None = None
        if ask is None and (key := os.getenv("TYPESAFE_API_KEY", "").strip()):
            self._client = httpx.AsyncClient(
                base_url=os.getenv("TYPESAFE_BASE_URL", "https://api.typesafe.ai"),
                headers={"Authorization": f"Bearer {key}"},
                timeout=float(os.getenv("EMERGENCY_TIMEOUT_SECS", "1.5")),
            )
            client = self._client
            ask = lambda text: ask_jev(client, text)  # noqa: E731
        self._ask = ask
        self._flow_manager = None
        self._worker = None
        self._escalated = False
        self._tasks: set[asyncio.Task] = set()

    @property
    def enabled(self) -> bool:
        return self._ask is not None

    def bind(self, flow_manager, worker) -> None:
        self._flow_manager = flow_manager
        self._worker = worker

    def _spawn(self, coro, name: str) -> None:
        """Off the turn path, like ``AffirmationWatch._spawn``: never hold the caller's audio."""
        task = asyncio.get_running_loop().create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def pick_up(self) -> None:
        """Open the connection as the call connects: the TLS handshake costs a cold check a second."""
        if self._client is not None:
            self._spawn(self._warm_up(), "emergency-warm-up")

    async def _warm_up(self) -> None:
        try:
            await self._client.get("/v1/models")
        except httpx.HTTPError as exc:
            logger.warning("Jev warm-up failed: {}", type(exc).__name__)

    def consider(self) -> None:
        """A caller turn has landed: check it beside the model's turn, not before it."""
        if not self.enabled or self._escalated or self._flow_manager is None:
            return
        self._spawn(self.check(), "emergency-check")

    def _heard(self) -> str:
        turns = [
            chat_text(m) or ""
            for m in self._flow_manager.get_current_context()
            if chat_role(m) == "user"
        ]
        return " ".join(t for t in turns[-_TURNS_ASKED:] if t)

    async def check(self) -> None:
        """Ask, and escalate on a yes. Never lets the watcher break a call."""
        try:
            text = self._heard()
            probability = await self._ask(text) if text else None
            logger.debug("Emergency watch: Jev says {}", probability)
            if probability is None or probability < _threshold() or self._escalated:
                return
            if getattr(self._flow_manager, "current_node", None) == "emergency":
                return  # the model got there first
            self._escalated = True
            audit.audit(self._call_id, "emergency_watch", probability=probability, heard=text)
            # The model may already be answering this turn with an offer: cut it. The frame
            # itself, not InterruptionWorkerFrame: that one tours the pipeline before it
            # becomes an interruption, lands after the emergency node's LLMRunFrame and
            # flushes it (seen live: the caller heard only the holding line).
            await self._worker.queue_frames([InterruptionFrame()])
            _, node = await flag_emergency({}, self._flow_manager)
            await self._flow_manager.set_node_from_config(node)
        except Exception as exc:
            logger.error("Emergency watch failed: {}", type(exc).__name__)
            audit.audit(self._call_id, "emergency_watch_failed", error=type(exc).__name__)

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
