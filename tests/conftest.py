import os
import sys
import tempfile

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app

PASSWORD = "Passw0rd!"


@pytest.fixture()
def app():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.unlink(path)
    application = create_app(test_db=path)
    application.config["TESTING"] = True
    yield application
    if os.path.exists(path):
        os.remove(path)


@pytest.fixture()
def client(app):
    return app.test_client()


def login_as(client, email, password=PASSWORD):
    return client.post("/login", data={"email": email, "password": password}, follow_redirects=False)
