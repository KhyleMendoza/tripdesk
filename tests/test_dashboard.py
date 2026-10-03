from datetime import date

from tests.conftest import login_as


def test_dashboard_shows_today_departures_for_owner(client):
    login_as(client, "owner@tripdesk.local")
    body = client.get("/").data.decode()
    assert "Departures today" in body
    assert "Family beach getaway" in body
    assert "Anniversary city break" in body
    assert "Lakeside retreat" in body


def test_dashboard_shows_unpaid_balances(client):
    login_as(client, "owner@tripdesk.local")
    body = client.get("/").data.decode()
    assert "Unpaid balances" in body
    assert "Island honeymoon" in body
    assert "$5,900.00" in body


def test_dashboard_shows_per_branch_stats(client):
    login_as(client, "owner@tripdesk.local")
    body = client.get("/").data.decode()
    assert "Branch performance" in body
    assert "Downtown Branch" in body
    assert "North Branch" in body
    assert "West Branch" in body


def test_branch_manager_sees_only_own_branch(client):
    login_as(client, "manager2@tripdesk.local")
    body = client.get("/").data.decode()
    assert "Lakeside retreat" in body
    assert "Family beach getaway" not in body
    assert "North Branch" in body
    assert "Downtown Branch" not in body


def test_accountant_scoped_to_own_branch(client):
    login_as(client, "accountant@tripdesk.local")
    body = client.get("/").data.decode()
    assert date.today().isoformat() in body
    assert "Downtown Branch" in body
    assert "West Branch" not in body
