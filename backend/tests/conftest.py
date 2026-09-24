import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.core.crypto import hash_password
from app.main import create_app

ADMIN_PASSWORD = "correct-horse-battery"

SAMPLE_CONFIG = """Building configuration...

Current configuration : 1234 bytes
!
! Last configuration change at 10:00:00 UTC Mon Jan 1 2024
!
version 17.9
hostname {name}
!
interface Vlan1
 ip address {address} 255.255.255.0
!
end
"""


class FakeFetcher:
    """Stands in for SSH: returns canned configs or raises a queued error."""

    def __init__(self):
        self.configs: dict[str, str] = {}
        self.errors: dict[str, Exception] = {}
        self.calls: list[str] = []

    def __call__(self, target):
        self.calls.append(target.name)
        if target.name in self.errors:
            raise self.errors[target.name]
        return self.configs.get(target.name) or SAMPLE_CONFIG.format(
            name=target.name, address=target.address)


@pytest.fixture
def settings(tmp_path):
    return Settings(
        _env_file=None, data_dir=tmp_path / "data",
        local_admin_user="admin", local_admin_password_hash=hash_password(ADMIN_PASSWORD),
        ldap_url="", scheduler_enabled=False, backup_workers=2,
    ).prepare()


@pytest.fixture
def fetcher():
    return FakeFetcher()


@pytest.fixture
def app(settings, fetcher):
    return create_app(settings, fetcher=fetcher, start_scheduler=False)


@pytest.fixture
def client(app):
    with TestClient(app) as c:
        yield c


@pytest.fixture
def admin(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
    assert r.status_code == 200, r.text
    return client


@pytest.fixture
def credential(admin):
    r = admin.post("/api/credentials", json={
        "name": "switch-ro", "username": "backup", "password": "s3cret", "enable_secret": "en"})
    assert r.status_code == 201, r.text
    return r.json()


@pytest.fixture
def device(admin, credential):
    r = admin.post("/api/devices", json={
        "name": "core-sw1", "address": "10.0.0.1", "platform": "cisco_ios",
        "site": "HQ", "credential_id": credential["id"]})
    assert r.status_code == 201, r.text
    return r.json()
