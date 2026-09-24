from app.core.auth import Authenticator, LoginThrottle
from app.core.crypto import hash_password, verify_password


def test_password_hash_roundtrip():
    encoded = hash_password("hunter2hunter2")
    assert verify_password("hunter2hunter2", encoded)
    assert not verify_password("wrong", encoded)
    assert not verify_password("x", "garbage")


def test_empty_password_never_authenticates(settings):
    auth = Authenticator(settings)
    assert auth.authenticate("admin", "") is None
    assert auth.authenticate("", "anything") is None


def test_requires_login(client):
    assert client.get("/api/devices").status_code == 401
    assert client.get("/api/auth/me").status_code == 401


def test_login_logout(client):
    assert client.post("/api/auth/login", json={"username": "admin", "password": "nope"}).status_code == 401
    r = client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"})
    assert r.json()["role"] == "admin"
    assert client.get("/api/auth/me").json()["username"] == "admin"
    client.post("/api/auth/logout")
    assert client.get("/api/auth/me").status_code == 401


def test_login_throttle(client):
    for _ in range(5):
        client.post("/api/auth/login", json={"username": "admin", "password": "nope"})
    r = client.post("/api/auth/login", json={"username": "admin", "password": "correct-horse-battery"})
    assert r.status_code == 429


def test_throttle_window():
    t = LoginThrottle(max_failures=2, window_seconds=300)
    t.fail("k"); t.fail("k")
    assert t.blocked("k")
    t.reset("k")
    assert not t.blocked("k")


def test_viewer_is_read_only(app, client, device):
    app.state.authenticator.authenticate = lambda u, p: __import__(
        "app.core.auth", fromlist=["User"]).User(username=u, role="viewer")
    client.post("/api/auth/logout")
    client.post("/api/auth/login", json={"username": "jsmith", "password": "x"})
    assert client.get("/api/backup/devices").status_code == 200
    assert client.post("/api/devices", json={
        "name": "x", "address": "10.0.0.9", "platform": "cisco_ios"}).status_code == 403
    assert client.post(f"/api/backup/devices/{device['id']}/run").status_code == 403
    assert client.get("/api/credentials").status_code == 403
    assert client.get(f"/api/backup/devices/{device['id']}/versions").status_code == 403
