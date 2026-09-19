"""The clinic's standing restrictions, as pure functions over ``clinic.json``.

``/availability`` is the authority on *why* a provider is refused: it names the
restriction in ``blocked``, and ``API-avail-blocked`` says to submit that name as
the reason. But it only answers about providers, and only once the request is
already concrete. The rules here are facts the catalogue itself publishes, so
they can be decided before spending a round trip — and two of them cannot be
answered by the API at all:

- **The age window.** Paediatrics and general practice split at the 14th
  birthday with no gap and no overlap, so exactly one of them answers for a
  general complaint. A caller who asks for "the GP" for a five-year-old belongs
  in paediatrics; refusing them with ``not_eligible_age`` would fail a case whose
  answer is a booking.
- **The redirect.** A refusal is only correct when there is nowhere to go.
  Adeslas refuses gynaecology and there is one gynaecologist, so the refusal
  stands; a provider who refuses a plan leaves the specialty wide open, so the
  answer is a booking with someone else.

The order the checks run in is the order the clinic would hit them, widest
first: an under-14 asking for general practice is an age refusal whatever their
insurer says.
"""

from __future__ import annotations

import unicodedata
from dataclasses import dataclass, field
from datetime import date, datetime

from clinic_catalog import load_catalog
from service_locations import origin_from_spoken_place as _origin_from_sites

#: Self-pay. A plan a patient holds or does not — never a plan we quote to
#: unlock a booking their real plan refuses.
SELF_PAY = "privado"


def fold(value: str) -> str:
    """Lower-cased, accent-folded, periods removed — how published names match.

    The catalogue publishes names ("Dra. Carmen Ortiz Vidal") where the API
    publishes ids, so the two are reconciled by folding rather than by keeping a
    second table that could drift.
    """
    decomposed = unicodedata.normalize("NFD", (value or "").lower())
    return "".join(c for c in decomposed if not unicodedata.combining(c)).replace(".", "")


@dataclass(frozen=True)
class Verdict:
    """One rule, and what it forbids.

    ``reason`` is what gets submitted. ``redirect_to`` is the list of provider
    ids who could serve the same request instead — empty when the rule leaves
    nowhere to go, which is what makes the refusal correct rather than lazy.
    """

    reason: str
    detail: str
    redirect_to: list[str] = field(default_factory=list)
    redirect_specialty: str | None = None


# ---------------------------------------------------------------------------
# Age
# ---------------------------------------------------------------------------


def age_in_months(born: date, on: date) -> int:
    """Complete months lived. The 14th birthday is 168 months, to the day."""
    months = (on.year - born.year) * 12 + (on.month - born.month)
    if on.day < born.day:
        months -= 1
    return months


def _age_fits(specialty: dict, months: int) -> bool:
    low, high = specialty.get("min_age_months"), specialty.get("max_age_months")
    if low is not None and months < low:
        return False
    return not (high is not None and months > high)


def specialty_for_age(catalogue: dict, months: int) -> dict | None:
    """The age-limited specialty whose window holds this age.

    Paediatrics and general practice tile the calendar with no gap, so this is
    what an age refusal redirects to.
    """
    for specialty in catalogue["specialties"]:
        if specialty.get("min_age_months") is None and specialty.get("max_age_months") is None:
            continue
        if _age_fits(specialty, months):
            return specialty
    return None


def _patient_age_months(patient: dict | None, today: date) -> int | None:
    if not patient:
        return None
    born = patient.get("date_of_birth")
    if not born:
        return None
    try:
        return age_in_months(date.fromisoformat(str(born)[:10]), today)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# Catalogue lookups
# ---------------------------------------------------------------------------


def specialty_by_id(catalogue: dict, specialty_id: str | None) -> dict | None:
    if not specialty_id:
        return None
    return next((s for s in catalogue["specialties"] if s["id"] == specialty_id), None)


def provider_by_name(catalogue: dict, name: str | None) -> dict | None:
    """An exact published-name lookup. Not how a spoken name is resolved.

    A caller says "Dr. Iglesias", not "Dra. Elena Iglesias", so this is only a
    convenience for callers that already hold a published name. Spoken names are
    resolved fuzzily in the flow, and the rules take the id that resolution
    produced.
    """
    wanted = fold(name or "")
    if not wanted:
        return None
    return next((p for p in catalogue["providers"] if fold(p["name"]) == wanted), None)


def provider_by_id(catalogue: dict, provider_id: str | None) -> dict | None:
    if not provider_id:
        return None
    return next((p for p in catalogue["providers"] if p["id"] == provider_id), None)


def plan_covers_specialty(catalogue: dict, plan: dict | None, specialty_id: str | None) -> bool:
    """Whether this plan pays for this specialty at all.

    A plan that refuses a specialty refuses every provider of it, so no redirect
    inside that specialty can ever be valid. Keeping this here means every
    caller of :func:`providers_for` inherits the rule instead of re-deriving it.
    """
    if plan is None:
        return True
    specialty = specialty_by_id(catalogue, specialty_id)
    return _plan_covers(
        plan, "uncovered_specialty_names", specialty["name"] if specialty else None
    )


def accepts_plan(catalogue: dict, provider: dict, plan: dict | None) -> bool:
    """Whether this provider bills this plan. Refusal is per provider."""
    if plan is None:
        return True
    if not plan_covers_specialty(catalogue, plan, provider["specialty_id"]):
        return False
    insurer = plan["id"].lower()
    if any(ref.get("id", "").lower() == insurer for ref in provider.get("refused_insurers") or []):
        return False
    return not any(fold(name) == fold(provider["name"]) for name in plan.get("refused_by") or [])


def providers_for(
    catalogue: dict,
    specialty_id: str | None,
    *,
    location_name: str | None = None,
    plan: dict | None = None,
    exclude: set[str] | None = None,
) -> list[str]:
    """Everyone who could take this request. The redirect list, in one place."""
    out = []
    for provider in catalogue["providers"]:
        if specialty_id and provider["specialty_id"] != specialty_id:
            continue
        if location_name and not any(
            fold(name) == fold(location_name) for name in provider["location_names"]
        ):
            continue
        if exclude and provider["id"] in exclude:
            continue
        if not accepts_plan(catalogue, provider, plan):
            continue
        out.append(provider["id"])
    return out


def sites_serving(
    catalogue: dict, specialty_id: str | None, location_name: str | None = None
) -> list[dict]:
    """The sites where this request could physically happen."""
    hosts = {
        fold(name)
        for provider in catalogue["providers"]
        if not specialty_id or provider["specialty_id"] == specialty_id
        for name in provider["location_names"]
    }
    return [
        loc
        for loc in catalogue["locations"]
        if fold(loc["name"]) in hosts
        and (not location_name or fold(loc["name"]) == fold(location_name))
    ]


# ---------------------------------------------------------------------------
# Plans
# ---------------------------------------------------------------------------


def resolve_plan(catalogue: dict, patient: dict | None, named: str | None) -> dict | None:
    """The plan this request is quoted against.

    The caller's word wins: the second policy of PR-17 exists nowhere in the API,
    so a plan named on the call is the only way to find it. The one exception is
    self-pay, which is a plan a patient holds or does not — quoting ``privado``
    for someone whose record says otherwise is inventing cover to get past a
    refusal, so it falls back to the plan on the record.
    """
    wanted = (named or "").strip().lower()
    on_file = str((patient or {}).get("insurer") or "").strip().lower()
    if wanted == SELF_PAY and on_file and on_file != SELF_PAY:
        wanted = on_file
    if not wanted:
        wanted = on_file
    if not wanted:
        return None
    return next((p for p in catalogue["plans"] if p["id"].lower() == wanted), None)


def _plan_covers(plan: dict, key: str, name: str | None) -> bool:
    if not name:
        return True
    return not any(fold(other) == fold(name) for other in plan.get(key) or [])


def holds_referral(patient: dict | None, specialty_id: str | None) -> bool:
    if not patient or not specialty_id:
        return False
    return specialty_id.lower() in {
        str(r).lower() for r in (patient.get("referrals") or [])
    }


# ---------------------------------------------------------------------------
# The checks
# ---------------------------------------------------------------------------


def check_patient_rules(
    *,
    specialty_id: str | None,
    location_name: str | None = None,
    patient: dict | None,
    plan: dict | None,
    today: date,
) -> Verdict | None:
    """The rules that bite before any particular doctor is chosen.

    ``None`` means these rules allow it — not that a slot exists, and not that
    the plan's own referral or allowance rules do, which only ``/availability``
    knows and names in ``blocked``.
    """
    catalogue = load_catalog()
    specialty = specialty_by_id(catalogue, specialty_id)

    # 1. Age. The widest rule: it decides which specialty the caller belongs in
    #    before anything about insurance is asked.
    months = _patient_age_months(patient, today)
    if specialty and months is not None and not _age_fits(specialty, months):
        correct = specialty_for_age(catalogue, months)
        return Verdict(
            reason="not_eligible_age",
            detail=f"{specialty['name']} does not take {months} months",
            redirect_to=(
                providers_for(catalogue, correct["id"], location_name=location_name)
                if correct
                else []
            ),
            redirect_specialty=correct["id"] if correct else None,
        )

    # 2. The specialty's own referral requirement, against the referrals the
    #    directory record carries. A record that was never fetched is not the
    #    same as a missing referral, so this stands down without one and lets
    #    /availability answer.
    if specialty and specialty.get("referral_required") and patient and not holds_referral(
        patient, specialty_id
    ):
        return Verdict(
            reason="referral_required",
            detail=f"{specialty['name']} needs a referral the record does not hold",
        )

    if plan is None:
        return None

    # 3. The plan refuses the specialty. Adeslas and gynaecology: there is one
    #    gynaecologist, and the plan refuses the specialty itself, so there is
    #    nowhere to redirect to and the refusal is the answer.
    if not plan_covers_specialty(catalogue, plan, specialty_id):
        return Verdict(
            reason="specialty_not_covered",
            detail=f"{plan['name']} does not cover {specialty_id}",
            redirect_to=providers_for(catalogue, specialty_id, plan=plan),
        )

    # 4. The plan refuses the site. A refusal only when *no* site that could
    #    serve the request is covered — ASISA covers physiotherapy at Centro and
    #    Norte, and the one physiotherapist sits at Sur.
    candidates = sites_serving(catalogue, specialty_id, location_name)
    if candidates and not any(
        _plan_covers(plan, "uncovered_location_names", loc["name"]) for loc in candidates
    ):
        named = f" at {location_name}" if location_name else ""
        return Verdict(
            reason="location_not_covered",
            detail=f"{plan['name']} does not cover {specialty_id}{named}",
        )

    return None


def check_provider_rules(
    *,
    provider_id: str | None,
    specialty_id: str | None,
    location_name: str | None = None,
    plan: dict | None,
    today: date,
) -> Verdict | None:
    """The rules that bite once the caller has named a doctor.

    Takes the id the flow's fuzzy resolution produced, not a name: callers say
    "Dr. Iglesias" where the roster publishes "Dra. Elena Iglesias", and a name
    match that strict would silently miss the very redirect this rule exists to
    produce. Only consulted when ``/availability`` did not already name a
    restriction: the live API is the authority on its own providers.
    """
    catalogue = load_catalog()
    provider = provider_by_id(catalogue, provider_id)
    if provider is None:
        return None
    specialty = specialty_id or provider["specialty_id"]

    if not accepts_plan(catalogue, provider, plan):
        return Verdict(
            reason="provider_not_in_network",
            detail=f"{provider['name']} does not take {plan['name']}",
            redirect_to=providers_for(
                catalogue,
                specialty,
                location_name=location_name,
                plan=plan,
                exclude={provider["id"]},
            ),
            redirect_specialty=specialty,
        )

    leave = provider.get("leave")
    if leave and _on_leave(leave, today):
        return Verdict(
            reason="provider_on_leave",
            detail=str(leave.get("reason") or f"{provider['name']} is on leave"),
            redirect_to=providers_for(
                catalogue,
                specialty,
                location_name=location_name,
                plan=plan,
                exclude={provider["id"]},
            ),
            redirect_specialty=specialty,
        )
    return None


def _on_leave(leave: dict, today: date) -> bool:
    try:
        start = date.fromisoformat(str(leave["start"])[:10])
        end = date.fromisoformat(str(leave["end"])[:10])
    except (KeyError, ValueError):
        return False
    return start <= today <= end


# ---------------------------------------------------------------------------
# Nearest site (PR-15)
# ---------------------------------------------------------------------------

#: A degree of latitude, and of longitude at Madrid's latitude. Used only to
#: rank sites against each other, so an equirectangular approximation is exact
#: enough and avoids a geodesy dependency.
_KM_PER_DEGREE_LAT = 111.0
_KM_PER_DEGREE_LON = 85.0


def nearest_location(
    specialty_id: str | None, origin: tuple[float, float]
) -> dict | None:
    """The closest site that can actually serve this request.

    PR-15 is explicit that closest is not an answer when the closest site cannot
    serve: the rule says smallest distance *among sites that can serve*, so the
    serving set is the candidate list rather than a filter applied afterwards.
    """
    catalogue = load_catalog()
    candidates = sites_serving(catalogue, specialty_id) or catalogue["locations"]
    if not candidates:
        return None
    lat, lon = origin

    def distance_sq(location: dict) -> float:
        dlat = (location["latitude"] - lat) * _KM_PER_DEGREE_LAT
        dlon = (location["longitude"] - lon) * _KM_PER_DEGREE_LON
        return dlat * dlat + dlon * dlon

    return min(candidates, key=distance_sq)


_LANGUAGE_ALIASES = {
    "ca": "ca",
    "catalan": "ca",
    "catala": "ca",
    "es": "es",
    "spanish": "es",
    "espanol": "es",
    "castellano": "es",
    "en": "en",
    "english": "en",
    "ingles": "en",
    "gl": "gl",
    "galician": "gl",
    "gallego": "gl",
    "eu": "eu",
    "basque": "eu",
    "euskera": "eu",
    "euskara": "eu",
}


def normalize_language(spoken: str | None) -> str | None:
    """Map a spoken language name onto the catalogue's language codes."""
    if not spoken:
        return None
    key = fold(spoken).replace("-", " ").split()[0]
    return _LANGUAGE_ALIASES.get(key)


def language_constrains_booking(catalogue: dict, language: str | None) -> bool:
    """Whether this language is a booking filter.

    ``CL-language-default``: every provider already speaks Spanish, so pinning
    Spanish must not shrink the roster. A language only some of them speak
    (Catalan, English, …) is the constraint the case is testing.
    """
    if not language:
        return False
    return any(language not in (p.get("languages") or []) for p in catalogue["providers"])


def provider_speaks(catalogue: dict, provider_id: str, language: str | None) -> bool:
    if not language_constrains_booking(catalogue, language):
        return True
    provider = next((p for p in catalogue["providers"] if p["id"] == provider_id), None)
    return provider is not None and language in (provider.get("languages") or [])


def origin_from_spoken_place(spoken: str) -> tuple[float, float] | None:
    """Latitude/longitude of the hardcoded site neighbourhood the caller named."""
    return _origin_from_sites(spoken)


def location_from_spoken_place(
    spoken: str,
    specialty_id: str | None = None,
) -> dict | None:
    """The closest site that can serve, from a spoken street or neighbourhood.

    The origin is the published coordinate of whichever hardcoded service site
    the speech matches. Distance is then straight-line among sites that can
    serve (PR-15), so a neighbourhood next to a site that cannot take the
    specialty still lands on the next one that can.
    """
    origin = origin_from_spoken_place(spoken)
    if origin is None:
        return None
    return nearest_location(specialty_id, origin)


def today_in_madrid(now: datetime) -> date:
    """The clinic's date, which is the only date the rules are written against."""
    return now.date()
