from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

from clinic.clinic_catalog import location_ids, plan_literals, specialty_ids
from flow.nodes import create_identify_node
from flow.tools import (
    RAILS,
    act_functions,
    confirm_offer_schema,
    get_earliest_slot_schema,
    register_patient_schema,
)


def test_register_insurer_is_catalogue_only():
    enum = register_patient_schema().properties["insurer"]["enum"]
    assert "mapfre" in enum
    assert "Mapfre Salud" in enum
    assert "made_up_plan" not in enum
    for plan_id in ("sanitas", "privado", "nueva_mutua"):
        assert plan_id in enum
    assert set(plan_literals()) == set(enum)


def test_slot_search_schema_names_this_chart_uncovered():
    connected = datetime(2026, 9, 18, 10, 0, tzinfo=timezone(timedelta(hours=2)))
    schema = get_earliest_slot_schema(
        SimpleNamespace(
            state={
                "patient": {
                    "insurer": "adeslas",
                    "referrals": [],
                    "date_of_birth": "1967-03-20",
                },
                "connected_at": connected,
            }
        )
    )
    assert "uncovered: gynaecology" in schema.description
    assert "gynaecology" in schema.properties["specialty"]["enum"]


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


def _tool_name(tool):
    return tool.name if not callable(tool) else tool.__name__


def test_act_tools_hide_confirm_until_an_offer_and_drop_booking_after_refuse():
    empty = SimpleNamespace(state={"patient": None, "offers": {}})
    names = [_tool_name(t) for t in act_functions(empty)]
    assert names == ["get_earliest_slot", "revise_search", "decline_other_providers"]

    offered = SimpleNamespace(state={"patient": None, "offers": {"offer-1": {}}})
    names = [_tool_name(t) for t in act_functions(offered)]
    assert "confirm_offer" in names

    refused = SimpleNamespace(state={"hard_refuse": "specialty_not_covered", "offers": {"offer-1": {}}})
    assert act_functions(refused) == []


def test_rails_always_advertise_search_and_register():
    names = [_tool_name(t) for t in RAILS]
    assert names[:2] == ["search_patient", "register_patient"]
    assert "flag_emergency" in names
    assert "decline_out_of_scope" in names
    assert create_identify_node()["functions"] == []
