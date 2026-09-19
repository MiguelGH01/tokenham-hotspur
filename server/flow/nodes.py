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

_CLOSE_SPOKEN = {
    "booked": "You're booked. Goodbye.",
    "registered": "You're on the clinic records. Goodbye.",
    "unidentified": "I could not find you in the records. Please call back with your document. Goodbye.",
    "refused": "I cannot book that request. Goodbye.",
    "emergency": "Please hang up and call emergency services.",
    "out_of_scope": "I'm not able to help with that. Goodbye.",
}


def create_close_node(kind: str) -> NodeConfig:
    text = _CLOSE_SPOKEN.get(kind, _CLOSE_SPOKEN["unidentified"])
    return NodeConfig(
        name="close",
        task_messages=[{"role": "developer", "content": text}],
        respond_immediately=False,
        pre_actions=[
            {"type": "tts_say", "text": text, "append_text_to_context": False},
            {"type": "function", "handler": flush_submission},
            {"type": "end_conversation"},
        ],
    )


def create_identify_node() -> NodeConfig:
    return NodeConfig(
        name="identify",
        role_message=ROLE_MESSAGE,
        task_messages=[
            {
                "role": "developer",
                "content": (
                    "Establish who the appointment is for. Get the patient's full name and ONE "
                    "exact identifier: DNI or NIE including the letter, or their phone. If this "
                    "turn already has both, call search_patient immediately — do not ask again. "
                    "If they are new and already dictated both surnames, date of birth, phone, "
                    "email and insurer, call register_patient immediately. Never search by name "
                    "alone. If status is misheard_id, ask them to repeat the identifier slowly. "
                    "If status is not_found, collect the register fields and call register_patient. "
                    "Do not book a namesake. Do not check availability until they are found, and "
                    "do not book after a registration."
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
                    f"The patient is {patient['given_name']} {patient['first_surname']}. Call "
                    "get_earliest_slot as soon as you know the specialty; pass the spoken doctor, "
                    "site, and when-phrase. Do not ask them to repeat a day, site, or doctor they "
                    "already said. If provider_missing, wait for yes/no; if they refuse anyone "
                    "else, call decline_other_providers. If they change their mind, call "
                    "record_final_intent then get_earliest_slot again, or revise_search. "
                    "If no_slots, ask whether they would drop a constraint then search again. "
                    "The tools already speak offers and refusals — do not repeat them. "
                    "As soon as they accept, call confirm_offer. Do not say goodbye without it."
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
