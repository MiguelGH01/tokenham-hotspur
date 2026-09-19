"""Pipecat Flows nodes: prompt + which tools are allowed at this stage.

A node does not implement clinic logic. It only tells the LLM what to ask
and which functions from ``flow.tools`` it may call.
"""

from pipecat.flows import FlowManager, NodeConfig

from flow.prompts import ROLE_MESSAGE
from flow.tools import (
    confirm_offer_schema,
    end_without_booking,
    flush_submission,
    get_earliest_slot_schema,
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
                    "name alone. Callers dictate an identifier in chunks, pausing between groups of "
                    "digits. A DNI is 8 digits then a letter, a NIE is a letter, 7 digits, then a "
                    "letter, a phone number is 9 digits. While what you have heard so far is shorter "
                    "than that, the caller is still dictating: reply with only 'Yes?' or 'Go on', "
                    "do not call search_patient, and join the chunks across turns into one identifier. "
                    "If the result is misheard_id or not_found, say you could not find them "
                    "and ask them to repeat the identifier slowly, digit by digit. Whenever the "
                    "caller then gives a complete identifier, call search_patient again, even if it "
                    "is identical to one that already failed: the tool counts the attempts and ends "
                    "the call when they run out, so never answer a repeated identifier yourself."
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
    return NodeConfig(
        name="find_slot",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    f"The patient is {patient['given_name']} {patient['first_surname']}. Find out "
                    "which specialty they need, mapping their words to one of the allowed values. "
                    "Only pass site, weekday or part_of_day if the caller asked for them. Then call "
                    "get_earliest_slot. If the caller named a doctor, pass provider. If it returns "
                    "provider_not_found, say there is no such doctor on the clinic's staff and ask "
                    "whether another doctor of that specialty would do: if yes, call "
                    "get_earliest_slot again without provider. If it returns no_slots with a "
                    "reason, a clinic rule stops this booking: tell them why in one sentence using "
                    "the explanation, do not offer another specialty in its place, and call "
                    "end_without_booking unless they ask for something else. If it returns "
                    "no_slots without a reason, say nothing is available for that request and ask "
                    "whether they would drop a constraint. "
                    "If lookup_failed, apologise and ask them to call back shortly. If the caller "
                    "wants nothing you can offer, call end_without_booking."
                ),
            }
        ],
        functions=[get_earliest_slot_schema(), end_without_booking],
    )


OFFER_NOTES = {
    "on_leave": "First say the doctor they asked for is on leave, so this is another doctor of "
    "the same specialty at the same site. ",
    "other_day": "First say the doctor they asked for is not at that site on the day they asked, "
    "so this is that doctor's earliest day there. ",
    "not_in_network": "First say the doctor they asked for does not take their insurance plan, "
    "so this is another doctor of the same specialty who does. ",
    "age_redirect": "First say that at the patient's age this kind of visit is seen in the "
    "specialty of the doctor below, not the one they asked for, so you looked there instead. ",
}


def create_confirm_node(flow_manager: FlowManager, note: str | None = None) -> NodeConfig:
    return NodeConfig(
        name="confirm",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    f"{OFFER_NOTES.get(note, '')}"
                    "Offer the appointment from the summary the tool returned: doctor, site, day "
                    "and time. Ask if that works. Nothing is booked until they say yes. On yes, "
                    "call confirm_offer. If they want something different, call revise_search. "
                    "If they want no appointment at all, call end_without_booking."
                ),
            }
        ],
        functions=[confirm_offer_schema(flow_manager), revise_search, end_without_booking],
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


def create_close_node() -> NodeConfig:
    """No booking, by the caller's choice. The reason to submit was set by the last tool."""
    return NodeConfig(
        name="close",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Apologise that you could not book what they wanted, confirm nothing has "
                    "been booked, and say goodbye. One or two sentences."
                ),
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
