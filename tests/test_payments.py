from datetime import date

from tests.conftest import login_as


def test_payments_require_login(client):
    response = client.get("/payments", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_agent_cannot_open_payments(client):
    login_as(client, "agent@tripdesk.local")
    assert client.get("/payments").status_code == 403
    assert client.get("/payments/new").status_code == 403
    assert client.get("/payments/1/receipt").status_code == 403


def test_payment_list_shows_log_and_balances(client):
    login_as(client, "accountant@tripdesk.local")
    body = client.get("/payments").data.decode()
    assert "R-5100" in body
    assert "Family beach getaway" in body
    assert "Hannah Whitfield" in body


def test_manager_scoped_to_own_branch(client):
    login_as(client, "manager2@tripdesk.local")
    assert client.get("/payments/1/receipt").status_code == 404


def test_record_deposit_updates_trip_balance(client):
    login_as(client, "owner@tripdesk.local")
    before = client.get("/trips/1").data.decode()
    assert "$3,650.00" in before
    client.post("/logout")
    login_as(client, "accountant@tripdesk.local")
    created = client.post(
        "/payments/new",
        data={
            "trip_id": "1",
            "amount": "650.00",
            "kind": "deposit",
            "method": "card",
            "created_on": date.today().isoformat(),
            "receipt_no": "R-9001",
        },
        follow_redirects=False,
    )
    assert created.status_code == 302
    assert "/receipt" in created.headers["Location"]
    receipt = client.get(created.headers["Location"]).data.decode()
    assert "R-9001" in receipt
    assert "$650.00" in receipt
    assert "$3,000.00" in receipt
    client.post("/logout")
    login_as(client, "owner@tripdesk.local")
    after = client.get("/trips/1").data.decode()
    assert "$3,000.00" in after
    assert "R-9001" in after


def test_record_payment_rejects_bad_amount(client):
    login_as(client, "accountant@tripdesk.local")
    response = client.post(
        "/payments/new",
        data={
            "trip_id": "1",
            "amount": "-50",
            "kind": "deposit",
            "method": "card",
            "created_on": date.today().isoformat(),
            "receipt_no": "R-9002",
        },
    )
    assert response.status_code == 200
    assert "above zero" in response.data.decode()


def test_refund_cannot_exceed_paid(client):
    login_as(client, "owner@tripdesk.local")
    response = client.post(
        "/payments/new",
        data={
            "trip_id": "5",
            "amount": "500.00",
            "kind": "refund",
            "method": "card",
            "created_on": date.today().isoformat(),
            "receipt_no": "R-9003",
        },
    )
    assert response.status_code == 200
    assert "cannot exceed" in response.data.decode()
