"""Pipecat Flows nodes: prompt + which tools are allowed at this stage.

A node does not implement clinic logic. It only tells the LLM what to ask
and which functions from ``flow.tools`` it may call.
"""

from pipecat.flows import FlowManager, NodeConfig

from flow.prompts import ROLE_MESSAGE
from flow.tools import (
    confirm_offer_schema,
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
                    "name alone. If the result is misheard_id or not_found, say you could not find them "
                    "and ask them to repeat the identifier slowly, digit by digit."
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
                    "get_earliest_slot. If it returns no_slots, say nothing is available for that "
                    "request and ask whether they would drop a constraint. If lookup_failed, "
                    "apologise and ask them to call back shortly."
                ),
            }
        ],
        functions=[get_earliest_slot_schema()],
    )


def create_confirm_node(flow_manager: FlowManager) -> NodeConfig:
    return NodeConfig(
        name="confirm",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Offer the appointment from the summary the tool returned: doctor, site, day "
                    "and time. Ask if that works. Nothing is booked until they say yes. On yes, "
                    "call confirm_offer. If they want something different, call revise_search."
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
