from datetime import datetime, timedelta, timezone

from clinic.clinic_catalog import match_plan, match_providers, remap_specialty


CONNECTED = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=2)))


def test_saez_is_gp_not_saenz():
    hits = match_providers("Dr. Sáez", "general_practice")
    assert [p["id"] for p in hits] == ["PR03"]


def test_iglesias_is_not_iglesia():
    hits = match_providers("Dra. Iglesias", "dermatology")
    assert [p["id"] for p in hits] == ["PR05"]
    assert match_providers("Fuentes", "orthopaedics") == []


def test_mapfre_salud_to_id():
    assert match_plan("Mapfre Salud") == "mapfre"
    assert match_plan("Cigna") == "cigna"


def test_child_gp_remaps_to_paediatrics():
    patient = {"date_of_birth": "2017-05-12"}
    assert remap_specialty("general_practice", patient, CONNECTED) == "paediatrics"


def test_adult_derm_is_not_remapped():
    patient = {"date_of_birth": "1996-08-14"}
    assert remap_specialty("dermatology", patient, CONNECTED) == "dermatology"
