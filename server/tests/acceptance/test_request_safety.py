import asyncio
from datetime import date
from types import SimpleNamespace

import pytest

from dates import resolve_named
from flows.common import gated_confirmation
from submission import CallSubmission


def test_missing_confirmation_context_fails_closed():
    manager = SimpleNamespace(state={})
    assert gated_confirmation('confirm', manager)[0]['reason_code'] == 'missing_confirmation'


def test_conflicting_weekday_requires_clarification():
    with pytest.raises(ValueError, match='weekday'):
        resolve_named('tuesday', 12, 10, 2026, date(2026, 9, 19))


def test_uncertain_payload_cannot_be_replaced():
    class Client:
        async def post_submission(self, payload):
            raise OSError('timeout')
    submission = CallSubmission('c', Client())
    submission.set_cancel('A1')
    assert asyncio.run(submission.flush()) is False
    with pytest.raises(RuntimeError):
        submission.set_cancel('A2')


def test_partial_plan_still_needs_delivery():
    class Client:
        async def post_submission(self, payload):
            if payload['appointment_id'] == 'A2':
                raise OSError('timeout')
    submission = CallSubmission('c', Client())
    submission.set_cancel('A1')
    submission.add_action({'action': 'CANCEL', 'appointment_id': 'A2'})
    asyncio.run(submission.flush())
    assert submission.needs_delivery


def test_requests_deliver_independently_without_replacing_accepted_effects():
    posted = []
    class Client:
        async def post_submission(self, payload):
            posted.append(payload)
    root = CallSubmission('c', Client())
    first = root.new_request()
    first.set_cancel('A1')
    asyncio.run(root.flush())
    second = root.new_request()
    second.set_cancel('A2')
    asyncio.run(root.flush())
    assert [p['appointment_id'] for p in posted] == ['A1', 'A2']
    assert len(root.actions) == 2


def test_revision_invalidates_old_offer_and_requires_later_confirmation():
    from flows.requests import prepare_proposal, revise_request, valid_proposal
    messages = [{'role': 'user', 'content': 'yes'}]
    manager = SimpleNamespace(state={'offers': {}}, get_current_context=lambda: messages)
    prepare_proposal(manager, 'offer-1')
    assert not valid_proposal(manager, 'offer-1')
    messages.append({'role': 'user', 'content': 'yes'})
    assert valid_proposal(manager, 'offer-1')
    revise_request(manager)
    assert not valid_proposal(manager, 'offer-1')


def test_cancel_route_prepares_verified_appointment_and_waits_for_new_yes():
    from flows.appointments import confirm_cancellation, select_appointment
    from flows.identification import search_patient
    from flows.reception import route_request
    posted = []
    class Client:
        async def search_directory(self, **kwargs):
            return [dict(patient_id='P1', national_id='12345678Z', given_name='Ana', first_surname='Test', has_visited_before=True)]
        async def appointments(self, patient_id):
            return [dict(appointment_id='A1', patient_id=patient_id, start_time='2026-10-10T10:00:00+02:00', provider_id='PR01', location_id='centro', appointment_type_id='review')]
        async def post_submission(self, payload):
            posted.append(payload)
    messages = [{'role': 'user', 'content': 'cancel my appointment'}]
    client = Client()
    manager = SimpleNamespace(state={'submission': CallSubmission('c', client), 'client': client}, get_current_context=lambda: messages)
    result, node = asyncio.run(route_request({'intent': 'cancel'}, manager))
    assert node['name'] == 'identify'
    result, node = asyncio.run(search_patient(dict(id_type='national_id', id_value='12345678Z', stated_name='Ana Test'), manager))
    assert node['name'] == 'appointments'
    assert asyncio.run(select_appointment({'appointment_id': 'invented'}, manager))[0]['status'] == 'invalid_appointment'
    assert asyncio.run(select_appointment({'appointment_id': 'A1'}, manager))[0]['status'] == 'needs_confirmation'
    assert not posted
    assert asyncio.run(confirm_cancellation({}, manager))[0]['status'] == 'needs_confirmation'
    messages.append({'role': 'user', 'content': 'yes'})
    assert asyncio.run(confirm_cancellation({}, manager))[0]['status'] == 'accepted'
    assert posted == [{'call_id': 'c', 'action': 'CANCEL', 'appointment_id': 'A1'}]


def test_cancel_two_posts_both_diary_ids():
    from flows.appointments import confirm_cancellation, select_appointment
    from flows.identification import search_patient
    from flows.reception import route_request
    posted = []
    class Client:
        async def search_directory(self, **kwargs):
            return [dict(patient_id='P1', national_id='12345678Z', given_name='Ana', first_surname='Test', has_visited_before=True)]
        async def appointments(self, patient_id):
            return [
                dict(appointment_id='A1', patient_id=patient_id, start_time='2026-10-10T10:00:00+02:00', provider_id='PR01', location_id='centro', appointment_type_id='review'),
                dict(appointment_id='A2', patient_id=patient_id, start_time='2026-10-12T11:00:00+02:00', provider_id='PR02', location_id='norte', appointment_type_id='review'),
            ]
        async def post_submission(self, payload):
            posted.append(payload)
    messages = [{'role': 'user', 'content': 'cancel both my appointments'}]
    client = Client()
    manager = SimpleNamespace(state={'submission': CallSubmission('c', client), 'client': client}, get_current_context=lambda: messages)
    asyncio.run(route_request({'intent': 'cancel'}, manager))
    asyncio.run(search_patient(dict(id_type='national_id', id_value='12345678Z', stated_name='Ana Test'), manager))
    result, node = asyncio.run(select_appointment({'appointment_ids': ['A1', 'A2']}, manager))
    assert result['status'] == 'needs_confirmation'
    assert node['name'] == 'cancel_confirm'
    assert not posted
    messages.append({'role': 'user', 'content': 'yes, both'})
    result, node = asyncio.run(confirm_cancellation({}, manager))
    assert result['status'] == 'accepted'
    assert [p['appointment_id'] for p in posted] == ['A1', 'A2']
    assert all(p['action'] == 'CANCEL' for p in posted)


def test_reschedule_submission_uses_exact_contract():
    posted = []
    class Client:
        async def post_submission(self, payload):
            posted.append(payload)
    submission = CallSubmission('c', Client())
    submission.set_reschedule('A1', dict(patient_id='P1', appointment_type_id='review', provider_id='PR01', location_id='centro', slot='2026-10-10T10:00:00+02:00', policy_id='sanitas'))
    asyncio.run(submission.flush())
    assert set(posted[0]) == {'call_id', 'action', 'appointment_id', 'provider_id', 'location_id', 'slot', 'policy_id'}


def test_nonaffirmative_transcript_is_not_consent():
    manager = SimpleNamespace(state={}, get_current_context=lambda: [{'role':'user', 'content':'my phone is 612345678'}])
    assert gated_confirmation('confirm', manager)[0]['reason_code'] == 'missing_confirmation'


def test_gated_confirmation_reads_llm_specific_messages():
    from pipecat.processors.aggregators.llm_context import LLMSpecificMessage

    manager = SimpleNamespace(
        state={},
        get_current_context=lambda: [
            LLMSpecificMessage(
                llm="google",
                message={"role": "user", "content": "Yes, that one please."},
            )
        ],
    )
    assert gated_confirmation("confirm", manager) is None


def test_reschedule_keeps_existing_type_and_requires_later_consent():
    from datetime import datetime

    from flows.booking import confirm_offer, get_earliest_slot, revise_search
    posted = []
    class Client:
        async def availability(self, *args, **kwargs):
            return {'slots': [dict(provider_id='PR01', provider_name='Doctor', location_id='centro', specialty_id='general_practice', appointment_type_id=kind, start_time=time, payable_with=['sanitas']) for kind, time in [('first_visit','2026-09-21T09:00:00+02:00'), ('review','2026-09-21T10:00:00+02:00')]], 'blocked': []}
        async def post_submission(self, payload):
            posted.append(payload)
    client = Client()
    messages = [{'role':'user','content':'move my appointment'}]
    manager = SimpleNamespace(state={'connected_at':datetime.fromisoformat('2026-09-19T10:00:00+02:00'), 'intent':'reschedule', 'client':client, 'submission':CallSubmission('c', client), 'offers':{}, 'patient':{'patient_id':'P1','insurer':'sanitas','given_name':'Ana','first_surname':'Test'}, 'appointment':{'appointment_id':'A1','patient_id':'P1','appointment_type_id':'review'}}, get_current_context=lambda: messages)
    result, _ = asyncio.run(get_earliest_slot({'specialty':'general_practice'}, manager))
    assert manager.state['offers'][result['offer_id']]['appointment_type_id'] == 'review'
    assert asyncio.run(confirm_offer({'offer_id':result['offer_id']}, manager))[0]['status'] == 'needs_confirmation'
    premature = asyncio.run(confirm_offer({'offer_id':result['offer_id']}, manager))[0]
    assert premature['status'] == 'needs_confirmation'
    assert 'Ask them' not in premature['instruction']
    assert 'Stay completely silent' in premature['instruction']
    messages.append({'role':'user','content':'yes'})
    assert asyncio.run(confirm_offer({'offer_id':result['offer_id']}, manager))[0]['status'] == 'accepted'
    assert posted[0]['action'] == 'RESCHEDULE'
    assert posted[0]['appointment_id'] == 'A1'
    asyncio.run(revise_search(manager))
    assert result['offer_id'] in manager.state['offers']
