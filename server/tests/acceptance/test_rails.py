import asyncio
from types import SimpleNamespace

from flows.common import ROLE_MESSAGE
from flows.identification import create_identify_node
from flows.rails import RAILS, decline_out_of_scope, flag_emergency
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


def test_rails_advertise_emergency_and_scope_tools():
    assert [tool.name for tool in RAILS] == ["flag_emergency", "decline_out_of_scope"]


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
