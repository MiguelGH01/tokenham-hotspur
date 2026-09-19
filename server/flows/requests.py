"""Per-request revisions and confirmation evidence, independent of LLM memory."""

from copy import deepcopy

from llm_messages import chat_fields, chat_role


def user_turns(manager):
    try:
        return [
            deepcopy(chat_fields(m))
            for m in manager.get_current_context()
            if chat_role(m) == "user"
        ]
    except Exception:
        return []


def revise_request(manager):
    """The request moved: every readback is now evidence about a different ask.

    Bumping the revision is what invalidates the proposals, not emptying the
    offer table: an offer handle is a record of what was read out loud, and the
    audit of a call needs it after the fact. A handle from an older revision is
    answered as ``expired`` rather than re-offered.
    """
    state = manager.state
    state["revision"] = state.get("revision", 0) + 1
    state.pop("proposal", None)
    state.pop("registration_draft", None)


def prepare_proposal(manager, key):
    state = manager.state
    state["proposal"] = {
        "key": key,
        "revision": state.get("revision", 0),
        "patient": deepcopy(state.get("patient")),
        "turns": user_turns(manager),
    }


def _answered_in_a_later_turn(manager, proposal) -> bool:
    turns = user_turns(manager)
    snapshot = proposal.get("turns") or []
    return len(turns) > len(snapshot) and turns[: len(snapshot)] == snapshot


def proposal_status(manager, key) -> str:
    """Where a prepared handle stands: ``ok``, ``unconfirmed`` or ``stale``.

    - ``ok`` — still the live handle for this request, and the caller has
      answered it in a turn of their own since it was read back.
    - ``unconfirmed`` — still the live handle, but the readback is not consent
      yet: the confirmation has to come in a later turn of the caller's own, which
      is what stops the model confirming its own offer in the same breath.
    - ``stale`` — nothing current stands behind the handle any more: the request
      was revised, or another proposal replaced it. It must be replaced by a
      fresh search, never re-offered.
    """
    state = manager.state
    proposal = state.get("proposal")
    if (
        not proposal
        or proposal.get("key") != key
        or proposal.get("revision") != state.get("revision", 0)
        or proposal.get("patient") != state.get("patient")
    ):
        return "stale"
    return "ok" if _answered_in_a_later_turn(manager, proposal) else "unconfirmed"


def valid_proposal(manager, key) -> bool:
    return proposal_status(manager, key) == "ok"


def live_keys(manager) -> list[str]:
    """Handles the current request still stands behind, confirmation or not.

    What a readback node may offer: the handle prepared for this revision and
    patient either is the one awaiting an answer or already has it.
    """
    state = manager.state
    proposal = state.get("proposal")
    if not proposal:
        return []
    if proposal.get("revision") != state.get("revision", 0) or proposal.get(
        "patient"
    ) != state.get("patient"):
        return []
    return [proposal["key"]]


def begin_request(manager, intent):
    state = manager.state
    root = state.setdefault("call_submission", state.get("submission"))
    requests = state.setdefault("requests", [])
    if state.get("request_id"):
        requests.append({key: deepcopy(state.get(key)) for key in (
            "request_id", "intent", "patient", "revision", "proposal"
        )})
    state["request_id"] = f"request-{len(requests) + 1}"
    state["intent"] = intent
    state["patient"] = None
    state["identify_attempts"] = 0
    state.pop("appointment", None)
    state.pop("appointments", None)
    if root is not None:
        state["submission"] = root.new_request()
    revise_request(manager)
