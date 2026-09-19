"""Three stages only: identify, act, close.

Clinic fallbacks are tool results, not extra nodes. Rails are global on FlowManager.
"""

from pipecat.flows import FlowManager, NodeConfig

from flow.prompts import ROLE_MESSAGE
from flow.tools import (
    confirm_offer_schema,
    decline_other_providers,
    flush_submission,
    get_earliest_slot_schema,
    register_patient_schema,
    revise_search,
    search_patient_schema,
)

_CLOSE_TASK = {
    "booked": "Confirm the appointment is booked using only the last tool summary, then say goodbye in one short sentence.",
    "registered": "Confirm they are now on the clinic records. Do not offer an appointment. Say goodbye in one short sentence.",
    "unidentified": (
        "Apologise that you could not find them in the clinic records, suggest they "
        "call back with their document at hand, and say goodbye. One or two sentences."
    ),
    "refused": "Explain briefly that you cannot book that request, using only the last tool reason, then say goodbye.",
    "emergency": "Tell them to hang up and call emergency services. Do not book. One short sentence.",
    "out_of_scope": "Politely refuse without reading any identifiers. Say goodbye in one short sentence.",
}


def create_identify_node() -> NodeConfig:
    return NodeConfig(
        name="identify",
        role_message=ROLE_MESSAGE,
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Establish who the appointment is for. Get the patient's full name and ONE "
                    "exact identifier: DNI or NIE including the letter, or their phone. Ask for "
                    "whatever is missing, one short question at a time, then call search_patient. "
                    "Never search by name alone. If status is misheard_id, ask them to repeat the "
                    "identifier slowly. If status is not_found, they are not on file: collect both "
                    "surnames, date of birth, phone, email as dictated, and insurer, then call "
                    "register_patient. Do not book a namesake. Do not check availability until they "
                    "are found, and do not book after a registration."
                ),
            }
        ],
        respond_immediately=False,
        functions=[search_patient_schema(), register_patient_schema()],
    )


def create_act_node(flow_manager: FlowManager) -> NodeConfig:
    patient = flow_manager.state["patient"]
    return NodeConfig(
        name="act",
        task_messages=[
            {
                "role": "developer",
                "content": (
                    f"The patient is {patient['given_name']} {patient['first_surname']}. You decide "
                    "what to ask next. Map their words to tool arguments; never invent a slot or "
                    "doctor. Call get_earliest_slot with specialty plus any spoken doctor, site, and "
                    "when-phrase. Read back the tool summary. If provider_missing, ask whether anyone "
                    "else will do; if they refuse, call decline_other_providers. If they change their "
                    "mind, call record_final_intent then get_earliest_slot again, or revise_search. "
                    "If no_slots, say so and ask whether they would drop a constraint — then search "
                    "again. If status is refused, explain the reason from the tool and do not invent "
                    "another. Nothing is booked until they say yes; then call confirm_offer with "
                    "the offer_id from the last offer."
                ),
            }
        ],
        functions=[
            get_earliest_slot_schema(),
            confirm_offer_schema(),
            revise_search,
            decline_other_providers,
        ],
    )


def create_close_node(kind: str) -> NodeConfig:
    return NodeConfig(
        name="close",
        task_messages=[
            {
                "role": "developer",
                "content": _CLOSE_TASK.get(kind, _CLOSE_TASK["unidentified"]),
            }
        ],
        post_actions=[{"type": "function", "handler": flush_submission}, {"type": "end_conversation"}],
    )
