import asyncio

from submission import CallSubmission

NO_ACTION = {"action": "NO_ACTION", "reason": "patient_not_found"}
OFFER = {"patient_id": "P00001", "provider_id": "PR01", "location_id": "centro",
         "appointment_type_id": "review", "slot": "2026-09-19T11:00:00+02:00", "policy_id": "mapfre"}


class FakeClient:
    def __init__(self):
        self.posted = []

    async def post_submission(self, action):
        self.posted.append(action)


def test_default_is_no_action():
    client = FakeClient()
    sub = CallSubmission("call-1", client)
    assert sub.pending == NO_ACTION
    asyncio.run(sub.flush())
    assert len(client.posted) == 1
    assert client.posted[0]["call_id"] == "call-1"
    assert all(client.posted[0][k] == v for k, v in NO_ACTION.items())


def test_set_book_then_flush():
    client = FakeClient()
    sub = CallSubmission("call-2", client)
    sub.set_book(OFFER)
    asyncio.run(sub.flush())
    assert len(client.posted) == 1
    assert client.posted[0]["action"] == "BOOK"
    assert client.posted[0]["call_id"] == "call-2"
    assert all(client.posted[0][k] == v for k, v in OFFER.items())


def test_flush_is_idempotent():
    client = FakeClient()
    sub = CallSubmission("call-3", client)
    asyncio.run(sub.flush())
    asyncio.run(sub.flush())
    assert len(client.posted) == 1


def test_set_register():
    client = FakeClient()
    sub = CallSubmission("call-4", client)
    sub.set_register({
        "given_name": "Joaquín",
        "first_surname": "González",
        "second_surname": "Ortega",
        "national_id": "18921027P",
        "date_of_birth": "1970-06-25",
        "phone": "783869132",
        "email": "joaquingonzalez24@hotmail.com",
        "insurer": "cigna",
    })
    asyncio.run(sub.flush())
    assert client.posted[0]["action"] == "REGISTER"
    assert client.posted[0]["insurer"] == "cigna"
    assert client.posted[0]["national_id"] == "18921027P"
