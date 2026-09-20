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


@pytest.mark.parametrize(
    "utterance",
    [
        "Please book it.",
        "Uh, yes, sorry.  Monday the 21st at 9. Go ahead.",
        "Oh, yeah.",
        "Well, yes, go ahead.",
    ],
)
def test_filler_prefixed_confirmation_is_still_consent(utterance):
    """A caller rarely opens a yes with the bare word "yes" — real calls (e.g.
    call 7dc4da3b-4e52-5dd1-9fac-247815355eea) lose the case to five straight
    rejections of these exact phrases because the affirmative check was
    anchored past any disfluency or politeness opener."""
    assert is_clean_yes(utterance)


def test_filler_prefix_does_not_hide_a_real_correction():
    assert not is_clean_yes("well, yes, but I need to check with my husband first")
