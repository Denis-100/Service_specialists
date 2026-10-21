from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import sqlite3

import pytest
from app import create_app, now


@pytest.fixture
def app(tmp_path):
    return create_app({'TESTING': True, 'DATABASE': str(tmp_path / 'test.sqlite'), 'SECRET_KEY': 'test-only'})


def client(app, role='client'):
    c = app.test_client()
    token = c.get('/api/session').json['csrf']
    r = c.post('/api/login', json={'email': role + '@svobodno.demo', 'password': 'Demo2026!'}, headers={'X-CSRF-Token': token})
    assert r.status_code == 200
    return c


def post(c, url, data):
    token = c.get('/api/session').json['csrf']
    return c.post('/api' + url, json=data, headers={'X-CSRF-Token': token})


def test_catalog_and_consultation(app):
    c = app.test_client()
    data = c.get('/api/catalog').json
    assert len(data['specialists']) == 6
    assert len(data['specialists'][0]['services']) == 2
    assert len(c.get('/api/slots/1').json['slots']) == 28
    assert post(c, '/consult', {'message': 'Протекает кран'}).json['specialist_id'] == 1
    assert post(c, '/consult', {'message': 'запах газа'}).json['specialist_id'] is None
    assert post(c, '/consult', {'message': ''}).status_code == 400


def test_booking_cancellation_and_persistence(app):
    c = client(app)
    slot = c.get('/api/slots/1').json['slots'][0]
    payload = {'slot_id': slot['id'], 'address': 'Киров, Ленина 10', 'quantity': 1}
    assert post(c, '/bookings', {'slot_id': slot['id']}).status_code == 400
    booking = post(c, '/bookings', payload)
    assert booking.status_code == 201
    assert post(c, '/bookings', payload).status_code == 409
    assert c.get('/api/slots/1').json['slots'][0]['remaining'] == 0
    fresh = client(app)
    assert any(b['id'] == booking.json['id'] for b in fresh.get('/api/dashboard').json['bookings'])
    assert post(c, f"/bookings/{booking.json['id']}/cancel", {}).status_code == 200
    assert c.get('/api/slots/1').json['slots'][0]['remaining'] == 1
    assert post(c, f"/bookings/{booking.json['id']}/cancel", {}).status_code == 409


def test_group_capacity_and_concurrent_booking(app):
    a, b = client(app), client(app, 'ivan')
    slot = next(s for s in a.get('/api/slots/4').json['slots'] if s['capacity'] == 6)
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda c: post(c, '/bookings', {'slot_id': slot['id'], 'quantity': 4}).status_code, [a, b]))
    assert sorted(results) == [201, 409]
    remaining = next(s for s in a.get('/api/slots/4').json['slots'] if s['id'] == slot['id'])['remaining']
    assert remaining == 2


def test_roles_csrf_and_other_clients(app):
    anon = app.test_client()
    assert anon.get('/api/dashboard').status_code == 401
    assert anon.post('/api/login', json={}).status_code == 403
    c, other, master = client(app), client(app, 'ivan'), client(app, 'master')
    assert c.get('/api/admin').status_code == 403
    assert c.get('/api/master').status_code == 403
    assert post(master, '/bookings', {}).status_code == 403
    slot = c.get('/api/slots/1').json['slots'][0]
    b = post(c, '/bookings', {'slot_id': slot['id'], 'address': 'Киров, Ленина 10'}).json
    assert post(other, f"/bookings/{b['id']}/cancel", {}).status_code == 403
    assert post(master, f"/bookings/{b['id']}/complete", {}).status_code == 409
    assert post(master, f"/master/slots/{slot['id']}/close", {}).status_code == 409
    assert post(master, f"/bookings/{b['id']}/cancel", {}).status_code == 200
    assert post(master, f"/master/slots/{slot['id']}/close", {}).status_code == 200


def test_schedule_and_service_creation(app):
    c = client(app, 'master')
    services = c.get('/api/master').json['services']
    when = (now() + timedelta(days=20)).replace(hour=10, minute=0).isoformat(timespec='minutes')
    d = {'service_id': services[0]['id'], 'starts_at': when, 'capacity': 1}
    assert post(c, '/master/slots', d).status_code == 201
    assert post(c, '/master/slots', d).status_code == 409
    d['starts_at'] = (now() - timedelta(days=1)).isoformat(timespec='minutes')
    assert post(c, '/master/slots', d).status_code == 400
    d['starts_at'] = when
    d['capacity'] = 2
    assert post(c, '/master/slots', d).status_code == 400
    assert post(c, '/master/services', {'title': 'Семинар о сантехнике', 'price': 500, 'duration': 60, 'mode': 'Группа'}).status_code == 201
    service = c.get('/api/master').json['services'][-1]
    d.update(service_id=service['id'], starts_at=(now()+timedelta(days=21)).isoformat(timespec='minutes'), capacity=10)
    assert post(c, '/master/slots', d).status_code == 201
    # A different specialist's service cannot be used.
    d['service_id'] = 7
    assert post(c, '/master/slots', d).status_code == 404


def test_review_moderation(app):
    c, admin = client(app), client(app, 'admin')
    booking = next(b for b in c.get('/api/dashboard').json['bookings'] if b['status'] == 'completed')
    payload = {'booking_id': booking['id'], 'rating': 5, 'content': 'Очень аккуратная работа, всё исправлено!'}
    assert post(c, '/reviews', payload).status_code == 201
    assert post(c, '/reviews', payload).status_code == 409
    review = c.get('/api/dashboard').json['reviews'][0]
    assert review['status'] == 'pending'
    assert not c.get('/api/catalog').json['specialists'][0]['reviews']
    assert post(c, '/admin/moderate', {'kind': 'review', 'id': review['id'], 'status': 'approved'}).status_code == 403
    assert post(admin, '/admin/moderate', {'kind': 'review', 'id': review['id'], 'status': 'approved'}).status_code == 200
    assert len(c.get('/api/catalog').json['specialists'][0]['reviews']) == 1
    assert post(admin, '/admin/moderate', {'kind': 'review', 'id': review['id'], 'status': 'rejected'}).status_code == 200
    assert not c.get('/api/catalog').json['specialists'][0]['reviews']


def test_moderation_blocks_booking(app):
    c, admin = client(app), client(app, 'admin')
    slot = c.get('/api/slots/1').json['slots'][0]
    assert post(admin, '/admin/moderate', {'kind': 'specialist', 'id': 1, 'active': False}).status_code == 200
    assert len(c.get('/api/catalog').json['specialists']) == 5
    assert not c.get('/api/slots/1').json['slots']
    assert post(c, '/bookings', {'slot_id': slot['id'], 'address': 'Киров, Ленина 10'}).status_code == 409
    assert post(c, '/consult', {'message': 'протекает кран'}).json['specialist_id'] is None


def test_registration_and_search_history(app):
    c = app.test_client()
    d = {'name': 'Новый клиент', 'email': 'new@example.test', 'password': 'testpassword'}
    result = post(c, '/register', d)
    assert result.status_code == 200
    assert result.json['user']['role'] == 'client'
    assert post(c, '/searches', {'query': 'Английский'}).status_code == 200
    assert c.get('/api/dashboard').json['searches'][0]['query'] == 'Английский'
    assert post(c, '/logout', {}).status_code == 200
    assert c.get('/api/dashboard').status_code == 401
    assert post(c, '/register', d).status_code == 409


def test_complete_and_review_new_visit(app):
    c, master = client(app), client(app, 'master')
    slot = c.get('/api/slots/1').json['slots'][0]
    b = post(c, '/bookings', {'slot_id': slot['id'], 'address': 'Киров, Ленина 10'}).json
    assert post(c, '/reviews', {'booking_id': b['id'], 'rating': 5, 'content': 'Прекрасно'}).status_code == 403
    with sqlite3.connect(app.config['DATABASE']) as con:
        con.execute('UPDATE slots SET starts_at=? WHERE id=?', ((now()-timedelta(hours=2)).isoformat(timespec='minutes'), slot['id']))
    assert post(master, f"/bookings/{b['id']}/complete", {}).status_code == 200
    assert post(c, '/reviews', {'booking_id': b['id'], 'rating': 5, 'content': 'Прекрасно'}).status_code == 201


@pytest.mark.parametrize('qty', [0, -1, 51, '2', True, 1.5])
def test_invalid_quantity(app, qty):
    c = client(app)
    slot = c.get('/api/slots/4').json['slots'][0]
    assert post(c, '/bookings', {'slot_id': slot['id'], 'quantity': qty}).status_code == 400
