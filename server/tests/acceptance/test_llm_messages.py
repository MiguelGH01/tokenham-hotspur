from pipecat.processors.aggregators.llm_context import LLMSpecificMessage

from llm_messages import chat_role, chat_text


def test_chat_helpers_read_dicts_and_llm_specific_messages():
    plain = {"role": "assistant", "content": "Could you give me your DNI or NIE?"}
    wrapped = LLMSpecificMessage(llm="google", message=plain)
    gemini = LLMSpecificMessage(
        llm="google",
        message={"role": "model", "content": "Clínica Arenal, how can I help you?"},
    )

    assert chat_role(plain) == "assistant"
    assert "DNI or NIE" in (chat_text(plain) or "")
    assert chat_role(wrapped) == "assistant"
    assert "DNI or NIE" in (chat_text(wrapped) or "")
    assert chat_role(gemini) == "assistant"
    assert chat_text(gemini)
