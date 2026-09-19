import asyncio
from types import SimpleNamespace

from flows.common import ROLE_MESSAGE
from flows.identification import create_identify_node
from flows.rails import (
    RAILS,
    answer_clinic_question,
    decline_out_of_scope,
    flag_emergency,
    pin_language,
    record_final_intent,
)
from submission import CallSubmission


def test_role_prompt_has_triage_and_scope_examples():
    assert "temperature" in ROLE_MESSAGE
    assert "orthopaedics" in ROLE_MESSAGE
    assert "flag_emergency" in ROLE_MESSAGE
    assert "what medicine should I give him" in ROLE_MESSAGE
    assert "Not out of scope" in ROLE_MESSAGE


def test_identify_prompt_names_the_child_as_the_patient():
    content = create_identify_node()["task_messages"][0]["content"]
    assert "the patient is the child" in content


def test_rails_advertise_the_always_on_tools():
    assert [tool.name for tool in RAILS] == [
        "flag_emergency",
        "decline_out_of_scope",
        "pin_language",
        "record_final_intent",
        "answer_clinic_question",
    ]


def test_flag_emergency_submits_escalate():
    class Client:
        async def post_submission(self, payload):
            return {"received": True}

    manager = SimpleNamespace(state={"submission": CallSubmission("c", Client())})
    result, node = asyncio.run(flag_emergency({}, manager))
    assert result["status"] == "escalated"
    assert manager.state["submission"].pending == {
        "action": "ESCALATE",
        "reason": "medical_emergency",
    }
    assert node["name"] == "emergency"


def test_decline_out_of_scope_states_the_refusal():
    class Client:
        async def post_submission(self, payload):
            return {"received": True}

    manager = SimpleNamespace(state={"submission": CallSubmission("c", Client())})
    result, node = asyncio.run(decline_out_of_scope({}, manager))
    assert result["status"] == "declined"
    assert manager.state["submission"].pending["reason"] == "out_of_scope"
    assert node["name"] == "out_of_scope"


def test_pin_language_normalises_catalan():
    manager = SimpleNamespace(state={})
    result, node = asyncio.run(pin_language({"language": "Català"}, manager))
    assert result["status"] == "pinned"
    assert manager.state["language"] == "ca"
    assert node is None


def test_record_final_intent_bumps_the_revision():
    manager = SimpleNamespace(state={"revision": 1})
    result, node = asyncio.run(
        record_final_intent({"intent_text": "book myself instead"}, manager)
    )
    assert result["status"] == "revised"
    assert manager.state["revision"] == 2
    assert manager.state["final_intent"] == "book myself instead"
    assert node is None


def test_answer_clinic_question_is_catalogue_only():
    manager = SimpleNamespace(state={})
    result, node = asyncio.run(answer_clinic_question({"question": "which sites?"}, manager))
    assert result["status"] == "answered"
    names = {site["name"] for site in result["facts"]["sites"]}
    assert names == {"Arenal Centro", "Arenal Norte", "Arenal Sur"}
    assert node is None
