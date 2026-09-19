"""The call's action list, delivered until the platform accepts it.
One call submits **a list**: ``record.actions: [...]`` is the documented response
shape, and two problems need more than one verb in a single call (cancel plus
book, or two intents stacked). The list is ordered and immutable once an action
is decided — a correction replaces the plan, it never rewrites history.

Delivery is the last place a case can still be lost. A call with no accepted
record scores nothing, so an action is retried until the platform accepts it and
the caller is told the truth when it does not.

Two entry points, and the difference matters:

- :meth:`flush` delivers only what the call has actually decided. A background
  retry loop uses it, and it never invents a decision.
- :meth:`close` is the end of the call. Only here does an undecided call fall
  back to a reasoned refusal, because an empty list scores nothing while a
  wrong-but-stated refusal at least states something. That fallback belongs to
  the **call**, not to each intent: a call that asked three things and decided
  none of them states one refusal, not three. That fallback belongs to
  the **call**, not to each intent: a call that asked three things and decided
  none of them states one refusal, not three.

**A refusal the conversation can still revise is not a decision.** A call that
says "there is nothing free in that window" and then finds a slot once the
caller drops a constraint has to be able to submit the booking — not the
refusal it already posted five seconds into the call. So ``set_no_action`` is
*provisional* by default, the retry loop delivers only decided actions, and
:meth:`decide` promotes the plan at the moment the conversation stops being able
to change its mind. ``close()`` still delivers a provisional refusal, so a call
that simply ends keeps the guarantee that it never submits nothing.
"""

import asyncio
from copy import deepcopy

from loguru import logger

import audit

#: What a call that never decided anything still has to submit. Silence is
#: never cheaper than a stated answer, so the fallback is the broadest
#: non-rule ending in the closed vocabulary.
DEFAULT_ACTION = {"action": "NO_ACTION", "reason": "out_of_scope"}

#: The offer fields a reschedule carries. The route takes no
#: ``appointment_type_id``: moving an appointment keeps it.
RESCHEDULE_FIELDS = ("provider_id", "location_id", "slot", "policy_id")


def book_action(offer: dict) -> dict:
    return {"action": "BOOK", **offer}


def reschedule_action(appointment_id: str, offer: dict) -> dict:
    return {
        "action": "RESCHEDULE",
        "appointment_id": appointment_id,
        **{key: offer[key] for key in RESCHEDULE_FIELDS},
    }


def cancel_action(appointment_id: str) -> dict:
    return {"action": "CANCEL", "appointment_id": appointment_id}


def register_action(patient: dict) -> dict:
    return {"action": "REGISTER", **patient}


#: The confirmed writes, in one place: a retry compares the action the frozen
#: plan carries against the action being confirmed, and a second copy of each
#: shape would eventually disagree with the first.


class CallSubmission:
    """Ordered actions for one call, delivered in order and retried until taken."""

    def __init__(self, call_id, client):
        self.call_id = call_id
        self._client = client
        self._actions: list[dict] = []
        #: Parallel to ``_actions``: True while the action is still revisable.
        self._provisional: list[bool] = []
        self._delivered = 0
        self._attempted = False
        self._requests: list[CallSubmission] = []
        self._closed = False
        self._lock = asyncio.Lock()

    # --- the plan ---------------------------------------------------------

    @property
    def actions(self) -> list[dict]:
        """The ordered plan, as it would be submitted."""
        return deepcopy(self._actions) + [action for request in self._requests for action in request.actions]

    @property
    def pending(self) -> dict:
        """The action this call currently stands behind.

        Before anything is decided this reads as the fallback refusal, so a
        caller of this property always sees a specific answer rather than an
        empty plan. It is a copy: the plan is frozen once decided, and a reader
        must not be able to rewrite what will be delivered.
        """
        return deepcopy(self._actions[0]) if self._actions else dict(DEFAULT_ACTION)

    @property
    def delivery_attempted(self) -> bool:
        """True once any action was POSTed: the plan can no longer be rewritten.

        Not the same as accepted. A request that returned an error, a timeout or
        a 5xx may still have been recorded on the platform, so the payload that
        was sent is frozen and only that payload may be retried.
        """
        return self._attempted

    @property
    def needs_delivery(self) -> bool:
        """Unaccepted decided actions remain, even after partial acceptance."""
        return bool(
            self._outstanding(release_provisional=self._closed, fallback=False)
        ) or any(request.needs_delivery for request in self._requests)

    def carries(self, action: dict) -> bool:
        """Whether the plan this call will submit already holds this exact action.

        The question a late confirmation asks: a retry of what is already
        decided is safe, a different action cannot become the record any more.
        """
        return action in self.actions

    def new_request(self):
        """An independent intent cannot overwrite a previous request's effects."""
        if self._closed:
            raise RuntimeError("The call has already ended")
        request = CallSubmission(self.call_id, self._client)
        self._requests.append(request)
        return request
    def _reject_write_after_decision(self) -> None:
        if self._closed:
            raise RuntimeError("The call has already ended")
        if self._attempted:
            raise RuntimeError("Delivery has already been attempted")

    def _set_primary(self, action: dict, *, provisional: bool = False) -> None:
        self._reject_write_after_decision()
        self._actions = [deepcopy(action), *self._actions[1:]]
        self._provisional = [provisional, *self._provisional[1:]]
        audit.audit(
            self.call_id,
            "plan_set",
            verb=action.get("action"),
            reason=action.get("reason"),
            provisional=provisional,
        )

    def decide(self) -> None:
        """The conversation can no longer change its mind, so the plan is final.

        A terminal node calls this before it flushes: a refusal that has already
        been said out loud is the answer the call stands behind, and it must
        reach the platform. Promoting an already-decided plan is a no-op, and so
        is calling this with nothing decided.
        """
        self._provisional = [False] * len(self._provisional)

    def add_action(self, action: dict) -> None:
        """Append another verb to this call (PR-08 cancel plus book, PR-18)."""
        self._reject_write_after_decision()
        self._actions.append(deepcopy(action))
        self._provisional.append(False)

    def set_book(self, offer):
        self._set_primary(book_action(offer))

    def set_register(self, patient):
        self._set_primary(register_action(patient))

    def set_cancel(self, appointment_id):
        self._set_primary(cancel_action(appointment_id))

    def set_reschedule(self, appointment_id, offer):
        self._set_primary(reschedule_action(appointment_id, offer))

    def set_no_action(self, reason, *, provisional: bool = True):
        """State a refusal.

        Provisional by default, because the flows set this while the caller can
        still change the request — an ambiguous doctor they go on to clarify, a
        window they go on to widen. A terminal node promotes it with
        :meth:`decide` on its way out.
        """
        self._set_primary({"action": "NO_ACTION", "reason": reason}, provisional=provisional)

    def set_escalate(self, reason):
        self._set_primary({"action": "ESCALATE", "reason": reason})

    # --- delivery ---------------------------------------------------------

    def _outstanding(self, *, release_provisional: bool, fallback: bool) -> list[dict]:
        """This plan's unaccepted actions, in order, stopping at a live refusal.

        A provisional refusal is not a decision yet. Delivering it would freeze
        the record while the conversation is still able to produce a booking,
        and nothing behind it may overtake it either, so the run stops there
        rather than skipping ahead.

        ``fallback`` is the refusal a call with *nothing* decided still owes the
        platform (``SC-no-silence``), and it belongs to the call rather than to
        one intent: it is offered at most once, and only when the call is over.
        """
        if self._actions:
            pending: list[dict] = []
            for index, action in enumerate(self._actions):
                if index < self._delivered:
                    continue
                if not release_provisional and self._provisional[index]:
                    break
                pending.append(action)
            return pending
        if not fallback or self._delivered:
            return []
        return [dict(DEFAULT_ACTION)]

    async def flush(self) -> bool:
        """Deliver what the call has decided so far. Never invents an action."""
        accepted = await self._deliver(release_provisional=False, fallback=False)
        for request in self._requests:
            accepted = await request.flush() and accepted
        return accepted

    async def close(self) -> bool:
        """End of call: deliver the plan, or the one refusal that covers the call."""
        self._closed = True
        accepted = await self._deliver(release_provisional=True, fallback=False)
        for request in self._requests:
            accepted = await request._close_request() and accepted
        if not self._actions and not self._delivered_anything():
            # Silence is never cheaper than a stated answer, but a per-intent
            # request is not a call: the fallback is stated once, for the call.
            accepted = await self._deliver(release_provisional=True, fallback=True) and accepted
        return accepted

    async def _close_request(self) -> bool:
        """Close one intent of the call, without inventing the call's fallback."""
        self._closed = True
        return await self._deliver(release_provisional=True, fallback=False)

    def _delivered_anything(self) -> bool:
        return self._delivered > 0 or any(
            request._delivered_anything() for request in self._requests
        )

    async def _deliver(self, *, release_provisional: bool, fallback: bool) -> bool:
        """Deliver every outstanding action, in order. True when all were taken.

        Ordering matters: a cancel that must precede a book cannot be swapped,
        and a partially delivered plan resumes where it stopped rather than
        replaying an action the platform already recorded.

        ``call_id`` is a required field on every submit request, and the platform
        insists it is the id it dialled with rather than a fresh one, so it is
        stamped on here — the one place that owns both the plan and the id.
        """
        async with self._lock:
            for action in self._outstanding(
                release_provisional=release_provisional, fallback=fallback
            ):
                payload = {"call_id": self.call_id, **deepcopy(action)}
                audit.audit(
                    self.call_id,
                    "submission_attempt",
                    verb=action.get("action"),
                    payload={k: v for k, v in action.items()},
                    final=release_provisional,
                )
                self._attempted = True
                try:
                    await self._client.post_submission(payload)
                except Exception as exc:
                    logger.error(
                        "Submission failed for {}: {}",
                        action.get("action"),
                        exc,
                    )
                    audit.audit(
                        self.call_id,
                        "submission_result",
                        verb=action.get("action"),
                        ok=False,
                        error=type(exc).__name__,
                    )
                    return False
                self._delivered += 1
                logger.info("Submission accepted: {}", action.get("action"))
                audit.audit(
                    self.call_id,
                    "submission_result",
                    verb=action.get("action"),
                    ok=True,
                )
            return True
