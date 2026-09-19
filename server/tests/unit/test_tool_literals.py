from types import SimpleNamespace

from clinic.clinic_catalog import location_ids, plan_literals, specialty_ids
from flow.tools import confirm_offer_schema, get_earliest_slot_schema, register_patient_schema


def test_register_insurer_is_catalogue_only():
    enum = register_patient_schema().properties["insurer"]["enum"]
    assert "mapfre" in enum
    assert "Mapfre Salud" in enum
    assert "made_up_plan" not in enum
    for plan_id in ("sanitas", "privado", "nueva_mutua"):
        assert plan_id in enum
    assert set(plan_literals()) == set(enum)


def test_slot_search_enums_are_catalogue_ids():
    props = get_earliest_slot_schema().properties
    assert props["specialty"]["enum"] == specialty_ids()
    assert props["site"]["enum"] == location_ids()
    assert get_earliest_slot_schema().required == []


def test_confirm_offer_enum_is_live_availability_ids():
    empty = confirm_offer_schema().properties["offer_id"]
    assert "enum" not in empty
    live = confirm_offer_schema(
        SimpleNamespace(state={"offers": {"offer-1": {}, "offer-2": {}}})
    ).properties["offer_id"]["enum"]
    assert live == ["offer-1", "offer-2"]
