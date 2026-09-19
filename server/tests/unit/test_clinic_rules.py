"""PR-06: a clinic rule stops the request. Redirect when someone else can see them, else say why."""

import asyncio

from flow.tools import get_earliest_slot
from tests.unit.test_flow_tools import PATIENT, FakeClinic, _flow
from tests.unit.test_named_doctor import _slot


class ClinicBySpecialty(FakeClinic):
    """Live shape: each specialty answers with its own slots and its own `blocked` list."""

    def __init__(self, responses):
        super().__init__()
        self.responses, self.asked = responses, []

    async def availability(self, date_from, date_to, specialty, *args, **kwargs):
        self.asked.append(specialty)
        return self.responses[specialty]


def _search(flow, **args):
    return asyncio.run(get_earliest_slot(args, flow))


def test_doctor_who_refuses_the_plan_is_swapped_for_a_colleague_who_takes_it():
    # Live: Iglesias (PR05) refuses DKV, so she is in `blocked` and only Vilar (PR12) has slots.
    clinic = FakeClinic(slots=[_slot("PR12", "2026-09-21T10:15:00+02:00", "norte")],
                        blocked=[{"provider_id": "PR05", "restriction": "provider_not_in_network"}])
    flow = _flow(clinic, patient=PATIENT)

    result, node = _search(flow, specialty="dermatology", provider="PR05")

    assert result["status"] == "offer" and result["note"] == "not_in_network" and node["name"] == "confirm"
    assert flow.state["offers"]["offer-1"]["provider_id"] == "PR12"


def test_child_asking_for_a_gp_is_booked_into_paediatrics_not_refused():
    gp_doctors = [{"provider_id": p, "restriction": "not_eligible_age"} for p in ("PR01", "PR02", "PR03", "PR07")]
    clinic = ClinicBySpecialty({
        "general_practice": {"slots": [], "blocked": gp_doctors},
        "paediatrics": {"slots": [_slot("PR08", "2026-09-21T10:00:00+02:00", "norte")], "blocked": []},
    })
    flow = _flow(clinic, patient=PATIENT)

    result, _ = _search(flow, specialty="general_practice")
    asyncio.run(flow.state["submission"].flush())

    assert clinic.asked == ["general_practice", "paediatrics"]
    assert result["status"] == "offer" and result["note"] == "age_redirect"
    assert flow.state["offers"]["offer-1"]["provider_id"] == "PR08"
    assert clinic.posted[0]["reason"] != "not_eligible_age"  # an offer is on the table, not a refusal


def test_the_age_boundary_works_both_ways():
    # CL-age-boundary: every age has exactly one general-care specialty. An adult asking for
    # paediatrics belongs in general practice.
    too_old = [{"provider_id": "PR04", "restriction": "not_eligible_age"}]
    clinic = ClinicBySpecialty({
        "paediatrics": {"slots": [], "blocked": too_old},
        "general_practice": {"slots": [_slot("PR01", "2026-09-21T09:15:00+02:00")], "blocked": []},
    })
    flow = _flow(clinic, patient=PATIENT)

    result, _ = _search(flow, specialty="paediatrics")

    assert result["note"] == "age_redirect" and flow.state["offers"]["offer-1"]["provider_id"] == "PR01"


def test_only_a_general_complaint_is_redirected_by_age():
    # Gynaecology is not general care (plans exclude it): a child asking for it is refused.
    too_young = [{"provider_id": "PR11", "restriction": "not_eligible_age"}]
    clinic = ClinicBySpecialty({"gynaecology": {"slots": [], "blocked": too_young}})
    flow = _flow(clinic, patient=PATIENT)

    result, node = _search(flow, specialty="gynaecology")

    assert clinic.asked == ["gynaecology"] and node is None
    assert result["status"] == "no_slots" and result["reason"] == "not_eligible_age"


def test_a_refusal_tells_the_model_which_rule_and_why():
    blocked = [{"provider_id": p, "restriction": "referral_required"} for p in ("PR05", "PR12")]
    clinic = FakeClinic(slots=[], blocked=blocked)
    flow = _flow(clinic, patient=PATIENT)

    result, node = _search(flow, specialty="dermatology")
    asyncio.run(flow.state["submission"].flush())

    assert node is None and result["status"] == "no_slots" and result["reason"] == "referral_required"
    assert "referral" in result["explanation"].lower()
    assert clinic.posted[0]["reason"] == "referral_required"
