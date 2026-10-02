"""End-to-end upgrade procedures against the network simulator (tests/fake_network.py):
every driver and path, live and dry run, through the API and the worker queue."""

from datetime import timedelta

import pytest
from fastapi.testclient import TestClient

from app.core.db import session_scope, utcnow
from app.main import create_app
from app.tools.firmware_upgrade import jobs as jb
from app.tools.firmware_upgrade.models import UpgradeJob
from app.worker import UpgradeWorker
from tests import firmware_samples as s
from tests.conftest import ADMIN_PASSWORD, FakeFetcher
from tests.fake_network import FP, IOS_HEALTH, FakeNetwork, SimDevice

IF_DOWN = s.IP_BRIEF.replace(
    "GigabitEthernet2/0/13  unassigned      YES unset  up                    up",
    "GigabitEthernet2/0/13  unassigned      YES unset  down                  down")
ASA_OUT = {"show cpu usage": s.ASA_CPU, "show memory": s.ASA_MEM,
           "show running-config ssh": "ssh scopy enable\n", "show running-config boot": ""}
NXOS_OUT = {"show running-config | include scp-server": "feature scp-server\n"}
AW_OUT = {"show system": s.AWPLUS_SYSTEM, "show running-config ssh": "service ssh\nssh server scp\n"}


class Lab:
    def __init__(self, client, app, net, settings):
        self.c, self.app, self.net, self.settings = client, app, net, settings
        self.svc = app.state.job_service
        self.svc.sleep, self.svc.monotonic = net.sleep, net.monotonic
        self.worker = UpgradeWorker(settings, self.svc)
        self.ro = client.post("/api/credentials", json={"name": "ro", "username": "ro",
                                                        "password": "x"}).json()
        self.up = client.post("/api/credentials", json={"name": "upgrade-rw", "username": "up",
                                                        "password": "x"}).json()

    def device(self, sim: SimDevice, address: str, **settings) -> dict:
        self.net.add(sim)
        d = self.c.post("/api/devices", json={"name": sim.name, "address": address,
                                              "platform": sim.platform, "site": "LAB",
                                              "credential_id": self.ro["id"]}).json()
        r = self.c.put(f"/api/firmware/devices/{d['id']}/settings",
                       json={"upgrade_credential_id": self.up["id"], **settings})
        assert r.status_code == 200, r.text
        self.app.state.firmware_service.check(d["id"])
        return d

    def image(self, filename, platform, version, pattern="*") -> dict:
        self.net.versions[filename] = version
        r = self.c.put("/api/firmware/images/upload", params={
            "filename": filename, "platform": platform, "version": version,
            "model_pattern": pattern}, content=filename.encode() * 300)
        assert r.status_code == 201, r.text
        return r.json()

    def job(self, device, image, live=True, path="", window=True, **extra) -> dict:
        now = utcnow()
        body = {"device_id": device["id"], "image_id": image["id"], "live": live, "path": path,
                "change_ref": "CHG0099999", **extra}
        if window:
            body |= {"planned_start": (now - timedelta(hours=1)).isoformat(),
                     "planned_end": (now + timedelta(hours=4)).isoformat()}
        r = self.c.post("/api/firmware/jobs", json=body)
        assert r.status_code == 201, r.text
        return r.json()

    def act(self, job, action, expect=202) -> dict:
        r = self.c.post(f"/api/firmware/jobs/{job['id']}/{action}",
                        json={"confirm": job["device"]["name"]})
        assert r.status_code == expect, r.text
        if expect == 202:
            self.worker.process_once(wait=True)
        return self.get(job)

    def get(self, job) -> dict:
        return self.c.get(f"/api/firmware/jobs/{job['id']}").json()


@pytest.fixture
def lab(settings):
    settings.upgrade_live_platforms = "cisco_ios,cisco_nxos,cisco_asa,cisco_ftd,allied_awplus"
    net = FakeNetwork()
    app = create_app(settings, fetcher=FakeFetcher(), start_scheduler=False,
                     fw_collector=net.show, upgrade_io=net)
    with TestClient(app) as client:
        assert client.post("/api/auth/login", json={"username": "admin",
                                                    "password": ADMIN_PASSWORD}).status_code == 200
        yield Lab(client, app, net, settings)


def _check(job, phase, check_id, device=None):
    return next(c for c in job["checks"][phase] if c["check_id"] == check_id
                and (device is None or c["device"] == device))


def _msgs(job) -> str:
    return " | ".join(e["message"] for e in job["events"])


# --- IOS-XE install mode ---------------------------------------------------------------

@pytest.fixture
def xe(lab):
    sim = SimDevice("sw1", "cisco_ios", "17.09.04a", "flash:packages.conf", IOS_HEALTH)
    dev = lab.device(sim, "10.0.0.1")
    img = lab.image("cat9k_iosxe.17.12.04.SPA.bin", "cisco_ios", "17.12.04", "C9300-*")
    return sim, dev, img


def test_ios_xe_live_upgrade_commits_after_the_post_checks(lab, xe):
    sim, dev, img = xe
    job = lab.job(dev, img)
    assert job["path"] == "install" and not job["dry_run"] and job["live_allowed"]
    assert lab.act(job, "precheck")["status"] == "ready"
    j = lab.act(job, "stage")
    assert j["status"] == "ready" and j["units"][0]["staged"] and j["staged_at"]
    assert _check(j, "stage", "device_md5")["status"] == "pass"
    assert sim.files["cat9k_iosxe.17.12.04.SPA.bin"][1] == img["md5"]
    assert sim.reloads == 0  # staging never reloads

    j = lab.act(job, "start")
    assert j["status"] == "completed", _msgs(j)
    assert sim.version == "17.12.04" and not sim.uncommitted and sim.reloads == 1
    changes = [c for _, c in lab.net.log]
    assert changes == ["copy cat9k_iosxe.17.12.04.SPA.bin",
                       "install add file flash:cat9k_iosxe.17.12.04.SPA.bin",
                       "install activate auto-abort-timer 120 prompt-level none",
                       "install commit"]
    assert not j["pending_commit"] and j["pre_backup_commit"] and j["post_backup_commit"]
    assert _check(j, "post", "post_version")["status"] == "pass"
    assert "is back after" in _msgs(j)


def test_ios_xe_post_check_failure_then_roll_back_with_install_abort(lab, xe):
    sim, dev, img = xe
    sim.after_reload = {"show ip interface brief": IF_DOWN}
    job = lab.job(dev, img)
    lab.act(job, "precheck")
    j = lab.act(job, "start")
    assert j["status"] == "failed" and sim.uncommitted
    assert j["pending_commit"] and j["abort_deadline"]
    assert "reverts by itself" in _msgs(j)
    assert set(j["allowed"]) == {"cancel", "postcheck", "rollback"}
    assert _check(j, "post", "post_interfaces")["status"] == "fail"

    sim.after_reload = {"show ip interface brief": s.IP_BRIEF}  # the old version is fine
    j = lab.act(job, "rollback")
    assert j["status"] == "rolled_back", _msgs(j)
    assert sim.version == "17.09.04a" and lab.net.log[-1] == ("sw1", "install abort prompt-level none")
    assert not j["pending_commit"] and j["units"][0]["status"] == "rolled_back"


def test_ios_xe_failure_overridden_then_continue_commits(lab, xe):
    sim, dev, img = xe
    sim.after_reload = {"show ip interface brief": IF_DOWN}
    job = lab.job(dev, img)
    lab.act(job, "precheck")
    j = lab.act(job, "start")
    failed = _check(j, "post", "post_interfaces")
    r = lab.c.post(f"/api/firmware/jobs/{job['id']}/checks/{failed['id']}/override",
                   json={"reason": "Gi2/0/13 is a decommissioned printer port (CHG0012399)"})
    assert r.json()["status"] == "paused" and "continue" in r.json()["allowed"]
    assert sim.uncommitted  # still not committed until Continue
    j = lab.act(job, "continue")
    assert j["status"] == "completed_overrides" and not sim.uncommitted
    assert lab.net.log[-1] == ("sw1", "install commit")


def test_ios_xe_abort_timer_running_out(lab, xe):
    sim, dev, img = xe
    sim.after_reload = {"show ip interface brief": IF_DOWN}
    job = lab.job(dev, img)
    lab.act(job, "precheck")
    lab.act(job, "start")
    with session_scope() as db:
        db.get(UpgradeJob, job["id"]).abort_deadline = utcnow() - timedelta(minutes=1)
    lab.svc.watch_abort_timers()
    j = lab.get(job)
    assert j["status"] == "needs_attention" and not j["pending_commit"]
    assert "gone back to the previous version by itself" in _msgs(j)


def test_wrong_checksum_on_the_device_stops_before_any_reload(lab, xe):
    sim, dev, img = xe
    sim.fail.add("copy_md5")
    job = lab.job(dev, img)
    lab.act(job, "precheck")
    j = lab.act(job, "stage")
    md5 = _check(j, "stage", "device_md5")
    assert j["status"] == "ready" and md5["status"] == "fail" and not md5["overridable"]
    r = lab.c.post(f"/api/firmware/jobs/{job['id']}/checks/{md5['id']}/override",
                   json={"reason": "trying to wave a bad image through"})
    assert r.status_code == 409
    j = lab.act(job, "start")
    assert j["status"] == "failed" and sim.reloads == 0 and "doesn't match its MD5" in _msgs(j)
    assert "continue" in j["allowed"]  # retry the copy once fixed
    sim.fail.clear()
    assert lab.act(job, "continue")["status"] == "completed"


def test_stop_after_this_step_and_continue(lab, xe):
    sim, dev, img = xe
    job = lab.job(dev, img)
    lab.act(job, "precheck")

    def on_step(job_id, label):
        if "install add" in label:
            with session_scope() as db:
                jb.request_stop(db, db.get(UpgradeJob, job_id), "eng1")

    lab.svc.on_step = on_step
    j = lab.act(job, "start")
    assert j["status"] == "paused" and sim.reloads == 0 and "stop requested" in _msgs(j)
    lab.svc.on_step = lambda *a: None
    assert lab.act(job, "continue")["status"] == "completed"


def test_window_end_pauses_before_the_reload(lab, xe):
    sim, dev, img = xe
    job = lab.job(dev, img)
    lab.act(job, "precheck")

    def on_step(job_id, label):
        if "install add" in label:
            with session_scope() as db:
                db.get(UpgradeJob, job_id).planned_end = utcnow() - timedelta(seconds=1)

    lab.svc.on_step = on_step
    j = lab.act(job, "start")
    assert j["status"] == "paused" and sim.reloads == 0
    assert "change window has ended" in _msgs(j)
    r = lab.c.post(f"/api/firmware/jobs/{job['id']}/continue", json={"confirm": "sw1"})
    assert r.status_code == 409 and "change window" in r.json()["detail"]


def test_live_safeguards(lab, xe):
    sim, dev, img = xe
    # live needs a window
    r = lab.c.post("/api/firmware/jobs", json={"device_id": dev["id"], "image_id": img["id"],
                                               "live": True})
    assert r.status_code == 422 and "window" in r.json()["detail"]
    # live Start needs the device name typed
    job = lab.job(dev, img)
    lab.act(job, "precheck")
    r = lab.c.post(f"/api/firmware/jobs/{job['id']}/start", json={"confirm": "wrong"})
    assert r.status_code == 422
    # and the platform switched on
    lab.settings.upgrade_live_platforms = "cisco_nxos"
    r = lab.c.post(f"/api/firmware/jobs/{job['id']}/start", json={"confirm": "sw1"})
    assert r.status_code == 409 and "switched off" in r.json()["detail"]
    lab.c.post(f"/api/firmware/jobs/{job['id']}/cancel", json={"reason": "re-plan as a dry run"})
    r = lab.c.post("/api/firmware/jobs", json={"device_id": dev["id"], "image_id": img["id"],
                                               "live": True, "planned_start": utcnow().isoformat(),
                                               "planned_end": (utcnow() + timedelta(hours=1)).isoformat()})
    assert r.status_code == 422 and "switched off" in r.json()["detail"]


def test_dry_run_on_the_simulator_changes_nothing(lab, xe):
    sim, dev, img = xe
    job = lab.job(dev, img, live=False, window=False)
    assert job["dry_run"]
    lab.act(job, "precheck")
    lab.act(job, "stage")
    j = lab.act(job, "start")
    assert j["status"] == "completed", _msgs(j)
    assert lab.net.log == [] and sim.reloads == 0 and not sim.files
    assert "DRY RUN - would run on sw1: install activate auto-abort-timer 120" in _msgs(j)


def test_device_that_does_not_come_back(lab, xe):
    sim, dev, img = xe
    sim.fail.add("no_return")
    job = lab.job(dev, img)
    lab.act(job, "precheck")
    j = lab.act(job, "start")
    assert j["status"] == "needs_attention" and "didn't come back" in _msgs(j)
    assert set(j["allowed"]) == {"cancel", "postcheck", "rollback"}


def test_worker_restart_mid_upgrade(lab, xe):
    sim, dev, img = xe
    job = lab.job(dev, img)
    lab.act(job, "precheck")
    with session_scope() as db:
        j = db.get(UpgradeJob, job["id"])
        j.status, j.current_step = "running", "sw1: install activate"
    lab.c.post(f"/api/firmware/jobs/{job['id']}/stop")  # allowed while running
    UpgradeWorker(lab.settings, lab.svc).start()
    j = lab.get(job)
    assert j["status"] == "needs_attention" and "install activate" in _msgs(j)


def test_install_mode_check_belongs_to_the_install_path(lab):
    sim = SimDevice("sw9", "cisco_ios", "17.09.04a", "flash:cat9k_iosxe.17.09.04a.SPA.bin",
                    IOS_HEALTH)
    dev = lab.device(sim, "10.0.0.9")
    img = lab.image("cat9k_iosxe.17.12.04.SPA.bin", "cisco_ios", "17.12.04")
    job = lab.job(dev, img, path="install")
    j = lab.act(job, "precheck")
    assert j["status"] == "blocked" and _check(j, "pre", "install_mode")["status"] == "fail"


# --- IOS classic / bundle mode -------------------------------------------------------------

def test_ios_bundle_mode_upgrade_and_rollback(lab):
    old = "c2960x-universalk9-mz.152-7.E8.bin"
    sim = SimDevice("acc1", "cisco_ios", "15.2(7)E8", f"flash:{old}", IOS_HEALTH)
    sim.files[old] = (30_000_000, "a" * 32)
    dev = lab.device(sim, "10.0.1.1")
    img = lab.image("c2960x-universalk9-mz.152-7.E10.bin", "cisco_ios", "15.2(7)E10")
    sim.after_reload = {"show ip interface brief": IF_DOWN}
    job = lab.job(dev, img)
    assert job["path"] == "bundle"
    assert lab.act(job, "precheck")["status"] == "ready"
    j = lab.act(job, "start")
    assert j["status"] == "failed" and sim.version == "15.2(7)E10"
    assert sim.boot == ["flash:c2960x-universalk9-mz.152-7.E10.bin", f"flash:{old}"]
    sim.after_reload = {"show ip interface brief": s.IP_BRIEF}
    j = lab.act(job, "rollback")
    assert j["status"] == "rolled_back" and sim.version == "15.2(7)E8"
    assert sim.boot == [f"flash:{old}"]


# --- NX-OS -------------------------------------------------------------------------------------

def _nxos(lab, name, address, role=""):
    sim = SimDevice(name, "cisco_nxos", "9.3(10)", "bootflash:///nxos.9.3.10.bin", NXOS_OUT)
    sim.role = role
    return sim, lab.device(sim, address)


def test_nxos_impact_check_stops_before_the_reload(lab):
    sim, dev = _nxos(lab, "nx1", "10.0.2.1")
    img = lab.image("nxos64-cs.10.2.5.M.bin", "cisco_nxos", "10.2(5)")
    sim.fail.add("impact")
    job = lab.job(dev, img)
    assert lab.act(job, "precheck")["status"] == "ready"
    j = lab.act(job, "start")
    assert j["status"] == "failed" and sim.reloads == 0
    assert _check(j, "stage", "install_impact")["status"] == "fail"
    sim.fail.clear()
    j = lab.act(job, "continue")
    assert j["status"] == "completed" and sim.version == "10.2(5)"
    assert ("nx1", "install all nxos bootflash:nxos64-cs.10.2.5.M.bin non-interruptive") in lab.net.log


def test_nxos_vpc_pair_secondary_first(lab):
    p, dev_p = _nxos(lab, "nx-a", "10.0.2.11", "primary")
    sec, dev_s = _nxos(lab, "nx-b", "10.0.2.12", "secondary")
    p.peer, sec.peer = sec, p
    r = lab.c.put(f"/api/firmware/devices/{dev_p['id']}/settings",
                  json={"upgrade_credential_id": lab.up["id"], "peer_device_id": dev_s["id"],
                        "default_path": "vpc_pair"})
    assert r.json()["peer"] == "nx-b"
    assert lab.c.get(f"/api/firmware/devices/{dev_s['id']}/settings").json()["peer"] == "nx-a"
    img = lab.image("nxos64-cs.10.2.5.M.bin", "cisco_nxos", "10.2(5)")
    job = lab.job(dev_p, img)
    assert job["path"] == "vpc_pair" and [u["name"] for u in job["units"]] == ["nx-a", "nx-b"]
    # the other switch can't get its own job meanwhile
    r = lab.c.post("/api/firmware/jobs", json={"device_id": dev_s["id"], "image_id": img["id"],
                                               "path": "standalone"})
    assert r.status_code == 409
    assert lab.act(job, "precheck")["status"] == "ready"
    j = lab.act(job, "start")
    assert j["status"] == "completed", _msgs(j)
    installs = [d for d, c in lab.net.log if c.startswith("install all")]
    assert installs == ["nx-b", "nx-a"] and p.version == sec.version == "10.2(5)"


# --- ASA ------------------------------------------------------------------------------------------

def test_asa_failover_pair(lab):
    a = SimDevice("fw-a", "cisco_asa", "9.18(4)", "disk0:/cisco-asa-fp2k.9.18.4.SPA", ASA_OUT)
    b = SimDevice("fw-b", "cisco_asa", "9.18(4)", "disk0:/cisco-asa-fp2k.9.18.4.SPA", ASA_OUT)
    a.role, b.role, a.peer, b.peer = "active", "standby", b, a
    dev_a, dev_b = lab.device(a, "10.0.3.1"), lab.device(b, "10.0.3.2")
    lab.c.put(f"/api/firmware/devices/{dev_a['id']}/settings",
              json={"upgrade_credential_id": lab.up["id"], "peer_device_id": dev_b["id"]})
    img = lab.image("cisco-asa-fp2k.9.20.2.SPA", "cisco_asa", "9.20(2)")
    job = lab.job(dev_a, img)
    assert job["path"] == "failover_pair"
    j = lab.act(job, "precheck")
    assert j["status"] == "ready", [c for c in j["checks"]["pre"] if c["status"] == "fail"]
    assert _check(j, "pre", "pair_roles")["status"] == "pass"
    j = lab.act(job, "start")
    assert j["status"] == "completed", _msgs(j)
    assert a.version == b.version == "9.20(2)"
    order = [(d, c) for d, c in lab.net.log if "failover" in c]
    assert order == [("fw-a", "failover exec mate write memory"), ("fw-a", "failover reload-standby"),
                     ("fw-b", "failover active"), ("fw-a", "failover reload-standby")]


def test_asa_standalone_path_refuses_a_pair_member(lab):
    a = SimDevice("fw-c", "cisco_asa", "9.18(4)", "disk0:/cisco-asa-fp2k.9.18.4.SPA", ASA_OUT)
    a.role = "active"
    dev = lab.device(a, "10.0.3.5")
    img = lab.image("cisco-asa-fp2k.9.20.2.SPA", "cisco_asa", "9.20(2)")
    j = lab.act(lab.job(dev, img, path="standalone"), "precheck")
    assert j["status"] == "blocked" and _check(j, "pre", "standalone")["status"] == "fail"
    r = lab.c.post("/api/firmware/jobs", json={"device_id": dev["id"], "image_id": img["id"],
                                               "path": "failover_pair"})
    assert r.status_code == 422 and "peer" in r.json()["detail"]


# --- FTD (FDM) ----------------------------------------------------------------------------------

def _ftd(lab, name, address, role=""):
    sim = SimDevice(name, "cisco_ftd", "7.2.5", "build 208", {})
    sim.role = role
    return sim, lab.device(sim, address)


def test_ftd_standalone_through_the_fdm_api(lab):
    sim, dev = _ftd(lab, "ftd1", "10.0.4.1")
    img = lab.image("Cisco_FTD_Upgrade-7.4.1-172.sh.REL.tar", "cisco_ftd", "7.4.1")
    job = lab.job(dev, img)
    j = lab.act(job, "precheck")
    assert j["status"] == "blocked" and "isn't trusted" in _check(j, "pre", "fdm_api")["detail"]
    # an admin fetches and trusts the certificate
    fp = lab.c.post(f"/api/firmware/devices/{dev['id']}/fdm-fingerprint").json()["fingerprint"]
    lab.c.put(f"/api/firmware/devices/{dev['id']}/settings",
              json={"upgrade_credential_id": lab.up["id"], "fdm_fingerprint": fp})
    sim.pending = [{"id": "x"}]
    j = lab.act(job, "precheck")
    assert _check(j, "pre", "fdm_pending")["status"] == "fail"
    sim.pending = []
    assert lab.act(job, "precheck")["status"] == "ready"
    j = lab.act(job, "start")
    assert j["status"] == "completed", _msgs(j)
    assert sim.version == "7.4.1"
    assert [c for _, c in lab.net.log] == ["fdm:upload Cisco_FTD_Upgrade-7.4.1-172.sh.REL.tar",
                                           "fdm:readiness file-1",
                                           "fdm:upgrade Cisco_FTD_Upgrade-7.4.1-172.sh.REL.tar"]


def test_ftd_changed_certificate_is_refused(lab):
    sim, dev = _ftd(lab, "ftd2", "10.0.4.2")
    lab.c.put(f"/api/firmware/devices/{dev['id']}/settings",
              json={"upgrade_credential_id": lab.up["id"], "fdm_fingerprint": FP})
    sim.fingerprint = "11:22"
    img = lab.image("Cisco_FTD_Upgrade-7.4.1-172.sh.REL.tar", "cisco_ftd", "7.4.1")
    j = lab.act(lab.job(dev, img), "precheck")
    assert "certificate changed" in _check(j, "pre", "fdm_api")["detail"]


def test_ftd_readiness_failure_and_revert(lab):
    sim, dev = _ftd(lab, "ftd3", "10.0.4.3")
    lab.c.put(f"/api/firmware/devices/{dev['id']}/settings",
              json={"upgrade_credential_id": lab.up["id"], "fdm_fingerprint": FP})
    img = lab.image("Cisco_FTD_Upgrade-7.4.1-172.sh.REL.tar", "cisco_ftd", "7.4.1")
    job = lab.job(dev, img)
    lab.act(job, "precheck")
    sim.fail.add("readiness")
    j = lab.act(job, "start")
    assert j["status"] == "failed" and sim.reloads == 0
    assert _check(j, "stage", "fdm_readiness")["status"] == "fail"
    sim.fail.clear()
    sim.after_reload = {}
    j = lab.act(job, "continue")
    assert j["status"] == "completed" and sim.version == "7.4.1"


def test_ftd_ha_pair_standby_first_then_failover(lab):
    f1, d1 = _ftd(lab, "ftd-a", "10.0.4.11", "active")
    f2, d2 = _ftd(lab, "ftd-b", "10.0.4.12", "standby")
    f1.peer, f2.peer = f2, f1
    for d in (d1, d2):
        lab.c.put(f"/api/firmware/devices/{d['id']}/settings",
                  json={"upgrade_credential_id": lab.up["id"], "fdm_fingerprint": FP,
                        "peer_device_id": d2["id"] if d is d1 else d1["id"]})
    img = lab.image("Cisco_FTD_Upgrade-7.4.1-172.sh.REL.tar", "cisco_ftd", "7.4.1")
    job = lab.job(d1, img)
    assert job["path"] == "fdm_ha"
    j = lab.act(job, "precheck")
    assert j["status"] == "ready", [c for c in j["checks"]["pre"] if c["status"] == "fail"]
    j = lab.act(job, "start")
    assert j["status"] == "completed", _msgs(j)
    assert f1.version == f2.version == "7.4.1"
    steps = [(d, c.split()[0]) for d, c in lab.net.log if c.startswith("fdm:upgrade")
             or c == "fdm:ha_failover"]
    assert steps == [("ftd-b", "fdm:upgrade"), ("ftd-a", "fdm:ha_failover"), ("ftd-a", "fdm:upgrade")]
    assert f2.role == "active" and f1.role == "standby"


# --- AlliedWare Plus ------------------------------------------------------------------------------

def test_awplus_upgrade(lab):
    sim = SimDevice("aw1", "allied_awplus", "5.5.2", "flash:/x930-5.5.2-0.4.rel", AW_OUT)
    dev = lab.device(sim, "10.0.5.1")
    img = lab.image("x930-5.5.4-1.1.rel", "allied_awplus", "5.5.4-1.1")
    job = lab.job(dev, img)
    j = lab.act(job, "precheck")
    assert j["status"] == "ready", [c for c in j["checks"]["pre"] if c["status"] == "fail"]
    j = lab.act(job, "start")
    assert j["status"] == "completed", _msgs(j)
    assert _check(j, "stage", "device_size")["status"] == "pass"
    assert sim.boot == ["flash:/x930-5.5.4-1.1.rel"]
    assert ("aw1", "boot system backup flash:/x930-5.5.2-0.4.rel") in lab.net.log
    assert sim.version == "5.5.4-1.1"


def test_report_and_paths_api(lab, xe):
    sim, dev, img = xe
    job = lab.job(dev, img)
    lab.act(job, "precheck")
    lab.act(job, "start")
    html = lab.c.get(f"/api/firmware/jobs/{job['id']}/report.html").text
    assert "LIVE" in html and "install commit" in html and "IOS-XE install mode" in html
    paths = {p["platform"]: p for p in lab.c.get("/api/firmware/upgrade-paths").json()}
    assert [x["id"] for x in paths["cisco_ftd"]["paths"]] == ["standalone", "fdm_ha"]
    assert paths["cisco_ios"]["paths"][0]["live_allowed"]
    assert lab.c.get("/api/firmware/worker").json()["alive"] is False


def test_worker_shutdown_pauses_before_the_next_step(lab, xe):
    sim, dev, img = xe
    job = lab.job(dev, img)
    lab.act(job, "precheck")

    def on_step(job_id, label):
        if "install add" in label:
            lab.svc.stopping = True  # SIGTERM arrived while this step ran

    lab.svc.on_step = on_step
    j = lab.act(job, "start")
    assert j["status"] == "paused" and sim.reloads == 0
    assert "worker is shutting down" in _msgs(j)
    lab.svc.stopping, lab.svc.on_step = False, lambda *a: None
    assert lab.act(job, "continue")["status"] == "completed"
    lab.worker.heartbeat()
    w = lab.c.get("/api/firmware/worker").json()
    assert w["alive"] and w["pid"]


def test_cli_upgrades_running(lab, xe, capsys, monkeypatch):
    from app import cli

    sim, dev, img = xe
    job = lab.job(dev, img)
    monkeypatch.setattr("app.core.config.get_settings", lambda: lab.settings)
    monkeypatch.setattr("app.core.db.init_engine", lambda url: None)
    assert cli.main(["upgrades-running"]) == 0
    with session_scope() as db:
        db.get(UpgradeJob, job["id"]).status = "running"
    assert cli.main(["upgrades-running"]) == 3
    assert "LIVE" in capsys.readouterr().out
