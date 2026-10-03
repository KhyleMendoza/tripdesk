from tests.conftest import login_as

ACCOUNTS = [
    "owner@tripdesk.local",
    "manager@tripdesk.local",
    "agent@tripdesk.local",
    "accountant@tripdesk.local",
]


def test_login_page_shows_only_sign_in_form(client):
    body = client.get("/login").data.decode()
    assert "Sign in" in body
    assert "Passw0rd" not in body
    assert "owner@tripdesk.local" not in body


def test_each_role_can_sign_in(client):
    for email in ACCOUNTS:
        response = login_as(client, email)
        assert response.status_code == 302
        assert response.headers["Location"] == "/"
        client.post("/logout")


def test_wrong_password_rejected(client):
    response = login_as(client, "owner@tripdesk.local", "wrong-password")
    assert response.status_code == 200
    assert "incorrect" in response.data.decode().lower()


def test_dashboard_requires_login(client):
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_logout_returns_to_login(client):
    login_as(client, "owner@tripdesk.local")
    response = client.post("/logout", follow_redirects=False)
    assert response.headers["Location"] == "/login"
    assert client.get("/", follow_redirects=False).status_code == 302
