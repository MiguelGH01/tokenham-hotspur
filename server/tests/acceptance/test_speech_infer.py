from flow.prompts import ROLE_MESSAGE
from clinic.speech import (
    accepts_offer,
    greeting_stripped,
    infer_provider_spoken,
    infer_site,
    infer_specialty,
    parse_register,
    register_complete,
    resolve_slot_query,
    wants_register,
    wants_soonest,
)


def test_infer_site_from_spoken_name():
    assert infer_site("at Arenal Norte please") == "norte"
    assert infer_site("Arenal Centro on Monday") == "centro"


def test_infer_saez_not_saenz():
    name = infer_provider_spoken("I'd like to see Dr. Sáez the GP", "general_practice")
    assert name and "Sáez" in name and "Sáenz" not in name


def test_infer_iglesias():
    name = infer_provider_spoken("could I see Dra. Iglesias?", "dermatology")
    assert name and "Iglesias" in name


def test_fuentes_is_nobody():
    assert infer_provider_spoken("Dr. Fuentes, the orthopaedic surgeon", "orthopaedics") is None


def test_greeting_does_not_eat_saturday_morning():
    assert "saturday morning" in greeting_stripped("Good morning, could I see a GP on Saturday morning").lower()


def test_infer_specialty_from_named_specialty():
    assert infer_specialty("I'd like a GP on Saturday morning") == "general_practice"
    assert infer_specialty("orthopaedics this coming Thursday, hip") == "orthopaedics"
    assert infer_specialty("I twisted my ankle") is None
    assert infer_specialty("my son's had a temperature for two days") is None


def test_parse_register_fills_from_speech():
    spoken = (
        "I am a new patient. Joaquín González Ortega, DNI 18921027P, "
        "born on the twenty-fifth of June 1970. Phone 783869132, "
        "email joaquingonzalez24@hotmail.com, insured with Cigna."
    )
    fields = parse_register({"given_name": "Joaquín", "first_surname": "González", "second_surname": "Ortega"}, spoken)
    assert fields["national_id"].replace(" ", "").upper() == "18921027P"
    assert fields["date_of_birth"] == "1970-06-25"
    assert fields["email"] == "joaquingonzalez24@hotmail.com"
    assert register_complete(fields)
    assert wants_register(spoken)


def test_infer_specialty_from_catalogue_name():
    assert infer_specialty("I need Paediatrics please") == "paediatrics"
    assert infer_specialty("physiotherapy for my back") == "physiotherapy"


def test_resolve_ignores_invented_site_and_specialty():
    spoken = "Hello, I need an appointment. I'm Amelia Hughes White. A GP, the earliest, at Arenal Centro."
    query = resolve_slot_query(
        {"specialty": "dermatology", "site": "norte", "weekday": "monday"},
        spoken,
    )
    assert query["specialty"] == "general_practice"
    assert query["site"] == "centro"
    assert query["weekday"] is None


def test_resolve_keeps_llm_specialty_when_caller_only_described_symptoms():
    spoken = "Yeah, so my son's had a temperature for two days and he's off his food."
    query = resolve_slot_query({"specialty": "paediatrics", "site": "norte"}, spoken)
    assert query["specialty"] == "paediatrics"
    assert query["site"] is None


def test_resolve_does_not_invent_site_when_caller_omitted_it():
    spoken = "Josefa Domínguez Navarro. The GP please. Tomorrow if possible."
    query = resolve_slot_query({"specialty": "general_practice", "site": "centro"}, spoken)
    assert query["specialty"] == "general_practice"
    assert query["site"] is None


def test_resolve_named_doctor_sets_specialty():
    spoken = "With doctor Saez at Arenal Centro, please."
    query = resolve_slot_query({"specialty": "dermatology"}, spoken)
    assert query["site"] == "centro"
    assert query["provider"] and "Sáez" in query["provider"]
    assert query["specialty"] == "general_practice"


def test_accepts_offer_and_soonest():
    assert accepts_offer("Perfect, book it.")
    assert accepts_offer("yes that works")
    assert not accepts_offer("tomorrow morning please")
    assert wants_soonest("A GP, the earliest you have, at Arenal Centro.")
    assert not wants_soonest("With doctor Saez at Arenal Centro, please.")


def test_role_prompt_has_triage_and_scope_examples():
    assert "temperature" in ROLE_MESSAGE
    assert "orthopaedics" in ROLE_MESSAGE
    assert "flag_emergency" in ROLE_MESSAGE
    assert "what medicine should I give him" in ROLE_MESSAGE
    assert "Not out of scope" in ROLE_MESSAGE
