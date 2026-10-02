import pytest

from app.tools.firmware_upgrade import checks as ck
from tests import firmware_samples as s
from tests.test_circuits import make_xlsx

IOS_OUT = {
    "show version": s.IOS_XE_VERSION, "dir": s.IOS_DIR,
    "show facility-alarm status": s.ALARMS_OK, "show environment all": s.ENV_OK,
    "show processes cpu | include CPU utilization": s.CPU_OK,
    "show processes memory | include Processor Pool": s.MEM_OK,
    "show install summary": s.INSTALL_OK, "show archive config differences": s.UNSAVED_NONE,
    "show switch": s.STACK_OK, "show ip interface brief": s.IP_BRIEF,
    "show cdp neighbors detail": s.CDP_DETAIL, "show lldp neighbors detail": s.LLDP_DETAIL,
    "show etherchannel summary": s.ETHERCHANNEL, "show mac address-table count": s.MAC_COUNT,
    "show running-config | include ip scp server": "ip scp server enable\n",
    "show running-config | include ^boot system": "",
    "show install rollback": "ID  Label     Description\n1   No Label  No Description\n",
}
IMAGE_DATA = b"cat9k image" * 1000


@pytest.fixture
def env(app, admin, device, fw_collector):
    """A switch with an upgrade credential, a known version and an image."""
    fw_collector.by_command["core-sw1"] = dict(IOS_OUT)
    app.state.firmware_service.check(device["id"])
    up = admin.post("/api/credentials", json={"name": "upgrade-rw", "username": "netops-up",
                                              "password": "x"}).json()
    r = admin.put(f"/api/firmware/devices/{device['id']}/settings",
                  json={"upgrade_credential_id": up["id"]})
    assert r.status_code == 200 and r.json()["upgrade_credential"] == "upgrade-rw"
    img = admin.put("/api/firmware/images/upload", params={
        "filename": "cat9k_iosxe.17.12.04.SPA.bin", "platform": "cisco_ios",
        "version": "17.12.04", "model_pattern": "C9300-*"}, content=IMAGE_DATA).json()
    return {"device": device, "image": img, "svc": app.state.job_service,
            "outputs": fw_collector.by_command["core-sw1"], "collector": fw_collector}


def _create(admin, env, **extra):
    r = admin.post("/api/firmware/jobs", json={"device_id": env["device"]["id"],
                                               "image_id": env["image"]["id"],
                                               "change_ref": "CHG0012345", **extra})
    assert r.status_code == 201, r.text
    return r.json()


def _job(admin, job_id):
    return admin.get(f"/api/firmware/jobs/{job_id}").json()


def _check(job, phase, check_id):
    return next(c for c in job["checks"][phase] if c["check_id"] == check_id)


def test_full_dry_run_with_an_overridden_alarm(admin, env):
    job = _create(admin, env)
    assert job["status"] == "planned" and job["from_version"] == "17.09.04a"
    assert job["allowed"] == ["cancel", "precheck"]

    env["outputs"]["show facility-alarm status"] = s.ALARMS_BAD
    assert env["svc"].precheck(job["id"], "admin") == "blocked"
    j = _job(admin, job["id"])
    alarm = _check(j, "pre", "alarms")
    assert alarm["status"] == "fail" and "Power Supply Bay 2" in alarm["detail"]
    assert _check(j, "pre", "login")["status"] == "pass"
    assert env["collector"].calls[-1] == ("core-sw1", "netops-up")  # upgrade account used

    # Start is refused while blocked; an override needs a real reason
    assert admin.post(f"/api/firmware/jobs/{job['id']}/start").status_code == 409
    url = f"/api/firmware/jobs/{job['id']}/checks/{alarm['id']}/override"
    assert admin.post(url, json={"reason": "short"}).status_code == 422
    r = admin.post(url, json={"reason": "PSU 2 is a known fault, spare on order (INC123)"})
    assert r.status_code == 200 and r.json()["status"] == "ready"

    # Start re-runs the pre-checks; the same alarm keeps its override
    assert env["svc"].start(job["id"], "admin") == "completed_overrides"
    j = _job(admin, job["id"])
    assert _check(j, "pre", "alarms")["overridden_by"] == "admin" and j["pre_run"] == 2
    post_alarm = _check(j, "post", "alarms")
    assert post_alarm["status"] == "fail" and post_alarm["overridden_by"] == "admin"
    assert "carried over from the pre-check" in post_alarm["override_reason"]
    assert all(c["status"] in ("pass", "skip") or c["severity"] != "blocker" or c["overridden_by"]
               for c in j["checks"]["post"])
    msgs = " | ".join(e["message"] for e in j["events"])
    assert "DRY RUN - would run on core-sw1: install add file flash:cat9k_iosxe.17.12.04.SPA.bin" in msgs
    assert "install activate auto-abort-timer 120 prompt-level none" in msgs
    assert "DRY RUN - would copy cat9k_iosxe.17.12.04.SPA.bin" in msgs
    assert "DRY RUN - would run on core-sw1: install commit" in msgs
    assert j["pre_backup_commit"] and j["post_backup_commit"]
    assert all(st["kind"] for st in j["steps"]) and j["step_index"] == len(j["steps"])
    assert j["started_by"] == "admin" and j["finished_at"]

    report = admin.get(f"/api/firmware/jobs/{job['id']}/report.html")
    assert report.status_code == 200 and "DRY RUN" in report.text
    assert "PSU 2 is a known fault" in report.text and "CHG0012345" in report.text
    assert "style-src 'unsafe-inline'" in report.headers["content-security-policy"]
    csv = admin.get(f"/api/firmware/jobs/{job['id']}/report.csv").text
    assert "No critical or major alarms" in csv

    hist = admin.get(f"/api/firmware/devices/{env['device']['id']}/jobs").json()
    assert [h["status"] for h in hist] == ["completed_overrides"]


def test_recheck_after_fixing_and_a_new_alarm_needs_a_new_override(admin, env):
    job = _create(admin, env)
    env["outputs"]["show facility-alarm status"] = s.ALARMS_BAD
    env["svc"].precheck(job["id"], "admin")
    alarm = _check(_job(admin, job["id"]), "pre", "alarms")
    admin.post(f"/api/firmware/jobs/{job['id']}/checks/{alarm['id']}/override",
               json={"reason": "known PSU fault, accepted by change board"})
    env["outputs"]["show facility-alarm status"] = s.ALARMS_BAD.replace(
        "Power Supply Bay 2 Failed", "Fan Tray 1 Failed")
    assert env["svc"].precheck(job["id"], "admin") == "blocked"  # different alarm
    env["outputs"]["show facility-alarm status"] = s.ALARMS_OK      # fixed
    assert env["svc"].precheck(job["id"], "admin") == "ready"
    j = _job(admin, job["id"])
    assert _check(j, "pre", "alarms")["overridden_by"] is None
    assert env["svc"].start(job["id"], "admin") == "completed"


def test_post_check_failure_then_override_or_rollback(admin, env, app):
    job = _create(admin, env)
    assert env["svc"].precheck(job["id"], "admin") == "ready"
    # Simulate an interface going down after the "upgrade"
    def on_step(job_id, label):
        if "post-checks" in label:
            env["outputs"]["show ip interface brief"] = s.IP_BRIEF.replace(
                "GigabitEthernet2/0/13  unassigned      YES unset  up                    up",
                "GigabitEthernet2/0/13  unassigned      YES unset  down                  down")

    env["svc"].on_step = on_step
    assert env["svc"].start(job["id"], "admin") == "failed"
    j = _job(admin, job["id"])
    failed = _check(j, "post", "post_interfaces")
    assert failed["status"] == "fail" and "GigabitEthernet2/0/13" in failed["detail"]
    assert set(j["allowed"]) == {"cancel", "postcheck", "rollback"}
    assert j["steps"][j["step_index"]]["kind"] == "install_commit"  # not committed

    # Re-running the post-checks while it is still down keeps it failed
    assert env["svc"].postcheck(job["id"], "admin") == "failed"
    # Rolling back is the engineer's choice (and here it brings the interface back)
    env["svc"].on_step = lambda *a: None
    env["outputs"]["show ip interface brief"] = s.IP_BRIEF
    assert env["svc"].rollback(job["id"], "admin") == "rolled_back"
    msgs = " | ".join(e["message"] for e in _job(admin, job["id"])["events"])
    assert "DRY RUN - would run on core-sw1: install rollback to id 1 prompt-level none" in msgs
    assert "Post-checks on core-sw1: " in msgs


def test_post_failure_overridden_with_a_note(admin, env):
    job = _create(admin, env)
    env["svc"].precheck(job["id"], "admin")
    def on_step(job_id, label):
        if "post-checks" in label:
            env["outputs"]["show cdp neighbors detail"] = s.CDP_DETAIL.split(
                "-------------------------\nDevice ID: GH-AS02")[0]

    env["svc"].on_step = on_step
    assert env["svc"].start(job["id"], "admin") == "failed"
    cdp = _check(_job(admin, job["id"]), "post", "post_cdp")
    r = admin.post(f"/api/firmware/jobs/{job['id']}/checks/{cdp['id']}/override",
                   json={"reason": "GH-AS02 is down for separate works (CHG0012399)"})
    # the commit is still to do: Continue carries on
    assert r.json()["status"] == "paused" and r.json()["allowed"] == [
        "cancel", "continue", "postcheck", "rollback"]
    assert env["svc"].continue_(job["id"], "admin") == "completed_overrides"
    assert _job(admin, job["id"])["finished_at"]


def test_blockers_without_device_problems(admin, env, device, app):
    # No upgrade credential: blocked without logging in
    admin.put(f"/api/firmware/devices/{device['id']}/settings", json={"upgrade_credential_id": None})
    job = _create(admin, env)
    assert env["svc"].precheck(job["id"], "admin") == "blocked"
    j = _job(admin, job["id"])
    assert _check(j, "pre", "upgrade_credential")["status"] == "fail"
    assert _check(j, "pre", "login")["status"] == "fail"
    # Image file changed on disk after upload
    (app.state.settings.firmware_dir / env["image"]["filename"]).write_bytes(b"tampered")
    env["svc"].precheck(job["id"], "admin")
    assert _check(_job(admin, job["id"]), "pre", "image_md5")["status"] == "fail"


def test_already_on_target_and_create_validation(admin, env, device):
    same = admin.put("/api/firmware/images/upload", params={
        "filename": "cat9k_iosxe.17.09.04a.SPA.bin", "platform": "cisco_ios",
        "version": "17.09.04a", "model_pattern": "*"}, content=b"x" * 100).json()
    job = _create(admin, env, image_id=same["id"])
    env["svc"].precheck(job["id"], "admin")
    assert _check(_job(admin, job["id"]), "pre", "version_differs")["status"] == "fail"
    # one open job per device
    assert admin.post("/api/firmware/jobs", json={"device_id": device["id"],
                                                  "image_id": env["image"]["id"]}).status_code == 409
    admin.post(f"/api/firmware/jobs/{job['id']}/cancel", json={"reason": "wrong image picked"})
    assert _job(admin, job["id"])["status"] == "cancelled"
    # wrong model / platform
    other = admin.put("/api/firmware/images/upload", params={
        "filename": "c9500.bin", "platform": "cisco_ios", "version": "17.12.04",
        "model_pattern": "C9500-*"}, content=b"y" * 10).json()
    r = admin.post("/api/firmware/jobs", json={"device_id": device["id"], "image_id": other["id"]})
    assert r.status_code == 422 and "C9500" in r.json()["detail"]
    # images and devices with history can't be deleted
    assert admin.delete(f"/api/firmware/images/{same['id']}").status_code == 409
    assert admin.delete(f"/api/devices/{device['id']}").status_code == 409


def test_circuits_are_frozen_with_the_job(admin, env, device):
    rows = [[*r[:15], "core-sw1", *r[16:]] for r in __import__("tests.test_circuits",
                                                               fromlist=["ROWS"]).ROWS]
    imp = admin.put("/api/circuits/imports/upload", params={"filename": "c.xlsx"},
                    content=make_xlsx(rows=rows)).json()
    admin.post(f"/api/circuits/imports/{imp['id']}/commit")
    job = _create(admin, env)
    assert job["circuits"]["planned"]["count"] == 5
    env["svc"].start(job["id"], "admin") if env["svc"].precheck(job["id"], "admin") == "ready" else None
    j = _job(admin, job["id"])
    assert j["circuits"]["started"]["count"] == 5
    assert "A-DUB01-GH-100-3" in admin.get(f"/api/firmware/jobs/{job['id']}/report.html").text


def test_api_actions_are_queued_for_the_worker(admin, env, settings):
    from app.worker import UpgradeWorker

    job = _create(admin, env)
    assert admin.post(f"/api/firmware/jobs/{job['id']}/precheck").status_code == 202
    j = _job(admin, job["id"])
    assert j["busy"] and j["queued"] == "precheck" and j["allowed"] == []
    # a second request while one is waiting is refused
    assert admin.post(f"/api/firmware/jobs/{job['id']}/precheck").status_code == 409
    worker = UpgradeWorker(settings, env["svc"])
    assert worker.process_once(wait=True) == 1
    assert worker.process_once(wait=True) == 0  # nothing runs twice
    j = _job(admin, job["id"])
    assert j["status"] == "ready" and not j["busy"]
    # with the worker required but not running, actions are refused
    settings.upgrade_require_worker = True
    r = admin.post(f"/api/firmware/jobs/{job['id']}/precheck")
    assert r.status_code == 409 and "worker" in r.json()["detail"]
    worker.heartbeat()
    assert admin.post(f"/api/firmware/jobs/{job['id']}/precheck").status_code == 202


def test_permissions(client, app, admin, env, settings):
    job = _create(admin, env)
    from app.core.auth import ADMIN, VIEWER, User

    def login_as(role, can_upgrade):
        app.state.authenticator.authenticate = lambda u, p: User(username=u, role=role,
                                                                 can_upgrade=can_upgrade)
        client.post("/api/auth/logout")
        client.post("/api/auth/login", json={"username": "u", "password": "x"})

    login_as(VIEWER, False)
    assert client.get(f"/api/firmware/jobs/{job['id']}").status_code == 200
    assert client.post(f"/api/firmware/jobs/{job['id']}/precheck").status_code == 403
    login_as(ADMIN, False)  # admin outside the upgrader group
    assert client.post("/api/firmware/jobs", json={"device_id": env["device"]["id"],
                                                   "image_id": env["image"]["id"]}).status_code == 403
    login_as(VIEWER, True)  # upgrader who isn't an admin
    assert client.post(f"/api/firmware/jobs/{job['id']}/cancel",
                       json={"reason": "testing upgrader rights"}).status_code == 200
    assert client.put(f"/api/firmware/devices/{env['device']['id']}/settings",
                      json={"upgrade_credential_id": None}).status_code == 403


def test_service_restart_marks_running_jobs(admin, env, app):
    from app.core.db import session_scope
    from app.tools.firmware_upgrade.models import UpgradeJob

    from app.tools.firmware_upgrade.models import JobUnit

    job = _create(admin, env)
    with session_scope() as db:
        db.get(UpgradeJob, job["id"]).status = "pre_checking"
    env["svc"].recover_interrupted()
    j = _job(admin, job["id"])
    assert j["status"] == "needs_attention" and "restarted" in j["events"][-1]["message"]
    assert set(j["allowed"]) == {"cancel", "precheck"}  # nothing changed on the device yet
    with session_scope() as db:
        db.get(UpgradeJob, job["id"]).status = "running"
        db.query(JobUnit).filter_by(job_id=job["id"]).one().status = "activated"
    env["svc"].recover_interrupted()
    assert set(_job(admin, job["id"])["allowed"]) == {"cancel", "postcheck", "rollback"}


def test_report_escapes_values(admin, env):
    job = _create(admin, env, notes="<script>alert(1)</script>")
    html = admin.get(f"/api/firmware/jobs/{job['id']}/report.html").text
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_image_repository_details(admin, env):
    r = admin.put(f"/api/firmware/images/{env['image']['id']}/info",
                  json={"recommended": True, "release_ref": "Cisco suggested, 2026-09"})
    assert r.status_code == 200
    img = next(i for i in admin.get("/api/firmware/images").json() if i["id"] == env["image"]["id"])
    assert img["vendor"] == "Cisco" and img["recommended"] and img["job_count"] == 0
    assert ck.BLOCKER == "blocker"
