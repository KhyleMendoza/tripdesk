from datetime import date, timedelta

from tests.conftest import login_as


def test_client_phone_rejects_unformatted_number(client):
    login_as(client, "agent@tripdesk.local")
    bad = client.post(
        "/clients/new",
        data={
            "full_name": "Phone Check",
            "email": "phone@example.com",
            "phone": "abc",
            "address": "1 Test Way",
        },
    )
    assert bad.status_code == 200
    assert "(512) 555-0142" in bad.data.decode()


def test_client_phone_accepts_formatted_number(client):
    login_as(client, "agent@tripdesk.local")
    good = client.post(
        "/clients/new",
        data={
            "full_name": "Phone Fine",
            "email": "phonefine@example.com",
            "phone": "(512) 555-0199",
            "address": "2 Test Way",
        },
        follow_redirects=False,
    )
    assert good.status_code == 302


def test_trip_new_defaults_return_week_after_departure(client):
    login_as(client, "owner@tripdesk.local")
    body = client.get("/trips/new").data.decode()
    today = date.today().isoformat()
    week = (date.today() + timedelta(days=7)).isoformat()
    assert 'value="%s"' % today in body
    assert 'value="%s"' % week in body
    assert "Booking context" in body


def test_payments_subtitle_labels_collected_and_refunded(client):
    login_as(client, "owner@tripdesk.local")
    body = client.get("/payments").data.decode()
    assert "collected" in body
    assert "refunded" in body
    assert "net collected" not in body
