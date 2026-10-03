from tests.conftest import login_as


def test_client_list_requires_login(client):
    response = client.get("/clients", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_accountant_cannot_open_clients(client):
    login_as(client, "accountant@tripdesk.local")
    assert client.get("/clients").status_code == 403
    assert client.get("/clients/1").status_code == 403


def test_owner_sees_all_clients_with_passport_flags(client):
    login_as(client, "owner@tripdesk.local")
    body = client.get("/clients").data.decode()
    assert "Hannah Whitfield" in body
    assert "Rachel Kim" in body
    assert "Expires" in body or "Valid to" in body


def test_manager_scoped_to_own_branch(client):
    login_as(client, "manager2@tripdesk.local")
    body = client.get("/clients").data.decode()
    assert "James Carter" in body
    assert "Hannah Whitfield" not in body
    assert client.get("/clients/1").status_code == 404


def test_client_search_filters_results(client):
    login_as(client, "owner@tripdesk.local")
    body = client.get("/clients?q=marcus").data.decode()
    assert "Marcus Bell" in body
    assert "Hannah Whitfield" not in body


def test_client_detail_shows_contact_and_trip_history(client):
    login_as(client, "agent@tripdesk.local")
    body = client.get("/clients/1").data.decode()
    assert "h.whitfield@example.com" in body
    assert "(512) 555-0142" in body
    assert "Family beach getaway" in body
    assert "$3,650.00" in body


def test_create_client_validates_and_saves(client):
    login_as(client, "agent@tripdesk.local")
    bad = client.post(
        "/clients/new",
        data={"full_name": "", "email": "nope", "phone": "", "address": ""},
    )
    assert bad.status_code == 200
    assert "Enter the client name" in bad.data.decode()
    good = client.post(
        "/clients/new",
        data={
            "full_name": "Test Traveler",
            "email": "test@example.com",
            "phone": "(512) 555-0100",
            "address": "1 Test Way",
            "passport_number": "X999",
            "passport_expiry": "2030-01-01",
        },
        follow_redirects=False,
    )
    assert good.status_code == 302
    assert "/clients/" in good.headers["Location"]
    body = client.get(good.headers["Location"]).data.decode()
    assert "Test Traveler" in body
    assert "Downtown Branch" in body


def test_agent_client_forced_to_own_branch(client):
    login_as(client, "agent2@tripdesk.local")
    response = client.post(
        "/clients/new",
        data={
            "full_name": "Branch Two Guest",
            "email": "b2@example.com",
            "phone": "(512) 555-0101",
            "address": "2 Test Way",
            "branch_id": "3",
        },
        follow_redirects=False,
    )
    assert response.status_code == 302
    body = client.get(response.headers["Location"]).data.decode()
    assert "North Branch" in body
