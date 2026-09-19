import asyncio
from datetime import datetime
from types import SimpleNamespace

from submission import CallSubmission


def test_failed_submission_can_retry():
    class Client:
        calls = 0

        async def post_submission(self, action):
            self.calls += 1
            if self.calls == 1:
                raise OSError("offline")

    client = Client()
    sub = CallSubmission("x", client)
    asyncio.run(sub.close())
    asyncio.run(sub.close())
    assert client.calls == 2


def test_registration_validation():
    from flows.registration import validate_registration

    values = dict(
        given_name="Ana",
        first_surname="Test",
        second_surname="Test",
        national_id="12345678Z",
        date_of_birth="1990-01-02",
        phone="612345678",
        email="ana@example.com",
        insurer="sanitas",
    )
    assert validate_registration(values, datetime(2026, 9, 19))[1] == []
    values["national_id"] = "12345678A"
    assert "national_id" in validate_registration(values, datetime(2026, 9, 19))[1]


def test_reception_routes_without_losing_state():
    from flows.reception import route_request

    manager = SimpleNamespace(state={"offers": {}, "patient": None})
    result, node = asyncio.run(route_request({"intent": "register"}, manager))
    assert node["name"] == "registration"
    assert manager.state["intent"] == "register"


def test_roster_titles_are_spoken_in_full():
    from flows.common import spoken_provider_name

    assert spoken_provider_name("Dra. Elena Iglesias") == "Doctora Elena Iglesias"
    assert spoken_provider_name("D. Álvaro Cid") == "Don Álvaro Cid"


def test_a_tool_speaks_a_fixed_line_before_it_runs():
    from pipecat.frames.frames import TTSSpeakFrame

    from flows.common import TOOL_PROGRESS, announce
    from flows.identification import create_identify_node

    spoken = []

    async def queue_frames(frames):
        spoken.extend(frames)

    @announce("search_patient")
    async def lookup(args, flow_manager):
        return "ran"

    manager = SimpleNamespace(state={}, worker=SimpleNamespace(queue_frames=queue_frames))
    assert asyncio.run(lookup({}, manager)) == "ran"
    assert isinstance(spoken[0], TTSSpeakFrame)
    assert spoken[0].text == TOOL_PROGRESS["search_patient"]
    assert {fn.name for fn in create_identify_node()["functions"]} >= {
        "search_patient",
        "start_registration",
    }


def test_near_names_require_clarification():
    from flows.booking import resolve_provider

    assert len(resolve_provider("Saez")) == 2
    assert len(resolve_provider("Saez", "general_practice")) == 1


def test_a_spoken_title_separates_the_near_miss_pair():
    """The roster publishes "Dr."/"Dra." and that alone halves the candidates."""
    from flows.booking import resolve_provider

    assert [p["id"] for p in resolve_provider("Dra. Iglesias")] == ["PR05"]
    assert [p["id"] for p in resolve_provider("Dr. Saez")] == ["PR03"]


def test_an_adult_patient_removes_the_paediatric_candidate():
    """Sáez/Sáenz no longer needs a question when the patient is an adult.

    A neutral "Doctor Sáez" matches both, and age settles it: paediatrics
    cannot take a 61-year-old.
    """
    from flows.booking import resolve_provider

    adult = {"date_of_birth": "1965-02-24", "insurer": "asisa", "referrals": []}
    assert [p["id"] for p in resolve_provider("Dr. Saez", patient=adult)] == ["PR03"]


def test_extra_words_do_not_hide_a_unique_name():
    from flows.booking import resolve_provider

    assert [p["id"] for p in resolve_provider("I want Dr Ortiz please")] == ["PR01"]
    assert [p["id"] for p in resolve_provider("Martin Saez")] == ["PR03"]
    assert [p["id"] for p in resolve_provider("Elena Iglesias")] == ["PR05"]


def test_a_wrong_specialty_does_not_drop_a_unique_name():
    """The model often guesses the specialty enum. A unique spoken name still wins."""
    from flows.booking import resolve_provider

    assert [p["id"] for p in resolve_provider("Elena Iglesias", "general_practice")] == [
        "PR05"
    ]


def test_a_neutral_title_still_leaves_iglesias_ambiguous():
    """Pin the deliberate limit: neither specialty is ruled out here, so the
    clarifying question stays. Nobody should mistake this for a bug later."""
    from clinic_catalog import load_catalog
    from flows.booking import resolve_provider

    gloria = {
        "date_of_birth": "1999-06-15",
        "insurer": "dkv",
        "referrals": ["dermatology"],
    }
    plan = next(p for p in load_catalog()["plans"] if p["id"] == "dkv")
    found = resolve_provider("Doctor Iglesias", patient=gloria, plan=plan)
    assert sorted(p["id"] for p in found) == ["PR05", "PR06"]
    # The spoken title does settle it, which is the improvement worth having.
    assert [p["id"] for p in resolve_provider("Dra. Iglesias", patient=gloria, plan=plan)] == [
        "PR05"
    ]


def test_a_repeated_confirmation_is_a_retry_not_a_conflict():
    """A caller answering "yes" twice must not hear that the booking failed."""
    from handlers import confirm_offer, get_earliest_slot
    class Client:
        async def availability(self, *args, **kwargs):
            return {
                "slots": [
                    dict(
                        provider_id="PR01",
                        provider_name="Dra. Carmen Ortiz Vidal",
                        location_id="centro",
                        specialty_id="general_practice",
                        appointment_type_id="review",
                        start_time="2026-09-21T09:00:00+02:00",
                        payable_with=["sanitas"],
                    )
                ],
                "blocked": [],
            }

        async def post_submission(self, action):
            return {"received": True}

    client = Client()
    # A readback is not consent: the yes has to come in a turn of the caller's
    # own, after the offer was read out.
    messages = [{"role": "user", "content": "A review, as soon as you have one."}]
    manager = SimpleNamespace(
        state={
            "connected_at": datetime.fromisoformat("2026-09-19T10:00:00+02:00"),
            "client": client,
            "submission": CallSubmission("call-x", client),
            "patient": {"patient_id": "P1", "insurer": "sanitas"},
            "offers": {},
        },
        get_current_context=lambda: messages,
    )
    result, _ = asyncio.run(get_earliest_slot({"specialty": "general_practice"}, manager))
    messages.append({"role": "user", "content": "Yes, that one please."})
    first, _ = asyncio.run(confirm_offer({"offer_id": result["offer_id"]}, manager))
    second, node = asyncio.run(confirm_offer({"offer_id": result["offer_id"]}, manager))
    assert first["status"] == "accepted"
    assert second["status"] == "accepted"
    # Another request may now follow in the same call, so the booking ends in the
    # "anything else?" node; the farewell comes from finish_call.
    assert node["name"] == "request_complete"


def test_concurrent_call_isolation():
    from flows.reception import route_request
    from flows.registration import confirm_registration, prepare_registration

    async def run():
        posted = []

        class Client:
            async def search_directory(self, **kwargs):
                await asyncio.sleep(0)
                return []

            async def post_submission(self, action):
                await asyncio.sleep(0)
                posted.append(action)

        async def call(i):
            messages = [{"role": "user", "content": f"I am Caller{i}, a new patient."}]
            manager = SimpleNamespace(
                state={
                    "connected_at": datetime(2026, 9, 19),
                    "client": Client(),
                    "submission": CallSubmission(str(i), Client()),
                    "offers": {},
                },
                get_current_context=lambda: messages,
            )
            await route_request({"intent": "register"}, manager)
            result, node = await prepare_registration(
                dict(
                    given_name=f"Caller{i}",
                    first_surname="Test",
                    second_surname="Test",
                    national_id="12345678Z",
                    date_of_birth="1990-01-02",
                    phone="612345678",
                    email=f"caller{i}@example.com",
                    insurer="sanitas",
                ),
                manager,
            )
            assert result["status"] == "needs_confirmation"
            assert not posted or all(p["call_id"] != str(i) for p in posted)
            messages.append({"role": "user", "content": "Yes, those details are correct."})
            await confirm_registration({"confirmed": True}, manager)
            await asyncio.gather(
                manager.state["submission"].flush(), manager.state["submission"].flush()
            )

        await asyncio.gather(*(call(i) for i in range(20)))
        assert len(posted) == 20
        assert all(
            p["action"] == "REGISTER" and p["given_name"] == "Caller" + p["call_id"] for p in posted
        )

    asyncio.run(run())


def test_provider_site_and_weekday_preserved():
    from handlers import get_earliest_slot

    class Client:
        async def availability(self, *args, **kwargs):
            return {
                "slots": [
                    dict(
                        provider_id=p,
                        provider_name=p,
                        location_id=l,
                        appointment_type_id="review",
                        start_time=t,
                        payable_with=["sanitas"],
                    )
                    for p, l, t in [
                        ("PR01", "centro", "2026-09-21T09:00:00+02:00"),
                        ("PR03", "sur", "2026-09-21T09:00:00+02:00"),
                        ("PR03", "centro", "2026-09-25T09:00:00+02:00"),
                    ]
                ],
                "blocked": [],
            }

    client = Client()
    manager = SimpleNamespace(
        state={
            "connected_at": datetime.fromisoformat("2026-09-19T10:00:00+02:00"),
            "client": client,
            "submission": CallSubmission("x", client),
            "patient": {"patient_id": "P1", "insurer": "sanitas"},
            "offers": {},
        }
    )
    result, node = asyncio.run(
        get_earliest_slot(
            {"provider_name": "Martin Saez", "site": "centro", "weekday": "monday"}, manager
        )
    )
    offer = manager.state["offers"][result["offer_id"]]
    assert offer["provider_id"] == "PR03" and offer["location_id"] == "centro"
    assert offer["slot"].startswith("2026-09-25")


def test_real_node_schemas_construct():
    from pipecat.flows import FlowsFunctionSchema

    from flows.reception import create_reception_node
    from flows.registration import create_registration_confirm_node, create_registration_node

    for node in (
        create_reception_node(),
        create_registration_node(),
        create_registration_confirm_node(),
    ):
        assert node["task_messages"]
        assert all(isinstance(f, FlowsFunctionSchema) for f in node["functions"])


def test_unpayable_and_closed_slots_rejected():
    from booking import pick_offer

    connected = datetime.fromisoformat("2026-10-10T10:00:00+02:00")
    patient = {"patient_id": "P1", "insurer": "sanitas"}
    slots = [
        dict(provider_id="PR01", start_time=t, payable_with=p)
        for t, p in [("2026-10-11T10:00:00+02:00", []), ("2026-10-12T10:00:00+02:00", ["sanitas"])]
    ]
    assert pick_offer({"slots": slots}, patient, connected) is None


def test_client_register_flat_and_duplicate(monkeypatch):
    import httpx

    from clients.clinic_client import ClinicClient

    real_client = httpx.AsyncClient
    requests = []

    def handle(request):
        import json

        requests.append(json.loads(request.content))
        return httpx.Response(409, json={"received": True})

    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real_client(**kwargs, transport=httpx.MockTransport(handle)),
    )
    client = ClinicClient("https://example.invalid/api", "test")
    result = asyncio.run(
        client.post_submission({"action": "REGISTER", "call_id": "x", "given_name": "Ana"})
    )
    assert result == {"received": True}
    assert requests == [{"call_id": "x", "given_name": "Ana"}]


def test_provider_leave_requires_fallback_agreement():
    from handlers import get_earliest_slot

    class Client:
        async def availability(self, *args, **kwargs):
            return {
                "slots": [
                    dict(
                        provider_id="PR07",
                        provider_name="Other",
                        location_id="norte",
                        appointment_type_id="review",
                        start_time="2026-09-21T09:00:00+02:00",
                        payable_with=["sanitas"],
                    )
                ],
                "blocked": [{"provider_id": "PR02", "restriction": "provider_on_leave"}],
            }

    c = Client()
    manager = SimpleNamespace(
        state={
            "connected_at": datetime.fromisoformat("2026-09-19T10:00:00+02:00"),
            "client": c,
            "submission": CallSubmission("x", c),
            "patient": {"patient_id": "P", "insurer": "sanitas"},
            "offers": {},
        }
    )
    result, _ = asyncio.run(
        get_earliest_slot({"provider_name": "Pablo Requena", "site": "norte"}, manager)
    )
    # A doctor on leave is a redirect, not a refusal: another doctor of the same
    # specialty at the same site can serve, and the answer shape PR-03 expects is
    # a booking. Consent is still taken — by the confirm node, out loud, before
    # anything is submitted.
    assert result["status"] == "offer"
    assert result["note"] == "provider_on_leave"
    assert manager.state["submission"].pending["action"] == "NO_ACTION"


def test_redirect_never_widens_the_site_on_its_own():
    """If nobody at the requested site can serve, say so rather than widen."""
    from handlers import get_earliest_slot

    class Client:
        async def availability(self, *args, **kwargs):
            return {
                "slots": [],
                "blocked": [{"provider_id": "PR02", "restriction": "provider_on_leave"}],
            }

    c = Client()
    manager = SimpleNamespace(
        state={
            "connected_at": datetime.fromisoformat("2026-09-19T10:00:00+02:00"),
            "client": c,
            "submission": CallSubmission("x", c),
            "patient": {"patient_id": "P", "insurer": "sanitas"},
            "offers": {},
        }
    )
    result, _ = asyncio.run(
        get_earliest_slot({"provider_name": "Pablo Requena", "site": "norte"}, manager)
    )
    assert result["status"] == "no_slots"
    assert manager.state["submission"].pending["reason"] == "provider_on_leave"


def test_registration_accepts_catalogue_plan_name():
    from flows.registration import validate_registration

    values = dict(
        given_name="Ana",
        first_surname="Test",
        second_surname="Test",
        national_id="12345678Z",
        date_of_birth="1990-01-02",
        phone="612345678",
        email="ana@example.com",
        insurer="Mapfre Salud",
    )
    patient, errors = validate_registration(values, datetime(2026, 9, 19))
    assert not errors
    assert patient["insurer"] == "mapfre"


def test_twenty_concurrent_booking_calls_are_isolated():
    from flows.reception import route_request
    from handlers import confirm_offer, get_earliest_slot, search_patient

    async def run():
        posted = []

        class Client:
            def __init__(self, i):
                self.i = i

            async def search_directory(self, **kwargs):
                await asyncio.sleep(0)
                assert kwargs["name"] == f"Caller {self.i}"
                return [
                    dict(
                        patient_id=f"P{self.i}",
                        given_name=f"Caller{self.i}",
                        first_surname="Test",
                        has_visited_before=True,
                        phone=f"6{self.i:08d}",
                        insurer="sanitas",
                    )
                ]

            async def availability(self, *args, **kwargs):
                await asyncio.sleep(0)
                assert args[3] == f"P{self.i}"
                return {
                    "slots": [
                        dict(
                            provider_id="PR01",
                            provider_name="Doctor",
                            location_id="centro",
                            specialty_id="general_practice",
                            appointment_type_id="review",
                            start_time=f"2026-09-21T{9 + self.i // 4:02d}:{15 * (self.i % 4):02d}:00+02:00",
                            payable_with=["sanitas"],
                        )
                    ],
                    "blocked": [],
                }

            async def post_submission(self, action):
                await asyncio.sleep(0)
                posted.append(action)

        async def call(i):
            client = Client(i)
            messages = [{"role": "user", "content": f"I am Caller {i}."}]
            manager = SimpleNamespace(
                state={
                    "connected_at": datetime.fromisoformat("2026-09-19T10:00:00+02:00"),
                    "client": client,
                    "submission": CallSubmission(f"call{i}", client),
                    "patient": None,
                    "offers": {},
                    "identify_attempts": 0,
                },
                get_current_context=lambda: messages,
            )
            await route_request({"intent": "book"}, manager)
            result, _ = await search_patient(
                {"stated_name": f"Caller {i}", "id_type": "phone", "id_value": f"6{i:08d}"}, manager
            )
            assert result["status"] == "found"
            result, _ = await get_earliest_slot(
                {"specialty": "general_practice", "site": "centro"}, manager
            )
            offer_id = result["offer_id"]
            messages.append({"role": "user", "content": "Yes, that one please."})
            await confirm_offer({"offer_id": offer_id}, manager)
            await confirm_offer({"offer_id": offer_id}, manager)
            await manager.state["submission"].flush()

        await asyncio.gather(*(call(i) for i in range(20)))
        assert len(posted) == 20
        for payload in posted:
            i = int(payload["call_id"][4:])
            assert payload["patient_id"] == f"P{i}"
            assert payload["slot"] == f"2026-09-21T{9 + i // 4:02d}:{15 * (i % 4):02d}:00+02:00"
            assert payload["action"] == "BOOK"

    asyncio.run(run())


def test_identification_is_active_extracted_flow():
    from flows.identification import create_identify_node, search_patient
    from flows.reception import route_request

    manager = SimpleNamespace(state={})
    _, node = asyncio.run(route_request({"intent": "book"}, manager))
    assert node["name"] == create_identify_node()["name"]
    assert node["functions"][0].handler is search_patient


def test_spoken_identity_and_booking_cues_reuse_what_was_said():
    from flows.booking import spoken_booking_cues
    from flows.identification import create_identify_node, spoken_identity

    ctx = [
        {
            "role": "user",
            "content": (
                "My name is Josefa Dominguez Navarro, DNI 12345678Z, "
                "I need the GP at Arenal Sur"
            ),
        }
    ]
    manager = SimpleNamespace(get_current_context=lambda: ctx)
    identity = spoken_identity(manager)
    assert identity["id_value"] == "12345678Z"
    assert "Josefa" in identity["name"]
    cues = spoken_booking_cues(manager)
    assert cues["specialty"] == "general_practice"
    assert cues["site"] == "sur"
    prompt = create_identify_node(manager)["task_messages"][0]["content"]
    assert "12345678Z" in prompt
    assert "Do not ask for them again" in prompt


def test_immutable_payload_after_failed_delivery():
    class Client:
        def __init__(self):
            self.posted = []

        async def post_submission(self, action):
            self.posted.append(action)
            if len(self.posted) == 1:
                raise OSError("offline")

    client = Client()
    sub = CallSubmission("x", client)
    sub.set_book({"patient_id": "P"})
    asyncio.run(sub.flush())
    sub.pending["patient_id"] = "mutated"
    asyncio.run(sub.flush())
    assert client.posted[0] == client.posted[1]
