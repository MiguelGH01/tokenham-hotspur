"""Pipecat Flows nodes: prompt + which tools are allowed at this stage.

A node does not implement clinic logic. It only tells the LLM what to ask
and which functions from ``flow.tools`` it may call.
"""

from pipecat.flows import FlowManager, NodeConfig

from clinic_catalog import provider_roster
from flow.prompts import ROLE_MESSAGE
from flow.tools import (
    begin_register,
    confirm_offer_schema,
    decline_register,
    flush_submission,
    get_earliest_slot_schema,
    register_patient_schema,
    refuse_unlisted_provider,
    revise_search,
    search_patient_schema,
)


def create_identify_node() -> NodeConfig:
    return NodeConfig(
        name="identify",
        role_message=ROLE_MESSAGE,
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "You have already greeted the caller: do not greet again or re-introduce the "
                    "clinic. Establish the patient's full name and ONE exact identifier: their DNI "
                    "or NIE including the letter, or their phone number. Ask for everything that is "
                    "missing in a single short question, then call search_patient. Never search by "
                    "name alone. If the result is misheard_id, say you could not find them and ask "
                    "them to repeat the identifier slowly, digit by digit — that id was not even "
                    "well-formed. If the result is not_found instead, the identifier was valid but "
                    "matched nobody: follow the next instruction you are given rather than asking "
                    "them to repeat it, since repeating it again will not help."
                ),
            }
        ],
        # The fixed greeting is queued by bot.py after initialize(): as a tts_say pre_action,
        # a caller barging into it drops Flows' ActionFinishedFrame and the node never loads.
        respond_immediately=False,
        functions=[search_patient_schema()],
    )


def create_slot_node(flow_manager: FlowManager) -> NodeConfig:
    patient = flow_manager.state["patient"]
    connected_at = flow_manager.state["connected_at"]
    return NodeConfig(
        name="find_slot",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    f"The patient is {patient['given_name']} {patient['first_surname']}. This call "
                    f"connected on {connected_at.strftime('%A %d %B %Y')} — resolve any relative or "
                    "named date the caller gives against that, not against any other assumed date.\n\n"
                    "Find out which specialty they need, mapping their words to one of the allowed "
                    "values. Only pass site, weekday or part_of_day if the caller asked for them.\n\n"
                    f"Known doctors by specialty — {provider_roster()}.\n"
                    "If the caller names a doctor who is on that list, pass their name exactly as "
                    "`provider`; a named doctor's own schedule decides the day, so do not also pass "
                    "weekday or part_of_day then. If the caller names a doctor who is NOT on that "
                    "list, say so and ask whether another doctor in that specialty would do — do not "
                    "call get_earliest_slot yet. If they refuse anyone else, call "
                    "refuse_unlisted_provider.\n\n"
                    "If the caller names an exact calendar day (e.g. 'the twelfth of October'), pass "
                    "it as `date` (YYYY-MM-DD, using the call's own year above unless they name a "
                    "different one); for a relative or recurring weekday ('this Thursday', 'Saturday "
                    "morning') use `weekday`/`part_of_day` instead — never both.\n\n"
                    "Then call get_earliest_slot. If it returns no_slots, say nothing is available for "
                    "that request and ask whether they would drop a constraint. If lookup_failed, "
                    "apologise and ask them to call back shortly. If the result includes a note, weave "
                    "it into the offer briefly (why a different doctor, day, or plan applies)."
                ),
            }
        ],
        functions=[get_earliest_slot_schema(), refuse_unlisted_provider],
    )


def create_confirm_node(flow_manager: FlowManager) -> NodeConfig:
    return NodeConfig(
        name="confirm",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Offer the appointment from the summary the tool returned: doctor, site, day "
                    "and time. If the result included a note, mention it briefly first. Ask if that "
                    "works. Nothing is booked until they say yes. On yes, call confirm_offer. If they "
                    "want something different, call revise_search."
                ),
            }
        ],
        functions=[confirm_offer_schema(flow_manager), revise_search],
    )


def create_goodbye_node() -> NodeConfig:
    return NodeConfig(
        name="goodbye",
        task_messages=[
            {
                "role": "developer",
                "content": "Confirm the appointment is booked and say goodbye, in one short sentence.",
            }
        ],
        post_actions=[{"type": "function", "handler": flush_submission}, {"type": "end_conversation"}],
    )


def create_giveup_node() -> NodeConfig:
    return NodeConfig(
        name="giveup",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Apologise that you could not find them in the clinic records, suggest they "
                    "call back with their document at hand, and say goodbye. One or two sentences."
                ),
            }
        ],
        post_actions=[{"type": "function", "handler": flush_submission}, {"type": "end_conversation"}],
    )


def create_declined_node() -> NodeConfig:
    return NodeConfig(
        name="declined",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Apologise briefly, stating plainly why you can't help — use the `explanation` "
                    "field from the last tool result word for word in substance (e.g. plan coverage, "
                    "a missing referral, or that the named doctor isn't on staff), not a paraphrase "
                    "that changes the reason. Say goodbye in one short sentence. Do not book anything "
                    "and do not offer any further alternative."
                ),
            }
        ],
        post_actions=[{"type": "function", "handler": flush_submission}, {"type": "end_conversation"}],
    )


def create_register_offer_node() -> NodeConfig:
    return NodeConfig(
        name="register_offer",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Nobody on file matched that identifier. Say so, and ask whether they would like "
                    "to be registered as a new patient. If yes, call begin_register. If they only "
                    "wanted a booking today and do not want to register, call decline_register."
                ),
            }
        ],
        functions=[begin_register, decline_register],
    )


def create_register_node() -> NodeConfig:
    return NodeConfig(
        name="register",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Collect both surnames, their DNI or NIE with the check letter, date of birth, "
                    "phone, email, and insurer — mapping any spoken plan name to its catalogue id "
                    "(e.g. 'Mapfre Salud' -> mapfre). Do not correct or normalise the email "
                    "local-part. As soon as every field is known — including from earlier in this "
                    "call — call register_patient right away, in the same turn, without reading the "
                    "details back or asking them to confirm first. This registers them; it does not "
                    "book an appointment."
                ),
            }
        ],
        functions=[register_patient_schema()],
    )


def create_registered_node() -> NodeConfig:
    return NodeConfig(
        name="registered",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Confirm they are now registered with the clinic and say goodbye in one short "
                    "sentence. Do not offer or book any appointment."
                ),
            }
        ],
        post_actions=[{"type": "function", "handler": flush_submission}, {"type": "end_conversation"}],
    )
