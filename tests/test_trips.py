from datetime import date, timedelta

from tests.conftest import login_as


def iso(delta):
    return (date.today() + timedelta(days=delta)).isoformat()


def test_trip_list_requires_login(client):
    response = client.get("/trips", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_accountant_cannot_open_trips(client):
    login_as(client, "accountant@tripdesk.local")
    assert client.get("/trips").status_code == 403
    assert client.get("/trips/1").status_code == 403


def test_trip_list_shows_balances_and_filters(client):
    login_as(client, "owner@tripdesk.local")
    body = client.get("/trips").data.decode()
    assert "Island honeymoon" in body
    assert "$5,900.00" in body
    pending = client.get("/trips?status=pending").data.decode()
    assert "Lakeside retreat" in pending
    assert "Family beach getaway" not in pending


def test_manager_scoped_to_own_branch(client):
    login_as(client, "manager2@tripdesk.local")
    body = client.get("/trips").data.decode()
    assert "Lakeside retreat" in body
    assert "Family beach getaway" not in body
    assert client.get("/trips/1").status_code == 404


def test_trip_detail_shows_itinerary_and_suppliers(client):
    login_as(client, "agent@tripdesk.local")
    body = client.get("/trips/1").data.decode()
    assert "Family beach getaway" in body
    assert "Skyline Air" in body
    assert "Harborview Hotel Group" in body
    assert "SKY-1000" in body
    assert "Hannah Whitfield" in body


def test_create_trip_and_add_item(client):
    login_as(client, "agent@tripdesk.local")
    created = client.post(
        "/trips/new",
        data={
            "title": "Weekend test trip",
            "client_id": "1",
            "departure_date": iso(30),
            "return_date": iso(33),
        },
        follow_redirects=False,
    )
    assert created.status_code == 302
    trip_url = created.headers["Location"]
    bad_item = client.post(
        trip_url + "/items",
        data={"kind": "flight", "title": "", "item_date": iso(30), "price": "-5"},
    )
    assert bad_item.status_code == 200
    assert "Price cannot be negative" in bad_item.data.decode()
    added = client.post(
        trip_url + "/items",
        data={
            "kind": "flight",
            "title": "Test flight",
            "item_date": iso(30),
            "price": "450.00",
            "supplier_id": "1",
            "voucher": "SKY-9999",
        },
        follow_redirects=False,
    )
    assert added.status_code == 302
    body = client.get(trip_url).data.decode()
    assert "Test flight" in body
    assert "SKY-9999" in body
    assert "$450.00" in body


def test_create_trip_rejects_bad_dates(client):
    login_as(client, "owner@tripdesk.local")
    response = client.post(
        "/trips/new",
        data={
            "title": "Bad dates",
            "client_id": "1",
            "branch_id": "1",
            "agent_id": "3",
            "departure_date": iso(10),
            "return_date": iso(5),
        },
    )
    assert response.status_code == 200
    assert "Return must be on or after departure" in response.data.decode()


def test_manager_can_change_status_agent_cannot(client):
    login_as(client, "manager@tripdesk.local")
    changed = client.post("/trips/8/status", data={"status": "confirmed"}, follow_redirects=False)
    assert changed.status_code == 302
    assert "Confirmed" in client.get("/trips/8").data.decode()
    client.post("/logout")
    login_as(client, "agent@tripdesk.local")
    denied = client.post("/trips/8/status", data={"status": "pending"})
    assert denied.status_code == 403
