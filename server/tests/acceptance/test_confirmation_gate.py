"""The confirmation gate: a qualified "yes" is not consent.

Whether a confirmation is unqualified is scored, so the decision lives in
deterministic code. The cases below are phrased the way callers in this clinic
actually agree, hedge and deflect, in the three languages the private pool uses.
"""

import pytest

from confirmation import gate_result, has_unresolved_qualification, is_clean_yes


@pytest.mark.parametrize(
    "utterance",
    [
        "yes that works",
        "Yes, perfect, book it.",
        "vale, perfecto",
        "sí, está bien así",
        "va bé, d'acord",
        "that's great, thank you",
        "ok see you then",
    ],
)
def test_plain_confirmation_never_blocks(utterance):
    assert gate_result(utterance) is None
    assert not has_unresolved_qualification(utterance)


@pytest.mark.parametrize(
    "utterance,reason",
    [
        ("yes, but can you check another time?", "correction"),
        ("sí, pero antes de aceptar quiero saber el precio", "price_question"),
        ("how much does it cost?", "price_question"),
        ("cuánto cuesta la cita?", "price_question"),
        ("quant costa?", "price_question"),
        ("wait, I need to check with my wife first", "negation"),
        ("espera, me he equivocado de día", "negation"),
        ("encara no, deixem-ho estar", "negation"),
        ("can you look for another doctor?", "alternative_request"),
        ("¿puede buscar otro médico?", "alternative_request"),
        ("no, thanks", "negation"),
        ("i meant Tuesday, not Monday", "correction"),
    ],
)
def test_qualifications_block(utterance, reason):
    assert gate_result(utterance) == reason


def test_no_problem_is_not_a_negation():
    assert gate_result("no problem") is None
    assert gate_result("no worries, go ahead") is None


def test_missing_or_empty_utterance_never_blocks():
    assert gate_result("") is None
    assert gate_result(None) is None


def test_price_topic_alone_does_not_block():
    """A bare mention of payment is not a price question."""
    assert gate_result("yes, I'll pay with mapfre") is None


@pytest.mark.parametrize(
    "utterance",
    [
        "that works for me",
        "that's fine",
        "sounds good",
        "está bien",
        "me va bien",
    ],
)
def test_spoken_acceptance_without_yes_is_still_consent(utterance):
    assert is_clean_yes(utterance)
