"""Submit the plan the caller just agreed to, on the affirmation itself.

The model is told to call ``confirm_offer`` after a yes, and it usually does —
one LLM round trip later, on a three-minute clock, and a turn it can skip
entirely when the conversation is already at its limit. This watches the turns
instead: a short, unqualified yes in a turn of the caller's own, landing on the
agent's read-back question, runs the same confirmation handler the model would
have called, right there.

Where the trigger comes from is load-bearing. It is **not** the pipeline's
frames: the user aggregator consumes the final ``TranscriptionFrame`` rather than
forwarding it, and an agent turn that is a bare tool call emits no text frame at
all, so a frame-path watcher silently misses the very yes it exists for. The
trigger is the aggregator's own ``on_user_turn_message_added`` event — the moment
the finalized caller turn is written to the context — wired in ``bot.py``.

Three things keep it safe:

- it only fires when the call is **holding an action** the caller was read back
  (``resolution.prepared_action``), so there is never anything to send that the
  conversation had not already drawn up;
- it calls the flow's own confirm handler, so the deterministic gate
  (``flows.common.gated_confirmation``) still judges the utterance: this is not a
  second, weaker consent path, it is the same one, earlier;
- the handler is idempotent: a submission that already left is a retry, and the
  platform answers the duplicate with a 409 that counts as accepted.

See ``docs/scoring-design-notes.md`` for why the trigger has to be the
aggregator's turn event rather than a pipeline frame watcher.
"""

from __future__ import annotations

import asyncio

from loguru import logger

import audit
import confirmation
import resolution
from llm_messages import chat_role, chat_text


class AffirmationWatch:
    """One per call. Decides, and submits off the turn path."""

    def __init__(self, flow_manager=None):
        self._flow_manager = flow_manager
        #: The (agent question, caller answer) pair already acted on, so one turn
        #: fires once however many events it raises.
        self._acted_on: tuple[str, str] | None = None
        self._tasks: set[asyncio.Task] = set()

    def bind(self, flow_manager) -> None:
        """Attach the conversation this watcher reads, once it exists."""
        self._flow_manager = flow_manager

    # --- the trigger ------------------------------------------------------

    def consider(self, user_text: str | None = None) -> None:
        """A caller turn has landed: submit the plan it just agreed to, if it did."""
        handler = self.pending_submission(user_text)
        if handler is not None:
            self._spawn(self.submit_confirmation(handler))

    def _spawn(self, coro) -> None:
        """Run the submission off the turn path, and keep hold of the task.

        Awaiting an HTTP POST inside the aggregator's event would hold the
        caller's own audio behind it, so it goes out as a task whose failure is
        logged rather than raised into the pipeline.
        """
        try:
            task = asyncio.get_running_loop().create_task(coro, name="affirmation-submit")
        except RuntimeError:  # no loop: nothing can be sent from here
            coro.close()
            return
        self._tasks.add(task)
        task.add_done_callback(self._finished)

    def _finished(self, task: asyncio.Task) -> None:
        self._tasks.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.debug("affirmation submission ended with an exception")

    # --- the decision -----------------------------------------------------

    def _messages(self) -> list[dict]:
        if self._flow_manager is None:
            return []
        try:
            return list(self._flow_manager.get_current_context())
        except Exception:  # no context aggregator: nothing to watch
            return []

    def pending_submission(self, user_text: str | None = None):
        """The confirmation this turn earns, or ``None``.

        Synchronous on purpose: the turn is claimed here, before anything is
        awaited, and returning the handler instead of running it is what makes
        the decision testable without a running pipeline.
        """
        if self._flow_manager is None:
            return None
        state = self._flow_manager.state
        submission = state.get("submission")
        if submission is None or submission.delivery_attempted:
            return None
        messages = self._messages()
        assistant = [chat_text(m) or "" for m in messages if chat_role(m) == "assistant"]
        assistant_text = assistant[-1] if assistant else ""
        utterance = user_text if user_text is not None else self._last_user_turn(messages)
        if not utterance or not confirmation.is_short_clean_yes(utterance):
            return None
        action = resolution.prepared_action(state)
        if action is None:
            return None
        if not confirmation.looks_like_confirmation_question(assistant_text, prepared=True):
            return None
        if (assistant_text, utterance) == self._acted_on:
            return None
        handler = self._confirm_handler(state)
        if handler is None:
            return None
        self._acted_on = (assistant_text, utterance)
        audit.audit(
            state.get("call_id", "unknown"),
            "affirmation_submit",
            verb=action["action"],
            utterance=utterance,
        )
        return handler

    @staticmethod
    def _last_user_turn(messages: list) -> str:
        for message in reversed(messages):
            if chat_role(message) == "user":
                return chat_text(message) or ""
        return ""

    def _confirm_handler(self, state):
        """The handler the model would have called, with its own arguments.

        Dispatching here rather than building the action twice is what keeps one
        confirmation path: whatever the model's call would have validated and
        submitted is what this submits.
        """
        from flows.appointments import confirm_cancellation
        from flows.booking import confirm_offer
        from flows.registration import confirm_registration

        if state.get("intent") == "cancel":
            return lambda: confirm_cancellation({}, self._flow_manager)
        held = resolution.live_offer(state)
        if held is not None:
            return lambda: confirm_offer({"offer_id": held[0]}, self._flow_manager)
        if state.get("registration_draft"):
            return lambda: confirm_registration({"confirmed": True}, self._flow_manager)
        return None

    async def submit_confirmation(self, handler) -> None:
        """Run the confirmation, never letting the watcher break a call."""
        try:
            await handler()
        except Exception as exc:
            logger.error("Affirmation submit failed: {}", type(exc).__name__)
            audit.audit(
                self._flow_manager.state.get("call_id", "unknown"),
                "affirmation_submit_failed",
                error=type(exc).__name__,
            )
