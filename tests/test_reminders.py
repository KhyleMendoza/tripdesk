from tests.conftest import login_as


def test_reminders_require_login(client):
    response = client.get("/reminders", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_accountant_cannot_open_reminders(client):
    login_as(client, "accountant@tripdesk.local")
    assert client.get("/reminders").status_code == 403


def test_outbox_lists_drafts_with_copy(client):
    login_as(client, "agent@tripdesk.local")
    body = client.get("/reminders").data.decode()
    assert "Balance due reminder" in body
    assert "Copy text" in body
    assert "Mark sent" in body


def test_compose_builds_deposit_departure_passport(client):
    login_as(client, "manager@tripdesk.local")
    response = client.post("/reminders/generate", follow_redirects=False)
    assert response.status_code == 302
    body = client.get("/reminders").data.decode()
    assert "balance of $" in body
    assert "departs" in body
    assert "passport" in body.lower()
    again = client.post("/reminders/generate", follow_redirects=True)
    assert again.status_code == 200


def test_mark_sent_and_discard(client):
    login_as(client, "agent@tripdesk.local")
    sent = client.post("/reminders/1/sent", follow_redirects=False)
    assert sent.status_code == 302
    body = client.get("/reminders?status=sent").data.decode()
    assert "Balance due reminder" in body
    discarded = client.post("/reminders/3/delete", follow_redirects=False)
    assert discarded.status_code == 302
    assert "Passport expiring soon" not in client.get("/reminders").data.decode()


def test_manager_scoped_to_own_branch(client):
    login_as(client, "manager2@tripdesk.local")
    assert client.post("/reminders/1/sent").status_code == 404
