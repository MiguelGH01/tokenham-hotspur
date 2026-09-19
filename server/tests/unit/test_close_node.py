from flow.nodes import create_close_node


def test_close_does_not_run_another_llm_turn():
    node = create_close_node("booked")
    assert node["respond_immediately"] is False
    types = [a["type"] for a in node["pre_actions"]]
    assert types == ["tts_say", "function"]
    assert "end_conversation" not in types
