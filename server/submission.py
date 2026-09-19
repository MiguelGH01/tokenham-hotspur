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
import os
from copy import deepcopy

from loguru import logger

import audit
from observability.events import safe_action_payload


def _schedule(factory) -> None:
    """Fire-and-forget an observability emit from a sync setter.

    ``factory`` is a zero-arg callable that returns the awaitable, so we do not
    construct a coroutine when there is no running loop.
    """
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _safe():
        try:
            await factory()
        except Exception:
            logger.debug("observability emit skipped")

    loop.create_task(_safe())


async def _emit_queued(call_id: str, action: dict, *, seq: int) -> None:
    from observability.emit import emit_action_queued

    await emit_action_queued(
        call_id,
        action=str(action.get("action")),
        reason=action.get("reason"),
        seq=seq,
        payload=safe_action_payload(action),
    )


async def _emit_posted(
    call_id: str,
    action: dict,
    *,
    http_status: int | None,
    ok: bool,
    error: str | None = None,
) -> None:
    from observability.emit import emit_submit_posted

    await emit_submit_posted(
        call_id,
        action=str(action.get("action")),
        http_status=http_status,
        ok=ok,
        reason=action.get("reason"),
        error=error,
    )
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


def cancel_action(appointment_id: str, *, appointment: dict | None = None) -> dict:
    """Cancel one diary row.

    Optional ``appointment`` adds provider/slot for the doctor calendar only;
    :meth:`ClinicClient.post_submission` strips those before the clinic POST.
    """
    action: dict = {"action": "CANCEL", "appointment_id": appointment_id}
    if not appointment:
        return action
    if appointment.get("provider_id"):
        action["provider_id"] = appointment["provider_id"]
    if appointment.get("location_id"):
        action["location_id"] = appointment["location_id"]
    slot = appointment.get("start_time") or appointment.get("slot")
    if slot:
        action["slot"] = slot
    if appointment.get("appointment_type_id"):
        action["appointment_type_id"] = appointment["appointment_type_id"]
    if appointment.get("duration_minutes") is not None:
        action["duration_minutes"] = appointment["duration_minutes"]
    return action


def register_action(patient: dict) -> dict:
    return {"action": "REGISTER", **patient}


#: The confirmed writes, in one place: a retry compares the action the frozen
#: plan carries against the action being confirmed, and a second copy of each
#: shape would eventually disagree with the first.


class CallSubmission:
    """Ordered actions for one call, delivered in order and retried until taken."""

    def __init__(self, call_id, client, *, fallback=None, fallback_timeout_secs=None):
        self.call_id = call_id
        self._client = client
        #: Asked once, when the call ends having decided nothing: see
        #: ``_resolve_fallback`` and ``resolution.py``.
        self._fallback = fallback
        self._fallback_timeout_secs = (
            fallback_timeout_secs
            if fallback_timeout_secs is not None
            else float(os.getenv("FALLBACK_TIMEOUT_SECS", "12"))
        )
        self._fallback_decision: dict | None = None
        self._actions: list[dict] = []
        #: Parallel to ``_actions``: True while the action is still revisable.
        self._provisional: list[bool] = []
        self._delivered = 0
        self._attempted = False
        self._requests: list[CallSubmission] = []
        self._closed = False
        self._lock = asyncio.Lock()
        #: Slot read back to the caller but not yet confirmed. A hang-up after
        #: the offer (the `flow/` graph still uses this) submits it as BOOK
        #: rather than inventing a refusal.
        self._offered: dict | None = None

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
        return bool(self._outstanding(release_provisional=self._closed)) or any(
            request.needs_delivery for request in self._requests
        )

    def carries(self, action: dict) -> bool:
        """Whether the plan this call will submit already holds this exact action.

        The question a late confirmation asks: a retry of what is already
        decided is safe, a different action cannot become the record any more.
        CANCEL compares on ``appointment_id`` only — calendar fields on the
        stored row must not make a retry look like a different decision.
        """
        if action.get("action") == "CANCEL":
            target = action.get("appointment_id")
            return any(
                a.get("action") == "CANCEL" and a.get("appointment_id") == target
                for a in self.actions
            )
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
        _schedule(lambda: _emit_queued(self.call_id, action, seq=1))

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
        _schedule(lambda: _emit_queued(self.call_id, action, seq=len(self._actions)))

    def set_offer(self, offer: dict) -> None:
        """Remember a slot that was spoken but not yet confirmed.

        A later confirmed book wins; a later explicit refusal cancels it. If
        the call ends on the offer, :meth:`close` submits it as BOOK.
        """
        self._offered = deepcopy(offer)
        if (
            self._actions
            and self._actions[0].get("action") == "NO_ACTION"
            and self._provisional[:1] == [True]
        ):
            self._actions = self._actions[1:]
            self._provisional = self._provisional[1:]

    def clear_offer(self) -> None:
        """The caller declined the live offer: never submit it as a hang-up book."""
        self._offered = None

    def set_book(self, offer):
        self._offered = None
        self._set_primary(book_action(offer))

    def set_register(self, patient):
        self._set_primary(register_action(patient))

    def set_cancel(self, appointment_id, *, appointment: dict | None = None):
        self._set_primary(cancel_action(appointment_id, appointment=appointment))

    def set_reschedule(self, appointment_id, offer):
        self._set_primary(reschedule_action(appointment_id, offer))

    def set_no_action(self, reason, *, provisional: bool = True):
        """State a refusal.

        Provisional by default, because the flows set this while the caller can
        still change the request — an ambiguous doctor they go on to clarify, a
        window they go on to widen. A terminal node promotes it with
        :meth:`decide` on its way out.
        """
        self._offered = None
        self._set_primary({"action": "NO_ACTION", "reason": reason}, provisional=provisional)

    def set_escalate(self, reason):
        self._set_primary({"action": "ESCALATE", "reason": reason})

    # --- delivery ---------------------------------------------------------

    def _outstanding(self, *, release_provisional: bool) -> list[dict]:
        """This plan's unaccepted actions, in order, stopping at a live refusal.

        A provisional refusal is not a decision yet. Delivering it would freeze
        the record while the conversation is still able to produce a booking,
        and nothing behind it may overtake it either, so the run stops there
        rather than skipping ahead.
        """
        pending: list[dict] = []
        for index, action in enumerate(self._actions):
            if index < self._delivered:
                continue
            if not release_provisional and self._provisional[index]:
                break
            pending.append(action)
        return pending

    def _plan_exists(self) -> bool:
        """Whether anything at all is left to send, this plan's or a request's."""
        return bool(self._outstanding(release_provisional=True)) or any(
            request._plan_exists() for request in self._requests
        )

    async def _resolve_fallback(self) -> dict:
        """The call's last action, from the resolver the session wired in.

        Never raises and never blocks the close: a resolver that fails or hangs
        leaves the unscored refusal, which is still a stated answer. Asked once,
        so a repeated ``close()`` retries that same ending instead of searching
        again.
        """
        if self._fallback_decision is not None:
            return deepcopy(self._fallback_decision)
        action = dict(DEFAULT_ACTION)
        if self._fallback is not None:
            try:
                resolved = await asyncio.wait_for(
                    self._fallback(), timeout=self._fallback_timeout_secs
                )
            except Exception as exc:
                logger.error("Fallback resolution failed: {}", type(exc).__name__)
                audit.audit(
                    self.call_id,
                    "submission_result",
                    verb="FALLBACK",
                    ok=False,
                    error=type(exc).__name__,
                )
            else:
                if resolved:
                    action = dict(resolved)
        self._fallback_decision = action
        return deepcopy(action)

    async def flush(self) -> bool:
        """Deliver what the call has decided so far. Never invents an action."""
        accepted = await self._deliver(self._outstanding(release_provisional=False), final=False)
        for request in self._requests:
            accepted = await request.flush() and accepted
        return accepted

    async def close(self) -> bool:
        """End of call: deliver the plan, or the one ending that covers the call."""
        self._closed = True
        accepted = await self._deliver(self._outstanding(release_provisional=True), final=True)
        for request in self._requests:
            accepted = await request._close_request() and accepted
        if self._plan_exists() or self._delivered_anything():
            return accepted
        if self._offered:
            return await self._deliver([book_action(self._offered)], final=True) and accepted
        # Silence is never cheaper than a stated answer, but "nothing decided"
        # is not the same as "nothing known": the call resolves the best ending
        # it can still stand behind before settling for the unscored refusal.
        # One ending per call, not one per intent.
        return await self._deliver([await self._resolve_fallback()], final=True) and accepted

    async def _close_request(self) -> bool:
        """Close one intent of the call, without inventing the call's fallback."""
        self._closed = True
        return await self._deliver(self._outstanding(release_provisional=True), final=True)

    def _delivered_anything(self) -> bool:
        return self._delivered > 0 or any(
            request._delivered_anything() for request in self._requests
        )

    async def _deliver(self, pending: list[dict], *, final: bool) -> bool:
        """Deliver every outstanding action, in order. True when all were taken.

        Ordering matters: a cancel that must precede a book cannot be swapped,
        and a partially delivered plan resumes where it stopped rather than
        replaying an action the platform already recorded.

        Each action is retried on the same payload until a bounded number of
        attempts is spent. Retrying the exact payload is the one safe retry:
        once an attempt was POSTed the plan is frozen to it (see
        ``delivery_attempted``), and the platform's 409 answers a duplicate as
        already accepted. The seconds a closing call has left are worth more
        than the certainty of an undelivered record — the one way a decided,
        correct case still scores nothing.

        A permanently failed action still blocks the actions behind it: the
        list is ordered (a cancel before a book), so skipping ahead could
        submit an ending the platform would record in the wrong order.

        ``call_id`` is a required field on every submit request, and the platform
        insists it is the id it dialled with rather than a fresh one, so it is
        stamped on here — the one place that owns both the plan and the id.
        """
        attempts = max(1, int(os.getenv("SUBMIT_DELIVERY_ATTEMPTS", "3")))
        backoff = max(0.0, float(os.getenv("SUBMIT_DELIVERY_BACKOFF_SECS", "1.0")))
        async with self._lock:
            for action in pending:
                # CANCEL may carry provider/slot for the doctor calendar; the
                # clinic route only accepts appointment_id (+ call_id).
                deliver = action
                if action.get("action") == "CANCEL":
                    deliver = {
                        "action": "CANCEL",
                        "appointment_id": action["appointment_id"],
                    }
                payload = {"call_id": self.call_id, **deepcopy(deliver)}
                for attempt in range(1, attempts + 1):
                    audit.audit(
                        self.call_id,
                        "submission_attempt",
                        verb=action.get("action"),
                        payload={k: v for k, v in deliver.items()},
                        final=final,
                        attempt=attempt,
                    )
                    self._attempted = True
                    try:
                        await self._client.post_submission(payload)
                    except Exception as exc:
                        logger.error(
                            "Submission failed for {} (attempt {}/{}): {}",
                            action.get("action"),
                            attempt,
                            attempts,
                            exc,
                        )
                        # A ClinicApiError carries the HTTP status; recording it
                        # is the difference between "retry was hopeless" and
                        # "retry never had time" when a scored run is dissected.
                        status = getattr(exc, "status", None)
                        failure = {
                            "verb": action.get("action"),
                            "ok": False,
                            "error": type(exc).__name__,
                        }
                        if status is not None:
                            failure["status"] = status
                        audit.audit(self.call_id, "submission_result", **failure)
                        if attempt == attempts:
                            await _emit_posted(
                                self.call_id,
                                action,
                                http_status=status,
                                ok=False,
                                error=type(exc).__name__,
                            )
                            return False
                        await asyncio.sleep(backoff * attempt)
                    else:
                        break
                self._delivered += 1
                logger.info("Submission accepted: {}", action.get("action"))
                audit.audit(
                    self.call_id,
                    "submission_result",
                    verb=action.get("action"),
                    ok=True,
                )
                await _emit_posted(
                    self.call_id,
                    action,
                    http_status=200,
                    ok=True,
                )
            return True
