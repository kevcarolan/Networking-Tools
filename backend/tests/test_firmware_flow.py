import hashlib

from netmiko.exceptions import NetmikoAuthenticationException

from tests import firmware_samples as fw


def _row(client, device_id):
    return client.get(f"/api/firmware/devices/{device_id}").json()


def _upload(client, data=b"image-bytes" * 1000, **params):
    query = {"filename": "cat9k_iosxe.17.09.04a.SPA.bin", "platform": "cisco_ios",
             "version": "17.09.04a", "model_pattern": "C9300-*", **params}
    return client.put("/api/firmware/images/upload", params=query, content=data)


def test_version_check_and_compliance(app, admin, device):
    svc = app.state.firmware_service
    row = _row(admin, device["id"])
    assert row["facts"]["status"] == "never" and row["compliance"] == "no_standard"

    assert svc.check(device["id"]) == "success"
    facts = _row(admin, device["id"])["facts"]
    assert facts["version"] == "17.09.04a" and facts["model"] == "C9300-48P"
    assert facts["boot_mode"] == "install" and facts["flash_free"] == 8151625728

    r = admin.post("/api/firmware/standards", json={
        "platform": "cisco_ios", "model_pattern": "C9300-*", "target_version": "17.12.4"})
    assert r.status_code == 201, r.text
    assert _row(admin, device["id"])["compliance"] == "behind"
    assert admin.get("/api/firmware/summary").json()["behind"] == 1

    admin.put(f"/api/firmware/standards/{r.json()['id']}", json={
        "platform": "cisco_ios", "model_pattern": "C9300-*", "target_version": "17.9.4a"})
    row = _row(admin, device["id"])
    assert row["compliance"] == "compliant" and row["target_version"] == "17.9.4a"

    csv = admin.get("/api/firmware/devices.csv").text
    assert "core-sw1" in csv and "17.09.04a" in csv and "compliant" in csv


def test_version_change_is_recorded(app, admin, device, fw_collector):
    svc = app.state.firmware_service
    svc.check(device["id"])
    fw_collector.outputs["core-sw1"] = [fw.IOS_XE_VERSION.replace("17.09.04a", "17.12.04"),
                                        fw.IOS_DIR]
    svc.check(device["id"])
    facts = _row(admin, device["id"])["facts"]
    assert facts["version"] == "17.12.04" and facts["previous_version"] == "17.09.04a"
    assert facts["version_changed_at"] is not None


def test_failed_check_keeps_last_known_version(app, admin, device, fw_collector):
    svc = app.state.firmware_service
    svc.check(device["id"])
    fw_collector.errors["core-sw1"] = NetmikoAuthenticationException("bad")
    assert svc.check(device["id"]) == "failed"
    facts = _row(admin, device["id"])["facts"]
    assert facts["status"] == "failed" and facts["last_error_type"] == "auth"
    assert facts["version"] == "17.09.04a"
    assert svc.due_device_ids() == []  # retried after failure_retry_minutes, not at once


def test_unparseable_output_is_a_command_error(app, admin, device, fw_collector):
    fw_collector.outputs["core-sw1"] = [fw.INVALID, ""]
    assert app.state.firmware_service.check(device["id"]) == "failed"
    assert _row(admin, device["id"])["facts"]["last_error_type"] == "command"


def test_device_without_credential(app, admin):
    d = admin.post("/api/devices", json={"name": "nocred", "address": "10.0.0.9",
                                         "platform": "cisco_nxos"}).json()
    assert app.state.firmware_service.check(d["id"]) == "failed"
    assert _row(admin, d["id"])["facts"]["last_error_type"] == "setup"


def test_new_devices_are_due(app, admin, device):
    assert app.state.firmware_service.due_device_ids() == [device["id"]]


def test_image_upload_and_checksum(app, admin, settings):
    data = b"\x00firmware" * 50000
    md5 = hashlib.md5(data).hexdigest()

    bad = _upload(admin, data, checksum="0" * 32)
    assert bad.status_code == 422 and "does not match" in bad.json()["detail"]
    assert not list(settings.firmware_dir.iterdir())  # partial file removed

    r = _upload(admin, data, checksum=md5.upper())
    assert r.status_code == 201, r.text
    img = r.json()
    assert img["md5"] == md5 and img["verified"] and img["size"] == len(data)
    assert img["sha512"] == hashlib.sha512(data).hexdigest()
    assert (settings.firmware_dir / img["filename"]).read_bytes() == data

    assert _upload(admin, data).status_code == 409  # duplicate file name
    assert _upload(admin, data, filename="../etc/passwd").status_code == 422
    assert _upload(admin, data, filename="x.bin", platform="juniper").status_code == 422
    assert _upload(admin, b"", filename="empty.bin").status_code == 422
    assert [i["filename"] for i in admin.get("/api/firmware/images").json()] == [img["filename"]]


def test_upload_size_limit(app, admin, settings):
    settings.firmware_max_upload_mb = 1
    r = _upload(admin, b"x" * (2**20 + 1))
    assert r.status_code == 422 and "limit" in r.json()["detail"]
    assert not list(settings.firmware_dir.iterdir())


def test_standard_linked_to_image(admin, settings):
    img = _upload(admin).json()
    body = {"platform": "cisco_ios", "model_pattern": "C9300-*", "target_version": "17.12.4",
            "image_id": img["id"]}
    r = admin.post("/api/firmware/standards", json=body)
    assert r.status_code == 422 and "version" in r.json()["detail"]
    r = admin.post("/api/firmware/standards", json={**body, "target_version": "17.09.04a"})
    assert r.status_code == 201 and r.json()["image_filename"] == img["filename"]
    assert admin.post("/api/firmware/standards",
                      json={**body, "target_version": "17.09.04a"}).status_code == 409

    assert admin.delete(f"/api/firmware/images/{img['id']}").status_code == 409
    admin.delete(f"/api/firmware/standards/{r.json()['id']}")
    assert admin.delete(f"/api/firmware/images/{img['id']}").status_code == 204
    assert not (settings.firmware_dir / img["filename"]).exists()


def test_standard_validation(admin):
    base = {"platform": "cisco_ios", "model_pattern": "C9300-*", "target_version": "17.9.4a"}
    assert admin.post("/api/firmware/standards",
                      json={**base, "platform": "juniper"}).status_code == 422
    assert admin.post("/api/firmware/standards",
                      json={**base, "target_version": "17.9; rm"}).status_code == 422
    assert admin.post("/api/firmware/standards",
                      json={**base, "model_pattern": ""}).json()["model_pattern"] == "*"


def test_viewer_is_read_only(client, device, app):
    from app.core.auth import VIEWER, User

    app.state.authenticator.authenticate = lambda u, p: User(username=u, role=VIEWER)
    client.post("/api/auth/logout")
    assert client.post("/api/auth/login", json={"username": "v", "password": "x"}).status_code == 200
    assert client.get("/api/firmware/devices").status_code == 200
    assert client.post(f"/api/firmware/devices/{device['id']}/check").status_code == 403
    assert client.post("/api/firmware/check-all").status_code == 403
    assert _upload(client).status_code == 403
    assert client.post("/api/firmware/standards", json={
        "platform": "cisco_ios", "target_version": "17.9.4a"}).status_code == 403
