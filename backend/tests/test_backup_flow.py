from netmiko.exceptions import NetmikoAuthenticationException

from tests.conftest import SAMPLE_CONFIG


def _row(client, device_id):
    return client.get(f"/api/backup/devices/{device_id}").json()["backup"]


def test_device_crud_and_validation(admin, credential, device):
    assert device["platform_label"] == "Cisco IOS / IOS-XE"
    bad = admin.post("/api/devices", json={"name": "bad name!", "address": "10.0.0.2", "platform": "cisco_ios"})
    assert bad.status_code == 422
    bad = admin.post("/api/devices", json={"name": "x", "address": "10.0.0.2", "platform": "juniper"})
    assert bad.status_code == 422
    dup = admin.post("/api/devices", json={"name": "core-sw1", "address": "10.0.0.2", "platform": "cisco_ios"})
    assert dup.status_code == 409

    r = admin.put(f"/api/devices/{device['id']}", json={**device, "address": "10.0.0.5"})
    assert r.status_code == 200 and r.json()["address"] == "10.0.0.5"

    # Credential in use can't be deleted; passwords are never returned.
    creds = admin.get("/api/credentials").json()
    assert creds[0]["device_count"] == 1 and "password" not in str(creds)
    assert admin.delete(f"/api/credentials/{credential['id']}").status_code == 409

    assert admin.delete(f"/api/devices/{device['id']}").status_code == 204
    assert admin.get("/api/backup/devices").json() == []


def test_settings_and_summary(admin, device):
    r = admin.put(f"/api/backup/devices/{device['id']}/settings", json={"frequency_minutes": 60, "enabled": True})
    assert r.json()["backup"]["frequency_minutes"] == 60
    assert admin.put(f"/api/backup/devices/{device['id']}/settings",
                     json={"frequency_minutes": 1}).status_code == 422
    assert admin.get("/api/backup/summary").json()["never"] == 1


def test_successful_backup_then_change(app, admin, device, fetcher):
    svc = app.state.backup_service
    svc.run(device["id"])
    b = _row(admin, device["id"])
    assert b["status"] == "success" and b["last_change"] is not None

    first_change = b["last_change"]
    svc.run(device["id"])  # same config -> no new version
    assert _row(admin, device["id"])["last_change"] == first_change
    assert len(admin.get(f"/api/backup/devices/{device['id']}/versions").json()) == 1

    fetcher.configs["core-sw1"] = SAMPLE_CONFIG.format(name="core-sw1", address="10.0.0.1") + "vlan 20\n"
    svc.run(device["id"])
    versions = admin.get(f"/api/backup/devices/{device['id']}/versions").json()
    assert len(versions) == 2
    diff = admin.get(f"/api/backup/devices/{device['id']}/diff").text
    assert "+vlan 20" in diff
    config = admin.get(f"/api/backup/devices/{device['id']}/config").text
    assert "hostname core-sw1" in config and "Current configuration" not in config

    runs = admin.get(f"/api/backup/devices/{device['id']}/runs").json()
    assert [r["changed"] for r in runs] == [True, False, True]


def test_failed_backup_recorded(app, admin, device, fetcher):
    fetcher.errors["core-sw1"] = NetmikoAuthenticationException("denied")
    svc = app.state.backup_service
    svc.run(device["id"])
    svc.run(device["id"])
    b = _row(admin, device["id"])
    assert b["status"] == "failed"
    assert b["last_error_type"] == "auth"
    assert b["consecutive_failures"] == 2
    assert admin.get("/api/backup/summary").json()["failed"] == 1

    del fetcher.errors["core-sw1"]
    svc.run(device["id"])
    b = _row(admin, device["id"])
    assert b["status"] == "success" and b["consecutive_failures"] == 0 and b["last_error"] is None


def test_missing_credential_is_setup_error(app, admin):
    d = admin.post("/api/devices", json={"name": "fw1", "address": "10.1.1.1", "platform": "cisco_ftd"}).json()
    app.state.backup_service.run(d["id"])
    b = _row(admin, d["id"])
    assert b["status"] == "failed" and b["last_error_type"] == "setup"


def test_scheduler_picks_due_devices(app, admin, device):
    svc = app.state.backup_service
    assert svc.due_device_ids() == [device["id"]]  # never backed up
    svc.run(device["id"])
    assert svc.due_device_ids() == []  # daily schedule, just ran
    admin.put(f"/api/backup/devices/{device['id']}/settings", json={"frequency_minutes": 60, "enabled": False})
    assert svc.due_device_ids() == []


def test_run_now_endpoint_queues(app, admin, device, fetcher):
    r = admin.post(f"/api/backup/devices/{device['id']}/run")
    assert r.status_code == 202 and r.json()["queued"]
    app.state.backup_service._pool.shutdown(wait=True)
    assert fetcher.calls == ["core-sw1"]
    assert _row(admin, device["id"])["status"] == "success"


def test_site_change_moves_config(app, admin, device):
    svc = app.state.backup_service
    svc.run(device["id"])
    admin.put(f"/api/devices/{device['id']}", json={**device, "site": "DC2"})
    svc.run(device["id"])
    versions = admin.get(f"/api/backup/devices/{device['id']}/versions").json()
    assert versions[0]["path"] == "DC2/core-sw1.cfg"
    assert versions[-1]["path"] == "HQ/core-sw1.cfg"


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200 and "NetOps Tools" in r.text
    assert "default-src 'self'" in r.headers["content-security-policy"]
