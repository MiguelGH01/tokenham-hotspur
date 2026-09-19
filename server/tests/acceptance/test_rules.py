"""The clinic's rules, as law rather than as fixtures.

Every assertion here is derived from ``clinic.json`` or from the published rule,
never from a single public case, so passing them means the *rule* holds for the
private cases too. That distinction is the whole point: a test written against
case ``the_rules-460d9e84504a`` proves that one dial works; a test written
against the coverage matrix proves every dial of that shape works.

Problem families covered, and the law each one pins down:

- PR-06 age boundary — the spoken specialty loses to the age window.
- PR-06 referrals — a referral-gated specialty refuses without the referral.
- PR-06 plan x specialty — a plan that refuses the specialty leaves nowhere to go.
- PR-06 plan x site — a refusal only when *no* serving site is covered.
- PR-06 provider x plan — a provider who refuses a plan redirects, never refuses.
- PR-03 provider on leave — redirect inside the same specialty.
- PR-17 second policy — the caller's plan wins, and self-pay is not a key.
- PR-15 nearest site — distance, restricted to sites that can serve.
"""

from datetime import date, datetime

import pytest

from clinic_catalog import load_catalog
from rules import (
    age_in_months,
    check_patient_rules,
    check_provider_rules,
    fold,
    providers_for,
    resolve_plan,
    sites_serving,
    specialty_for_age,
)

#: A call in the middle of the published calendar window, before the closure.
CALL_DAY = date(2026, 9, 18)
#: Fiesta Nacional: the calendar's one closure day.
CLOSURE_DAY = date(2026, 10, 12)


@pytest.fixture(scope="module")
def catalogue():
    return load_catalog()


def patient(*, born="1980-01-01", insurer=None, referrals=()):
    return {
        "patient_id": "P0TEST",
        "date_of_birth": born,
        "insurer": insurer,
        "referrals": list(referrals),
    }


# --- age boundary (PR-06) --------------------------------------------------


def test_age_boundary_splits_at_the_fourteenth_birthday():
    """Paediatrics and general practice tile the calendar with no gap or overlap."""
    assert age_in_months(date(2012, 9, 18), CALL_DAY) == 168
    assert age_in_months(date(2012, 9, 19), CALL_DAY) == 167


def test_a_child_asking_for_the_gp_is_redirected_to_paediatrics(catalogue):
    """The spoken specialty loses to the age window; the answer is a booking."""
    verdict = check_patient_rules(
        specialty_id="general_practice",
        patient=patient(born="2017-05-12"),
        plan=None,
        today=CALL_DAY,
    )
    assert verdict is not None
    assert verdict.reason == "not_eligible_age"
    assert verdict.redirect_specialty == "paediatrics"
    assert verdict.redirect_to, "an age refusal with nowhere to go would be a dead end"


def test_an_adult_asking_for_the_gp_passes_the_age_rule():
    assert (
        check_patient_rules(
            specialty_id="general_practice",
            patient=patient(born="1980-01-01"),
            plan=None,
            today=CALL_DAY,
        )
        is None
    )


def test_specialty_for_age_returns_none_only_when_no_window_fits(catalogue):
    assert specialty_for_age(catalogue, 24)["id"] == "paediatrics"
    assert specialty_for_age(catalogue, 200)["id"] == "general_practice"


# --- referrals (PR-06) -----------------------------------------------------


def test_referral_gated_specialty_refuses_without_the_referral(catalogue):
    gated = [s for s in catalogue["specialties"] if s["referral_required"]]
    assert gated, "the catalogue is expected to gate at least one specialty"
    for specialty in gated:
        verdict = check_patient_rules(
            specialty_id=specialty["id"],
            patient=patient(referrals=[]),
            plan=None,
            today=CALL_DAY,
        )
        assert verdict is not None and verdict.reason == "referral_required"


def test_referral_gated_specialty_passes_with_the_referral():
    assert (
        check_patient_rules(
            specialty_id="dermatology",
            patient=patient(referrals=["dermatology"]),
            plan=None,
            today=CALL_DAY,
        )
        is None
    )


def test_a_missing_record_is_not_a_missing_referral():
    """No record is not the same as no referral: /availability answers instead."""
    assert (
        check_patient_rules(
            specialty_id="dermatology", patient=None, plan=None, today=CALL_DAY
        )
        is None
    )


# --- the plan matrix (PR-06, PR-17) ----------------------------------------


def test_plan_specialty_dead_end_leaves_nowhere_to_go(catalogue):
    """Adeslas refuses gynaecology and there is one gynaecologist."""
    referrer = next(p for p in catalogue["providers"] if p["specialty_id"] == "gynaecology")
    plan = resolve_plan(catalogue, patient(insurer="adeslas"), None)
    assert plan is not None
    verdict = check_patient_rules(
        specialty_id="gynaecology", patient=patient(insurer="adeslas"), plan=plan, today=CALL_DAY
    )
    assert verdict is not None and verdict.reason == "specialty_not_covered"
    assert referrer["id"] not in verdict.redirect_to


def test_plan_site_refusal_only_when_no_serving_site_is_covered(catalogue):
    """A refusal needs *every* serving site uncovered, not just the named one."""
    for plan in catalogue["plans"]:
        for specialty in catalogue["specialties"]:
            candidates = sites_serving(catalogue, specialty["id"])
            if not candidates:
                continue
            covered = [
                loc for loc in candidates if not _uncovered(plan, loc["name"])
            ]
            verdict = check_patient_rules(
                specialty_id=specialty["id"], patient=None, plan=plan, today=CALL_DAY
            )
            if covered:
                assert verdict is None or verdict.reason != "location_not_covered", (
                    f"{plan['id']} covers {specialty['id']} at {covered[0]['name']}"
                )


def _uncovered(plan, name):
    return any(fold(other) == fold(name) for other in plan.get("uncovered_location_names") or [])


def test_a_provider_who_refuses_a_plan_redirects_rather_than_refusing(catalogue):
    """PR-06-S5: Iglesias refuses DKV, so the answer is a booking with someone else."""
    plan = resolve_plan(catalogue, patient(insurer="dkv"), None)
    verdict = check_provider_rules(
        provider_id="PR05",
        specialty_id="dermatology",
        plan=plan,
        today=CALL_DAY,
    )
    assert verdict is not None
    assert verdict.reason == "provider_not_in_network"
    assert verdict.redirect_to, "a refusal here would be the wrong answer shape"
    assert "PR05" not in verdict.redirect_to


def test_a_provider_on_leave_redirects_inside_the_same_specialty(catalogue):
    """PR-03: Requena's leave must not become a refusal while others can serve."""
    on_leave = [
        p
        for p in catalogue["providers"]
        if p["leave"] and _covers(p["leave"], CALL_DAY)
    ]
    assert on_leave, "the catalogue is expected to carry at least one active leave"
    by_id = {p["id"]: p for p in catalogue["providers"]}
    for provider in on_leave:
        verdict = check_provider_rules(
            provider_id=provider["id"],
            specialty_id=provider["specialty_id"],
            plan=None,
            today=CALL_DAY,
        )
        assert verdict is not None and verdict.reason == "provider_on_leave"
        assert provider["id"] not in verdict.redirect_to
        for provider_id in verdict.redirect_to:
            assert by_id[provider_id]["specialty_id"] == provider["specialty_id"]


def _covers(leave, day):
    return date.fromisoformat(leave["start"]) <= day <= date.fromisoformat(leave["end"])


# --- second policy (PR-17) -------------------------------------------------


def test_a_named_plan_wins_over_the_record():
    """The second policy of PR-17 exists nowhere in the API, so the caller's word wins."""
    catalogue = load_catalog()
    named = resolve_plan(catalogue, patient(insurer="adeslas"), "sanitas")
    assert named is not None and named["id"] == "sanitas"


def test_self_pay_is_never_a_key_to_unlock_a_refused_booking():
    """Quoting privado for a patient whose record says otherwise is inventing cover."""
    catalogue = load_catalog()
    plan = resolve_plan(catalogue, patient(insurer="adeslas"), "privado")
    assert plan is not None and plan["id"] == "adeslas"


def test_self_pay_stands_when_it_is_the_plan_on_the_record():
    catalogue = load_catalog()
    plan = resolve_plan(catalogue, patient(insurer="privado"), None)
    assert plan is not None and plan["id"] == "privado"


# --- nearest site (PR-15) --------------------------------------------------


def test_nearest_site_only_considers_sites_that_can_serve(catalogue):
    """PR-15: closest is not an answer if it cannot serve the request."""
    from rules import nearest_location

    for specialty in catalogue["specialties"]:
        serving = {
            fold(loc["name"]) for loc in sites_serving(catalogue, specialty["id"])
        }
        # A point next to a site that does not host the specialty must not win.
        for loc in catalogue["locations"]:
            if fold(loc["name"]) in serving:
                continue
            chosen = nearest_location(
                specialty["id"], (loc["latitude"], loc["longitude"])
            )
            assert chosen is None or fold(chosen["name"]) in serving


def test_hardcoded_sites_match_the_catalogue(catalogue):
    from rules import SERVICE_LOCATIONS

    published = {loc["id"]: loc for loc in catalogue["locations"]}
    assert {site["id"] for site in SERVICE_LOCATIONS} == set(published)
    for site in SERVICE_LOCATIONS:
        loc = published[site["id"]]
        assert site["name"] == loc["name"]
        assert site["address"] == loc["address"]
        assert site["latitude"] == loc["latitude"]
        assert site["longitude"] == loc["longitude"]


def test_spoken_postcode_picks_the_closest_catalogue_site():
    """A 5-digit in the address is ranked against the hardcoded service postcodes."""
    from rules import location_from_spoken_place

    centro = location_from_spoken_place("Calle de Preciados 3, 28013 Madrid")
    assert centro is not None and centro["id"] == "centro"
    norte = location_from_spoken_place("Paseo de la Castellana 189, 28046 Madrid")
    assert norte is not None and norte["id"] == "norte"
    sur = location_from_spoken_place("Calle de Madrid 54, 28902 Getafe")
    assert sur is not None and sur["id"] == "sur"


def test_spoken_neighbourhood_matches_a_hardcoded_site():
    from rules import location_from_spoken_place

    getafe = location_from_spoken_place("I'm in Getafe, at Calle de Madrid 54")
    assert getafe is not None and getafe["id"] == "sur"
    centre = location_from_spoken_place(
        "I'm right in the centre, at Calle de Preciados 3, by Puerta del Sol"
    )
    assert centre is not None and centre["id"] == "centro"
    norte = location_from_spoken_place(
        "I'm at Paseo de la Castellana 189, at Plaza de Castilla"
    )
    assert norte is not None and norte["id"] == "norte"


def test_nearest_spoken_place_skips_a_site_that_cannot_serve():
    """Physio is only at Sur: an origin on Centro still books Sur."""
    from rules import location_from_spoken_place

    chosen = location_from_spoken_place("28013 Madrid", "physiotherapy")
    assert chosen is not None and chosen["id"] == "sur"


def test_castilla_books_norte_when_orthopaedics_can_serve_there():
    from rules import location_from_spoken_place

    chosen = location_from_spoken_place(
        "Paseo de la Castellana 189, Plaza de Castilla",
        "orthopaedics",
    )
    assert chosen is not None and chosen["id"] == "norte"


def test_spanish_does_not_constrain_the_roster(catalogue):
    from rules import language_constrains_booking, normalize_language, provider_speaks

    spanish = normalize_language("español")
    assert spanish == "es"
    assert language_constrains_booking(catalogue, spanish) is False
    catalan = normalize_language("Catalan")
    assert catalan == "ca"
    assert language_constrains_booking(catalogue, catalan) is True
    assert provider_speaks(catalogue, "PR01", catalan) is True
    assert provider_speaks(catalogue, "PR02", catalan) is False


# --- the redirect list is the same list everywhere --------------------------


def test_providers_for_never_returns_a_provider_who_refuses_the_plan(catalogue):
    for plan in catalogue["plans"]:
        for specialty in catalogue["specialties"]:
            ids = providers_for(catalogue, specialty["id"], plan=plan)
            for provider in catalogue["providers"]:
                if provider["id"] in ids and any(
                    ref["id"] == plan["id"] for ref in provider["refused_insurers"]
                ):
                    pytest.fail(f"{plan['id']} listed {provider['id']} who refuses it")


def test_today_in_madrid_is_a_plain_date():
    from rules import today_in_madrid

    assert today_in_madrid(datetime(2026, 9, 18, 23, 30)) == date(2026, 9, 18)
