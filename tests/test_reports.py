from datetime import date

from tests.conftest import login_as


def current_month():
    return date.today().strftime("%Y-%m")


def test_reports_require_login(client):
    response = client.get("/reports", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_agent_and_manager_cannot_open_reports(client):
    login_as(client, "agent@tripdesk.local")
    assert client.get("/reports").status_code == 403
    client.post("/logout")
    login_as(client, "manager@tripdesk.local")
    assert client.get("/reports").status_code == 403


def test_report_shows_revenue_commissions_outstanding(client):
    login_as(client, "accountant@tripdesk.local")
    body = client.get("/reports?month=" + current_month()).data.decode()
    assert "Revenue by branch" in body
    assert "Commissions by agent" in body
    assert "Outstanding balances" in body
    assert "Priya Nair" in body or "Leo Martinez" in body


def test_owner_filters_by_branch(client):
    login_as(client, "owner@tripdesk.local")
    body = client.get("/reports?month=" + current_month() + "&branch=1").data.decode()
    assert "<td>Downtown Branch</td>" in body
    assert "<td>North Branch</td>" not in body


def test_owner_locks_month_as_reviewed(client):
    login_as(client, "owner@tripdesk.local")
    assert "Reviewed" not in client.get("/reports?month=2001-01").data.decode()
    locked = client.post("/reports/review", data={"month": "2001-01"}, follow_redirects=False)
    assert locked.status_code == 302
    assert "Reviewed" in client.get("/reports?month=2001-01").data.decode()


def test_accountant_cannot_lock_month(client):
    login_as(client, "accountant@tripdesk.local")
    assert client.post("/reports/review", data={"month": "2002-02"}).status_code == 403
